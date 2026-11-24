"""A tracklist slot says who wrote it (album_tracks.source), and a removal
takes the slots its files' tags wrote (PI-NODE-2D item 5, F21).

Three writers shared `album_tracks` with no word of which wrote a row, so a
removed copy left its album and tracks behind as a phantom built from the
person's tags: 436 albums and 4 161 tracks with no file on a box after
"Remove albums that are away". The materialiser's rows are 'tag', the mint's
'mb'; the removal — the scanner's, the CUE supersede's, an HQPlayer
library's forget — deletes the 'tag' slots of the albums and tracks of the
files it took, and nothing else of the table.

On a real PostgreSQL — the module's scratch database (conftest.scratch_dsn),
the pool and the SQLAlchemy sessions pointed at it. Skipped where there is no
cluster.
"""

import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("sqlalchemy")

import psycopg2.extras  # noqa: E402
import psycopg2.pool  # noqa: E402

import scanner  # noqa: E402
from tests import conftest  # noqa: E402

MIGRATIONS = REPO / "desktop" / "migrations"
DELTA = next(MIGRATIONS.glob("*_tracklist_source.sql"))
SEAL = ("ab" * 32, "cd" * 64)


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A music folder that answers and holds none of the catalogued files:
    every seeded file is missing, the folder itself is there."""
    from config import settings
    root = tmp_path / "music"
    (root / "Elsewhere").mkdir(parents=True)
    monkeypatch.setattr(settings, "music_library_path", str(root))
    monkeypatch.setattr(settings, "music_host_path", (tmp_path / "host" / "Music").as_posix())
    monkeypatch.setattr(scanner, "library_unreachable", lambda: False)
    return settings.library_db_root()


@pytest.fixture
def cur(scratch_dsn, monkeypatch, library):
    import database
    import db_pool
    from sqlalchemy.orm import sessionmaker
    pool = psycopg2.pool.ThreadedConnectionPool(1, 4, dsn=scratch_dsn)
    engine = database.make_engine(scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    monkeypatch.setattr(database, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute("TRUNCATE listening_history, tracks, albums, artists, genres, hqp_endpoints CASCADE")
        yield c
    conn.close()
    pool.closeall()
    engine.dispose()


def _file(root, artist, album, number, title, folder=None, seconds=200.0):
    """What extract_metadata hands the import for one file of a rip."""
    return (f"{root}/{folder or album}",
            {"file_path": f"{root}/{folder or album}/{number:02d}. {title}.flac",
             "title": title, "artist": artist, "album_artist": artist, "album": album,
             "track_number": number, "disc_number": 1, "duration_seconds": seconds,
             "file_format": "FLAC", "is_lossless": True, "sample_rate": 44100, "bit_depth": 16})


def _scan(root, artist, album, titles, folder=None):
    """A copy scanned: the scanner's own import, then the materialiser as
    every signing pass runs it."""
    scanner.import_metadata([_file(root, artist, album, n, t, folder) for n, t in enumerate(titles, 1)],
                            sink=scanner.LOCAL_FILES, stats={})


def _materialise(cur):
    import sign_audio
    return sign_audio._materialize_owned_tracklists(cur)


def _album(cur, title):
    cur.execute("SELECT id::text AS id FROM albums WHERE title = %s", (title,))
    return cur.fetchone()["id"]


def _slots(cur, album_title=None):
    cur.execute("""SELECT al.title AS album, t.title AS track, at.position, at.source::text AS source
                     FROM album_tracks at JOIN albums al ON al.id = at.album_id
                     JOIN tracks t ON t.id = at.track_id
                    WHERE %(a)s::text IS NULL OR al.title = %(a)s
                    ORDER BY al.title, at.disc, at.position""", {"a": album_title})
    return [(r["album"], r["track"], r["position"], r["source"]) for r in cur.fetchall()]


def _titles(cur, table):
    cur.execute(f"SELECT {'name' if table == 'artists' else 'title'} AS t FROM {table} ORDER BY 1")
    return [r["t"] for r in cur.fetchall()]


def _away(cur, album_title):
    cur.execute("""UPDATE album_variants av SET absent_since = now() FROM albums al
                    WHERE al.id = av.album_id AND al.title = %s""", (album_title,))


def _remove_away():
    """The Away row's removal: the plan (rolled back), then the yes held to
    the plan's counts. Returns (the plan's counts, the removal's)."""
    plan = scanner.plan_away_removal()
    asked = scanner.prune_counts(plan)
    done = scanner.prune_missing_files(plan["file_ids"], asked)
    assert not done.get("grew")
    return asked, scanner.prune_counts(done)


