"""The albums earlier removals left behind are removed once
(db_migrate's `tracklist_leftovers_v1`; PI-NODE-2D §6.6, the owner's yes of
2026-11-24).

While no tracklist slot said who wrote it, a removed copy left its album and
tracks behind, held by the slots its own tags had written. The step takes
exactly the albums with no copy, no release group, not imported and with no
imported slot: their slots, then those slots' tracks that nothing else
refers to, then the albums and the artists nothing credits any more — and
nothing of a phantom, a peer's record or an album that has a copy.

On a real PostgreSQL — the module's scratch database (conftest.scratch_dsn).
Skipped where there is no cluster.
"""

import logging
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("sqlalchemy")

import db_migrate  # noqa: E402
from desktop import db_init  # noqa: E402

GROUP = "0a0b0c0d-0000-4000-8000-00000000000a"


@pytest.fixture
def conn(scratch_dsn):
    c = psycopg2.connect(scratch_dsn)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("TRUNCATE listening_history, tracks, albums, artists, hqp_endpoints CASCADE")
        cur.execute(f"DELETE FROM {db_init.MIGRATIONS_TABLE} WHERE filename = 'tracklist_leftovers_v1'")
    yield c
    c.close()


class _Seed:
    def __init__(self, conn):
        self.cur = conn.cursor()

    def artist(self, name):
        aid = str(uuid.uuid4())
        self.cur.execute("INSERT INTO artists (id, name) VALUES (%s, %s)", (aid, name))
        return aid

    def track(self, title, artist=None, composer=None):
        tid = str(uuid.uuid4())
        self.cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (tid, title))
        if artist:
            self.cur.execute("INSERT INTO track_artists (track_id, artist_id, role) "
                             "VALUES (%s, %s, 'primary')", (tid, artist))
        if composer:
            self.cur.execute("INSERT INTO track_credits (track_id, role, artist_id, source, position) "
                             "VALUES (%s, 'composer', %s, 'tag', 1)", (tid, composer))
        return tid

    def album(self, title, artist=None, group=None, imported=False):
        aid = str(uuid.uuid4())
        self.cur.execute("INSERT INTO albums (id, title, musicbrainz_id, imported) VALUES (%s, %s, %s, %s)",
                         (aid, title, group, imported))
        if artist:
            self.cur.execute("INSERT INTO album_artists (album_id, artist_id, role) "
                             "VALUES (%s, %s, 'primary')", (aid, artist))
        return aid

    def slots(self, album, tracks, imported=(), source="mb"):
        for position, track in enumerate(tracks, 1):
            self.cur.execute("""INSERT INTO album_tracks (album_id, track_id, disc, position, imported, source)
                                VALUES (%s, %s, 1, %s, %s, %s::tracklist_source)""",
                             (album, track, position, track in imported, source))

    def copy(self, album, tracks, folder):
        self.cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, %s) "
                         "RETURNING id", (album, folder))
        variant = self.cur.fetchone()[0]
        for n, track in enumerate(tracks, 1):
            self.cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path, track_number) "
                             "VALUES (%s, %s, %s, %s)", (track, variant, f"{folder}/{n:02d}.flac", n))

    def titles(self, table):
        self.cur.execute(f"SELECT {'name' if table == 'artists' else 'title'} FROM {table} ORDER BY 1")
        return [r[0] for r in self.cur.fetchall()]

    def slot_count(self, album):
        self.cur.execute("SELECT count(*) FROM album_tracks WHERE album_id = %s", (album,))
        return self.cur.fetchone()[0]


def _step(dsn):
    """The step as apply_pending runs it: on a connection of its own."""
    c = psycopg2.connect(dsn)
    try:
        return db_migrate._remove_tracklist_leftovers(c)
    finally:
        c.close()


def test_a_leftover_goes_with_its_slots_and_the_tracks_nothing_else_holds(conn, scratch_dsn):
    s = _Seed(conn)
    left_artist, kept_artist = s.artist("Only The Leftover's"), s.artist("Still Owned")
    composer, heard_composer = s.artist("A Composer Of The Gone"), s.artist("A Composer Of The Heard")
    stub = s.artist("A Similar Nothing Credits")           # the phantom layer's: not this step's
    gone = s.track("Gone", left_artist, composer)
    owned = s.track("Owned Elsewhere", kept_artist)
    heard = s.track("Heard", left_artist, heard_composer)
    s.cur.execute("INSERT INTO listening_history (track_id, started_at, completed) VALUES (%s, now(), TRUE)",
                  (heard,))
    leftover = s.album("Best", left_artist)
    s.slots(leftover, [gone, owned, heard], source="tag")
    empty = s.album("Left With Nothing", s.artist("Of The Empty One"))    # no copy, no group, no slot
    elsewhere = s.album("The Real Album", kept_artist)
    s.copy(elsewhere, [owned], "/music/The Real Album")

    assert _step(scratch_dsn) == {"albums": 2, "slots": 3, "tracks": 1, "artists": 2}

    assert s.titles("albums") == ["The Real Album"]
    assert s.titles("tracks") == ["Heard", "Owned Elsewhere"]
    assert (s.slot_count(leftover), s.slot_count(elsewhere)) == (0, 0)
    # the heard track keeps its credits; the gone track's composer and the
    # empty album's artist went; the uncredited stub was never in the scope
    assert s.titles("artists") == ["A Composer Of The Heard", "A Similar Nothing Credits",
                                   "Only The Leftover's", "Still Owned"]


