"""The shared sync walk (desktop/p2p/sync_walk.py) — the pieces that need
neither network nor DB: the self-address guard, the request/bind handshake,
the dead-address backoff."""

import asyncio
import time

from desktop.p2p import sync_walk
from desktop.p2p.sync_walk import SyncWalk


class _Identity:
    pubkey = "AB" * 32


class _Client:
    """Stands in for BackendAPIClient: /health answers with a fixed node key
    per address, or not at all."""
    answers: dict = {}

    def __init__(self, base_url, peer=None, expected_pubkey=None):
        self.base_url = base_url
        self.peer_pubkey = _Client.answers.get(base_url)

    def get_health(self):
        return {"status": "ok"} if self.peer_pubkey else None


def _walk():
    return SyncWalk("postgresql://unused", identity=lambda: _Identity(),
                    sharing_enabled=lambda: False)


def test_own_address_is_never_a_peer(monkeypatch):
    # A DHT lookup lists us too, and a Docker node has no external IP to
    # subtract itself with — the key /health returns is the guard.
    monkeypatch.setattr(sync_walk, "BackendAPIClient", _Client)
    _Client.answers = {"https://198.51.100.7:8801": "ab" * 32,     # us, via the router
                       "https://198.51.100.9:21766": "cd" * 32}    # a peer
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


def test_dead_address_backs_off_doubling_to_a_day():
    walk = _walk()
    for _ in range(8):
        walk._note_unreachable("198.51.100.7:8801")
    retry_after, strikes = walk._unreachable["198.51.100.7:8801"]
    assert strikes == 8
    assert retry_after - time.time() <= sync_walk.UNREACHABLE_BACKOFF_MAX + 1
    walk._unreachable.clear()
    walk._note_unreachable("198.51.100.7:8801")
    first, _ = walk._unreachable["198.51.100.7:8801"]
    assert abs((first - time.time()) - sync_walk.UNREACHABLE_BACKOFF_BASE) < 2
