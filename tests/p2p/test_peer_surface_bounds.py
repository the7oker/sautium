"""The Docker peer surface is bounded against one peer (backend/p2p_app.py,
backend/db_pool.py, backend/serving.py, backend/routers/{sync,peer_chat,
peer_diag}.py): a body over its route's cap is refused before it is read
whole, and one that does not arrive in time is refused too; a peer's SQL
runs on the peer lanes, never on the loop that serves the Web UI; /health
reads its counts at most once a minute; a 500 says nothing of its cause; a
nested document is a 400; the diag routes answer on the master only, and
refuse a stranger before reading its body; a connection that sends no
request head is closed.

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


async def _call(method, path, *, headers=(), chunks=(), delay=0.0):
    """One request through the whole peer app: (status, body, bytes it read)."""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "https", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "server": ("peer", 8801),
             "client": (next(_addr), 40000),
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


def test_a_peers_sql_runs_on_the_peer_lane_never_on_the_loop(monkeypatch):
    threads = []

    def query_one(sql, params=None):
        threads.append(threading.current_thread().name)
        return {"tracks": [], "embeddings": [], "audio_features": []}

    monkeypatch.setattr(sync, "_require_sharing", lambda: None)
    monkeypatch.setattr(sync, "_db_query_one", query_one)
    asyncio.run(sync.get_inventory(sync.InventoryRequest(
        track_uuids=["00000000-0000-0000-0000-000000000001"])))
    assert threads and threads[0].startswith("peer-lane")


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


def test_health_reads_its_counts_once_a_minute(monkeypatch):
    calls = []
    monkeypatch.setattr(p2p_app, "_health_counts_at", {"at": float("-inf"), "value": None})
    monkeypatch.setattr(p2p_app, "mb_dump_version", lambda: calls.append("mb") or None)
    monkeypatch.setattr(p2p_app, "lb_dump_version", lambda: None)
    monkeypatch.setattr(p2p_app, "lb_inventory", lambda: (0, None))
    monkeypatch.setattr(p2p_app, "node_pubkey_hex", lambda: "ab" * 32)
    monkeypatch.setattr(sync, "mb_slice_queries", None)
    import p2p_identity
    monkeypatch.setattr(p2p_identity, "served_certificate", lambda s: None)
    for _ in range(3):
        body = asyncio.run(p2p_app.health())
        assert body["node_id"] == "ab" * 32 and body["mb_dump"] is None
    assert calls == ["mb"]


def test_a_failed_query_answers_500_with_no_word_of_its_cause(monkeypatch):
    def failing(sql, params=None):
        raise RuntimeError("could not connect to server at 192.0.2.7:5432 as musicai")

    monkeypatch.setattr(sync, "_require_sharing", lambda: None)
    monkeypatch.setattr(sync, "_db_query_one", failing)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(sync.get_inventory(sync.InventoryRequest(
            track_uuids=["00000000-0000-0000-0000-000000000001"])))
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


# -- the request head --------------------------------------------------------

def _protocols():
    from uvicorn.protocols.http.h11_impl import H11Protocol
    out = [H11Protocol]
    try:
        from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol
        out.append(HttpToolsProtocol)
    except ImportError:
        pass
    return out


@pytest.fixture(params=_protocols(), ids=lambda c: c.__name__)
def head_server(request):
    import uvicorn

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                            http=serving.header_deadline(request.param, 0.5), lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "the server never started"
        time.sleep(0.05)
    yield server.servers[0].sockets[0].getsockname()[1]
    server.should_exit = True
    thread.join(10)


def _closed_within(sock, seconds) -> bool:
    sock.settimeout(seconds)
    try:
        return sock.recv(1024) == b""
    except socket.timeout:
        return False


def test_a_connection_with_no_request_head_is_closed(head_server):
    with socket.create_connection(("127.0.0.1", head_server)) as silent:
        assert _closed_within(silent, 3)
    with socket.create_connection(("127.0.0.1", head_server)) as partial:
        partial.sendall(b"GET / HTTP/1.1\r\nHost: peer\r\n")
        assert _closed_within(partial, 3)
    with socket.create_connection(("127.0.0.1", head_server)) as whole:
        whole.sendall(b"GET / HTTP/1.1\r\nHost: peer\r\nConnection: close\r\n\r\n")
        whole.settimeout(3)
        assert whole.recv(1024).startswith(b"HTTP/1.1 200")
