"""The same listen, recorded twice (backend/play_stats.same_listen).

Sautium scrobbles what it plays, so a Last.fm import hands this node's own
listens back — a second or so off, and possibly under an autocorrected name.
Where one record is such an imported scrobble, "the same listen" is the start
within 10 s and the native record wins; two native records keep their exact
(track, start) key. Exercised through the life-data merge on a real
PostgreSQL (a throwaway database built from the migrations), skipped where
there is none.
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
life_merge = pytest.importorskip("life_merge")

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
DBNAME = "sautium_same_listen_test"
T0 = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def conn():
    try:
        admin = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL for the same-listen test: {e}")
    admin.autocommit = True
    from desktop import db_init, node_backup as nb
    nb._drop_database(admin, DBNAME)
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {DBNAME}")
    c = psycopg2.connect(dbname=DBNAME, options="-c timezone=UTC", **PG)
    db_init.apply_migrations(c)
    c.commit()
    yield c
    c.close()
    nb._drop_database(admin, DBNAME)
    admin.close()


def _scratch(cur):
    cur.execute(f"CREATE SCHEMA {life_merge.SCRATCH}")
    for table in life_merge.LIFE_TABLES:
        life_merge._create_scratch_table(cur, table, [])


def _listen(cur, table, track, at, *, source="sautium", completed=True):
    cur.execute(f"""INSERT INTO {table} (track_id, started_at, ended_at, duration_listened,
                                         completed, skipped, source)
                    VALUES (%s, %s, %s, 200, %s, %s, %s)""",
                (str(track), at, at + timedelta(seconds=200), completed, not completed, source))


def test_the_native_record_wins_and_two_natives_keep_their_key(conn):
    native, autocorrected, heard_here, heard_there, skipped = (uuid.uuid4() for _ in range(5))
    twin_native, twin_imported, parallel = (uuid.uuid4() for _ in range(3))
    live, scratch = "listening_history", f"{life_merge.SCRATCH}.listening_history"
    with conn.cursor() as cur:
        for t in (native, autocorrected, heard_here, heard_there, skipped,
                  twin_native, twin_imported, parallel):
            cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 'x')", (str(t),))
        _scratch(cur)
        # 1. Here: Last.fm's record of a listen (autocorrected to another
        #    track); there: the native record of it, a second off → replaced.
        _listen(cur, live, autocorrected, T0, source="lastfm")
        _listen(cur, scratch, native, T0 + timedelta(seconds=1))
        # 2. Here: a native listen; there: its scrobble imported → skipped.
        _listen(cur, live, heard_here, T0 + timedelta(hours=1))
        _listen(cur, scratch, heard_there, T0 + timedelta(hours=1, seconds=2), source="lastfm")
        # 3. Both records inside the backup → only the native one lands.
        _listen(cur, scratch, twin_native, T0 + timedelta(hours=2))
        _listen(cur, scratch, twin_imported, T0 + timedelta(hours=2, seconds=1), source="lastfm")
        # 4. Two machines playing at once: two native listens, both land.
        _listen(cur, live, parallel, T0 + timedelta(hours=3))
        _listen(cur, scratch, native, T0 + timedelta(hours=3, seconds=3))
        # 5. A skip keeps the exact key.
        _listen(cur, scratch, skipped, T0 + timedelta(hours=4), completed=False)
        # A card generated from imported listens is not merged.
        cur.execute(f"""INSERT INTO {life_merge.SCRATCH}.listening_sessions
                            (id, origin, ended_at, source) VALUES (%s, 'mix', %s, 'lastfm')""",
                    (str(uuid.uuid4()), T0))

        out = life_merge.merge_life(conn)

        cur.execute("SELECT track_id, source FROM listening_history ORDER BY started_at")
        rows = [(uuid.UUID(str(t)), s) for t, s in cur.fetchall()]
        assert rows == [(native, "sautium"), (heard_here, "sautium"), (twin_native, "sautium"),
                        (parallel, "sautium"), (native, "sautium"), (skipped, "sautium")]
        assert out["merged"]["listens"] == 4 and out["merged"]["listens_replaced"] == 1
        assert out["merged"]["sessions"] == 0
        cur.execute("SELECT count(*) FROM local_play_stats WHERE track_id = %s", (str(autocorrected),))
        assert cur.fetchone() == (0,)
        cur.execute("SELECT play_count FROM local_play_stats WHERE track_id = %s", (str(native),))
        assert cur.fetchone() == (2,)
    conn.rollback()


def test_two_nodes_that_imported_one_history_merge_to_one_listen_per_scrobble(conn):
    """Both nodes walked the same Last.fm account and each placed the
    scrobbles on the tracks it had; one node's backup is merged into the
    other. Every scrobble stays one listen: where both placed it, this node's
    placement stands; where only the backup did, on a track known here, it
    lands; a native record whose track is unknown here takes nothing away."""
    here, there, only_there, rip_there = (uuid.uuid4() for _ in range(4))
    live, scratch = "listening_history", f"{life_merge.SCRATCH}.listening_history"
    with conn.cursor() as cur:
        for t in (here, there, only_there):          # rip_there: the other machine's file only
            cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 'x')", (str(t),))
        _scratch(cur)
        # 1. Both placed the scrobble — on different tracks.
        _listen(cur, live, here, T0, source="lastfm")
        _listen(cur, scratch, there, T0, source="lastfm")
        # 2. Only the other node placed it, on a track this node knows.
        _listen(cur, scratch, only_there, T0 + timedelta(hours=1), source="lastfm")
        # 3. The other node played it on its own rip and scrobbled it; this
        #    node imported the scrobble. The rip is unknown here.
        _listen(cur, live, here, T0 + timedelta(hours=2), source="lastfm")
        _listen(cur, scratch, rip_there, T0 + timedelta(hours=2, seconds=1))
        from play_stats import refresh_play_stats
        refresh_play_stats(cur, [str(here)])

        out = life_merge.merge_life(conn)

        cur.execute("SELECT track_id, source FROM listening_history ORDER BY started_at")
        assert [(uuid.UUID(str(t)), s) for t, s in cur.fetchall()] == [
            (here, "lastfm"), (only_there, "lastfm"), (here, "lastfm")]
        assert (out["merged"]["listens"], out["merged"]["listens_replaced"]) == (1, 0)
        cur.execute("SELECT track_id, play_count FROM local_play_stats ORDER BY play_count")
        assert [(uuid.UUID(str(t)), n) for t, n in cur.fetchall()] == [(only_there, 1), (here, 2)]
    conn.rollback()
