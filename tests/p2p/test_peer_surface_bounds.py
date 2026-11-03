"""The Docker peer surface is bounded against one peer (backend/p2p_app.py,
backend/db_pool.py, backend/serving.py, backend/routers/{sync,peer_chat,
peer_diag}.py): a body over its route's cap is refused before it is read
whole, one that pauses or does not arrive in time is refused too, and a body
is read in a slot of its kind, a few of them per address; a peer's SQL and
the answer it makes run on the peer lanes, never on the loop that serves the
Web UI; /health reads its counts at most once a minute, one refresh at a
time, answering from the last ones meanwhile; a 500 says nothing of its
cause; a nested document is a 400; the diag routes answer on the master
only, and refuse a stranger before reading its body; a connection that
sends no request head, or too large a one, is closed, its TLS handshake
included; one that reads nothing of its answer is let go; one address
holds a few connections at most.

The full ASGI app is driven in-process; nothing here needs a database."""

import asyncio
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

p2p_app = pytest.importorskip("p2p_app")
from fastapi import HTTPException  # noqa: E402

import db_pool  # noqa: E402
import serving  # noqa: E402
from routers import peer_chat, peer_diag, sync  # noqa: E402

_addr = iter(f"198.51.100.{i}" for i in range(10, 250))


async def _call(method, path, *, headers=(), chunks=(), delay=0.0, addr=None):
    """One request through the whole peer app: (status, body, bytes it read).
    Past its chunks the body stalls."""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "https", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "server": ("peer", 8801),
             "client": (addr or next(_addr), 40000),
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers]}
    pending, read, out = list(chunks), {"n": 0}, {"status": None, "body": b""}

    async def receive():
        if pending:
            if delay:
                await asyncio.sleep(delay)
            chunk = pending.pop(0)
            read["n"] += len(chunk)
            return {"type": "http.request", "body": chunk, "more_body": bool(pending)}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    await p2p_app.app(scope, receive, send)
    return out["status"], out["body"], read["n"]


class _Chat:
    """A chat service that answers nothing: whatever reaches it is past
    the bounds under test."""

    def friend_for_key(self, pubkey):
        return None

    def get_friend_by_public_key(self, pubkey):
        return None


def test_a_declared_body_over_its_cap_is_refused_unread():
    status, body, read = asyncio.run(_call(
        "POST", "/api/chat/message",
        headers=[("content-type", "application/json"),
                 ("content-length", str(2 * 1024 * 1024))],
        chunks=[b"x" * (64 * 1024)] * 32))
    assert status == 413 and read == 0


def test_a_chunked_body_is_refused_at_the_chunk_that_crosses_the_cap():
    chunk = b"x" * (64 * 1024)
    status, body, read = asyncio.run(_call(
        "POST", "/api/chat/message",
        headers=[("content-type", "application/json"), ("transfer-encoding", "chunked")],
        chunks=[chunk] * 40))
    assert status == 413
    assert read <= p2p_app.body_cap("/api/chat/message") + len(chunk)


def test_a_body_that_does_not_arrive_in_time_is_refused(monkeypatch):
    monkeypatch.setattr(p2p_app, "BODY_DEADLINE_S", 1)
    started = time.monotonic()
    status, body, read = asyncio.run(_call(
        "POST", "/api/chat/message",
        headers=[("content-type", "application/json"), ("content-length", "100")],
        chunks=[b"x" * 10] * 10, delay=0.3))
    assert status == 408 and time.monotonic() - started < 2.5


def _request(accept="gzip"):
    from starlette.requests import Request
    return Request({"type": "http", "headers": [(b"accept-encoding", accept.encode())]})


def test_a_body_that_pauses_is_refused(monkeypatch):
    monkeypatch.setattr(p2p_app, "BODY_IDLE_S", 0.3)
    status, body, read = asyncio.run(_call(
        "POST", "/api/chat/message",
        headers=[("content-type", "application/json"), ("content-length", "100")],
        chunks=[b"x" * 10] * 10, delay=0.6))
    assert status == 408 and read <= 10


