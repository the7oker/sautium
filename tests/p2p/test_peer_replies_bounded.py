"""What a peer answers is bounded in bytes, shape and time on the client
side (desktop/api_client.py, desktop/p2p/wire.py, desktop/p2p/bloom.py, the
slice verifiers, the walk): a reply past its bound is refused, a gzip body
is inflated against the same bound, an exchange past its deadline ends —
headers included —, a redirect is not followed, and a peer's whole session
and the walk's run end by their deadlines.

A real HTTP server on loopback plays the peer; the client is a peer client
(a PeerIdentity), so every exchange takes the bounded path."""

import asyncio
import base64
import gzip
import http.server
import json
import threading
import time
import zlib
from types import SimpleNamespace

import pytest

from desktop import api_client
from desktop.api_client import BackendAPIClient
from desktop.p2p import bloom, lb_slice_queries, mb_slice_queries, peer_auth, sync_walk, wire
from desktop.p2p.bloom import BloomFilter
from desktop.p2p.sync_walk import SyncWalk


class _Peer(http.server.BaseHTTPRequestHandler):
    """Answers by path; `server.answers[path]` is a callable(handler)."""

    def _answer(self):
        self.server.hits.append(self.path)
        self.server.answers[self.path.split("?")[0]](self)

    do_GET = do_POST = _answer

    def log_message(self, *args):
        pass


def _send(h, body: bytes, status=200, headers=()):
    h.send_response(status)
    h.send_header("Content-Type", "application/json")
    for k, v in headers:
        h.send_header(k, v)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


@pytest.fixture
def peer():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Peer)
    srv.daemon_threads = True
    srv.hits = []
    # A plain-HTTP peer has no channel key, so the client signs nothing and
    # its /health is refused by the channel rule — what matters here is the
    # exchange itself.
    srv.answers = {"/health": lambda h: _send(h, b'{"node_id": ""}')}
    srv.gone = threading.Event()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.gone.set()
    srv.shutdown()
    srv.server_close()


def _client(srv) -> BackendAPIClient:
    ident = peer_auth.PeerIdentity(pubkey="ab" * 32, sign=lambda m: b"\0" * 64,
                                   cert_bundle=lambda: None)
    return BackendAPIClient(f"http://127.0.0.1:{srv.server_port}", peer=ident)


def _within(seconds, fn):
    """fn()'s result, or pytest.fail when it does not return in time."""
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("v", fn()), daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        pytest.fail(f"the exchange was still running after {seconds} s")
    return out["v"]


def test_a_reply_over_its_bound_is_refused(peer):
    peer.answers["/api/gate/quote"] = lambda h: _send(
        h, json.dumps({"pad": "a" * (300 * 1024)}).encode())
    assert _client(peer)._get_json("/api/gate/quote") is None


def test_a_gzip_reply_is_inflated_against_the_bound(peer):
    small = gzip.compress(json.dumps({"tasks": [], "n": 0}).encode())
    peer.answers["/api/gate/quote"] = lambda h: _send(
        h, small, headers=[("Content-Encoding", "gzip")])
    assert _client(peer)._get_json("/api/gate/quote") == {"tasks": [], "n": 0}

    bomb = gzip.compress(json.dumps({"pad": "a" * (300 * 1024)}).encode())
    assert len(bomb) < 4096          # small on the wire, over the bound once inflated
    peer.answers["/api/gate/quote"] = lambda h: _send(
        h, bomb, headers=[("Content-Encoding", "gzip")])
    assert _client(peer)._get_json("/api/gate/quote") is None


def test_a_slow_reply_ends_at_the_exchange_deadline(peer, monkeypatch):
    monkeypatch.setitem(api_client._PEER_BOUNDS, "gate.quote", (256 * 1024, 2))

    def drip(h):
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.end_headers()
        while not h.server.gone.is_set():          # a byte well inside each read timeout
            h.wfile.write(b" ")
            h.wfile.flush()
            time.sleep(0.2)

    peer.answers["/api/gate/quote"] = drip
    started = time.monotonic()
    assert _within(15, lambda: _client(peer)._get_json("/api/gate/quote")) is None
    assert time.monotonic() - started < 10


def test_slow_headers_end_at_the_exchange_deadline(peer, monkeypatch):
    monkeypatch.setitem(api_client._PEER_BOUNDS, "gate.quote", (256 * 1024, 2))

    def drip_headers(h):
        h.wfile.write(b"HTTP/1.1 200 OK\r\n")
        while not h.server.gone.is_set():
            h.wfile.write(b"X-Pad: a\r\n")
            h.wfile.flush()
            time.sleep(0.2)

    peer.answers["/api/gate/quote"] = drip_headers
    assert _within(15, lambda: _client(peer)._get_json("/api/gate/quote")) is None


def test_a_redirect_is_not_followed(peer):
    peer.answers["/api/gate/quote"] = lambda h: _send(
        h, b"{}", status=302,
        headers=[("Location", f"http://127.0.0.1:{h.server.server_port}/elsewhere")])
    peer.answers["/elsewhere"] = lambda h: _send(h, b'{"tasks": []}')
    assert _client(peer)._get_json("/api/gate/quote") is None
    assert "/elsewhere" not in peer.hits


def test_a_session_past_its_deadline_starts_no_request(peer):
    peer.answers["/api/sync/inventory"] = lambda h: _send(h, b'{"tracks": []}')
    client = _client(peer)
    client.session_deadline = time.monotonic() - 1
    assert client.sync_inventory(["00000000-0000-0000-0000-000000000000"]) is None
    assert not [p for p in peer.hits if p.startswith("/api/sync/")]


