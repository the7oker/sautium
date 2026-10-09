"""Shared setup for the test tree.

The suite runs inside the backend container — the node's own environment,
its database named in POSTGRES_DB — and nothing here may reach that
database. A test that needs PostgreSQL builds a scratch database from its
own DSN (the SAUTIUM_TEST_PG* settings) and points db_pool and the
SQLAlchemy sessions at it. Everything that connects through config.settings
instead — modules that read the database at import (mb_backend picks its
MusicBrainz source there), a timer that outlives its test, a pool re-created
lazily after a fixture closed its scratch one — lands in this session's own
database: built from the migrations before collection, dropped at the end.
On 2026-10-09 a leaked queue persist overwrote the node's player.queue, and
the backend's next start crash-looped on it.

The peer protocol between two real nodes is not tested here: that is the
two-node checks (launcher stand <-> Docker) and the `--selftest` CLIs, per
CLAUDE.md "Testing Expectations".

Run from the repo root: `python -m pytest tests/`.
"""

import os
import sys
from pathlib import Path

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


def pytest_sessionstart(session):
    admin = _admin()
    if admin is None:
        return
    import psycopg2
    from desktop import db_init, node_backup
    node_backup._drop_database(admin, SESSION_DATABASE)   # what a killed run left
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {SESSION_DATABASE}")
    admin.close()
    conn = psycopg2.connect(dbname=SESSION_DATABASE, **PG)
    db_init.apply_migrations(conn)
    conn.commit()
    conn.close()


def pytest_sessionfinish(session, exitstatus):
    admin = _admin()
    if admin is None:
        return
    from desktop import node_backup
    node_backup._drop_database(admin, SESSION_DATABASE)
    admin.close()
