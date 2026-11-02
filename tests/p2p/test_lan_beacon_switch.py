"""The LAN beacon names the node — its key, username and invite code — to
every device on the network it joins, so it goes out only while P2P sharing
is on: LANDiscovery's broadcast loop waits while beaconing is off and sends
at once when it comes on; the launcher sets it from the switch at start and
on the backend's NOTIFY sautium_sharing, which moving the switch sends (a
real PostgreSQL for that part; skipped without a cluster)."""

import asyncio
import select
import sys
import threading
import types
from pathlib import Path

import pytest

from desktop.p2p import lan_discovery
from desktop.p2p.lan_discovery import LANDiscovery

BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


class _Socket:
    def __init__(self):
        self.sent = threading.Event()

    def sendto(self, data, addr):
        self.sent.set()


def test_no_beacon_goes_out_while_sharing_is_off_and_one_at_once_when_it_is_on(monkeypatch):
    monkeypatch.setattr(lan_discovery, "BEACON_INTERVAL", 0.01)
    lan = LANDiscovery(sync_port=24001, node_id="ab" * 32, invite_code="someone#AAAA-BBBB-CCCC")
    monkeypatch.setattr(lan, "_get_broadcast_addresses", lambda: [])
    made = []
    real_make = lan._make_beacon
    monkeypatch.setattr(lan, "_make_beacon", lambda **kw: made.append(1) or real_make(**kw))

    async def run():
        lan._sock, lan._running = _Socket(), True
        task = asyncio.create_task(lan._broadcast_loop(asyncio.get_event_loop()))
        for _ in range(5):
            await asyncio.sleep(0)
        assert made == []                                   # sharing off: nothing said
        lan.set_beaconing(True)
        assert await asyncio.get_event_loop().run_in_executor(None, lan._sock.sent.wait, 5)
        lan.set_beaconing(False)
        await asyncio.sleep(0.02)                           # the beacon in flight ends its turn
        after = len(made)
        await asyncio.sleep(0.1)                            # ten intervals
        assert len(made) == after                           # off again: nothing more
        lan._running = False
        task.cancel()

    asyncio.run(run())


def test_the_launcher_beacons_as_the_switch_says():
    from desktop.p2p.p2p_manager import P2PManager
    lan = LANDiscovery(sync_port=24001, node_id="ab" * 32)
    for sharing in (True, False, True):
        node = types.SimpleNamespace(
            _lan_discovery=lan,
            _sync_server=types.SimpleNamespace(sharing_changed=lambda s=sharing: s))
        asyncio.run(P2PManager._sharing_changed(node))
        assert lan._beaconing.is_set() is sharing


def test_moving_the_switch_wakes_the_launcher(scratch_dsn, monkeypatch):
    psycopg2 = pytest.importorskip("psycopg2")
    import psycopg2.pool

    import db_pool
    from routers import settings as settings_router
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    listener = psycopg2.connect(scratch_dsn)
    listener.autocommit = True
    try:
        with listener.cursor() as cur:
            cur.execute("LISTEN sautium_sharing")
        settings_router.put_sync_prefs(settings_router.SyncPrefs(p2p_enabled=False))
        assert select.select([listener], [], [], 5)[0], "no NOTIFY within 5 s"
        listener.poll()
        assert [n.channel for n in listener.notifies] == ["sautium_sharing"]
        listener.notifies.clear()
        settings_router.put_sync_prefs(settings_router.SyncPrefs(auto_interval_min=30))
        assert not select.select([listener], [], [], 0.5)[0]   # another setting: no wake
    finally:
        listener.close()
        pool.closeall()