@pytest.fixture
def slots(monkeypatch):
    """The body guard's slots afresh, before and after: the BODY_SLOTS a
    test sets, and none bound to a loop that test ran."""
    asyncio.run(_call("GET", "/nothing-here"))               # the app's stack is built
    node = p2p_app.app.middleware_stack
    while not isinstance(node, p2p_app.PeerBodyGuard):
        node = node.app
    node._slots = None
    yield node
    node._slots = None


async def _held(path, addr):
    """A request whose body's first chunk arrives and the rest never does."""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "https", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "server": ("peer", 8801),
             "client": (addr, 40000),
             "headers": [(b"content-type", b"application/json"), (b"content-length", b"100")]}
    first = [True]

    async def receive():
        if first:
            first.pop()
            return {"type": "http.request", "body": b"{", "more_body": True}
        await asyncio.sleep(3600)

    async def send(message):
        pass

    await p2p_app.app(scope, receive, send)


def test_a_body_is_read_in_a_slot_of_its_kind_and_one_address_holds_a_few(slots, monkeypatch):
    monkeypatch.setattr(p2p_app, "BODY_SLOTS", {"chat": (8, 2), "bulk": (2, 2), "large": (1, 1)})
    monkeypatch.setattr(p2p_app, "BODY_SLOT_WAIT_S", 0.2)
    monkeypatch.setattr(peer_chat, "get_peer_chat", lambda: _Chat())
    json_body = lambda n: [("content-type", "application/json"), ("content-length", str(n))]

    async def scene():
        held = [asyncio.ensure_future(_held("/api/sync/inventory", "203.0.113.1"))
                for _ in range(2)]
        await asyncio.sleep(0.1)
        bulk = await _call("POST", "/api/sync/inventory", headers=json_body(2),
                           chunks=[b"{}"], addr="203.0.113.2")
        chat = await _call("POST", "/api/chat/message", headers=json_body(2),
                           chunks=[b"{}"], addr="203.0.113.1")
        for task in held:
            task.cancel()
        return bulk, chat

    bulk, chat = asyncio.run(scene())
    assert bulk[0] == 503 and bulk[2] == 0             # the bulk slots are taken: unread
    assert chat[0] != 503 and chat[2] == 2             # the chat lane's are its own

    monkeypatch.setattr(p2p_app, "BODY_SLOTS", {"chat": (8, 1), "bulk": (8, 1), "large": (1, 1)})
    slots._slots = None

    async def one_address():
        held = asyncio.ensure_future(_held("/api/sync/inventory", "203.0.113.3"))
        await asyncio.sleep(0.1)
        again = await _call("POST", "/api/sync/inventory", headers=json_body(2),
                            chunks=[b"{}"], addr="203.0.113.3")
        other = await _call("POST", "/api/sync/inventory", headers=json_body(2),
                            chunks=[b"{}"], addr="203.0.113.4")
        held.cancel()
        return again, other

    again, other = asyncio.run(one_address())
    assert again[0] == 503 and again[2] == 0           # its one bulk slot is held
    assert other[0] != 503 and other[2] == 2           # another address's is not


def test_a_peers_sql_and_its_answer_run_on_the_peer_lane_never_on_the_loop(monkeypatch):
    threads = []

    def query_one(sql, params=None):
        threads.append(threading.current_thread().name)
        return {"tracks": [], "embeddings": [], "audio_features": []}

    real = sync.wire.render

    def render(answer, gzip_ok):
        threads.append(threading.current_thread().name)
        return real(answer, gzip_ok)

    monkeypatch.setattr(sync, "_require_sharing", lambda: None)
    monkeypatch.setattr(sync, "_db_query_one", query_one)
    monkeypatch.setattr(sync.wire, "render", render)
    answer = asyncio.run(sync.get_inventory(sync.InventoryRequest(
        track_uuids=["00000000-0000-0000-0000-000000000001"]), _request()))
    assert len(threads) == 2 and all(t.startswith("peer-lane") for t in threads)
    assert json.loads(answer.body) == {"tracks": [], "embeddings": [], "audio_features": []}


