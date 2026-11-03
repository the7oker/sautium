"""The support switch, "Share diagnostics with support", against a real
PostgreSQL built from the migrations (desktop/p2p/diag_events.SWITCH_ALLOWS):
nothing is recorded for support while it is off; switched back on, nothing
from before goes — not an event that waited from an earlier on-period, not a
spooled one older than the change, not a bundle collected before it; and a
bundle withdrawn so is deleted, its warrant closed, with no request made.
Skipped without a cluster."""

import asyncio
import json
import time
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from desktop.p2p import diag_events  # noqa: E402


@pytest.fixture
def conn(scratch_dsn):
    c = psycopg2.connect(scratch_dsn)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("DELETE FROM diag_events")
        cur.execute("DELETE FROM diag_warrants")
        cur.execute("DELETE FROM user_settings WHERE key = 'support.diagnostics_enabled'")
    yield c
    c.close()


def _switch(conn, on: bool) -> None:
    """The switch as the Web UI writes it (routers/settings._write): dated by
    the database — an update by the table's own trigger."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO user_settings (key, value) VALUES ('support.diagnostics_enabled', %s::jsonb)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (json.dumps(on),))


def _db_now(conn) -> datetime:
    """The database's clock — the one the switch's last change is dated by."""
    with conn.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        return cur.fetchone()[0]


def _count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM diag_events")
        return cur.fetchone()[0]


def _warrant(conn) -> str:
    wid = str(uuid.uuid4())
    now = int(time.time())
    diag_events.receive_warrant(conn, {"id": wid, "issuer": "ab" * 32, "issued_at": now,
                                       "expires_at": now + 600, "scopes": ["system"],
                                       "since": None})
    return wid


def test_with_the_switch_off_nothing_is_recorded(conn):
    _switch(conn, False)
    assert diag_events.record(conn, "sync.failed", {"error": "x"}) is None
    assert _count(conn) == 0


def test_switched_back_on_nothing_recorded_before_goes(conn):
    # recorded while on and never sent: the master was out of reach
    before = datetime.now(timezone.utc) - timedelta(minutes=10)
    assert diag_events.record(conn, "sync.failed", {"error": "before"}, ts=before.isoformat())
    _switch(conn, False)
    _switch(conn, True)
    after = diag_events.record(conn, "sync.failed", {"error": "after"})
    assert {e["id"] for e in diag_events.drain_unreported(conn)} == {after}
    assert diag_events.pending_count(conn) == 1
    assert {e["id"] for e in diag_events.recent(conn, 50)} == {after}


def test_a_spooled_event_from_before_the_switch_last_changed_is_dropped(conn, tmp_path):
    diag_events.spool(tmp_path, "service.start_failed", {"step": "start_all"})
    _switch(conn, True)
    diag_events.drain_spool(conn, tmp_path)
    assert _count(conn) == 0
    # and one spooled since is recorded
    diag_events.spool(tmp_path, "service.start_failed", {"step": "start_all"})
    diag_events.drain_spool(conn, tmp_path)
    assert _count(conn) == 1


def test_a_bundle_leaves_only_while_the_switch_stayed_on_since_its_collection_began(conn):
    began = datetime.now(timezone.utc) - timedelta(minutes=5)
    kept = _warrant(conn)
    diag_events.mark_collected(conn, kept, 100, began)
    assert diag_events.may_upload(conn, kept)               # never switched: the default, on
    _switch(conn, False)
    assert not diag_events.may_upload(conn, kept)           # off
    _switch(conn, True)
    assert not diag_events.may_upload(conn, kept)           # back on: still withdrawn
    fresh = _warrant(conn)
    diag_events.mark_collected(conn, fresh, 100, _db_now(conn))
    assert diag_events.may_upload(conn, fresh)              # collected after it came back on
    assert not diag_events.may_upload(conn, _warrant(conn))  # nothing collected, nothing to leave


def test_a_withdrawn_bundle_is_deleted_and_closed_with_no_request_made(
        conn, scratch_dsn, tmp_path, monkeypatch):
    import aiohttp

    from desktop.p2p.p2p_manager import P2PManager

    def no_request(*args, **kwargs):
        raise AssertionError("a withdrawn bundle opened a connection")

    monkeypatch.setattr(aiohttp, "ClientSession", no_request)
    began = datetime.now(timezone.utc) - timedelta(minutes=5)
    wid = _warrant(conn)
    diag_events.mark_collected(conn, wid, 100, began)
    _switch(conn, False)
    stage = tmp_path / f"{wid}.box"
    stage.write_bytes(b"boxed")
    node = types.SimpleNamespace(db_dsn=scratch_dsn)
    node._diag_db = types.MethodType(P2PManager._diag_db, node)

    sent = asyncio.run(P2PManager._diag_upload(node, wid, stage, ("192.0.2.1", 20000)))

    assert sent is False and not stage.exists()
    with conn.cursor() as cur:
        cur.execute("SELECT error, uploaded_at FROM diag_warrants WHERE id = %s", (wid,))
        assert cur.fetchone() == (diag_events.WITHDRAWN, None)
    assert wid not in diag_events.resumable_warrants(conn)


def test_a_value_sent_again_withdraws_nothing(conn, scratch_dsn, monkeypatch):
    """The Web UI's own write (routers/settings._write): a tab re-sending the
    switch as it is leaves its last change where it was."""
    import sys
    from pathlib import Path
    backend = str(Path(__file__).resolve().parents[2] / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    import psycopg2.pool

    import db_pool
    from routers import settings as settings_router
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    try:
        settings_router._write("support.diagnostics_enabled", True)
        wid = _warrant(conn)
        diag_events.mark_collected(conn, wid, 100, _db_now(conn))
        settings_router._write("support.diagnostics_enabled", True)
        assert diag_events.may_upload(conn, wid)
        settings_router._write("support.diagnostics_enabled", False)
        assert not diag_events.may_upload(conn, wid)
    finally:
        pool.closeall()
