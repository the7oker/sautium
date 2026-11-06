"""What a chat request binds (desktop/p2p/chat_wire.py, the classic
handshake on both surfaces, the history export, the relay's admission):
a classic handshake is signed by its key, for its responder, just now,
under its own name — unsigned, it reads and writes nothing; a known friend
updates its presence only; one refusal for a blocked, an unknown and a
colliding key; a token's identity gate is awaited on the serving loop,
never on a lane thread; a history request made for one node fails at
another; a history page is bounded in messages and bytes, carries the ids it
may mark delivered and the cursor the next page starts after, and a pull
goes on from where the last one ended; a stored message is pushed at once on
its own notice; a stranger's relay slot needs the relay switch and a
verified identity."""

import asyncio
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient as AioClient, TestServer

BACKEND = Path(__file__).resolve().parent.parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from desktop import node_identity  # noqa: E402
from desktop.p2p import chat_wire  # noqa: E402
from desktop.p2p.sync_server import SyncServer  # noqa: E402

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")


def _who(name):
    key = ed25519.Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes_raw()
    return SimpleNamespace(key=key, pub=raw.hex(), name=name,
                           invite=node_identity.make_invite_code(name, raw))


HOST, FRIEND, STRANGER, OTHER_NODE = (_who(n) for n in ("host", "friend", "stranger", "other"))


def _body(who, *, signed_for=HOST, name=None):
    body = {"public_key_hex": who.pub, "username": name or who.name, "invite_code": who.invite}
    return chat_wire.sign_handshake(who.key.sign, body, signed_for.invite) if signed_for else body


def test_a_handshake_signature_holds_for_its_responder_time_name_and_key():
    ok = lambda body, responder=HOST: chat_wire.handshake_signed(
        body, responder.pub, lambda pk, m, s: _verify(pk, m, s))
    assert ok(_body(FRIEND))
    assert not ok(_body(FRIEND), responder=OTHER_NODE)                  # made for another node
    assert not ok(_body(FRIEND, signed_for=None))                       # unsigned
    assert not ok({**_body(FRIEND), "handshake_ts": int(time.time()) - 600})
    assert not ok({**_body(FRIEND), "username": "someone"})             # the name its code does not carry
    assert not ok({**_body(FRIEND), "public_key_hex": STRANGER.pub})


def _verify(pk, message, sig):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pk)).verify(bytes.fromhex(sig), message)
        return True
    except (InvalidSignature, ValueError):
        return False


class _Chat:
    """The launcher's chat service as the handshake handler sees it."""

    def __init__(self, friends):
        self.friends, self.writes = friends, []

    def get_friends(self):
        return list(self.friends)

    def add_friend(self, **kw):
        self.writes.append(("add", kw["public_key_hex"]))
        return 1

    def update_friend_last_seen(self, pk):
        self.writes.append(("seen", pk))

    def set_bound_identity(self, fid, pk):
        self.writes.append(("bound", fid))

    def db_conn(self):
        return None


def _friend(who, **over):
    return {"id": 7, "username": who.name, "public_key_hex": who.pub, "invite_code": who.invite,
            "is_blocked": False, "source": "manual", "join_token_id": None, **over}


async def _launcher(chat, body):
    server = SyncServer("postgresql://unused",
                        account_info={"public_key_hex": HOST.pub, "username": HOST.name,
                                      "invite_code": HOST.invite})
    server._chat_service = chat
    app = web.Application()
    app.router.add_post("/api/chat/handshake", server.handle_chat_handshake)
    async with AioClient(TestServer(app)) as client:
        r = await client.post("/api/chat/handshake", json=body)
        return r.status, await r.json()