def test_a_lane_takes_its_connections_from_its_own_pool(monkeypatch):
    made = []

    class Pool:
        closed = False

        def __init__(self, minconn, maxconn, **kwargs):
            made.append(maxconn)

        def getconn(self):
            return type("Conn", (), {"autocommit": True})()

        def putconn(self, conn):
            pass

    monkeypatch.setattr(db_pool.psycopg2.pool, "ThreadedConnectionPool", Pool)
    monkeypatch.setattr(db_pool, "_pool", None)
    monkeypatch.setattr(db_pool, "_lane_pools", {})

    def take():
        with db_pool.get_conn():
            return threading.current_thread().name

    assert asyncio.run(db_pool.in_chat_lane(take)).startswith("chat-lane")
    assert made == [2 * db_pool.PEER_LANES["chat"]]          # the lane's pool, not the Web UI's
    assert db_pool._pool is None


def test_health_reads_its_counts_once_a_minute_one_refresh_at_a_time(monkeypatch):
    calls, release = [], threading.Event()

    def mb_dump_version():
        calls.append(threading.current_thread().name)
        if len(calls) > 1:
            release.wait(5)                           # a refresh that takes its time
        return f"v{len(calls)}"

    monkeypatch.setattr(p2p_app, "_health_counts",
                        {"at": float("-inf"), "value": None, "refresh": None})
    monkeypatch.setattr(p2p_app, "mb_dump_version", mb_dump_version)
    monkeypatch.setattr(p2p_app, "lb_dump_version", lambda: None)
    monkeypatch.setattr(p2p_app, "lb_inventory", lambda: (0, None))
    monkeypatch.setattr(p2p_app, "node_pubkey_hex", lambda: "ab" * 32)
    monkeypatch.setattr(sync, "mb_slice_queries", None)
    import p2p_identity
    monkeypatch.setattr(p2p_identity, "served_certificate", lambda s: None)

    async def scene():
        first = [await p2p_app.health() for _ in range(3)]
        p2p_app._health_counts["at"] = float("-inf")           # a minute on
        during = await asyncio.gather(*(p2p_app.health() for _ in range(5)))
        release.set()
        await p2p_app._health_counts["refresh"]
        return first, during, await p2p_app.health()

    first, during, after = asyncio.run(scene())
    assert [b["mb_dump"] for b in first] == ["v1"] * 3
    assert [b["mb_dump"] for b in during] == ["v1"] * 5       # the last counts, meanwhile
    assert after["mb_dump"] == "v2" and len(calls) == 2         # one refresh for the five
    assert all(name.startswith("chat-lane") for name in calls)


def test_a_failed_query_answers_500_with_no_word_of_its_cause(monkeypatch):
    def failing(sql, params=None):
        raise RuntimeError("could not connect to server at 192.0.2.7:5432 as musicai")

    monkeypatch.setattr(sync, "_require_sharing", lambda: None)
    monkeypatch.setattr(sync, "_db_query_one", failing)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(sync.get_inventory(sync.InventoryRequest(
            track_uuids=["00000000-0000-0000-0000-000000000001"]), _request()))
    assert refused.value.status_code == 500
    assert "192.0.2.7" not in str(refused.value.detail) and "musicai" not in str(refused.value.detail)


def test_a_nested_document_is_a_400_not_an_error(monkeypatch):
    monkeypatch.setattr(peer_chat, "get_peer_chat", lambda: _Chat())
    body = b"[" * 100_000 + b"]" * 100_000
    status, _, _ = asyncio.run(_call(
        "POST", "/api/chat/key-rotation",
        headers=[("content-type", "application/json"), ("content-length", str(len(body)))],
        chunks=[body]))
    assert status == 400


