"""One definition of an orphan track (backend/canon/identity.py ORPHAN_TRACK_SQL).

A listen cascades with its track, so every deleter of tracks must spare what
the owner did: the orphan sweep, the discography reconcile (which also runs
the explicit phantom-layer removal) and the scanner's prune. Runs against a
real PostgreSQL — a throwaway database built from the migrations — skipped
where there is none.
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
sqlalchemy = pytest.importorskip("sqlalchemy")
identity = pytest.importorskip("canon.identity")

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
DBNAME = "sautium_orphan_tracks_test"
T0 = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def dsn():
    try:
        admin = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL for the orphan-track test: {e}")
    admin.autocommit = True
    from desktop import db_init, node_backup as nb
    nb._drop_database(admin, DBNAME)
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {DBNAME}")
    conn = psycopg2.connect(dbname=DBNAME, **PG)
    db_init.apply_migrations(conn)
    conn.commit()
    conn.close()
    yield (f"postgresql://{PG['user']}:{PG['password']}@{PG['host']}:{PG['port']}/{DBNAME}")
    nb._drop_database(admin, DBNAME)
    admin.close()


@pytest.fixture
def conn(dsn):
    c = psycopg2.connect(dsn, options="-c timezone=UTC")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("TRUNCATE tracks, albums, artists CASCADE")
    yield c
    c.close()


@pytest.fixture
def session(dsn):
    from sqlalchemy.orm import Session
    engine = sqlalchemy.create_engine(dsn)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _tracks(cur, *ids):
    for t in ids:
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (str(t), f"t{t.int}"))


def _listen(cur, track, at=T0, completed=True):
    cur.execute("""INSERT INTO listening_history
                       (track_id, started_at, ended_at, duration_listened, completed, skipped)
                   VALUES (%s, %s, %s, 200, %s, %s)""",
                (str(track), at, at + timedelta(seconds=200), completed, not completed))


def _survivors(cur, *ids):
    cur.execute("SELECT id FROM tracks WHERE id = ANY(CAST(%s AS uuid[]))",
                ([str(t) for t in ids],))
    return {uuid.UUID(str(r[0])) for r in cur.fetchall()}


def test_the_sweep_spares_a_listen_a_demo_play_and_a_slot(conn, session):
    bare, listened, demo, slotted = (uuid.uuid4() for _ in range(4))
    album = uuid.uuid4()
    with conn.cursor() as cur:
        _tracks(cur, bare, listened, demo, slotted)
        _listen(cur, listened)
        cur.execute("INSERT INTO demo_plays (track_id, provider) VALUES (%s, 'youtube')", (str(demo),))
        cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'A')", (str(album),))
        cur.execute("INSERT INTO album_tracks (album_id, track_id, position) VALUES (%s, %s, 1)",
                    (str(album), str(slotted)))
    assert identity.delete_orphan_tracks(session) == 1
    session.commit()
    with conn.cursor() as cur:
        assert _survivors(cur, bare, listened, demo, slotted) == {listened, demo, slotted}


def test_the_reconcile_takes_the_album_but_never_the_listen(conn, dsn, monkeypatch):
    import db_pool
    import discography
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=dsn, options="-c timezone=UTC")
    monkeypatch.setattr(db_pool, "_pool", pool)
    artist, album = uuid.uuid4(), uuid.uuid4()
    heard, unheard = uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'Phantom Artist')", (str(artist),))
        cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'Phantom Album')", (str(album),))
        cur.execute("INSERT INTO album_artists (album_id, artist_id) VALUES (%s, %s)",
                    (str(album), str(artist)))
        _tracks(cur, heard, unheard)
        for pos, t in enumerate((heard, unheard), 1):
            cur.execute("INSERT INTO track_artists (track_id, artist_id) VALUES (%s, %s)",
                        (str(t), str(artist)))
            cur.execute("INSERT INTO album_tracks (album_id, track_id, position) VALUES (%s, %s, %s)",
                        (str(album), str(t), pos))
        _listen(cur, heard)
    try:
        # Not canonized: the reconcile unlinks every phantom album of the artist.
        discography._reconcile_phantoms(str(artist), [])
    finally:
        pool.closeall()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM albums WHERE id = %s", (str(album),))
        assert cur.fetchone() == (0,)
        assert _survivors(cur, heard, unheard) == {heard}
        cur.execute("SELECT count(*) FROM listening_history WHERE track_id = %s", (str(heard),))
        assert cur.fetchone() == (1,)


def test_a_merge_rederives_the_survivors_stats_from_its_history(conn, session):
    old, new = uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        _tracks(cur, old, new)
        _listen(cur, old, T0)
        _listen(cur, old, T0 + timedelta(hours=1))
        _listen(cur, new, T0 + timedelta(hours=2))
        _listen(cur, new, T0 + timedelta(hours=3), completed=False)
        # A counter row that drifted from its history.
        cur.execute("""INSERT INTO local_play_stats (track_id, play_count, skip_count)
                       VALUES (%s, 99, 0)""", (str(new),))
    identity._update_track_uuid(session, old, new)
    session.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT play_count, skip_count FROM local_play_stats WHERE track_id = %s",
                    (str(new),))
        assert cur.fetchone() == (3, 1)
        assert _survivors(cur, old, new) == {new}


def test_stats_follow_a_history_that_is_gone(conn):
    from play_stats import refresh_play_stats
    track = uuid.uuid4()
    with conn.cursor() as cur:
        _tracks(cur, track)
        _listen(cur, track)
        refresh_play_stats(cur, [track])
        cur.execute("DELETE FROM listening_history WHERE track_id = %s", (str(track),))
        refresh_play_stats(cur, [track])
        cur.execute("SELECT count(*) FROM local_play_stats WHERE track_id = %s", (str(track),))
        assert cur.fetchone() == (0,)
