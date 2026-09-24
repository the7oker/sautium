"""Home › Favourite artists follows the taste of the last months
(backend/routers/home.py get_favourite_artists).

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
DBNAME = "sautium_favourite_artists_test"
# Two co-primary artists of one duet: their weights tie, the id decides.
DUO_A, DUO_B = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


@pytest.fixture(scope="module")
def dsn():
    try:
        admin = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL for the favourite artists test: {e}")
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
        c.execute("TRUNCATE listening_history, seed_picks, tracks, albums, artists CASCADE")
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


@pytest.mark.parametrize("newest", [datetime.now(timezone.utc) - timedelta(hours=2),
                                    datetime(2019, 5, 1, 20, 0, tzinfo=timezone.utc)],
                         ids=["active", "went-quiet"])
def test_the_shelf_follows_the_taste_of_the_last_months(cur, newest):
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


def test_a_node_with_no_listens_shows_the_seed_picks(cur):
    first, second = _artist(cur, "First Pick"), _artist(cur, "Second Pick")
    cur.execute("INSERT INTO seed_picks (album_id, tier, rank) VALUES (%s, 2, 1), (%s, 1, 2)",
                (_album(cur, "One", second), _album(cur, "Two", first)))

    shelf = home.get_favourite_artists(limit=1)["artists"]

    assert [a["name"] for a in shelf] == ["First Pick"]