def test_the_diag_routes_answer_on_the_master_only_and_before_reading(monkeypatch):
    monkeypatch.setattr(peer_diag, "resolve_identity",
                        lambda s: {"public_key_hex": "cd" * 32})            # not the master
    body = b"x" * 1024
    status, _, read = asyncio.run(_call(
        "POST", "/api/diag/report",
        headers=[("content-length", str(len(body)))], chunks=[body]))
    assert status == 404 and read == 0


def test_the_master_refuses_a_stranger_before_reading_its_body(monkeypatch):
    from master_node import MASTER_PUBKEY_HEX
    monkeypatch.setattr(peer_diag, "resolve_identity",
                        lambda s: {"public_key_hex": MASTER_PUBKEY_HEX})
    monkeypatch.setattr(peer_diag, "get_peer_chat", lambda: _Chat())
    body = b"x" * (200 * 1024)
    status, _, read = asyncio.run(_call(
        "POST", "/api/diag/report",
        headers=[("content-length", str(len(body))), ("X-Sautium-Peer-Pubkey", "ef" * 32)],
        chunks=[body]))
    assert status == 403 and read == 0


# -- the connection ----------------------------------------------------------

def _protocols():
    from uvicorn.protocols.http.h11_impl import H11Protocol
    out = [H11Protocol]
    try:
        from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol
        out.append(HttpToolsProtocol)
    except ImportError:
        pass
    return out


BIG = 8 * 1024 * 1024


async def _app(scope, receive, send):
    if scope["type"] != "http":
        return
    body = b"x" * BIG if scope["path"] == "/big" else b"ok"
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def _serve(http):
    import uvicorn
    config = uvicorn.Config(_app, host="127.0.0.1", port=0, log_level="warning",
                            http=http, lifespan="off", timeout_keep_alive=1)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "the server never started"
        time.sleep(0.05)
    return server, thread


BOUNDS = dict(head_s=0.5, head_max=4096, stall_s=0.5, max_connections=64, per_address=3)


@pytest.fixture(params=_protocols(), ids=lambda c: c.__name__)
def head_server(request):
    protocol = serving.peer_protocol(request.param, **BOUNDS)
    server, thread = _serve(protocol)
    yield server.servers[0].sockets[0].getsockname()[1], protocol.census
    server.should_exit = True
    thread.join(10)


def _closed_within(sock, seconds) -> bool:
    sock.settimeout(seconds)
    try:
        while sock.recv(65536):
            pass
        return True
    except ConnectionResetError:
        return True
    except socket.timeout:
        return False


def _until(seconds, condition) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def test_a_connection_with_no_request_head_is_closed(head_server):
    port, _ = head_server
    with socket.create_connection(("127.0.0.1", port)) as silent:
        assert _closed_within(silent, 3)
    with socket.create_connection(("127.0.0.1", port)) as partial:
        partial.sendall(b"GET / HTTP/1.1\r\nHost: peer\r\n")
        assert _closed_within(partial, 3)
    with socket.create_connection(("127.0.0.1", port)) as whole:
        whole.sendall(b"GET / HTTP/1.1\r\nHost: peer\r\nConnection: close\r\n\r\n")
        whole.settimeout(3)
        assert whole.recv(1024).startswith(b"HTTP/1.1 200")


def test_a_request_head_over_its_bound_closes_the_connection(head_server):
    port, _ = head_server
    with socket.create_connection(("127.0.0.1", port)) as big:
        big.sendall(b"GET / HTTP/1.1\r\nHost: peer\r\nX-Pad: " + b"a" * (2 * BOUNDS["head_max"]))
        big.settimeout(3)
        reply = b""
        try:
            while chunk := big.recv(65536):
                reply += chunk
        except ConnectionResetError:
            pass
        assert not reply.startswith(b"HTTP/1.1 200")


