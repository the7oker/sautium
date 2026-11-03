"""The launcher's peer server is bounded against one peer
(desktop/p2p/sync_server.py, desktop/p2p/contact_log.py,
desktop/p2p/p2p_manager.py): the per-address window is checked before a
body is read or a signature checked, slice requests have a node-wide window,
a nested body is a refusal rather than an error, the contact log keeps what
its columns hold, and delivery has no unauthenticated trigger — a stored
reply wakes the friend's stream from the database's own notice, read on the
LISTEN loop's own connection; the key-rotation notice takes a nested body as
a refusal too."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from desktop.p2p import contact_log, p2p_manager, sync_server
from desktop.p2p.sync_server import SyncServer


class _Log:
    def __init__(self):
        self.rows = []

    def record(self, **row):
        self.rows.append(row)


class _Pricer:
    def would_be(self, *a):
        return 0

    def mode(self):
        return "shadow"


async def _ok(request):
    await request.read()
    return web.json_response({"ok": True})


def _server():
    server = SyncServer("postgresql://unused")       # no account: the anonymous lane
    server._contact_log = _Log()
    server._price = lambda: _Pricer()
    return server


async def _requests(server, path, n, body=b"", method="POST"):
    app = web.Application(middlewares=[server._inbound_middleware])
    app.router.add_route(method, path, _ok)
    statuses = []
    async with TestServer(app) as ts:
        for _ in range(n):
            reader, writer = await asyncio.open_connection(ts.host, ts.port)
            writer.write(f"{method} {path} HTTP/1.1\r\nHost: peer\r\nContent-Type: application/json\r\n"
                         f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
            reply = (await reader.read()).decode("latin-1")
            writer.close()
            statuses.append(int(reply.split(" ", 2)[1]))
    return statuses


def test_the_window_is_checked_before_the_body_or_the_signature():
    server = _server()
    authenticated = []
    real = server._authenticate_peer

    async def counting(request):
        authenticated.append(1)
        return await real(request)

    server._authenticate_peer = counting
    limit = sync_server.RATE_LIMIT_PER_MINUTE
    statuses = asyncio.run(_requests(server, "/api/sync/inventory", limit + 3, b'{"track_uuids": []}'))
    assert statuses[:limit] == [200] * limit and statuses[limit:] == [429] * 3
    assert len(authenticated) == limit                  # the refused ones cost no auth


def test_slice_requests_have_a_node_wide_window(monkeypatch):
    monkeypatch.setattr(sync_server, "SLICE_RATE_GLOBAL", 5)
    server = _server()
    server._slice_global["/api/lb/slice"] = []
    statuses = asyncio.run(_requests(server, "/api/lb/slice", 7, b'{"artist_mbids": []}'))
    assert statuses == [200] * 5 + [429] * 2


def test_a_nested_body_is_a_refusal_not_an_error():
    body = b"[" * 100_000 + b"]" * 100_000
    statuses = asyncio.run(_requests(_server(), "/api/sync/inventory", 1, body))
    assert statuses == [200]                             # the stub answers; no 500 on the way


def test_a_nested_rotation_notice_is_a_refusal_not_an_error():
    from aiohttp.test_utils import TestClient
    server = _server()
    server._chat_service = SimpleNamespace()
    body = b"[" * 100_000 + b"]" * 100_000

    async def post():
        app = web.Application()
        app.router.add_post("/api/chat/key-rotation", server.handle_chat_key_rotation)
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/api/chat/key-rotation", data=body,
                                  headers={"Content-Type": "application/json"})
            return r.status

    assert asyncio.run(post()) == 400


def test_the_contact_log_keeps_what_its_columns_hold():
    log = contact_log.ContactLog(lambda: None)
    log.record(endpoint="sync.push", status=200, wall_ms=1.0, cpu_ms=1.0,
               bytes_in=10 ** 12, bytes_out=10 ** 12)
    row = log._queue[-1]
    assert row[7] == 2 ** 31 - 1 and row[8] == 2 ** 31 - 1
    items, targets = contact_log.extract_request_shape(
        "/api/mb/slice", json.dumps({"names": ["Nul\u0000name"]}).encode())
    assert items == 1 and targets == ["nulname"]


def test_delivery_has_no_unauthenticated_trigger(monkeypatch):
    from desktop import node_identity

    def no_tls():
        raise RuntimeError("plain HTTP for the test")

    monkeypatch.setattr(node_identity, "get_server_ssl_context", no_tls)

    async def go():
        server = SyncServer("postgresql://unused", port=0)
        monkeypatch.setattr(server, "_price", lambda: SimpleNamespace(
            calibrate_w=lambda: None, w_ms=1.0))
        monkeypatch.setattr(server, "_admission", lambda: None)
        await server.start()
        try:
            port = server._site._server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"POST /api/chat/trigger-send HTTP/1.1\r\nHost: peer\r\n"
                         b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            reply = await reader.read()
            writer.close()
            return reply
        finally:
            await server.stop()

    reply = asyncio.run(go())
    assert b" 404 " in reply.split(b"\r\n", 1)[0]


def test_a_stored_reply_wakes_that_friends_stream():
    notes = [SimpleNamespace(channel="sautium_chat", payload="msg:7:out"),
             SimpleNamespace(channel="sautium_chat", payload="msg:8:in"),
             SimpleNamespace(channel="sautium_diag", payload="msg:9:out"),
             SimpleNamespace(channel="sautium_chat", payload="msg:x:out")]
    assert p2p_manager.replied_friends(notes) == {7}

    pinged = []
    manager = p2p_manager.P2PManager.__new__(p2p_manager.P2PManager)
    manager._sync_server = SimpleNamespace(has_wake_streams=lambda: True,
                                           ping_wake=lambda pk: pinged.append(pk))
    manager.db_dsn = "postgresql://unused"

    class _Conn:
        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            self.ids = params[0]

        def fetchall(self):
            return [("ab" * 32,)] if self.ids == [7] else []

    original = p2p_manager.psycopg2.connect
    p2p_manager.psycopg2.connect = lambda dsn: pytest.fail("a connection per notice")
    try:
        asyncio.run(manager._ping_wake_for(_Conn(), {7}))      # the LISTEN loop's own
    finally:
        p2p_manager.psycopg2.connect = original
    assert pinged == ["ab" * 32]


def test_notices_a_query_collected_are_handled_before_the_next_wait(monkeypatch):
    """A query on the LISTEN connection (the wake ping's) leaves the notices
    it read in conn.notifies, with nothing left on the socket to wake a wait."""
    from desktop.p2p import sync_walk
    handled, waits = [], []
    manager = p2p_manager.P2PManager.__new__(p2p_manager.P2PManager)
    manager._running = True
    manager._chat_notify = None
    manager._sync_server = None
    manager._on_identity_rotated_cb = None
    manager.db_dsn = "postgresql://unused"

    class _Queue:
        def put_nowait(self, item):
            handled.append(item)

    manager._diag_queue = _Queue()

    class _Conn:
        notifies = [SimpleNamespace(channel="sautium_diag", payload="")]

        def set_isolation_level(self, level):
            pass

        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql):
            pass

        def poll(self):
            pass

        def close(self):
            pass

    def readable(conn, timeout):
        waits.append(len(conn.notifies))
        manager._running = False
        return False

    monkeypatch.setattr(p2p_manager.psycopg2, "connect", lambda dsn: _Conn())
    monkeypatch.setattr(sync_walk, "readable", readable)
    asyncio.run(manager._listen_for_db_notifications())
    assert handled == [("report",)] and waits == [0]       # handled first, then the wait


def test_the_relay_off_notice_ends_the_slots_and_the_capability(monkeypatch):
    from desktop.p2p import sync_walk
    closed, withdrawn = [], []
    manager = p2p_manager.P2PManager.__new__(p2p_manager.P2PManager)
    manager._running, manager._chat_notify, manager._on_identity_rotated_cb = True, None, None
    manager.db_dsn = "postgresql://unused"
    manager._sync_server = SimpleNamespace(close_relay_clients=lambda: closed.append(True))
    manager._dht_service = SimpleNamespace(withdraw_capability=withdrawn.append)

    class _Conn:
        notifies = [SimpleNamespace(channel="sautium_relay_off", payload="")]

        def set_isolation_level(self, level):
            pass

        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql):
            pass

        def poll(self):
            pass

        def close(self):
            pass

    def readable(conn, timeout):
        manager._running = False
        return False

    monkeypatch.setattr(p2p_manager.psycopg2, "connect", lambda dsn: _Conn())
    monkeypatch.setattr(sync_walk, "readable", readable)
    asyncio.run(manager._listen_for_db_notifications())
    assert closed == [True] and withdrawn == ["relay"]
