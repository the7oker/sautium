"""What a chat request binds (desktop/p2p/chat_wire.py, the classic
handshake on both surfaces, the history export, the relay's admission):
a classic handshake is signed by its key, for its responder, just now,
under its own name — unsigned, it reads and writes nothing; a known friend
updates its presence only; one refusal for a blocked, an unknown and a
colliding key; a history request made for one node fails at another; a
stranger's relay slot needs the relay switch and a verified identity."""

import asyncio
import sys
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

    def get_history_for_export(self, fid, since=None, limit=None):
        return []

    def mark_exported_delivered(self, fid, max_id):
        pass


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

    def request(responder):
        ts, nonce = int(time.time()), "n-1"
        sig = FRIEND.key.sign(chat_wire.history_payload(ts, nonce, responder.pub)).hex()
        return {"public_key_hex": FRIEND.pub, "ts": ts, "nonce": nonce, "signature": sig}

    with TestClient(app) as http:
        assert http.post("/api/chat/history", json=request(HOST)).status_code == 200
        assert http.post("/api/chat/history", json=request(OTHER_NODE)).status_code == 403


async def _wake(server, query, headers=None):
    app = web.Application()
    app.router.add_get("/api/relay/wake-stream", server.handle_wake_stream)
    async with AioClient(TestServer(app)) as client:
        r = await client.get("/api/relay/wake-stream", params=query, headers=headers or {})
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