def test_a_phantom_a_peers_record_and_an_album_with_a_copy_are_untouched(conn, scratch_dsn):
    s = _Seed(conn)
    artist = s.artist("Artist")
    albums = {}
    # a phantom: no copy, a release group
    tracks = [s.track("Phantom I", artist), s.track("Phantom II", artist)]
    albums["phantom"] = s.album("A Phantom", artist, group=GROUP)
    s.slots(albums["phantom"], tracks)
    # no copy, no release group, one slot a peer's
    tracks = [s.track("Peer I", artist), s.track("Peer II", artist)]
    albums["peer"] = s.album("With A Peer's Slot", artist)
    s.slots(albums["peer"], tracks, imported=tracks[1:], source="tag")
    # the seed's or a share's own album row
    tracks = [s.track("Imported I", artist)]
    albums["imported"] = s.album("An Imported Row", artist, imported=True)
    s.slots(albums["imported"], tracks)
    # an album with a copy and no release group, its slots the files'
    tracks = [s.track("Owned I", artist), s.track("Owned II", artist)]
    albums["owned"] = s.album("An Owned Album", artist)
    s.slots(albums["owned"], tracks, source="tag")
    s.copy(albums["owned"], tracks, "/music/An Owned Album")
    # a copy at an HQPlayer is a copy
    tracks = [s.track("Held I", artist)]
    albums["held"] = s.album("At The HQPlayer", artist)
    s.slots(albums["held"], tracks, source="tag")
    s.cur.execute("INSERT INTO hqp_endpoints (name) VALUES ('box') RETURNING id")
    s.cur.execute("""INSERT INTO album_variants (album_id, directory_path, location, hqp_endpoint_id)
                     VALUES (%s, '/smb/held', 'hqplayer', %s)""", (albums["held"], s.cur.fetchone()[0]))
    # an album the seed picked, owned before the seed and never bound, its copy removed since: the
    # mint and the album merges spare a seed's pick everywhere, and so does this step
    tracks = [s.track("Picked I", artist), s.track("Picked II", artist)]
    albums["picked"] = s.album("A Seed's Pick", artist)
    s.slots(albums["picked"], tracks, source="tag")
    s.cur.execute("INSERT INTO seed_picks (album_id, tier, rank) VALUES (%s, 1, 1)", (albums["picked"],))
    before = (s.titles("albums"), s.titles("tracks"), {k: s.slot_count(a) for k, a in albums.items()})

    assert _step(scratch_dsn) == {"albums": 0, "slots": 0, "tracks": 0, "artists": 0}

    assert (s.titles("albums"), s.titles("tracks"),
            {k: s.slot_count(a) for k, a in albums.items()}) == before
    assert before[2] == {"phantom": 2, "peer": 2, "imported": 1, "owned": 2, "held": 1, "picked": 2}


def test_the_start_runs_it_once_and_says_what_it_removed(conn, scratch_dsn, monkeypatch, caplog):
    """apply_pending with every other step taken as done: the step runs at
    the first start, its counts at info, and never again — an album of the
    same shape made later is a removal's to take, not a start's."""
    import database
    import seed_import
    from config import settings
    from sqlalchemy.orm import sessionmaker
    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port, postgres_user=url.username,
                           postgres_password=url.password, postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    engine = database.make_engine(scratch_dsn)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine))
    marked = db_migrate._marked
    monkeypatch.setattr(db_migrate, "_marked", lambda c, step: (
        marked(c, step) if step == "tracklist_leftovers_v1" else True))
    monkeypatch.setattr(seed_import, "bundle_info", lambda: None)
    s = _Seed(conn)
    try:
        s.slots(s.album("Best"), [s.track("Gone")], source="tag")
        with caplog.at_level(logging.INFO, logger="db_migrate"):
            out = db_migrate.apply_pending()
        assert out["tracklist_leftovers"] == {"albums": 1, "slots": 1, "tracks": 1, "artists": 0}
        said = [r.getMessage() for r in caplog.records
                if r.levelno == logging.INFO and r.getMessage().startswith("tracklist leftovers")]
        assert len(said) == 1 and "'albums': 1, 'slots': 1, 'tracks': 1, 'artists': 0" in said[0]
        assert (s.titles("albums"), s.titles("tracks")) == ([], [])

        s.slots(s.album("Left Later"), [s.track("Later")], source="tag")
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="db_migrate"):
            assert "tracklist_leftovers" not in db_migrate.apply_pending()
        assert not [r for r in caplog.records if r.getMessage().startswith("tracklist leftovers")]
        assert (s.titles("albums"), s.titles("tracks")) == (["Left Later"], ["Later"])
    finally:
        engine.dispose()
