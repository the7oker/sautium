"""Who says what on which channel, on a real PostgreSQL (B-138).

The load meter's band change is published on a channel of its own with its
snapshot — by the backend's meter and by the launcher's — and no longer on
sautium_identity, where every reader of the node's identity and of the
Library heard it every two seconds. What the pages do read from the identity
channel is said by its writer: the account's email verified, or no longer.
"""

import asyncio
import json
import select
import sys

import pytest

from tests.conftest import REPO_ROOT

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.pool  # noqa: E402

BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

SNAP = {"profile": "lite", "ceiling": 0.15, "cpu_frac": 0.031, "mem_available_mib": 812,
        "playback": False, "headroom": 0.793, "dormant": True, "pace": 1.0,
        "updated_at": "2026-11-24T10:00:00Z"}


@pytest.fixture
def db(scratch_dsn, monkeypatch):
    import db_pool
    pool = psycopg2.pool.ThreadedConnectionPool(1, 3, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("DELETE FROM user_settings WHERE key IN ('p2p.load', 'p2p.gate')")
        yield c
    conn.close()
    pool.closeall()


@pytest.fixture
def heard(scratch_dsn):
    """A listener on the channels the pages are woken through."""
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as c:
        for channel in ("sautium_load", "sautium_identity", "sautium_notices", "sautium_sync_done"):
            c.execute(f"LISTEN {channel}")

    def notifies(expected: int = 1) -> list:
        while len(conn.notifies) < expected and select.select([conn], [], [], 5)[0]:
            conn.poll()
        conn.poll()
        said, conn.notifies[:] = list(conn.notifies), []
        return said
    yield notifies
    conn.close()


def _stored(cur, key: str):
    cur.execute("SELECT value FROM user_settings WHERE key = %s", (key,))
    row = cur.fetchone()
    return row[0] if row else None


def test_the_backends_meter_says_a_band_change_on_the_loads_channel_alone(db, heard, monkeypatch):
    monkeypatch.chdir(BACKEND)        # main mounts static/ relative to its directory
    import main
    from desktop.p2p import pricing
    monkeypatch.setattr(pricing, "current", lambda: None)
    main._publish_load(SNAP)
    (note,) = heard()
    assert note.channel == "sautium_load"
    assert json.loads(note.payload) == {"load": SNAP, "gate": None}
    assert _stored(db, "p2p.load") == SNAP

    # with the gate's pricer in this process, its snapshot rides along
    monkeypatch.setattr(pricing, "current", lambda: type("P", (), {"snapshot": lambda self: {"mode": "shadow"}})())
    main._publish_load(SNAP)
    (note,) = heard()
    assert json.loads(note.payload)["gate"] == {"mode": "shadow"}


def test_the_launchers_meter_says_it_the_same_way(db, heard, scratch_dsn, monkeypatch):
    from desktop.p2p import p2p_manager, pricing
    manager = p2p_manager.P2PManager.__new__(p2p_manager.P2PManager)
    manager.db_dsn = scratch_dsn
    monkeypatch.setattr(pricing, "current", lambda: type("P", (), {"snapshot": lambda self: {"mode": "shadow"}})())
    manager._publish_load(SNAP)
    (note,) = heard()
    assert note.channel == "sautium_load"
    assert json.loads(note.payload) == {"load": SNAP, "gate": {"mode": "shadow"}}
    assert _stored(db, "p2p.load") == SNAP and _stored(db, "p2p.gate") == {"mode": "shadow"}


def test_what_the_channel_carries_reaches_the_stream_as_the_load_event(db, heard, monkeypatch):
    """The notify as PostgreSQL delivers it, through the listener's handler,
    to a page's Library stream: the snapshot, and no wake."""
    monkeypatch.chdir(BACKEND)
    import main
    from desktop.p2p import pricing
    from routers import settings as settings_router
    monkeypatch.setattr(pricing, "current", lambda: None)
    monkeypatch.setattr(settings_router, "_guidance", settings_router._GuidanceTrail())
    monkeypatch.setattr(settings_router, "_guidance_state", lambda: {"tasks": []})
    monkeypatch.setattr(settings_router, "_library_streams", [])

    async def scenario():
        events = (await settings_router.library_stream()).body_iterator
        assert await asyncio.wait_for(events.__anext__(), 5) == "data: {}\n\n"
        assert (await asyncio.wait_for(events.__anext__(), 5)).startswith("event: guidance\n")
        main._publish_load(SNAP)
        settings_router._on_sync_notify(heard())
        said = await asyncio.wait_for(events.__anext__(), 5)
        assert said == "event: load\ndata: " + json.dumps({"load": SNAP, "gate": None}) + "\n\n"
        await events.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("verified", [True, False])
def test_the_emails_verification_is_said_to_the_pages_by_its_writer(db, heard, verified):
    """The identity line offers the verification while it is missing and the
    guidance trail leads to it: both hear it on sautium_identity."""
    from routers import p2p as p2p_router
    db.execute("""INSERT INTO user_profile (id, email_verified) VALUES (1, %s)
                  ON CONFLICT (id) DO UPDATE SET email_verified = EXCLUDED.email_verified""",
               (not verified,))
    p2p_router._record_email_verified(verified)
    assert [n.channel for n in heard()] == ["sautium_identity"]
    db.execute("SELECT email_verified FROM user_profile WHERE id = 1")
    assert db.fetchone() == (verified,)