def test_the_materialisers_slots_are_the_files_and_a_removed_copy_takes_them(cur, library):
    """The box's case: a copy scanned, its tracklist sealed, set away and
    removed. The album and its tracks were left behind as a phantom nothing
    would fill."""
    _scan(library, "Solar Fields", "Movements", ["Sol", "Circles of Motion"])
    _materialise(cur)
    assert _slots(cur) == [("Movements", "Sol", 1, "tag"), ("Movements", "Circles of Motion", 2, "tag")]
    cur.execute("UPDATE album_tracks SET author_pubkey = %s, signature = %s", SEAL)

    _away(cur, "Movements")
    asked, removed = _remove_away()

    assert (_titles(cur, "albums"), _titles(cur, "tracks"), _slots(cur)) == ([], [], [])
    assert _titles(cur, "artists") == []
    # the question named what the yes removed
    assert asked == removed
    assert (asked["files"], asked["copies"], asked["tracks"], asked["albums"], asked["artists"]) == (
        2, 0, 2, 1, 1)


def test_a_track_that_was_heard_stays_and_its_album_goes(cur, library):
    _scan(library, "Solar Fields", "Movements", ["Sol", "Circles of Motion"])
    _materialise(cur)
    cur.execute("""INSERT INTO listening_history (track_id, started_at, duration_listened, completed)
                   SELECT id, now(), 200, TRUE FROM tracks WHERE title = 'Sol'""")

    _away(cur, "Movements")
    asked, removed = _remove_away()

    assert (_titles(cur, "albums"), _titles(cur, "tracks"), _slots(cur)) == ([], ["Sol"], [])
    assert asked == removed and (asked["tracks"], asked["albums"]) == (1, 1)
    assert _titles(cur, "artists") == ["Solar Fields"]      # the heard track's credit


def test_an_album_musicbrainz_wrote_first_stays_a_phantom_when_its_copy_goes(cur, library):
    """The mint's slots are MusicBrainz's: the copy that came later and went
    leaves the album as it was before it came."""
    _scan(library, "Solar Fields", "Movements", ["Sol", "Circles of Motion"])
    cur.execute("""INSERT INTO album_tracks (album_id, track_id, disc, position, length_ms)
                   SELECT av.album_id, mf.track_id, 1, mf.track_number, 200000
                     FROM media_files mf JOIN album_variants av ON av.id = mf.album_variant_id""")
    assert _materialise(cur) == []                 # the slots are held: nothing of the files' is written
    assert {s[3] for s in _slots(cur)} == {"mb"}

    _away(cur, "Movements")
    asked, removed = _remove_away()

    assert _titles(cur, "albums") == ["Movements"]
    assert _slots(cur) == [("Movements", "Sol", 1, "mb"), ("Movements", "Circles of Motion", 2, "mb")]
    assert _titles(cur, "tracks") == ["Circles of Motion", "Sol"]
    cur.execute("SELECT count(*) AS n FROM album_variants")
    assert cur.fetchone()["n"] == 0
    # the copy went, the music it was a copy of stays: asked about as a copy
    assert asked == removed and (asked["copies"], asked["tracks"], asked["albums"]) == (1, 0, 0)


def test_one_copy_of_two_removed_takes_nothing_of_the_tracklist(cur, library):
    _scan(library, "Solar Fields", "Movements", ["Sol", "Circles of Motion"])
    _scan(library, "Solar Fields", "Movements", ["Sol", "Circles of Motion"], folder="Movements [24-96]")
    _materialise(cur)
    before = _slots(cur)
    assert [s[3] for s in before] == ["tag", "tag"]

    cur.execute("UPDATE album_variants SET absent_since = now() WHERE directory_path LIKE '%%24-96]'")
    asked, removed = _remove_away()

    assert _slots(cur) == before
    assert (_titles(cur, "albums"), _titles(cur, "tracks")) == (["Movements"], ["Circles of Motion", "Sol"])
    assert asked == removed and (asked["copies"], asked["tracks"], asked["albums"]) == (1, 0, 0)


