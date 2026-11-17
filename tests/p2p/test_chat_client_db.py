"""The chat client both runtimes run (desktop/p2p/chat_client.py) as the
Docker backend builds and feeds it (backend/p2p_chat.py, main.py,
routers/p2p.py, p2p_app.py), on a real PostgreSQL built from the migrations
— skipped without a cluster; the network is faked where the other end is
another suite's subject (the peer surfaces' own tests).

Checked: a fresh node gets the Support contact once — pending, its token
and its key's fingerprint — and none when Support was removed or when it is
the master; the backend builds no client in launcher mode, none on the
master, and one otherwise; the backend's one LISTEN hands sautium_chat to
the client, which pushes a friend a reply was stored for at once; a message
relayed down a Docker node's stream is stored and its receipt sent, a
support warrant on it collects and sends nothing; a relay's receipt is
verified before a message counts as delivered; the verdict is written for
the Sync & P2P screen and its consumers follow it — the DHT's own announces
and the relay role; an internet vantage's request is proof of reachability,
the bridge gateway's is not."""

import asyncio
import hashlib
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import psycopg2
import pytest

REPO = Path(__file__).resolve().parents[2]
for p in (REPO, REPO / "backend"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from desktop import node_identity  # noqa: E402
from desktop.p2p import chat_client as cc, chat_wire, master_node  # noqa: E402
from desktop.p2p.chat_service import ChatService  # noqa: E402

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")


def _who(name):
    key = ed25519.Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes_raw()
    return SimpleNamespace(key=key, pub=raw.hex(), name=name,
                           invite=node_identity.make_invite_code(name, raw))


ME, FRIEND, RELAY, MASTER = (_who(n) for n in ("me", "friend", "relay", "Sautium"))


@pytest.fixture
def conn(scratch_dsn):
    c = psycopg2.connect(scratch_dsn)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("TRUNCATE friends CASCADE")
        cur.execute("DELETE FROM user_settings WHERE key LIKE 'p2p.%'")
    yield c
    c.close()


@pytest.fixture
def master(monkeypatch):
    """A test key in place of the shipped master's (both mirrors)."""
    import master_node as backend_master
    invite = node_identity.make_invite_code("Sautium", bytes.fromhex(MASTER.pub))
    for mod in (master_node, backend_master):
        monkeypatch.setattr(mod, "MASTER_PUBKEY_HEX", MASTER.pub)
        monkeypatch.setattr(mod, "MASTER_INVITE_CODE", invite)
    return invite


def _ident(who):
    return cc.ClientIdentity(public_key_hex=who.pub, username=who.name, invite_code=who.invite,
                             sign=who.key.sign, cert_bundle=lambda: None)


def _client(dsn, who=ME, **deps):
    store = ChatService(dsn, who.key.private_bytes_raw(), who.pub)
    client = cc.ChatClient(store, identity=lambda: _ident(who), announce_port=lambda: 8801, **deps)
    client.running = True
    return client


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def test_a_fresh_node_gets_the_support_contact_once(conn, scratch_dsn, master):
    client = _client(scratch_dsn)
    asyncio.run(client._ensure_master_contact())
    asyncio.run(client._ensure_master_contact())
    rows = _rows(conn, "SELECT public_key_hex, source::text, join_token_id::text, key_fingerprint "
                       "FROM friends")
    assert len(rows) == 1
    pubkey, source, token, fingerprint = rows[0]
    assert pubkey == f"pending:{master}" and source == "master"
    assert token == master_node.MASTER_TOKEN_ID
    assert bytes(fingerprint).hex().upper() == chat_wire.key_fingerprint(MASTER.pub).upper()


def test_no_support_contact_once_removed_nor_on_the_master(conn, scratch_dsn, master):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO user_settings (key, value) VALUES ('p2p.master_removed', 'true')")
    asyncio.run(_client(scratch_dsn)._ensure_master_contact())
    assert _rows(conn, "SELECT 1 FROM friends") == []
    with conn.cursor() as cur:
        cur.execute("DELETE FROM user_settings WHERE key = 'p2p.master_removed'")
    asyncio.run(_client(scratch_dsn, MASTER)._ensure_master_contact())
    assert _rows(conn, "SELECT 1 FROM friends") == []
    assert cc.master_runs_none(MASTER.pub) and not cc.master_runs_none(ME.pub)


def test_the_backend_builds_one_client_where_it_owns_the_peer_surface(conn, scratch_dsn, master,
                                                                       monkeypatch):
    import p2p_chat
    from config import settings
    from urllib.parse import urlparse
    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port,
                           postgres_user=url.username, postgres_password=url.password,
                           postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(settings, "p2p_enabled", True)
    monkeypatch.setattr(p2p_chat, "_client_lock", asyncio.Lock())
    monkeypatch.setattr(p2p_chat, "_client_deps", {"announce_port": lambda: 8801})
    keys = {"who": ME}
    monkeypatch.setattr(p2p_chat, "_keys", lambda: (
        {"public_key_hex": keys["who"].pub}, keys["who"].key.private_bytes_raw(), ()))
    monkeypatch.setattr(p2p_chat, "client_identity", lambda: _ident(keys["who"]))

    async def started(self):
        self.running = True
    monkeypatch.setattr(cc.ChatClient, "start", started)

    async def scene(port, who):
        monkeypatch.setattr(settings, "p2p_sync_port", port)
        keys["who"] = who
        client = await p2p_chat.start_client()
        again = await p2p_chat.start_client()
        await p2p_chat.stop_client()
        return client, again

    assert asyncio.run(scene(0, ME)) == (None, None)            # launcher mode: the launcher's client
    assert asyncio.run(scene(8801, MASTER)) == (None, None)     # the master runs none
    client, again = asyncio.run(scene(8801, ME))
    assert client is not None and again is client


class _Note(SimpleNamespace):
    pass


def test_the_one_listen_hands_a_reply_to_the_client_which_pushes_it_at_once(monkeypatch):
    import p2p_chat
    from routers import p2p as router
    pushed = []
    client = cc.ChatClient(SimpleNamespace(), identity=lambda: None, announce_port=lambda: 1)

    async def push_to(fid):
        pushed.append(fid)

    async def scene():
        client._loop, client._notify, client.running = asyncio.get_running_loop(), asyncio.Event(), True
        client._push_to = push_to
        monkeypatch.setattr(p2p_chat, "_client", client)
        monkeypatch.setattr("routers.peer_chat.ping_wake_by_friend_id", lambda fid: None)
        notes = [_Note(channel="sautium_chat", payload="msg:7:out"),
                 _Note(channel="sautium_chat", payload="msg:8:in")]
        await asyncio.to_thread(router._on_chat_notify, notes)     # the listener's thread
        for _ in range(50):
            if pushed:
                break
            await asyncio.sleep(0.01)
        return client._notify.is_set()

    assert asyncio.run(scene()) is True
    assert pushed == [7]


def _friend_of(conn, dsn, me, friend):
    return ChatService(dsn, me.key.private_bytes_raw(), me.pub).add_friend(
        public_key_hex=friend.pub, invite_code=friend.invite, username=friend.name)


def test_a_relayed_message_is_stored_and_receipted_and_a_warrant_collects_nothing(
        conn, scratch_dsn, master, caplog):
    _friend_of(conn, scratch_dsn, ME, FRIEND)
    client = _client(scratch_dsn)
    receipts = []

    async def receipt(envelope, relay_pubkey, ip, port):
        receipts.append(envelope["message_uuid"])
    client._send_delivery_receipt = receipt
    sender = ChatService(scratch_dsn, FRIEND.key.private_bytes_raw(), FRIEND.pub)
    msg_uuid, sent = str(uuid.uuid4()), datetime.now(timezone.utc).isoformat()
    frame = {"type": "deliver", "envelope": {
        "from_public_key": FRIEND.pub, "message_uuid": msg_uuid, "timestamp": sent,
        "encrypted": sender.encrypt_envelope("through the relay", msg_uuid, sent, ME.pub)}}
    import json
    asyncio.run(client._handle_relay_frame(json.dumps(frame), None, RELAY.pub, "192.0.2.9", 1))
    assert _rows(conn, "SELECT content FROM p2p_messages WHERE message_uuid = %s",
                 (msg_uuid,)) == [("through the relay",)]
    assert receipts == [msg_uuid]
    from desktop.p2p import diag_protocol
    warrant = json.dumps({"type": diag_protocol.FRAME_TYPE, "warrant": {"id": "w"}})
    with caplog.at_level("INFO", logger="desktop.p2p.chat_client"):
        asyncio.run(client._handle_relay_frame(warrant, None, MASTER.pub, "192.0.2.9", 1))
    assert "answers no warrant" in caplog.text and receipts == [msg_uuid]


class _Answer:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, answer):
        self.answer = answer

    def post(self, url, json=None, ssl=None):
        return self.answer(json)


