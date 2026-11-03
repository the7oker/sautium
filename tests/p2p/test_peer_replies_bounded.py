"""What a peer answers is bounded in bytes, shape and time on the client
side (desktop/api_client.py, desktop/p2p/wire.py, desktop/p2p/bloom.py, the
slice verifiers, the walk): a reply past its bound is refused, a gzip body
is inflated against the same bound, a reply that is no object is refused, an
exchange past its deadline ends — its connect, TLS handshake and headers
included —, a redirect is not followed, and a peer's whole session and the
walk's run end by their deadlines.

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

    def drip_header_line(h):
        # One line that never ends: nothing but the deadline stops reading it.
        h.wfile.write(b"HTTP/1.1 200 OK\r\nX-Pad: ")
        while not h.server.gone.is_set():
            h.wfile.write(b"a")
            h.wfile.flush()
            time.sleep(0.2)

    peer.answers["/api/gate/quote"] = drip_header_line
    started = time.monotonic()
    assert _within(15, lambda: _client(peer)._get_json("/api/gate/quote")) is None
    assert time.monotonic() - started < 6


def test_a_reply_that_is_no_object_is_refused(peer):
    peer.answers["/health"] = lambda h: _send(h, b"[1]")
    peer.answers["/api/gate/quote"] = lambda h: _send(h, b'"tasks"')
    client = _client(peer)
    assert client.get_health() is None                 # no AttributeError on the way
    assert client._get_json("/api/gate/quote") is None


def test_the_connect_is_inside_the_exchange_deadline(peer, monkeypatch):
    import socket
    asked = []
    real = socket.create_connection
    monkeypatch.setattr(socket, "create_connection",
                        lambda address, timeout, *rest: asked.append(timeout) or
                        real(address, timeout, *rest))
    monkeypatch.setitem(api_client._PEER_BOUNDS, "gate.quote", (256 * 1024, 2))
    peer.answers["/api/gate/quote"] = lambda h: _send(h, b"{}")
    _client(peer)._get_json("/api/gate/quote", timeout=600)
    assert asked and asked[-1] <= 2                    # the quote's connect: its 2 s, not the 600


def test_a_tls_handshake_that_never_ends_ends_at_the_exchange_deadline(monkeypatch):
    import socket
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)                                 # accepts, never answers the ClientHello
    monkeypatch.setitem(api_client._PEER_BOUNDS, "gate.quote", (256 * 1024, 2))
    ident = peer_auth.PeerIdentity(pubkey="ab" * 32, sign=lambda m: b"\0" * 64,
                                   cert_bundle=lambda: None)
    client = BackendAPIClient(f"https://127.0.0.1:{listener.getsockname()[1]}", peer=ident)
    started = time.monotonic()
    try:
        assert _within(15, lambda: client._get_json("/api/gate/quote", timeout=600)) is None
    finally:
        listener.close()
    assert time.monotonic() - started < 6


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


def test_a_refusal_that_says_when_to_come_back_is_a_wait_not_a_failure(peer):
    def busy(h):
        _send(h, b'{"error": "busy"}', status=503, headers=[("Retry-After", "5")])

    peer.answers["/api/mb/slice"] = busy
    assert _client(peer)._post_json("/api/mb/slice", {"names": ["Sade"]})["retry_after"] == 5


def test_lone_surrogates_are_neither_logged_nor_served():
    from desktop.p2p import contact_log
    assert contact_log._target("Sa\ud800de", 64) == "sade"
    assert not mb_slice_queries.servable_name("Sa\ud800de")
    body, _ = wire.render({"missing": ["Sa\ud800de"]}, False)
    assert json.loads(body) == {"missing": ["Sa\ud800de"]}       # echoed as its own escape


def test_a_slice_source_whose_session_is_over_keeps_its_place(monkeypatch):
    """Its session ended, not failed: no request is started past it, the
    names stay pending, and the source is not dropped as one that failed."""
    from desktop.p2p import mb_slice_cycle as cycle_mod
    asked, dropped, published = [], [], []
    cycle = cycle_mod.MbSliceCycle.__new__(cycle_mod.MbSliceCycle)
    cycle.db_dsn, cycle.config, cycle._status_cleared = "postgresql://unused", {}, False
    cycle._sources = SimpleNamespace(drop=dropped.append)

    async def publish(**state):
        published.append(state)

    async def sources():
        return [(SimpleNamespace(session_deadline=None, base_url="https://198.51.100.9:1"), "node")]

    class _Client:
        def __init__(self, api, db_dsn, source_node):
            pass

        def run(self, batch):
            asked.append(batch)
            return {"error": "no response"}

        def close(self):
            pass

    cycle._publish, cycle.find_sources = publish, sources
    monkeypatch.setattr(cycle_mod, "local_dump_available", lambda dsn: False)
    monkeypatch.setattr(cycle_mod, "pending_names", lambda dsn, n: [("A", 1), ("B", 1)])
    monkeypatch.setattr(cycle_mod, "PEER_SESSION_S", 0)
    monkeypatch.setattr(cycle_mod, "MBSliceClient", _Client)
    asyncio.run(cycle.run())
    assert asked == [] and dropped == []
    assert published[-1]["unserved"] == 2
    # A batch under way when the session ends fails by the session's end.
    monkeypatch.setattr(cycle_mod, "PEER_SESSION_S", 0.2)
    _Client.run = lambda self, batch: (asked.append(batch), time.sleep(0.3), {"error": "no response"})[2]
    asyncio.run(cycle.run())
    assert asked == [["A", "B"]] and dropped == []
    assert published[-1]["unserved"] == 2
