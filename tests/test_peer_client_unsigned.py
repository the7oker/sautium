"""A peer request is signed with a peer identity or not at all
(desktop/api_client.py; F6 of PI-NODE-2C's evening).

A node with no account dials the peers its walk and its slice cycles find.
Until 2026-11-24 the client it built for that — `BackendAPIClient(peer=None)`
— was the host's own client whatever it dialled: every request waited ten
half-seconds for the launcher's secret file, warned, and went unsigned (75
warnings a night on an unborn Docker node) — and would have been signed with
the host's secret had the file been there. A real HTTP server on loopback
stands for the peer; the walk's own `connect_peer` builds the client.
"""

import asyncio
import http.server
import json
import logging
import threading

import pytest

from desktop import api_client
from desktop.api_client import BackendAPIClient
from desktop.p2p import sync_walk


class _Peer(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        body = json.dumps({"status": "healthy", "node_id": "ab" * 32}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def peer():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Peer)
    srv.seen = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def no_secret(monkeypatch, tmp_path):
    """A node whose identity folder holds no .api_secret (a Docker node's
    launcher folder never does); the client's waits are counted, not slept."""
    waits = []
    monkeypatch.setattr(api_client, "_cached_secret", None)
    monkeypatch.setattr(api_client, "_secret_path", lambda: tmp_path / "node_identity" / ".api_secret")
    monkeypatch.setattr(api_client.time, "sleep", waits.append)
    return waits


def _signed(headers: dict) -> list:
    return sorted(name for name in headers if name.startswith("x-sautium-"))


def test_the_walks_dial_from_a_node_with_no_identity_waits_for_no_secret_and_goes_unsigned(
        peer, no_secret, caplog):
    walk = sync_walk.SyncWalk.__new__(sync_walk.SyncWalk)
    walk._identity = lambda: None
    walk.dead = sync_walk.DeadAddresses()
    with caplog.at_level(logging.DEBUG, logger="desktop.api_client"):
        api = asyncio.run(walk.connect_peer(f"http://127.0.0.1:{peer.server_port}"))
    assert no_secret == []
    assert "API secret" not in caplog.text
    ((path, headers),) = peer.seen
    assert path == "/health" and _signed(headers) == []
    # ... and what answered over plain HTTP is no peer: its /health names a key no channel proved
    assert api is None
    assert "the TLS channel belongs to <unauthenticated" in caplog.text


def test_a_peer_client_with_no_identity_is_a_peers_client_all_the_same(peer, no_secret):
    """Pinned to the node key where the channel is TLS, bounded like every
    peer exchange, never the host's secret — with a secret at hand as well."""
    api_client._cached_secret = b"the host's secret"
    plain = BackendAPIClient(f"http://127.0.0.1:{peer.server_port}", remote=True)
    assert plain._get_json("/api/sync/holdings") == {"status": "healthy", "node_id": "ab" * 32}
    assert _signed(peer.seen[-1][1]) == []
    limit, exchange = plain._bound("/api/sync/holdings")
    assert limit == api_client._PEER_BOUNDS["sync.holdings"][0] and exchange is not None
    exchange.close()
    assert plain.gate_pay("sync.pull") is None          # no identity: nothing is ever paid
    pinned = BackendAPIClient("https://192.0.2.1:8801", remote=True, expected_pubkey="cd" * 32)
    assert pinned._ssl_ctx is not None and pinned.peer is None


def test_the_hosts_own_client_still_waits_out_the_start_race_and_says_so(peer, no_secret, caplog):
    """The launcher's client may be asked before its backend wrote the
    secret: it waits for the file, and says when none came."""
    with caplog.at_level(logging.WARNING, logger="desktop.api_client"):
        health = BackendAPIClient(f"http://127.0.0.1:{peer.server_port}").get_health()
    assert health == {"status": "healthy", "node_id": "ab" * 32}
    assert no_secret == [0.5] * 10
    assert "API secret file not found" in caplog.text
    assert _signed(peer.seen[-1][1]) == []
