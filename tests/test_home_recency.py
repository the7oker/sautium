"""Home's Favourite artists and Recommendations follow the taste of the last
months, counted back from the newest listen (backend/routers/home.py): a node
whose owner went quiet keeps the shelves it had.

On a real PostgreSQL — a throwaway database built from the migrations, the
connection pool pointed at it. Skipped where there is no cluster.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
home = pytest.importorskip("routers.home")

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
DBNAME = "sautium_home_recency_test"
# Two co-primary artists of one duet: their weights tie, the id decides.
DUO_A, DUO_B = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
NEWEST = pytest.mark.parametrize(
    "newest", [datetime.now(timezone.utc) - timedelta(hours=2),
               datetime(2019, 5, 1, 20, 0, tzinfo=timezone.utc)],
    ids=["active", "went-quiet"])


@pytest.fixture(scope="module")
def dsn():
    try:
        admin = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL for the Home recency test: {e}")
    admin.autocommit = True
    from desktop import db_init, node_backup as nb
    nb._drop_database(admin, DBNAME)
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {DBNAME}")
    conn = psycopg2.connect(dbname=DBNAME, **PG)
    db_init.apply_migrations(conn)
    conn.commit()
    conn.close()
    yield f"postgresql://{PG['user']}:{PG['password']}@{PG['host']}:{PG['port']}/{DBNAME}"
    nb._drop_database(admin, DBNAME)
    admin.close()


@pytest.fixture
def cur(dsn, monkeypatch):
    import db_pool
    import psycopg2.pool
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=dsn, options="-c timezone=UTC")
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(dsn, options="-c timezone=UTC")
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("TRUNCATE listening_history, seed_picks, embedding_models, tracks, albums, "
                  "artists CASCADE")
        yield c
    conn.close()
    pool.closeall()


def _artist(cur, name, artist_id=None):
    artist_id = artist_id or str(uuid.uuid4())
    cur.execute("INSERT INTO artists (id, name) VALUES (%s, %s)", (artist_id, name))
    return artist_id


def _track(cur, *credits):
    track = str(uuid.uuid4())
    cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (track, f"t{track[:8]}"))
    for artist, role in credits:
        cur.execute("INSERT INTO track_artists (track_id, artist_id, role) VALUES (%s, %s, %s)",
                    (track, artist, role))
    return track


def _listen(cur, track, at, seconds, completed=True):
    cur.execute("""INSERT INTO listening_history
                       (track_id, started_at, ended_at, duration_listened, completed, skipped)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (track, at, at + timedelta(seconds=seconds), seconds, completed, not completed))


def _album(cur, title, artist):
    album = str(uuid.uuid4())
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, %s)", (album, title))
    cur.execute("INSERT INTO album_artists (album_id, artist_id, role) VALUES (%s, %s, 'primary')",
                (album, artist))
    return album


@NEWEST
def test_favourite_artists_follow_the_taste_of_the_last_months(cur, newest):
    recent = _artist(cur, "Recent Love")
    old = _artist(cur, "Old Flame")
    gone = _artist(cur, "Long Gone")
    guest = _artist(cur, "Guest Singer")
    skipped = _artist(cur, "Skipped Twice")
    seed = _artist(cur, "Seed Artist")
    _artist(cur, "Duo A", DUO_A)
    _artist(cur, "Duo B", DUO_B)

    # One ten-minute listen today outweighs 2.5 hours from 400 days ago
    # (600 against 9000 × e^-4.4 ≈ 106), and twenty hours from 800 days ago
    # are past the window; all-time totals would have ranked them backwards.
    _listen(cur, _track(cur, (recent, "primary"), (guest, "featured")), newest, 600)
    old_track = _track(cur, (old, "primary"))
    for n in range(5):
        _listen(cur, old_track, newest - timedelta(days=400, hours=n), 1800)
    gone_track = _track(cur, (gone, "primary"))
    for n in range(20):
        _listen(cur, gone_track, newest - timedelta(days=800, hours=n), 3600)
    _listen(cur, _track(cur, (skipped, "primary")), newest - timedelta(days=1), 3600,
            completed=False)
    _listen(cur, _track(cur, (DUO_B, "primary"), (DUO_A, "primary")),
            newest - timedelta(days=30), 300)

    # The owned artist styles as owned; the seed pick trails the listened ones.
    cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, '/music/old') "
                "RETURNING id", (_album(cur, "Old Album", old),))
    cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path) "
                "VALUES (%s, %s, '/music/old/01.flac')", (old_track, cur.fetchone()[0]))
    cur.execute("INSERT INTO seed_picks (album_id, tier, rank) VALUES (%s, 1, 1)",
                (_album(cur, "Seed Album", seed),))

    shelf = home.get_favourite_artists(limit=10)["artists"]

    assert [a["name"] for a in shelf] == [
        "Recent Love", "Duo A", "Duo B", "Old Flame", "Seed Artist"]
    assert [a["id"] for a in shelf][:3] == [recent, DUO_A, DUO_B]
    assert {a["name"]: a["is_owned"] for a in shelf} == {
        "Recent Love": False, "Duo A": False, "Duo B": False,
        "Old Flame": True, "Seed Artist": False}


