"""The shared sync walk (desktop/p2p/sync_walk.py) — the pieces that need
neither network nor DB: the self-address guard, the request/bind handshake,
the dead-address backoff."""

import asyncio
from types import SimpleNamespace

from desktop.p2p import sync_walk
from desktop.p2p.sync_walk import SyncWalk


class _Identity:
    pubkey = "AB" * 32


class _Client:
    """Stands in for BackendAPIClient: /health answers with a fixed node key
    per address, refuses with an HTTP status, or does not answer at all."""
    answers: dict = {}
    refusals: dict = {}

    def __init__(self, base_url, peer=None, expected_pubkey=None):
        self.base_url = base_url
        self.peer_pubkey = _Client.answers.get(base_url)
        self.last_http_error = None

    def get_health(self):
        self.last_http_error = _Client.refusals.get(self.base_url)
        return {"status": "ok"} if self.peer_pubkey and not self.last_http_error else None


def _walk():
    return SyncWalk("postgresql://unused", identity=lambda: _Identity(),
                    sharing_enabled=lambda: False)


def test_own_address_is_never_a_peer(monkeypatch):
    # A DHT lookup lists us too, and a Docker node has no external IP to
    # subtract itself with — the key /health returns is the guard.
    monkeypatch.setattr(sync_walk, "BackendAPIClient", _Client)
    monkeypatch.setattr(_Client, "refusals", {})
    monkeypatch.setattr(_Client, "answers", {
        "https://198.51.100.7:8801": "ab" * 32,                    # us, via the router
        "https://198.51.100.9:21766": "cd" * 32})                  # a peer
    walk = _walk()
    assert asyncio.run(walk.connect_peer("198.51.100.7:8801")) is None
    api = asyncio.run(walk.connect_peer("198.51.100.9:21766"))
    assert api is not None and api.peer_pubkey == "cd" * 32
    assert asyncio.run(walk.connect_peer("198.51.100.10:21766")) is None   # dead


def test_request_before_bind_is_delivered():
    walk = _walk()
    walk.request("lan-peer")             # the LAN beacon fires before the loop exists

    async def go():
        walk.bind()
        assert walk._notify.is_set() and walk._reasons == {"lan-peer"}
    asyncio.run(go())


def test_a_node_that_refuses_is_busy_not_dead(monkeypatch):
    # One 429 on the master's /health used to hide it from the walk and both
    # slice families for half an hour.
    monkeypatch.setattr(sync_walk, "BackendAPIClient", _Client)
    monkeypatch.setattr(_Client, "answers", {"https://198.51.100.7:8801": "ab" * 32,
                                             "https://198.51.100.11:21766": "cd" * 32})
    monkeypatch.setattr(_Client, "refusals", {"https://198.51.100.11:21766": 429})
    walk = _walk()
    for addr in ("198.51.100.11:21766",            # busy
                 "198.51.100.10:21766",            # nothing answers
                 "198.51.100.7:8801",              # us
                 "https://198.51.100.12:22000"):   # a manual or LAN peer
        assert asyncio.run(walk.connect_peer(addr)) is None
    assert not walk.dead.backing_off("198.51.100.11:21766")
    assert walk.dead.backing_off("198.51.100.10:21766")
    assert walk.dead.backing_off("198.51.100.7:8801")
    assert not walk.dead.backing_off("https://198.51.100.12:22000")