def test_a_relay_counts_as_delivery_only_with_the_recipients_receipt(conn, scratch_dsn, monkeypatch):
    fid = _friend_of(conn, scratch_dsn, ME, FRIEND)
    client = _client(scratch_dsn)
    msg = client.store.prepare_outgoing(fid, "hello")
    row = _rows(conn, "SELECT id, content, timestamp, message_uuid::text FROM p2p_messages")[0]
    message = {"id": row[0], "content": row[1], "timestamp": row[2], "message_uuid": row[3]}
    assert msg["message_uuid"] == row[3]

    async def read_json(resp, cap):
        return resp._body
    monkeypatch.setattr(cc.wire, "read_json", read_json)

    def receipt_by(signer):
        def answer(body):
            ct_hash = hashlib.sha256(body["envelope"]["encrypted"].encode()).hexdigest()
            sig = signer.key.sign(chat_wire.delivery_payload(row[3], ct_hash).encode()).hex()
            return _Answer(200, {"delivered": True, "ack": {
                "public_key_hex": FRIEND.pub, "message_uuid": row[3],
                "ciphertext_sha256": ct_hash, "signature": sig}})
        return answer

    forged = asyncio.run(client._forward_via_relay(
        _Session(receipt_by(RELAY)), RELAY.pub, [("192.0.2.9", 1)], None, fid, FRIEND.pub,
        [message], _ident(ME)))
    assert forged is False and _rows(conn, "SELECT delivered FROM p2p_messages") == [(False,)]
    real = asyncio.run(client._forward_via_relay(
        _Session(receipt_by(FRIEND)), RELAY.pub, [("192.0.2.9", 1)], None, fid, FRIEND.pub,
        [message], _ident(ME)))
    assert real is True and _rows(conn, "SELECT delivered FROM p2p_messages") == [(True,)]