def test_a_peer_that_reads_nothing_of_its_answer_is_let_go(head_server):
    port, census = head_server
    deaf = socket.socket()
    deaf.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    deaf.connect(("127.0.0.1", port))
    deaf.sendall(b"GET /big HTTP/1.1\r\nHost: peer\r\n\r\n")
    try:
        assert _until(2, lambda: census.open == 1)
        assert _until(5, lambda: census.open == 0)       # aborted, its answer freed
    finally:
        deaf.close()


def test_one_address_holds_a_few_connections(head_server):
    port, census = head_server
    held = [socket.create_connection(("127.0.0.1", port)) for _ in range(BOUNDS["per_address"])]
    try:
        assert _until(2, lambda: census.open == BOUNDS["per_address"])
        with socket.create_connection(("127.0.0.1", port)) as extra:
            assert _closed_within(extra, 0.3)               # at once, not at the head deadline
    finally:
        for sock in held:
            sock.close()


# -- TLS -----------------------------------------------------------------------

@pytest.fixture(params=_protocols(), ids=lambda c: c.__name__)
def tls_server(request, tmp_path):
    import ssl
    import tls_gen
    cert, key = tls_gen.ensure_cert(tmp_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    protocol = serving.peer_protocol(request.param, tls=ctx, **BOUNDS)
    server, thread = _serve(protocol)
    yield server.servers[0].sockets[0].getsockname()[1], protocol.census
    server.should_exit = True
    thread.join(10)


def _client_ctx():
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def test_a_tls_handshake_is_inside_the_head_deadline(tls_server):
    port, census = tls_server
    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", port)) as silent:
        assert _until(2, lambda: census.open == 1)          # counted from the accept
        assert _closed_within(silent, 5)
    assert time.monotonic() - started < 3
    assert _until(2, lambda: census.open == 0)


def test_a_tls_request_is_served_and_so_is_one_riding_the_handshakes_last_flight(tls_server):
    import ssl
    port, census = tls_server
    with _client_ctx().wrap_socket(socket.create_connection(("127.0.0.1", port))) as plain:
        plain.sendall(b"GET / HTTP/1.1\r\nHost: peer\r\nConnection: close\r\n\r\n")
        plain.settimeout(3)
        assert plain.recv(1024).startswith(b"HTTP/1.1 200")
    # The client's Finished and its request in one write: the request
    # reaches the protocol before start_tls hands the connection over.
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = _client_ctx().wrap_bio(incoming, outgoing)
    with socket.create_connection(("127.0.0.1", port)) as sock:
        sock.settimeout(3)
        while True:
            try:
                tls.do_handshake()
                break
            except ssl.SSLWantReadError:
                if outgoing.pending:
                    sock.sendall(outgoing.read())
                incoming.write(sock.recv(65536))
        tls.write(b"GET / HTTP/1.1\r\nHost: peer\r\nConnection: close\r\n\r\n")
        sock.sendall(outgoing.read())
        reply = b""
        while not reply.startswith(b"HTTP/1.1 200"):
            data = sock.recv(65536)
            assert data, "the connection closed with no answer"
            incoming.write(data)
            try:
                reply += tls.read(65536)
            except ssl.SSLWantReadError:
                pass
    assert _until(2, lambda: census.open == 0)


def test_a_tls_peer_that_reads_nothing_of_its_answer_is_let_go(tls_server):
    """The TLS layer takes the answer whole: the bound is the close — the
    keep-alive, then stall_s to deliver the rest."""
    port, census = tls_server
    raw = socket.socket()
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    raw.connect(("127.0.0.1", port))
    deaf = _client_ctx().wrap_socket(raw)
    deaf.sendall(b"GET /big HTTP/1.1\r\nHost: peer\r\n\r\n")
    try:
        assert _until(2, lambda: census.open == 1)
        assert _until(5, lambda: census.open == 0)       # aborted, its answer freed
    finally:
        deaf.close()