def test_a_refusals_retry_after_is_whole_seconds_an_hour_at_most(peer):
    peer.answers["/api/lb/slice"] = lambda h: _send(
        h, json.dumps({"error": "rate limited", "retry_after": "soon"}).encode(),
        status=429, headers=[("Retry-After", "86400000")])
    out = _client(peer).lb_slice(["00000000-0000-0000-0000-000000000000"])
    assert out["retry_after"] == 3600


# -- the holdings filter ------------------------------------------------------

@pytest.mark.parametrize("over", [
    lambda d: {"k": 10_000},                        # every lookup would pay 10 000 positions
    lambda d: {"k": "7"},
    lambda d: {"n": -1},
    lambda d: {"bits": "\n".join(d["bits"][i:i + 64] for i in range(0, len(d["bits"]), 64))},
])
def test_a_filter_outside_its_bounds_is_refused(over):
    f = BloomFilter.sized(1000)
    f.add("12345678-1234-5678-1234-567812345678")
    published = json.loads(json.dumps(f.to_dict()))
    assert "12345678-1234-5678-1234-567812345678" in BloomFilter.from_dict(published)
    with pytest.raises(ValueError):
        BloomFilter.from_dict({**published, **over(published)})
    assert bloom.MAX_HASHES < 10_000 and bloom.MAX_BITS >= published["m"]


# -- slice blobs ----------------------------------------------------------------

def _entry(raw: bytes) -> dict:
    return {"author_pubkey": "ab" * 32, "sig": "00" * 64,
            "blob_gz": base64.b64encode(gzip.compress(raw)).decode()}


def test_an_mb_blob_that_inflates_past_its_bound_is_refused_before_its_signature(monkeypatch):
    monkeypatch.setattr(mb_slice_queries, "BLOB_RAW_MAX", 1024)
    called = []
    monkeypatch.setattr("desktop.node_identity.verify_signature",
                        lambda *a: called.append(a) or True)
    assert mb_slice_queries.verify_slice_entry("x", _entry(b"{" + b" " * 4096 + b"}")) is None
    assert not called


def test_an_lb_blob_that_is_corrupt_or_too_large_is_refused_not_raised(monkeypatch):
    corrupt = {"author_pubkey": "ab" * 32, "sig": "00" * 64,
               "blob_gz": base64.b64encode(b"\x1f\x8b\x08\x00" + b"\xff" * 64).decode()}
    assert lb_slice_queries.verify_slice_entry(
        "00000000-0000-0000-0000-000000000000", corrupt) is None
    monkeypatch.setattr(lb_slice_queries, "BLOB_RAW_MAX", 1024)
    called = []
    monkeypatch.setattr("desktop.node_identity.verify_signature",
                        lambda *a: called.append(a) or True)
    assert lb_slice_queries.verify_slice_entry(
        "00000000-0000-0000-0000-000000000000", _entry(b" " * 4096)) is None
    assert not called


# -- wire -----------------------------------------------------------------------

def test_gunzip_refuses_truncated_trailing_and_oversized_streams():
    good = gzip.compress(b"x" * 100)
    assert wire.gunzip(good, 100) == b"x" * 100
    for bad, limit in ((good[:-4], 100), (good + b"junk", 100), (good, 99),
                       (zlib.compress(b"x"), 100)):
        with pytest.raises(wire.WireError):
            wire.gunzip(bad, limit)


class _Content:
    def __init__(self, data: bytes, step: int = 7):
        self._data, self._step = data, step

    async def read(self, n):
        n = min(n, self._step)
        out, self._data = self._data[:n], self._data[n:]
        return out


def test_read_json_takes_an_object_within_its_cap_and_refuses_the_rest():
    def read(body, cap):
        return asyncio.run(wire.read_json(SimpleNamespace(content=_Content(body)), cap))

    assert read(b'{"node_id": "x"}', 64) == {"node_id": "x"}
    for body, cap in ((b'{"pad": "' + b"a" * 100 + b'"}', 64),
                      (b"[1, 2]", 64),
                      (b"[" * 5000 + b"]" * 5000, 20_000)):
        with pytest.raises(wire.WireError):
            read(body, cap)


# -- the walk -------------------------------------------------------------------

def _walk():
    return SyncWalk("postgresql://unused", identity=lambda: None,
                    sharing_enabled=lambda: False)


def test_a_run_past_its_deadline_asks_no_further_peer(monkeypatch):
    walk = _walk()
    connected = []

    async def connect(addr, is_lan=False):
        connected.append(addr)

    monkeypatch.setattr(walk, "connect_peer", connect)
    walk._run_deadline = time.monotonic() - 1
    out = asyncio.run(walk._sync_from_peer(
        "198.51.100.9:21766", ([], []), lambda m: None, None))
    assert out == {"error": "run_deadline"} and not connected


def test_each_peer_session_ends_by_its_deadline_and_by_the_runs(monkeypatch):
    seen = []

    class _Sync:
        def __init__(self, api_client, **kwargs):
            self.api, self.peer_capabilities = api_client, set()

        def run_sync(self, core, bulk):
            seen.append(self.api.session_deadline)
            return {}

    monkeypatch.setattr(sync_walk, "SyncClient", _Sync)
    walk = _walk()
    api = SimpleNamespace(session_deadline=None)

    walk._run_deadline = time.monotonic() + 5            # the run ends first
    asyncio.run(walk._sync_from_peer("x", ([], []), lambda m: None, None, peer_api=api))
    walk._run_deadline = time.monotonic() + 10 * sync_walk.PEER_SESSION_S
    asyncio.run(walk._sync_from_peer("x", ([], []), lambda m: None, None, peer_api=api))
    assert seen[0] is not None and seen[0] <= time.monotonic() + 5
    assert seen[1] <= time.monotonic() + sync_walk.PEER_SESSION_S