def test_the_launchers_classic_handshake():
    run = lambda chat, body: asyncio.run(_launcher(chat, body))
    chat = _Chat([_friend(FRIEND)])
    assert run(chat, _body(FRIEND, signed_for=None))[0] == 403 and chat.writes == []
    status, reply = run(chat, _body(FRIEND))
    assert status == 200 and reply["public_key_hex"] == HOST.pub
    assert ("add", FRIEND.pub) not in chat.writes                       # presence only
    refusals = [run(_Chat([_friend(FRIEND, is_blocked=True)]), _body(FRIEND)),
                run(_Chat([]), _body(STRANGER))]
    assert all(s == 403 for s, _ in refusals) and refusals[0][1] == refusals[1][1]
    assert "public_key_hex" not in refusals[1][1]
    # A key whose code names a contact made with another key gets no row of its own.
    collide = _Chat([_friend(FRIEND, invite_code=STRANGER.invite)])
    assert run(collide, _body(STRANGER))[0] == 403 and collide.writes == []
    # A token stub resolves through the token handshake, never this path.
    stub = _Chat([_friend(FRIEND, public_key_hex="pending:x", join_token_id="t")])
    assert run(stub, _body(FRIEND))[0] == 403 and stub.writes == []
    pending = _Chat([_friend(FRIEND, public_key_hex="pending:x")])
    assert run(pending, _body(FRIEND))[0] == 200 and ("add", FRIEND.pub) in pending.writes


def _docker_app(monkeypatch, svc, match):
    from fastapi import FastAPI
    from routers import peer_chat
    monkeypatch.setattr(peer_chat, "get_peer_chat", lambda: svc)
    monkeypatch.setattr(peer_chat, "resolve_identity", lambda s: {
        "public_key_hex": HOST.pub, "username": HOST.name, "invite_code": HOST.invite})
    monkeypatch.setattr(peer_chat, "_find_friend_for_handshake", lambda invite, pk: match)
    app = FastAPI()
    app.include_router(peer_chat.chat_router)
    return app


class _Svc:
    def __init__(self, friend=None):
        self.writes, self.friend = [], friend

    def add_friend(self, *a, **kw):
        self.writes.append(("add", a[0]))
        return 1

    def update_friend_last_seen(self, pk):
        self.writes.append(("seen", pk))

    def friend_for_key(self, pk):
        return self.friend


def test_the_docker_classic_handshake(monkeypatch):
    from fastapi.testclient import TestClient
    svc = _Svc()
    with TestClient(_docker_app(monkeypatch, svc, _friend(FRIEND))) as http:
        assert http.post("/api/chat/handshake", json=_body(FRIEND, signed_for=None)).status_code == 403
        assert svc.writes == []
        r = http.post("/api/chat/handshake", json=_body(FRIEND))
        assert r.status_code == 200 and svc.writes == [("seen", FRIEND.pub)]
    collide = _Svc()
    with TestClient(_docker_app(monkeypatch, collide, _friend(FRIEND, invite_code=STRANGER.invite))) as http:
        r = http.post("/api/chat/handshake", json=_body(STRANGER))
        assert r.status_code == 403 and collide.writes == [] and "public_key_hex" not in r.json()


def test_a_history_request_made_for_one_node_fails_at_another(monkeypatch):
    from fastapi.testclient import TestClient
    from routers import peer_chat
    svc = _Svc(friend={"id": 7, "is_blocked": False, "username": FRIEND.name})
    app = _docker_app(monkeypatch, svc, None)
    monkeypatch.setattr(peer_chat.invite_tokens, "friend_has_right", lambda *a: True)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(peer_chat, "get_conn", lambda: _Conn())
    monkeypatch.setattr(peer_chat.chat_wire, "export_rows", lambda conn, fid, after: [])

    def request(responder):
        ts, nonce = int(time.time()), "n-1"
        sig = FRIEND.key.sign(chat_wire.history_payload(ts, nonce, responder.pub)).hex()
        return {"public_key_hex": FRIEND.pub, "ts": ts, "nonce": nonce, "signature": sig}

    with TestClient(app) as http:
        assert http.post("/api/chat/history", json=request(HOST)).status_code == 200
        assert http.post("/api/chat/history", json=request(OTHER_NODE)).status_code == 403