def test_a_removal_takes_the_slots_of_its_own_albums_and_tracks_only(cur, library):
    """Never a predicate over the whole table: another album's slots — a
    present copy's, a phantom's, and one an earlier removal left with no file
    — are not this removal's."""
    _scan(library, "Solar Fields", "Movements", ["Sol", "Circles of Motion"])
    _scan(library, "Carbon Based Lifeforms", "Hydroponic Garden", ["Central Plains", "Tensor"])
    _materialise(cur)
    # a slot of the files' with no file, in an album this removal does not touch
    left, phantom = str(uuid.uuid4()), str(uuid.uuid4())
    for album, title, source in ((left, "Left Behind", "tag"), (phantom, "Never Owned", "mb")):
        track = str(uuid.uuid4())
        cur.execute("INSERT INTO albums (id, title) VALUES (%s, %s)", (album, title))
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (track, title + " I"))
        cur.execute("""INSERT INTO album_tracks (album_id, track_id, disc, position, source)
                       VALUES (%s, %s, 1, 1, %s::tracklist_source)""", (album, track, source))
    # and one that names a track of the removed copy, in another album
    cur.execute("""INSERT INTO album_tracks (album_id, track_id, disc, position, source)
                   SELECT %s, id, 1, 2, 'tag' FROM tracks WHERE title = 'Sol'""", (left,))

    _away(cur, "Movements")
    _remove_away()

    assert _slots(cur) == [
        ("Hydroponic Garden", "Central Plains", 1, "tag"), ("Hydroponic Garden", "Tensor", 2, "tag"),
        ("Left Behind", "Left Behind I", 1, "tag"), ("Left Behind", "Sol", 2, "tag"),
        ("Never Owned", "Never Owned I", 1, "mb")]
    assert _titles(cur, "albums") == ["Hydroponic Garden", "Left Behind", "Never Owned"]
    # "Sol" is another album's slot still: it stays that album's track
    assert "Sol" in _titles(cur, "tracks") and "Circles of Motion" not in _titles(cur, "tracks")


def test_a_peers_slot_is_never_the_removals(cur, library):
    _scan(library, "Solar Fields", "Movements", ["Sol"])
    _materialise(cur)
    cur.execute("UPDATE album_tracks SET imported = TRUE")

    _away(cur, "Movements")
    _remove_away()

    assert _slots(cur) == [("Movements", "Sol", 1, "tag")]
    assert _titles(cur, "albums") == ["Movements"]


def test_writing_the_source_sheds_no_seal(cur, library):
    """Not a payload column: the guard names the six it named."""
    _scan(library, "Solar Fields", "Movements", ["Sol"])
    _materialise(cur)
    cur.execute("UPDATE album_tracks SET author_pubkey = %s, signature = %s", SEAL)
    cur.execute("UPDATE album_tracks SET source = 'mb'")
    cur.execute("SELECT author_pubkey, signature FROM album_tracks")
    row = cur.fetchone()
    assert (row["author_pubkey"], row["signature"]) == SEAL
    cur.execute("SELECT pg_get_functiondef('seal_guard_album_tracks'::regproc) AS def")
    guard = cur.fetchone()["def"]
    assert "source" not in guard
    assert all(f"NEW.{c} IS DISTINCT FROM OLD.{c}" in guard for c in (
        "album_id", "track_id", "disc", "position", "length_ms", "recording_mbid"))


def test_a_slot_musicbrainz_confirms_is_musicbrainzs_from_then_on(cur, library, monkeypatch):
    """The mint's upsert over an album a removed copy's tags had left: the
    slots it names are 'mb' — and one whose payload it does not change keeps
    its seal."""
    import discography
    import mb_answers
    from uuid_utils import album_uuid, artist_uuid, track_uuid
    artist, album = str(artist_uuid("Solar Fields")), str(album_uuid("Movements", "Solar Fields"))
    group, recordings = str(uuid.uuid4()), [str(uuid.uuid4()) for _ in range(3)]
    titles = ["Sol", "Circles of Motion", "The Road to Nothingness"]
    cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'Solar Fields')", (artist,))
    cur.execute("INSERT INTO albums (id, title, musicbrainz_id) VALUES (%s, 'Movements', %s)", (album, group))
    cur.execute("INSERT INTO album_artists (album_id, artist_id, role) VALUES (%s, %s, 'primary')",
                (album, artist))
    for position, title in enumerate(titles[:2], 1):
        track = str(track_uuid(title, "Solar Fields"))
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (track, title))
        cur.execute("""INSERT INTO album_tracks (album_id, track_id, disc, position, recording_mbid,
                                                 length_ms, source, author_pubkey, signature)
                       VALUES (%s, %s, 1, %s, %s, %s, 'tag', %s, %s)""",
                    (album, track, position, recordings[position - 1], 200000 + position, *SEAL))
    release = {"release_mbid": str(uuid.uuid4()), "tracks": [
        {"title": title, "disc": 1, "position": position, "recording_mbid": recordings[position - 1],
         "length_ms": 200000 + position + (1000 if position == 2 else 0),
         "credit": "Solar Fields", "credit_names": [{"name": "Solar Fields", "gid": None}]}
        for position, title in enumerate(titles, 1)]}
    monkeypatch.setattr(mb_answers, "required", lambda: False)
    monkeypatch.setattr(discography.mb, "fetch_release_tracklists", lambda rg: [release])

    assert discography._persist_phantom_tracklist(album, artist, "Solar Fields", group) == 3

    assert _slots(cur) == [("Movements", title, n, "mb") for n, title in enumerate(titles, 1)]
    cur.execute("SELECT position, signature FROM album_tracks ORDER BY position")
    # the first slot's payload is as it was sealed; the second's length moved
    assert [(r["position"], r["signature"]) for r in cur.fetchall()] == [
        (1, SEAL[1]), (2, None), (3, None)]


