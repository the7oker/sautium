"""The LAN beacon names the node — its key, username, invite code, port and
library size — to every device on the network it joins, so it does so only
while P2P sharing is on (desktop/p2p/lan_discovery.py). With sharing off a
launcher beacons only the sibling nodes on its host (a Docker backend cannot
broadcast for itself and keeps its own switch), and nothing without any; a
listener takes a siblings-only beacon and never its own. The launcher sets
the beacon from the switch at start, on the backend's NOTIFY sautium_sharing
— which moving the switch sends — and whenever its LISTEN is up again (a real
PostgreSQL for those parts; skipped without a cluster)."""

import asyncio
import json
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

NODE, CODE = "ab" * 32, "someone#AAAA-BBBB-CCCC"


class _Socket:
    def __init__(self):
        self.beacons: list[dict] = []
        self.sent = threading.Event()

    def sendto(self, data, addr):
        self.beacons.append(json.loads(data))
        self.sent.set()


def _lan(monkeypatch):
    monkeypatch.setattr(lan_discovery, "BEACON_INTERVAL", 0.01)
    lan = LANDiscovery(sync_port=24001, node_id=NODE, invite_code=CODE)
    monkeypatch.setattr(lan, "_get_broadcast_addresses", lambda: [])
    lan._sock, lan._running = _Socket(), True
    return lan


def _beacons_while(lan, sharing: bool, settle: float = 0.1) -> list:
    """What goes out within `settle` seconds (ten intervals) of the switch
    being set — the first beacon at once, since the switch wakes the loop."""
    async def run():
        task = asyncio.create_task(lan._broadcast_loop(asyncio.get_event_loop()))
        lan.set_beaconing(sharing)
        await asyncio.sleep(settle)
        lan._running = False
        lan._wake.set()
        await task
    asyncio.run(run())
    return lan._sock.beacons


def test_with_sharing_off_and_no_sibling_nothing_goes_out(monkeypatch):
    assert _beacons_while(_lan(monkeypatch), False) == []


def test_with_sharing_off_only_the_siblings_go_out(monkeypatch):
    lan = _lan(monkeypatch)
    lan._peers[("127.0.0.1", 8801)] = {"localhost": True, "last_seen": 1e12}
    beacons = _beacons_while(lan, False)
    assert beacons and {tuple(sorted(b)) for b in beacons} == {("beacon_id", "nodes", "sautium")}
    assert beacons[0]["nodes"] == [8801]
    assert NODE not in json.dumps(beacons) and CODE not in json.dumps(beacons)


def test_with_sharing_on_the_node_is_named_at_once(monkeypatch):
    lan = _lan(monkeypatch)
    beacons = _beacons_while(lan, True, settle=0.005)
    assert beacons and beacons[0]["node_id"] == NODE and beacons[0]["invite_code"] == CODE
    assert beacons[0]["sync_port"] == 24001


def test_a_listener_takes_a_siblings_only_beacon_and_never_its_own(monkeypatch):
    lan = _lan(monkeypatch)
    other = LANDiscovery(sync_port=24002, node_id="cd" * 32)
    beacon = json.dumps({"sautium": 1, "beacon_id": "f00d" * 4, "nodes": [8801]}).encode()
    assert other._parse_beacon(beacon, "192.0.2.10") == (
        "192.0.2.10", None, {"node_id": "", "artists": 0, "invite_code": "", "nodes": [8801]})
    lan._peers[("127.0.0.1", 8801)] = {"localhost": True, "last_seen": 1e12}
    own = lan._make_beacon()
    assert lan._parse_beacon(own, "192.0.2.11") is None
    assert other._parse_beacon(json.dumps({"sautium": 1, "beacon_id": "x"}).encode(),
                               "192.0.2.10") is None          # names nothing at all


def test_the_launcher_beacons_as_the_switch_says():
    from desktop.p2p.p2p_manager import P2PManager
    lan = LANDiscovery(sync_port=24001, node_id=NODE)
    for sharing in (True, False, True):
        node = types.SimpleNamespace(
            _lan_discovery=lan,
            _sync_server=types.SimpleNamespace(sharing_changed=lambda s=sharing: s))
        asyncio.run(P2PManager._sharing_changed(node))
        assert lan._sharing is sharing


def test_the_switch_is_read_again_whenever_the_listen_is_up(scratch_dsn):
    from desktop.p2p import sync_walk
    up = threading.Event()

    async def run():
        task = asyncio.create_task(sync_walk.listen_notifications(
            scratch_dsn, {"sautium_sharing": lambda: None}, lambda: not up.is_set(),
            on_listening=up.set))
        await asyncio.get_event_loop().run_in_executor(None, up.wait, 10)
        await asyncio.wait_for(task, 15)

    asyncio.run(run())
    assert up.is_set()


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