async def _wake(server, query, headers=None, answer=None):
    app = web.Application()
    app.router.add_get("/api/relay/wake-stream", server.handle_wake_stream)
    async with AioClient(TestServer(app)) as client:
        r = await client.get("/api/relay/wake-stream", params=query, headers=headers or {})
        if answer is not None:
            answer.update(r.headers)
        return r.status


def test_a_strangers_relay_slot_needs_the_switch_and_a_verified_identity(monkeypatch):
    from desktop.p2p import peer_auth
    from desktop.p2p.sync_server import voucher_payload

    server = SyncServer("postgresql://unused",
                        account_info={"public_key_hex": HOST.pub, "username": HOST.name,
                                      "invite_code": HOST.invite})
    server._chat_service = SimpleNamespace(friend_for_key=lambda pk: None)
    ts, until = int(time.time()), int(time.time()) + 3600
    query = {"pubkey": STRANGER.pub, "ts": str(ts), "instance": "a" * 16,
             "sig": STRANGER.key.sign(f"wake_subscribe:{ts}:{HOST.pub}:{STRANGER.pub}".encode()).hex(),
             "invite": STRANGER.invite, "voucher_until": str(until),
             "voucher_sig": STRANGER.key.sign(voucher_payload(STRANGER.pub, HOST.pub, until).encode()).hex()}
    monkeypatch.setattr(server, "_relay_enabled", lambda: False)
    assert asyncio.run(_wake(server, query)) == 403                     # not relaying
    monkeypatch.setattr(server, "_relay_enabled", lambda: True)
    assert asyncio.run(_wake(server, query)) == 403                     # no certificate
    admitted = []

    class _Gate:
        async def admit(self, pk, cert, proof, addr):
            admitted.append(pk)
            return SimpleNamespace(status="unverified", detail="identity proof required",
                                   http_status=403, retry_after=None)

    monkeypatch.setattr(server, "_identity_gate", lambda: _Gate())
    headers = {peer_auth.HDR_CERT: peer_auth.encode_cert_bundle({"pubkey": STRANGER.pub}, None)}
    assert asyncio.run(_wake(server, query, headers)) == 403 and admitted == [STRANGER.pub]


def test_a_tokens_identity_gate_is_awaited_on_the_serving_loop(monkeypatch):
    """While the gate decides, the chat lane — one thread here — serves on."""
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient
    import db_pool
    from routers import peer_chat
    svc = _Svc()
    app = _docker_app(monkeypatch, svc, None)
    monkeypatch.setitem(db_pool._lane_executors, "chat",
                        ThreadPoolExecutor(max_workers=1, thread_name_prefix="chat-lane"))
    gated = []

    class _Gate:
        async def admit(self, pk, cert, proof, addr):
            try:
                gated.append(await asyncio.wait_for(db_pool.in_chat_lane(lambda: "lane free"), 2))
            except asyncio.TimeoutError:
                gated.append("lane held by the waiting handshake")
            return SimpleNamespace(status="unverified", detail="identity proof required",
                                   http_status=403, retry_after=None)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(peer_chat, "_identity_gate", lambda: _Gate())
    monkeypatch.setattr(peer_chat, "get_conn", lambda: _Conn())
    monkeypatch.setattr(peer_chat.invite_tokens, "token_requires_cert", lambda conn, tid: True)
    monkeypatch.setattr(peer_chat.invite_tokens, "consume_token",
                        lambda conn, tid: pytest.fail("a use spent past a refusing gate"))
    ts, tid = int(time.time()), "t-1"
    body = {"public_key_hex": FRIEND.pub, "username": FRIEND.name, "invite_code": FRIEND.invite,
            "token_id": tid, "ts": ts,
            "signature": FRIEND.key.sign(f"token_handshake:{ts}:{tid}:{HOST.invite}".encode()).hex()}
    with TestClient(app) as http:
        r = http.post("/api/chat/handshake", json=body)
    assert r.status_code == 403 and r.json()["error"] == "identity proof required"
    assert gated == ["lane free"]