def test_a_superseded_whole_image_takes_the_slot_its_tags_wrote(cur, library):
    """The CUE supersede's statement, on the rows it works on: a whole-image
    row's slot held its track — and its position, against the slice that
    takes it."""
    from database import get_db_context
    _scan(library, "Solar Fields", "Movements", ["Movements (whole)"])
    _materialise(cur)
    cur.execute("SELECT mf.track_id::text AS t, av.album_id::text AS a FROM media_files mf "
                "JOIN album_variants av ON av.id = mf.album_variant_id")
    row = cur.fetchone()
    with get_db_context() as db:
        assert scanner.drop_tag_slots(db, [row["a"]], [row["t"]]) == 0       # its file holds it
        db.commit()
    cur.execute("DELETE FROM media_files")
    with get_db_context() as db:
        assert scanner.drop_tag_slots(db, [row["a"]], [row["t"]]) == 1
        db.commit()
    assert _slots(cur) == []


def test_a_forgotten_hqplayer_copy_takes_the_slots_it_was_the_last_to_hold(cur, library):
    """An HQPlayer's files write no slot, they keep one: the local copy's
    removal leaves the tracklist to the copy at the HQPlayer, and that copy's
    forgetting is then the removal that takes it."""
    import hqp_library
    from database import get_db_context
    _scan(library, "Solar Fields", "Movements", ["Sol", "Circles of Motion"])
    _materialise(cur)
    cur.execute("INSERT INTO hqp_endpoints (name) VALUES ('box') RETURNING id")
    endpoint = cur.fetchone()["id"]
    cur.execute("""INSERT INTO album_variants (album_id, directory_path, location, hqp_endpoint_id)
                   SELECT id, '/smb/Movements', 'hqplayer', %s FROM albums RETURNING id""", (endpoint,))
    variant = cur.fetchone()["id"]
    cur.execute("""INSERT INTO hqp_library_files (track_id, album_variant_id, hqp_path, track_number)
                   SELECT track_id, %s, '/smb/Movements/' || track_number, track_number FROM media_files
                   RETURNING id""", (variant,))
    held = [r["id"] for r in cur.fetchall()]

    cur.execute("UPDATE album_variants SET absent_since = now() WHERE location = 'local'")
    asked, _removed = _remove_away()
    assert (asked["copies"], asked["albums"], asked["tracks"]) == (1, 0, 0)
    assert [s[3] for s in _slots(cur)] == ["tag", "tag"]

    with get_db_context() as db:
        stats = hqp_library._forget(db, held)
        db.commit()

    assert (stats["orphan_albums"], stats["orphan_tracks"]) == (1, 2)
    assert (_titles(cur, "albums"), _titles(cur, "tracks"), _slots(cur)) == ([], [], [])