def test_favourites_of_a_node_with_no_listens_are_the_seed_picks(cur):
    first, second = _artist(cur, "First Pick"), _artist(cur, "Second Pick")
    cur.execute("INSERT INTO seed_picks (album_id, tier, rank) VALUES (%s, 2, 1), (%s, 1, 2)",
                (_album(cur, "One", second), _album(cur, "Two", first)))

    shelf = home.get_favourite_artists(limit=1)["artists"]

    assert [a["name"] for a in shelf] == ["First Pick"]


def _embed(cur, model, track, head):
    vector = "[" + ",".join(map(str, head + (0.0,) * (512 - len(head)))) + "]"
    cur.execute("INSERT INTO embeddings (vector, model_id, track_id) VALUES (%s::vector, %s, %s)",
                (vector, model, track))


def _phantom_album(cur, title, model, *vectors):
    """A tracklist-only album (no files: the owned-album fills stay empty),
    one analysed track per vector; returns the track ids."""
    album = str(uuid.uuid4())
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, %s)", (album, title))
    tracks = []
    for position, head in enumerate(vectors, 1):
        track = _track(cur)
        cur.execute("INSERT INTO album_tracks (album_id, track_id, position) VALUES (%s, %s, %s)",
                    (album, track, position))
        _embed(cur, model, track, head)
        tracks.append(track)
    return tracks


def _owned_album(cur, title, model, head):
    """A scanned album with no tracklist — reached through its files alone —
    of one analysed track; returns the track id."""
    album = str(uuid.uuid4())
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, %s)", (album, title))
    cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, %s) "
                "RETURNING id", (album, f"/music/{album}"))
    variant = cur.fetchone()[0]
    track = _track(cur)
    cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path) "
                "VALUES (%s, %s, %s)", (track, variant, f"/music/{album}/01.flac"))
    _embed(cur, model, track, head)
    return track


def _clap(cur):
    model = str(uuid.uuid4())
    cur.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, 'clap', 512)",
                (model,))
    return model


@NEWEST
def test_recommendations_stay_the_shelf_the_listener_left(cur, newest):
    model = _clap(cur)
    # The last listen seeds the shelf; its album is heard, so it never comes
    # back. Every other record sounds alike: never heard, heard 75 days
    # before the last listen (inside the forgotten threshold, still heard)
    # and heard 200 days before it (forgotten, after the unheard one).
    seed, _ = _phantom_album(cur, "Heard Last", model, (1.0,), (1.0, 0.05))
    _phantom_album(cur, "Never Heard", model, (1.0, 0.1))
    (season,) = _phantom_album(cur, "Heard This Season", model, (1.0, 0.15))
    (forgotten,) = _phantom_album(cur, "Long Forgotten", model, (1.0, 0.2))
    _listen(cur, seed, newest, 300)
    _listen(cur, season, newest - timedelta(days=75), 300)
    _listen(cur, forgotten, newest - timedelta(days=200), 300)
    home._ranking.rebuild()

    shelf = home.get_recommendations(limit=5)["albums"]

    assert [a["title"] for a in shelf] == ["Never Heard", "Long Forgotten"]


@NEWEST
def test_an_owned_record_heard_through_its_files_stays_off_the_shelf(cur, newest):
    model = _clap(cur)
    # The owned record heard this season sounds closest to the seed; only
    # its files make it the album of that listen.
    (seed,) = _phantom_album(cur, "Heard Last", model, (1.0,))
    season = _owned_album(cur, "Owned, Heard This Season", model, (1.0, 0.02))
    _owned_album(cur, "Owned, Never Heard", model, (1.0, 0.1))
    _phantom_album(cur, "Never Heard", model, (1.0, 0.15))
    _listen(cur, seed, newest, 300)
    _listen(cur, season, newest - timedelta(days=75), 300)
    home._ranking.rebuild()

    shelf = home.get_recommendations(limit=2)["albums"]

    assert [a["title"] for a in shelf] == ["Owned, Never Heard", "Never Heard"]


def test_home_serves_the_ranking_the_last_history_write_rebuilt(cur):
    model = _clap(cur)
    newest = datetime.now(timezone.utc) - timedelta(hours=2)
    (seed,) = _phantom_album(cur, "Heard Last", model, (1.0,))
    (unheard,) = _phantom_album(cur, "Never Heard", model, (1.0, 0.1))
    _phantom_album(cur, "Also Never Heard", model, (1.0, 0.2))
    _listen(cur, seed, newest, 300)
    home._ranking.rebuild()
    before = [a["title"] for a in home.get_recommendations(limit=5)["albums"]]

    # Home reads what the last rebuild stored; the listener rebuilds on the
    # write, and the record just heard leaves the shelf.
    _listen(cur, unheard, newest + timedelta(minutes=5), 300)
    stored = [a["title"] for a in home.get_recommendations(limit=5)["albums"]]
    home._ranking.rebuild()
    after = [a["title"] for a in home.get_recommendations(limit=5)["albums"]]

    assert before == stored == ["Never Heard", "Also Never Heard"]
    assert after == ["Also Never Heard"]


def test_every_history_write_wakes_the_ranking_listener(cur):
    track = _track(cur)
    cur.execute("LISTEN sautium_listens")

    _listen(cur, track, datetime.now(timezone.utc), 300)
    cur.execute("UPDATE listening_history SET skipped = NOT skipped")
    cur.execute("DELETE FROM listening_history")
    cur.connection.poll()

    assert [n.channel for n in cur.connection.notifies] == ["sautium_listens"] * 3
