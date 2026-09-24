"""The MB dump lock is a readers-writer lock (backend/mb_dump_load.py
MB_LOAD_LOCK_KEY): only a dump load holds it exclusive; canon runs, the
discography reconcile's probe and slice serving share it. When canon runs held
it exclusive, every reconcile beside them read "a dump load is running" and a
dump node answered slice requests with 503.

On a real PostgreSQL — a throwaway database built from the migrations (advisory
locks are per database), the connection pool pointed at it. Skipped where there
is no cluster.
"""

import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
canon = pytest.importorskip("canon")
discography = pytest.importorskip("discography")
mb_dump_load = pytest.importorskip("mb_dump_load")
from desktop.p2p import mb_slice_queries  # noqa: E402

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
DBNAME = "sautium_mb_load_lock_test"
KEY = mb_dump_load.MB_LOAD_LOCK_KEY


@pytest.fixture(scope="module")
def dsn():
    try:
        admin = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL for the dump lock test: {e}")
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
def pool(dsn, monkeypatch):
    import db_pool
    import psycopg2.pool
    pool = psycopg2.pool.ThreadedConnectionPool(1, 4, dsn=dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    yield
    pool.closeall()


@pytest.fixture
def other_session(dsn):
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    yield conn.cursor()
    conn.close()


def test_canon_runs_share_the_lock_with_the_reconcile_and_slice_serving(pool, dsn, other_session):
    with canon._dump_lock_held() as first, canon._dump_lock_held() as second:
        assert first and second
        assert discography._mb_load_in_progress() is False
        serving = psycopg2.connect(dsn)
        slice_ = mb_slice_queries.get_slice_one(serving, "Sade")
        serving.close()
        assert slice_["artists_matched"] == {"Sade": []}
        # A load starting now waits for the readers.
        other_session.execute("SELECT pg_try_advisory_lock(%s)", (KEY,))
        assert other_session.fetchone()[0] is False
    other_session.execute("SELECT pg_try_advisory_lock(%s)", (KEY,))
    assert other_session.fetchone()[0] is True
    other_session.execute("SELECT pg_advisory_unlock(%s)", (KEY,))


def test_a_dump_load_holds_every_reader_off(pool, dsn, other_session):
    other_session.execute("SELECT pg_advisory_lock(%s)", (KEY,))
    try:
        assert discography._mb_load_in_progress() is True
        with canon._dump_lock_held() as got:
            assert got is False
        serving = psycopg2.connect(dsn)
        with pytest.raises(mb_slice_queries.DumpBusy):
            mb_slice_queries.get_slice_one(serving, "Sade")
        serving.close()
    finally:
        other_session.execute("SELECT pg_advisory_unlock(%s)", (KEY,))
    assert discography._mb_load_in_progress() is False