def test_the_backfill_takes_a_slot_for_the_files_only_where_nothing_says_otherwise(cur, library):
    """The delta's backfill on a node from before the column, over a seeded
    mix: in doubt a slot is MusicBrainz's and stays."""
    _scan(library, "Solar Fields", "Movements", ["Sol", "Circles of Motion", "Patterns", "Dust", "Sky"])
    _scan(library, "Carbon Based Lifeforms", "Hydroponic Garden", ["Central Plains"])
    cur.execute("ALTER TABLE album_tracks DROP COLUMN source")
    try:
        _slot = """INSERT INTO album_tracks (album_id, track_id, disc, position, length_ms, imported, created_at)
                   SELECT av.album_id, mf.track_id, 1, mf.track_number + %(shift)s,
                          (mf.duration_seconds * 1000)::int + %(longer)s, %(imported)s,
                          av.created_at + %(age)s::interval
                     FROM media_files mf JOIN album_variants av ON av.id = mf.album_variant_id
                     JOIN tracks t ON t.id = mf.track_id WHERE t.title = %(title)s"""
        plain = {"shift": 0, "longer": 0, "imported": False, "age": "1 minute"}
        for title, changed in (
                ("Sol", {}),                                   # the materialiser's own row
                ("Central Plains", {"age": "0"}),              # made with its copy's row
                ("Circles of Motion", {"longer": 1}),          # MusicBrainz's length, not the file's
                ("Patterns", {"shift": 10}),                   # the track at another position
                ("Dust", {"age": "-1 minute"}),                # older than the album's first copy
                ("Sky", {"imported": True})):                  # a peer's record
            cur.execute(_slot, {**plain, **changed, "title": title})
        # no file of the album holds the track, and a phantom's slot
        phantom = str(uuid.uuid4())
        cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'Never Owned')", (phantom,))
        for album_sql, title in (("(SELECT id FROM albums WHERE title = 'Movements')", "Hidden Track"),
                                 ("%s::uuid" % ("'" + phantom + "'"), "Never Owned I")):
            track = str(uuid.uuid4())
            cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (track, title))
            cur.execute(f"""INSERT INTO album_tracks (album_id, track_id, disc, position, length_ms, created_at)
                            VALUES ({album_sql}, %s, 1, 99, 200000, now() + interval '1 hour')""", (track,))
        cur.execute("UPDATE album_tracks SET author_pubkey = %s, signature = %s", SEAL)
    finally:
        cur.execute(DELTA.read_text(encoding="utf-8"))

    by_track = {track: source for _album, track, _pos, source in _slots(cur)}
    assert by_track == {"Sol": "tag", "Central Plains": "tag", "Circles of Motion": "mb", "Patterns": "mb",
                        "Dust": "mb", "Sky": "mb", "Hidden Track": "mb", "Never Owned I": "mb"}
    cur.execute("SELECT count(*) AS n FROM album_tracks WHERE signature IS NULL")
    assert cur.fetchone()["n"] == 0                # the guard was on, and no seal was shed

    # a second run is a no-op, whatever was written since
    cur.execute("UPDATE album_tracks SET source = 'mb'")
    cur.execute(DELTA.read_text(encoding="utf-8"))
    assert set(s[3] for s in _slots(cur)) == {"mb"}


def _objects(conn):
    """The catalogue's own text of what the delta makes."""
    with conn.cursor() as c:
        c.execute("""SELECT column_name, udt_name, is_nullable, column_default
                       FROM information_schema.columns
                      WHERE table_schema = 'public' AND table_name = 'album_tracks' ORDER BY 1""")
        columns = c.fetchall()
        c.execute("""SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
                      WHERE t.typname = 'tracklist_source' ORDER BY e.enumsortorder""")
        labels = c.fetchall()
        c.execute("SELECT pg_get_functiondef('seal_guard_album_tracks'::regproc)")
        return {"columns": columns, "labels": labels, "guard": c.fetchall()}


def test_the_baseline_and_its_delta_make_the_same_column(scratch_dsn):
    """001 alone, a node from before the column brought up by the delta, and
    001 with every delta hold the same objects; the delta again is a no-op."""
    name = "sautium_test_tracklist_source_baseline"
    assert conftest._recreate(name, create=True)
    try:
        c = psycopg2.connect(dbname=name, **conftest.PG)
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute((MIGRATIONS / "001_initial.sql").read_text(encoding="utf-8"))
            alone = _objects(c)
            cur.execute("ALTER TABLE album_tracks DROP COLUMN source")
            cur.execute("DROP TYPE tracklist_source")
            for _ in range(2):
                cur.execute(DELTA.read_text(encoding="utf-8"))
                assert _objects(c) == alone
        c.close()
    finally:
        conftest._recreate(name, create=False)
    c = psycopg2.connect(scratch_dsn)
    try:
        assert _objects(c) == alone
    finally:
        c.close()
    assert ("source", "tracklist_source", "NO", "'mb'::tracklist_source") in alone["columns"]
    assert alone["labels"] == [("mb",), ("tag",)]
