"""Shared setup for the test tree.

The suite runs inside the backend container — the node's own environment,
its database named in POSTGRES_DB — and nothing here may reach that
database. A test that needs PostgreSQL takes `scratch_dsn` (a throwaway
database built from the migrations for its module) or builds its own, and
points db_pool and the SQLAlchemy sessions at it. Everything that connects
through config.settings instead lands in this session's database, created
empty before collection and dropped at the end: modules that read the
database at import still import (mb_backend takes an absent mb_artist for
"no dump", the same answer on every node), while anything that writes there
— a timer that outlives its test, a pool re-created lazily after a fixture
closed its scratch one — fails on the missing table instead of writing the
node's catalog. On 2026-10-09 a leaked queue persist overwrote the node's
player.queue, and the backend's next start crash-looped on it.

The peer protocol between two real nodes is not tested here: that is the
two-node checks (launcher stand <-> Docker) and the `--selftest` CLIs, per
CLAUDE.md "Testing Expectations".

Run from the repo root: `python -m pytest tests/`.
"""

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
SESSION_DATABASE = "sautium_pytest_session"

# Before any backend module builds config.settings.
os.environ.update(POSTGRES_HOST=PG["host"], POSTGRES_PORT=str(PG["port"]),
                  POSTGRES_USER=PG["user"], POSTGRES_PASSWORD=PG["password"],
                  POSTGRES_DB=SESSION_DATABASE)


def _admin():
    """The cluster's maintenance connection, or None where there is no
    cluster (no psycopg2, no server) — the database tests skip themselves."""
    try:
        import psycopg2
    except ImportError:
        return None
    try:
        conn = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError:
        return None
    conn.autocommit = True
    return conn


def _recreate(name: str, *, create: bool) -> bool:
    admin = _admin()
    if admin is None:
        return False
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        if create:
            cur.execute(f"CREATE DATABASE {name}")
    admin.close()
    return True


def pytest_sessionstart(session):
    _recreate(SESSION_DATABASE, create=True)


def pytest_sessionfinish(session, exitstatus):
    _recreate(SESSION_DATABASE, create=False)


@pytest.fixture(scope="module")
def scratch_dsn(request):
    """A throwaway database built from the migrations for the requesting
    module, dropped when the module is done."""
    name = "sautium_test_" + request.module.__name__.rsplit(".", 1)[-1].removeprefix("test_")
    if not _recreate(name, create=True):
        pytest.skip("no PostgreSQL for a scratch database")
    import psycopg2
    from desktop import db_init
    conn = psycopg2.connect(dbname=name, **PG)
    db_init.apply_migrations(conn)
    conn.commit()
    conn.close()
    yield f"postgresql://{PG['user']}:{PG['password']}@{PG['host']}:{PG['port']}/{name}"
    _recreate(name, create=False)