def test_dead_address_backs_off_doubling_to_a_day(monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(sync_walk, "time", SimpleNamespace(time=lambda: clock[0]))
    dead = _walk().dead
    addr = "198.51.100.7:8801"
    delays = []
    for _ in range(8):
        dead.failed(addr)
        dead.failed(addr)            # a probe of the same window (a slice cycle's)
        assert dead.backing_off(addr)
        retry_after, _ = dead._entries[addr]
        delays.append(retry_after - clock[0])
        clock[0] = retry_after
        assert not dead.backing_off(addr)
    base, cap = sync_walk.UNREACHABLE_BACKOFF_BASE, sync_walk.UNREACHABLE_BACKOFF_MAX
    assert delays == [min(base * 2 ** n, cap) for n in range(8)]
    dead.answered(addr)
    dead.failed(addr)
    assert dead._entries[addr][0] - clock[0] == base     # an answer resets the strikes


class _Walked:
    """A walk run with its stores and peers stood in for: one artist missing,
    the DHT and the directory naming the given addresses, the master hint
    naming the master, and only `answering` addresses answering /health."""

    MASTER = ("198.51.100.99", 8801)

    def __init__(self, monkeypatch, dht_nodes, volunteers, answering, empty_handed=()):
        self.probed, self.drained, self.hint_asked, self.expected = [], [], 0, {}
        walk = _walk()
        walk.dht = SimpleNamespace(is_available=True, tail_enabled=False,
                                   set_tail_enabled=lambda mode: None)
        walk._skip_addrs = None
        walk._gap_count = 1               # the gap set did not grow since the last run
        for addr in empty_handed:
            walk._empty_peers[addr] = 1e18

        async def db(fn, *args):
            return {"incomplete_artist_uuids": ["a1"], "unenriched_artist_uuids": ["a1"],
                    "core_and_bulk": (["t1"], [])}.get(fn.__name__, [])

        async def lookup():
            return list(dht_nodes)

        async def connect(addr, is_lan=False, expected_pubkey=None):
            self.probed.append(addr)
            self.expected[addr] = expected_pubkey
            return (SimpleNamespace(base_url=f"https://{addr}", peer_pubkey=None)
                    if addr in answering else None)

        async def pull(addr, gaps, progress, progress_cb, peer_api=None, is_lan=False):
            self.drained.append(addr)
            return {}

        async def measure(reply_size, sample, directory_total):
            return False

        def hint():
            self.hint_asked += 1
            return self.MASTER

        walk.db, walk._lookup_nodes_safe, walk.connect_peer = db, lookup, connect
        walk._sync_from_peer, walk._measure_network = pull, measure
        monkeypatch.setattr(sync_walk.node_hints, "fetch", lambda cap: list(volunteers))
        monkeypatch.setattr(sync_walk.node_hints, "total", lambda cap: None)
        monkeypatch.setattr(sync_walk.master_hint, "fetch", hint)
        asyncio.run(walk.run())


def test_the_master_hint_is_asked_when_no_node_answered_and_only_then(monkeypatch):
    # A newcomer with a dead DHT sees only directory entries that do not
    # answer: seeing them is not reaching a node, and the master is asked.
    w = _Walked(monkeypatch, dht_nodes=[("198.51.100.20", 21000)],
                volunteers=[("198.51.100.21", 21001, "ab" * 32)], answering={"198.51.100.99:8801"})
    assert w.hint_asked == 1
    assert "198.51.100.99:8801" in w.probed
    assert w.drained == ["198.51.100.99:8801"]
    # The hint names an address, never a node: only the master's key may
    # answer there, while every other address is dialled with no expectation.
    from desktop.p2p.master_node import MASTER_PUBKEY_HEX
    assert w.expected["198.51.100.99:8801"] == MASTER_PUBKEY_HEX
    assert w.expected["198.51.100.20:21000"] is None
    # It stays the last tier: not when a node answered this run, nor when one
    # answered lately with nothing for us.
    w = _Walked(monkeypatch, dht_nodes=[("198.51.100.20", 21000)], volunteers=[],
                answering={"198.51.100.20:21000"})
    assert w.hint_asked == 0 and w.drained == ["198.51.100.20:21000"]
    w = _Walked(monkeypatch, dht_nodes=[("198.51.100.20", 21000)], volunteers=[], answering=set(),
                empty_handed={"198.51.100.20:21000"})
    assert w.hint_asked == 0 and w.probed == []