def test_a_history_page_is_bounded_and_says_where_the_next_one_starts(monkeypatch):
    import uuid
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 11, 3, tzinfo=timezone.utc)
    rows = [{"id": i, "message_uuid": uuid.UUID(int=i),
             "direction": "out" if i % 2 else "in", "content": "x" * 10,
             "timestamp": t0 + timedelta(seconds=i)} for i in range(1, 5)]
    seal = lambda row: row["content"]
    monkeypatch.setattr(chat_wire, "HISTORY_EXPORT_MAX", 3)          # read one past it: 4 rows
    page, out_ids, after, more = chat_wire.export_page(rows, seal)
    assert [m["message_uuid"] for m in page] == [str(uuid.UUID(int=i)) for i in (1, 2, 3)]
    assert out_ids == [1, 3] and more                                # the outgoing ones it carries
    assert chat_wire.history_cursor(after) == (t0 + timedelta(seconds=3), 3)
    monkeypatch.setattr(chat_wire, "HISTORY_EXPORT_MAX", 10)
    monkeypatch.setattr(chat_wire, "HISTORY_EXPORT_BYTES", 15)       # the second message reaches it
    page, out_ids, after, more = chat_wire.export_page(rows, seal)
    assert len(page) == 2 and more and chat_wire.history_cursor(after)[1] == 2
    assert chat_wire.export_page(rows[:1], seal)[2:] == (f"{(t0 + timedelta(seconds=1)).isoformat()}|1", False)
    assert chat_wire.export_page([], seal) == ([], [], None, False)
    assert chat_wire.history_cursor("not a cursor") is None


def test_a_pull_reads_page_by_page_and_the_next_goes_on_from_there(monkeypatch):
    from desktop import node_identity
    from desktop.p2p import p2p_manager
    pages = {None: ({"messages": [1, 2], "next": "c1", "more": True}),
             "c1": ({"messages": [3], "next": "c2", "more": True}),
             "c2": ({"messages": [4], "next": "c3", "more": False}),
             "c3": ({"messages": [], "next": None, "more": False})}
    asked = []
    manager = p2p_manager.P2PManager.__new__(p2p_manager.P2PManager)
    manager._chat_service, manager._on_message_cb = SimpleNamespace(), None
    manager._history_after = {}
    manager._note_peer_alive = lambda *a: None

    async def page(url, pubkey, account, after):
        asked.append(after)
        return pages[after]

    manager._history_page = page
    manager._import_history_page = lambda friend, pk, data: {
        "imported": len(data["messages"]), "skipped": 0, "delivered": 0}
    monkeypatch.setattr(node_identity, "get_account_info", lambda: {"public_key_hex": HOST.pub})
    monkeypatch.setattr(chat_wire, "HISTORY_PAGES_PER_PULL", 2)
    friend = {"id": 7, "public_key_hex": FRIEND.pub, "username": "friend"}
    first = asyncio.run(manager._sync_chat_history(friend, [("198.51.100.5", 1)]))
    second = asyncio.run(manager._sync_chat_history(friend, [("198.51.100.5", 1)]))
    third = asyncio.run(manager._sync_chat_history(friend, [("198.51.100.5", 1)]))
    assert asked == [None, "c1", "c2", "c3"]
    assert (first["imported"], second["imported"], third["imported"]) == (3, 1, 0)
    assert manager._history_after[(7, "198.51.100.5:1")] == "c3"   # an empty page keeps it
    asyncio.run(manager._sync_chat_history(friend, [("198.51.100.6", 1)]))
    assert asked[4] is None                    # another node of the account: its own cursor


