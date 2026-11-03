"""The LAN beacon names the node — its key, username, invite code, port and
library size — to every device on the network it joins, so it does so only
while P2P sharing is on (desktop/p2p/lan_discovery.py). With sharing off a
launcher beacons only the sibling nodes on its host whose owner shares (a
Docker backend cannot broadcast for itself, keeps its own switch and says it
in its /health), and nothing without one; each beacon has an id of its own,
a listener takes a siblings-only beacon and never its own. The launcher sets
the beacon from the switch at start, on the backend's NOTIFY sautium_sharing
— which moving the switch sends — and whenever its LISTEN is up again, one
read at a time, and a move while a beacon goes out sends the next at once (a
real PostgreSQL for those parts; skipped without a cluster)."""

import asyncio
import json
import select
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        self.named = threading.Event()          # a beacon naming this node went out

    def sendto(self, data, addr):
        beacon = json.loads(data)
        self.beacons.append(beacon)
        if "node_id" in beacon:
            self.named.set()


class _SlowSocket(_Socket):
    """A send that takes until the test lets it go."""
    def __init__(self):
        super().__init__()
        self.sending = threading.Event()
        self.release = threading.Event()

    def sendto(self, data, addr):
        self.sending.set()
        self.release.wait(5)
        super().sendto(data, addr)


def _sibling(lan, port: int, **said) -> None:
    lan._peers[("127.0.0.1", port)] = {"localhost": True, "last_seen": 1e12, **said}


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


def test_with_sharing_off_only_the_siblings_that_share_go_out(monkeypatch):
    lan = _lan(monkeypatch)
    _sibling(lan, 8801, sharing=True)
    _sibling(lan, 8802, sharing=False)
    _sibling(lan, 8803)                         # a node a release behind says nothing
    beacons = _beacons_while(lan, False)
    assert beacons and {tuple(sorted(b)) for b in beacons} == {("beacon_id", "nodes", "sautium")}
    assert {tuple(b["nodes"]) for b in beacons} == {(8801,)}
    assert NODE not in json.dumps(beacons) and CODE not in json.dumps(beacons)


def test_with_no_sharing_anywhere_on_the_host_nothing_goes_out(monkeypatch):
    lan = _lan(monkeypatch)
    _sibling(lan, 8801, sharing=False)
    assert _beacons_while(lan, False) == []


def test_with_sharing_on_a_sibling_that_does_not_share_is_not_named(monkeypatch):
    lan = _lan(monkeypatch)
    _sibling(lan, 8801, sharing=False)
    beacons = _beacons_while(lan, True, settle=0.005)
    assert beacons and beacons[0]["node_id"] == NODE and "nodes" not in beacons[0]


def test_each_beacon_has_an_id_of_its_own_and_the_host_knows_them_all(monkeypatch):
    lan = _lan(monkeypatch)
    _sibling(lan, 8801, sharing=True)
    other = LANDiscovery(sync_port=24002, node_id="cd" * 32)
    beacons = [lan._make_beacon() for _ in range(3)]
    assert len({json.loads(b)["beacon_id"] for b in beacons}) == 3
    assert [lan._parse_beacon(b, "192.0.2.11") for b in beacons] == [None] * 3
    assert all(other._parse_beacon(b, "192.0.2.11") is not None for b in beacons)


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
    _sibling(lan, 8801, sharing=True)
    own = lan._make_beacon()
    assert lan._parse_beacon(own, "192.0.2.11") is None
    assert other._parse_beacon(json.dumps({"sautium": 1, "beacon_id": "x"}).encode(),
                               "192.0.2.10") is None          # names nothing at all


def test_a_switch_moved_while_a_beacon_goes_out_sends_the_next_at_once(monkeypatch):
    monkeypatch.setattr(lan_discovery, "BEACON_INTERVAL", 30)
    lan = LANDiscovery(sync_port=24001, node_id=NODE, invite_code=CODE)
    monkeypatch.setattr(lan, "_get_broadcast_addresses", lambda: [])
    lan._sock, lan._running = _SlowSocket(), True
    _sibling(lan, 8801, sharing=True)

    async def run():
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(lan._broadcast_loop(loop))
        await loop.run_in_executor(None, lan._sock.sending.wait, 5)   # siblings only, going out
        lan.set_beaconing(True)
        lan._sock.release.set()
        await loop.run_in_executor(None, lan._sock.named.wait, 2)
        lan._running = False
        lan._wake.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(run())
    assert ["node_id" in b for b in lan._sock.beacons] == [False, True]


