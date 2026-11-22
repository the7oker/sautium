"""The reachability probe's vantage, when it stands behind the prober's own
router (PI-NODE-2C DESIGN §5.2, R1) — both peer surfaces, the launcher's
sync server and the Docker node's route.

A prober behind the vantage's router reaches it from the router's public
address — the vantage's own. The call-back to that address lands on the
router's forward of the port: the vantage's own surface, whatever the prober
serves. Before this the vantage answered `reachable: false` (its pinned dial
met its own key), and the prober wrote `unreachable`, silenced its announces
and recruited relays (PI-NODE-2B §8.3). Now a call-back that meets the
vantage's own key, or a request from an address the vantage knows as its
own, is no verdict (`reachable: null`, `same_router`) and spends no cooldown.

Everything is on loopback, which the probe is told to take for an internet
address; the nodes answering the call-back are aiohttp apps over TLS bound to
a chosen key."""

import asyncio
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("psycopg2")
from aiohttp import web  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
for p in (REPO, REPO / "backend"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from desktop.p2p import addrs  # noqa: E402
from desktop.p2p.sync_server import SyncServer  # noqa: E402
from tests.p2p.test_witness_launcher import Node, Served, _tls_context  # noqa: E402


@pytest.fixture(autouse=True)
def loopback_is_the_internet(monkeypatch):
    monkeypatch.setattr(addrs, "is_internet_vantage", lambda host: True)


def _answering(node: Node, says: str = None) -> Served:
    """A peer surface's /health over TLS bound to `node`'s key, saying it is
    `says` (itself unless told)."""
    served = SimpleNamespace(hits=0)

    async def health(_request):
        served.hits += 1
        return web.json_response({"node_id": says or node.pub, "type": "sautium-peer"})
    app = web.Application()
    app.router.add_get("/health", health)
    server = Served(app, _tls_context(node))
    server.port = int(server.url.rsplit(":", 1)[1])
    server.counter = served
    return server


def _request(prober: Node, vantage: Node, port: int) -> dict:
    ts = int(time.time())
    sig = prober.key.sign(f"probe_request:{ts}:{vantage.pub}:{port}".encode()).hex()
    return {"public_key_hex": prober.pub, "port": port, "ts": ts, "signature": sig}


class Launcher:
    """The launcher's sync server, its probe route served over its own key."""

    def __init__(self, node: Node):
        self.node = node
        self.srv = SyncServer("unused", port=0, node_id=node.pub, account_info={
            "public_key_hex": node.pub, "username": node.name, "invite_code": node.invite})
        self.srv._chat_service = SimpleNamespace(friend_for_key=lambda key: {"id": 1})
        app = web.Application()
        app.router.add_get("/health", self._health)
        app.router.add_post("/api/relay/probe-connect", self.srv.handle_probe_connect)
        self.served = Served(app, _tls_context(node))
        self.port = int(self.served.url.rsplit(":", 1)[1])

    async def _health(self, _request):
        return web.json_response({"node_id": self.node.pub})

    def know_own_address(self, address):
        self.srv.own_addresses = lambda: frozenset({address})

    def probe(self, prober: Node, port: int):
        body = json.dumps(_request(prober, self.node, port)).encode()
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        req = urllib.request.Request(f"{self.served.url}/api/relay/probe-connect", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def close(self):
        self.served.close()


class Docker:
    """The Docker node's route, called as the peer app calls it; its own
    surface (the router's forward of its port) answers beside it."""

    def __init__(self, node: Node, monkeypatch):
        from routers import peer_chat
        self.node, self.peer_chat = node, peer_chat
        self.own = _answering(node)
        self.port = self.own.port

        async def lane(fn, *args, **kwargs):
            return fn(*args, **kwargs)
        monkeypatch.setattr(peer_chat, "get_peer_chat",
                            lambda: SimpleNamespace(friend_for_key=lambda key: {"id": 1}))
        monkeypatch.setattr(peer_chat, "resolve_identity", lambda _s: {"public_key_hex": node.pub})
        monkeypatch.setattr(peer_chat, "in_chat_lane", lane)
        monkeypatch.setattr(peer_chat, "_probe_last", {})
        monkeypatch.setattr(peer_chat, "_own_addresses", frozenset)

    def know_own_address(self, address):
        self.peer_chat.set_own_addresses(lambda: frozenset({address}))

    def probe(self, prober: Node, port: int):
        raw = json.dumps(_request(prober, self.node, port)).encode()

        async def body():
            return raw
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), body=body)
        resp = asyncio.run(self.peer_chat.probe_connect(request))
        return resp.status_code, json.loads(resp.body)

    def close(self):
        self.own.close()


@pytest.fixture(params=["launcher", "docker"])
def vantage(request, monkeypatch):
    node = Node("vantage")
    surface = Launcher(node) if request.param == "launcher" else Docker(node, monkeypatch)
    yield surface
    surface.close()


def test_a_call_back_that_meets_the_vantage_itself_is_no_verdict_and_costs_nothing(vantage):
    prober = Node("prober")
    for _ in range(2):                      # the second is not on cooldown: none was spent
        status, answer = vantage.probe(prober, vantage.port)
        assert status == 200
        assert answer["reachable"] is None and answer["same_router"] is True
        assert answer["error"] == "the address is this node's own"


def test_a_request_from_the_vantages_own_address_is_answered_before_any_dial(vantage):
    prober = Node("prober")
    there = _answering(prober)
    vantage.know_own_address("127.0.0.1")
    try:
        for _ in range(2):
            status, answer = vantage.probe(prober, there.port)
            assert (status, answer["reachable"], answer["same_router"]) == (200, None, True)
        assert there.counter.hits == 0
    finally:
        there.close()


def test_a_prober_that_answers_is_reachable_and_the_probe_is_counted(vantage):
    prober = Node("prober")
    there = _answering(prober)
    try:
        status, answer = vantage.probe(prober, there.port)
        assert (status, answer["reachable"]) == (200, True) and "same_router" not in answer
        assert vantage.probe(prober, there.port)[0] == 429
    finally:
        there.close()


def test_another_node_on_the_port_is_unreachable_as_before(vantage):
    prober, squatter = Node("prober"), Node("squatter")
    there = _answering(squatter)
    try:
        status, answer = vantage.probe(prober, there.port)
        assert (status, answer["reachable"]) == (200, False) and "same_router" not in answer
        assert vantage.probe(prober, there.port)[0] == 429
    finally:
        there.close()
