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
