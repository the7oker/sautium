"""Listen starts written before 2026-09-24 (backend/play_stats.py).

The tracker stamped a listen's start with the process's naive local clock and
wrote it through a UTC session, so a launcher's history sits hours off by the
host's UTC offset. `repaired_start` decides, per row, whether the stored start
is that shifted wall clock; `repair_naive_listen_times` rewrites the rows and
runs against a real PostgreSQL (a throwaway database built from the
migrations), skipped where there is none.

Zones are POSIX TZ strings, so the decision is exercised without the tz
database in the container.
"""

import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

play_stats = pytest.importorskip("play_stats")

KYIV = "EET-2EEST,M3.5.0/3,M10.5.0/4"
NEW_YORK = "EST5EDT,M3.2.0,M11.1.0"


@pytest.fixture
def zone(monkeypatch):
    def set_zone(tz: str) -> None:
        monkeypatch.setenv("TZ", tz)
        time.tzset()
    yield set_zone
    monkeypatch.undo()
    time.tzset()


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def stored(true_start: datetime, offset_hours: float) -> datetime:
    """What the pre-fix tracker wrote: the local wall clock read as UTC."""
    return true_start + timedelta(hours=offset_hours)


def test_an_east_listen_after_its_own_end_is_moved_back(zone):
    zone(KYIV)
    start = utc(2026, 9, 22, 16, 8, 17)
    assert play_stats.repaired_start(stored(start, 3), start + timedelta(seconds=173), 173) == start


def test_the_stand_row_closed_two_hours_later_is_still_moved_back(zone):
    # The newest row on the launcher stand: 173 s listened, the session closed
    # 2 h 10 min later. By fit alone the shifted start is closer to the end —
    # but a start after the end is impossible, and that decides first.
    zone(KYIV)
    start = utc(2026, 9, 22, 16, 8, 17)
    end = utc(2026, 9, 22, 18, 18, 56)
    assert play_stats.repaired_start(stored(start, 3), end, 173) == start


def test_winter_listens_move_by_the_winter_offset(zone):
    zone(KYIV)
    start = utc(2026, 1, 15, 10, 0, 0)
    assert play_stats.repaired_start(stored(start, 2), start + timedelta(seconds=200), 200) == start


def test_a_row_merged_from_a_utc_node_stays_in_the_east(zone):
    zone(KYIV)
    start = utc(2026, 9, 22, 16, 8, 17)
    assert play_stats.repaired_start(start, start + timedelta(seconds=173), 173) is None


def test_a_west_listen_that_began_hours_early_is_moved_forward(zone):
    zone(NEW_YORK)
    start = utc(2026, 9, 22, 16, 0, 0)
    assert play_stats.repaired_start(stored(start, -4), start + timedelta(seconds=180), 180) == start


def test_a_row_merged_from_a_utc_node_stays_in_the_west(zone):
    zone(NEW_YORK)
    start = utc(2026, 9, 22, 16, 0, 0)
    assert play_stats.repaired_start(start, start + timedelta(seconds=180), 180) is None


def test_a_utc_process_changes_nothing(zone):
    zone("UTC0")
    start = utc(2026, 9, 22, 16, 0, 0)
    # The master's rows whose PostgreSQL end landed a second before the start.
    assert play_stats.repaired_start(start, start - timedelta(seconds=1), 180) is None
    assert play_stats.repaired_start(start, start + timedelta(seconds=180), 180) is None


def test_a_row_without_an_end_is_left_alone(zone):
    zone(KYIV)
    assert play_stats.repaired_start(utc(2026, 9, 22, 19, 0, 0), None, 180) is None


# ---------------------------------------------------------------------------
# The rewrite, on a real cluster
# ---------------------------------------------------------------------------

psycopg2 = pytest.importorskip("psycopg2")

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
DBNAME = "sautium_listen_times_test"


@pytest.fixture(scope="module")
def db():
    try:
        admin = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL for the repair test: {e}")
    admin.autocommit = True
    from desktop import db_init, node_backup as nb
    nb._drop_database(admin, DBNAME)
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {DBNAME}")
    conn = psycopg2.connect(dbname=DBNAME, options="-c timezone=UTC", **PG)
    db_init.apply_migrations(conn)
    conn.commit()
    yield conn
    conn.close()
    nb._drop_database(admin, DBNAME)
    admin.close()


def test_the_rewrite_moves_shifted_rows_and_rederives_their_stats(db, zone):
    zone(KYIV)
    shifted, merged = uuid.uuid4(), uuid.uuid4()
    start = utc(2026, 9, 22, 16, 8, 17)
    with db.cursor() as cur:
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 'a'), (%s, 'b')",
                    (str(shifted), str(merged)))
        cur.execute("""INSERT INTO listening_history
                           (track_id, started_at, ended_at, duration_listened, completed, skipped)
                       VALUES (%s, %s, %s, 173, TRUE, FALSE), (%s, %s, %s, 173, TRUE, FALSE)""",
                    (str(shifted), stored(start, 3), start + timedelta(seconds=173),
                     str(merged), start, start + timedelta(seconds=173)))
        touched = play_stats.repair_naive_listen_times(cur)
        play_stats.refresh_play_stats(cur, touched)
        assert touched == [str(shifted)]
        cur.execute("SELECT track_id::text, started_at FROM listening_history ORDER BY track_id")
        assert {t: s for t, s in cur.fetchall()} == {str(shifted): start, str(merged): start}
        # A second pass finds nothing left to move.
        assert play_stats.repair_naive_listen_times(cur) == []
        cur.execute("SELECT play_count FROM local_play_stats WHERE track_id = %s", (str(shifted),))
        assert cur.fetchone() == (1,)
    db.rollback()
