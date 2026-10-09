"""The slice families in the active-conditions set
(backend/routers/settings._notices_state): a deferral is its cycle's status
row, dated by the onset the row keeps, and there is none on a node that
holds that family's dump, whatever the row still says.

On a real PostgreSQL — a throwaway database built from the migrations, the
connection pool pointed at it. Skipped where there is no cluster.
"""

import json
import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
settings_router = pytest.importorskip("routers.settings")

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
DBNAME = "sautium_notices_state_test"
ONSET = "2026-10-07T11:00:00+00:00"


@pytest.fixture(scope="module")
def dsn():
    try:
        admin = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL for the notices test: {e}")
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
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("DELETE FROM user_settings")
        yield c
    conn.close()
    pool.closeall()


def _setting(cur, key, value):
    cur.execute("INSERT INTO user_settings (key, value) VALUES (%s, %s::jsonb)",
                (key, json.dumps(value)))


def _status(cur, family, **facts):
    _setting(cur, f"{family}.status", {
        "pending": 200, "pending_capped": True, "served": 0, "unserved": 200,
        "reason": "no_sources", "sources": 0, "next_attempt_at": None,
        "since": ONSET, **facts})


def _deferred() -> dict:
    return {n["key"]: n for n in settings_router._notices_state()["items"]
            if n["key"].endswith(".deferred")}


def test_a_deferral_is_dated_by_its_onset(cur):
    _status(cur, "lb_slice")
    _status(cur, "mb_slice", served=200, unserved=0, reason="ok", since=None)
    items = _deferred()
    assert list(items) == ["lb_slice.deferred"]
    assert items["lb_slice.deferred"]["since"] == ONSET


def test_a_dump_node_has_no_deferral_for_its_family(cur):
    # The MB cycle's last run published "no source" while the load landed.
    _status(cur, "mb_slice")
    _status(cur, "lb_slice")
    _setting(cur, "musicbrainz.db_version", "20261007-002147")
    assert list(_deferred()) == ["lb_slice.deferred"]