def test_a_stored_message_is_pushed_at_once_and_again_if_another_arrives(monkeypatch):
    from desktop.p2p import p2p_manager
    pushed = []
    manager = p2p_manager.P2PManager.__new__(p2p_manager.P2PManager)
    manager._running = True
    manager._pushing, manager._push_again, manager._push_tasks = set(), set(), set()
    pending = [{"friend_id": 7, "public_key_hex": FRIEND.pub, "invite_code": FRIEND.invite}]
    manager._chat_service = SimpleNamespace(get_pending_messages=lambda: list(pending))

    async def peers(info, refresh=False):
        return [("198.51.100.5", 1)]

    async def push(fid, pubkey, messages, peer_list):
        pushed.append(fid)
        if len(pushed) == 1:
            manager._deliver_now({7})                       # another message meanwhile
            await asyncio.sleep(0)

    manager._find_friend_peers, manager._push_pending_messages = peers, push

    async def scene():
        manager._deliver_now({7})
        while manager._push_tasks:
            await asyncio.sleep(0.01)

    asyncio.run(scene())
    assert pushed == [7, 7] and not manager._pushing


def test_the_docker_relay_slot_needs_the_switch_room_and_a_verified_identity(monkeypatch):
    from fastapi.testclient import TestClient
    from desktop.p2p import peer_auth
    from desktop.p2p.sync_server import voucher_payload
    from routers import peer_chat, settings as settings_router
    app = _docker_app(monkeypatch, _Svc(), None)
    app.include_router(peer_chat.relay_router)
    ts, until = int(time.time()), int(time.time()) + 3600
    query = {"pubkey": STRANGER.pub, "ts": str(ts), "instance": "a" * 16,
             "sig": STRANGER.key.sign(f"wake_subscribe:{ts}:{HOST.pub}:{STRANGER.pub}".encode()).hex(),
             "invite": STRANGER.invite, "voucher_until": str(until),
             "voucher_sig": STRANGER.key.sign(voucher_payload(STRANGER.pub, HOST.pub, until).encode()).hex()}
    switch, room, gated = {"on": False}, {"free": True}, []
    monkeypatch.setattr(settings_router, "_read",
                        lambda key: switch["on"] if key == "p2p.relay_enabled" else None)
    monkeypatch.setattr(peer_chat, "relay_has_room", lambda: room["free"])

    class _Gate:
        async def admit(self, pk, cert, proof, addr):
            gated.append(pk)
            return SimpleNamespace(status="unverified", detail="identity proof required",
                                   http_status=403, retry_after=None)

    monkeypatch.setattr(peer_chat, "_identity_gate", lambda: _Gate())
    cert = {peer_auth.HDR_CERT: peer_auth.encode_cert_bundle({"pubkey": STRANGER.pub}, None)}
    with TestClient(app) as http:
        get = lambda headers=None: http.get("/api/relay/wake-stream", params=query,
                                            headers=headers or {})
        assert get(cert).json()["error"] == "not relaying"
        switch["on"] = True
        room["free"] = False
        assert get(cert).status_code == 429 and gated == []       # full: no proof is checked
        room["free"] = True
        assert get().json()["error"] == "identity certificate required"
        r = get(cert)
        assert r.status_code == 403 and r.json()["error"] == "identity proof required"
    assert gated == [STRANGER.pub]


def test_with_the_relay_off_a_friends_voucher_announces_nothing(monkeypatch):
    from desktop.p2p.sync_server import voucher_payload
    server = SyncServer("postgresql://unused",
                        account_info={"public_key_hex": HOST.pub, "username": HOST.name,
                                      "invite_code": HOST.invite})
    announced = []
    server._chat_service = SimpleNamespace(
        friend_for_key=lambda pk: _friend(FRIEND), update_friend_last_seen=lambda pk: None)
    server.set_client_announce_cbs(announced.append, lambda code: None)
    monkeypatch.setattr(server, "_relay_enabled", lambda: False)
    ts, until = int(time.time()), int(time.time()) + 3600
    query = {"pubkey": FRIEND.pub, "ts": str(ts), "instance": "b" * 16,
             "sig": FRIEND.key.sign(f"wake_subscribe:{ts}:{HOST.pub}:{FRIEND.pub}".encode()).hex(),
             "invite": FRIEND.invite, "voucher_until": str(until),
             "voucher_sig": FRIEND.key.sign(voucher_payload(FRIEND.pub, HOST.pub, until).encode()).hex()}
    answer = {}
    assert asyncio.run(_wake(server, query, answer=answer)) == 200   # its chat stream, said so
    assert answer.get("X-Sautium-Relay") == "off"
    assert announced == [] and FRIEND.pub not in server._relay_clients


