"""Nothing in the suite reaches the node's own database (tests/conftest.py):
a pool made from the configured settings — what a module reading the
database at import gets, or a timer that outlives its test once the test's
scratch pool is gone — opens the session's own database. Nor this machine's
HQPlayer folder: its settings and its log are the run's own, empty."""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from conftest import HQPLAYER_DIR, SESSION_DATABASE  # noqa: E402


def test_a_pool_made_from_the_settings_opens_the_sessions_database(monkeypatch):
    psycopg2 = pytest.importorskip("psycopg2")
    import db_pool
    from config import settings
    assert settings.postgres_db == SESSION_DATABASE

    monkeypatch.setattr(db_pool, "_pool", None)
    try:
        row = db_pool.db_query_one("SELECT current_database() AS db")
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL at the configured host: {e}")
    db_pool._pool.closeall()
    assert row["db"] == SESSION_DATABASE


def test_hqplayers_folder_is_the_runs_own_and_empty():
    # hqp_library.local_engine reads HQPlayer's settings from this folder too
    from config import settings
    from playback import hqp_diagnostics
    assert settings.hqplayer_data_dir == HQPLAYER_DIR
    assert hqp_diagnostics.local_log_dir() == Path(HQPLAYER_DIR)
    assert list(Path(HQPLAYER_DIR).iterdir()) == []