@pytest.mark.parametrize("verdict, heuristic, status", [
    (True, None, "reachable"), (False, None, "unreachable"),
    (False, ("cgnat", "router WAN 100.64.0.1 is private/carrier-grade"), "cgnat"),
    (None, None, "unknown")])
def test_the_verdict_is_recorded_and_its_consumers_follow_it(conn, scratch_dsn, monkeypatch,
                                                             verdict, heuristic, status):
    heard = []

    async def consumers(s, detail):
        heard.append(s)
    client = _client(scratch_dsn, heuristic=lambda: heuristic, on_reachability=consumers)

    async def probe():
        client.running = False                                 # one cycle
        return cc._ProbeResult(verdict, "the master's word")
    client._probe_reachability_via_master = probe
    monkeypatch.setattr(cc.asyncio, "sleep", _no_sleep)
    asyncio.run(client._reachability_loop())
    assert heard == [status] and client.reachability == status
    assert _rows(conn, "SELECT value FROM user_settings WHERE key = %s",
                 ("p2p.reachability",)) == [(status,)]


async def _no_sleep(_s):
    return None


def test_a_docker_nodes_relay_role_and_own_announces_follow_the_verdict(monkeypatch):
    monkeypatch.chdir(REPO / "backend")              # main mounts static/ from its folder
    import main
    from routers import peer_chat
    calls = []
    dht = SimpleNamespace(set_announces_enabled=lambda on: calls.append(("announces", on)),
                          withdraw_capability=lambda cap: calls.append(("withdraw", cap)))

    async def announce(cap):
        calls.append(("announce", cap))
    dht.announce_capability = announce
    monkeypatch.setattr(main, "_dht_service", dht)
    monkeypatch.setattr(peer_chat, "relay_has_room", lambda: True)
    monkeypatch.setattr(peer_chat, "close_relay_clients", lambda: calls.append(("close", None)))
    monkeypatch.setattr("routers.settings._read", lambda key: True)
    asyncio.run(main._apply_reachability("reachable", ""))
    assert calls == [("announces", True), ("announce", "relay")]
    calls.clear()
    asyncio.run(main._apply_reachability("unreachable", ""))
    assert calls == [("announces", False), ("withdraw", "relay"), ("close", None)]
    calls.clear()
    asyncio.run(main._apply_reachability("unknown", ""))
    assert calls == [("announces", True), ("withdraw", "relay"), ("close", None)]


def test_an_internet_vantage_proves_reachability_and_the_bridge_gateway_does_not(monkeypatch):
    import p2p_app
    import p2p_chat
    noted = []
    monkeypatch.setattr(p2p_chat, "_client",
                        SimpleNamespace(note_inbound_reachable=lambda: noted.append(1)))
    for addr in ("172.17.0.1", "10.9.8.7", "127.0.0.1", None):
        p2p_app._note_inbound(addr)
    assert noted == []
    p2p_app._note_inbound("8.8.4.4")                 # a global address (TEST-NET is not one)
    assert noted == [1]