def test_switching_the_relay_off_ends_the_slots_held():
    from desktop.p2p.wake_registry import WakeRegistry
    from routers import peer_chat

    async def scene(close, registry, clients):
        friend = registry.register(FRIEND.pub, "", "198.51.100.7")
        relayed = registry.register(STRANGER.pub, "", "198.51.100.8")
        clients[STRANGER.pub] = {"invite_code": STRANGER.invite}
        close()
        return friend.closed, relayed.closed

    server = SyncServer("postgresql://unused")
    assert asyncio.run(scene(server.close_relay_clients, server._wake, server._relay_clients)) \
        == (False, True)
    registry = WakeRegistry()
    peer_chat._wake, saved = registry, peer_chat._wake
    try:
        assert asyncio.run(scene(peer_chat.close_relay_clients, registry, peer_chat._relay_clients)) \
            == (False, True)
    finally:
        peer_chat._wake = saved
        peer_chat._relay_clients.pop(STRANGER.pub, None)


def test_the_pass_leaves_a_friend_a_push_is_under_way_for(monkeypatch):
    from desktop.p2p import p2p_manager
    manager = p2p_manager.P2PManager.__new__(p2p_manager.P2PManager)
    manager._running, manager._pushing = True, {7}
    manager._history_synced_friends = set()
    pending = [{"friend_id": 7, "public_key_hex": FRIEND.pub, "invite_code": FRIEND.invite}]
    manager._chat_service = SimpleNamespace(get_pending_messages=lambda: list(pending))
    pushed = []

    async def peers(info, refresh=False):
        return []

    async def push(*a):
        pushed.append(a[0])

    manager._find_friend_peers, manager._push_pending_messages = peers, push

    async def scene():
        manager._chat_notify = asyncio.Event()
        manager._chat_notify.set()
        task = asyncio.ensure_future(manager._sync_chat_histories())
        await asyncio.sleep(0.05)
        manager._running = False
        manager._chat_notify.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(scene())
    assert pushed == []


def test_switching_the_relay_off_is_heard_at_once_on_both_surfaces(monkeypatch):
    """The settings write ends Docker's slots in process and tells a
    launcher; the launcher's switch reads off from the close on, inside its
    cache's 10 s too."""
    from routers import peer_chat, settings as settings_router
    written, notified, closed = [], [], []
    monkeypatch.setattr(settings_router, "_write", lambda k, v: written.append((k, v)))
    monkeypatch.setattr(settings_router, "db_execute", lambda sql, *a: notified.append(sql))
    monkeypatch.setattr(settings_router, "_sync_state", lambda: {})
    monkeypatch.setattr(peer_chat, "close_relay_clients", lambda: closed.append(True))
    settings_router.put_sync_prefs(settings_router.SyncPrefs(relay_enabled=False))
    assert written == [("p2p.relay_enabled", False)] and closed == [True]
    assert notified == ["NOTIFY sautium_relay_off"]
    server = SyncServer("postgresql://unused")
    server._relay_on, server._relay_checked = True, time.time()        # read on, just now
    server.close_relay_clients()
    assert server._relay_enabled() is False


def test_a_cursor_without_its_offset_is_no_cursor():
    assert chat_wire.history_cursor("2026-11-03T00:00:00|5") is None
    assert chat_wire.history_cursor("2026-11-03T00:00:00+00:00|5")[1] == 5