def test_the_launcher_beacons_as_the_switch_says():
    from desktop.p2p.p2p_manager import P2PManager
    lan = LANDiscovery(sync_port=24001, node_id=NODE)
    for sharing in (True, False, True):
        node = types.SimpleNamespace(
            _lan_discovery=lan, _sharing_lock=asyncio.Lock(),
            _sync_server=types.SimpleNamespace(sharing_changed=lambda s=sharing: s))
        asyncio.run(P2PManager._sharing_changed(node))
        assert lan._sharing is sharing


class _Reads:
    """The switch read twice: the read that began first is slow, and says
    what the switch said before it moved."""
    def __init__(self):
        self._lock = threading.Lock()
        self._calls = 0
        self.first_in = threading.Event()
        self.release = threading.Event()
        self.second_done = threading.Event()

    def sharing_changed(self) -> bool:
        with self._lock:
            self._calls += 1
            first = self._calls == 1
        if first:
            self.first_in.set()
            self.release.wait(5)
            return True
        self.second_done.set()
        return False


def test_reads_of_the_switch_apply_in_the_order_they_began():
    from desktop.p2p.p2p_manager import P2PManager
    lan = LANDiscovery(sync_port=24001, node_id=NODE)
    reads = _Reads()

    async def run():
        node = types.SimpleNamespace(_lan_discovery=lan, _sync_server=reads,
                                     _sharing_lock=asyncio.Lock())
        loop = asyncio.get_running_loop()
        first = asyncio.create_task(P2PManager._sharing_changed(node))
        await loop.run_in_executor(None, reads.first_in.wait, 5)
        second = asyncio.create_task(P2PManager._sharing_changed(node))
        # Reads in no order would let the later one end first
        await loop.run_in_executor(None, reads.second_done.wait, 0.5)
        reads.release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 5)

    asyncio.run(run())
    assert lan._sharing is False


class _Health(BaseHTTPRequestHandler):
    said: dict = {}

    def do_GET(self):
        body = json.dumps({"status": "ok", "type": "sautium-peer", **self.said}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.mark.parametrize("said, named", [({"sharing": True}, True), ({"sharing": False}, False),
                                         ({}, False)])
def test_the_probe_keeps_what_a_sibling_says_of_its_switch(monkeypatch, said, named):
    monkeypatch.setattr(lan_discovery, "BEACON_INTERVAL", 0.01)
    handler = type("Handler", (_Health,), {"said": said})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        lan = LANDiscovery(sync_port=24001, node_id=NODE, localhost_probe_ports=[port],
                           on_new_peer=lambda ip, p: setattr(lan, "_running", False))
        lan._running = True
        asyncio.run(asyncio.wait_for(lan._localhost_probe_loop(), 30))
    finally:
        server.shutdown()
        server.server_close()
    assert ("127.0.0.1", port) in lan._peers
    assert lan._local_node_ports() == ([port] if named else [])


def test_a_docker_node_says_in_its_health_whether_its_owner_shares(scratch_dsn, monkeypatch,
                                                                    tmp_path):
    pytest.importorskip("psycopg2")
    import psycopg2.pool
    from fastapi.testclient import TestClient

    import db_pool
    import p2p_app
    import p2p_identity
    from config import settings
    from routers import settings as settings_router
    from routers import sync
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    monkeypatch.setattr(settings, "p2p_identity_dir", str(tmp_path))
    monkeypatch.setattr(p2p_identity, "_served", (None, None))
    try:
        http = TestClient(p2p_app.app)
        for on in (False, True):
            settings_router._write("sync.p2p_enabled", on)
            monkeypatch.setattr(sync, "_sharing_cache", (0.0, not on))     # past the cache
            assert http.get("/health").json()["sharing"] is on
    finally:
        pool.closeall()


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
