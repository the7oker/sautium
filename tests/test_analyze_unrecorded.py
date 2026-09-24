"""After a crash recovery PostgreSQL holds no analysis on record for any
table, and autovacuum's counters start from zero; the backend gives the
statistics back at start (backend/db_migrate.py analyze_unrecorded).

On a real PostgreSQL — a throwaway database built from the migrations, in the
state a recovery leaves: nothing analyzed yet. Skipped where there is no
cluster.
"""

import os
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
db_migrate = pytest.importorskip("db_migrate")

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
DBNAME = "sautium_analyze_unrecorded_test"


@pytest.fixture
def dsn():
    try:
        admin = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL for the analyze test: {e}")
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


def test_every_table_without_an_analysis_on_record_is_analyzed_once(dsn):
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'Sade'), (%s, 'Yello')",
                    (str(uuid.uuid4()), str(uuid.uuid4())))
        cur.execute("""SELECT count(*) FROM pg_stat_user_tables
                       WHERE last_analyze IS NULL AND last_autoanalyze IS NULL""")
        unrecorded = cur.fetchone()[0]
        assert unrecorded > 0

        assert db_migrate.analyze_unrecorded(dsn) == unrecorded

        cur.execute("SELECT count(*) FROM pg_stats WHERE tablename = 'artists'")
        assert cur.fetchone()[0] > 0
        # Recorded now: the next start — a clean one — has nothing to do.
        assert db_migrate.analyze_unrecorded(dsn) == 0
    conn.close()
