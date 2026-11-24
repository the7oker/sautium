"""The master's voucher for its Worker mailbox (SUPPORT-FIRST, BACKLOG B-118),
on a real PostgreSQL built from the migrations, with a throwaway key pinned in
place of the shipped master's: the master signs one only for a key it takes
mail from — a friend, not blocked, with `can_message`, whose identity its
gate verified, no ban — at the handshake's answer and the wake stream's
connect, on both peer surfaces, and nowhere else; a node keeps the newest it
was given for the key it signs with, and its drop carries that one alone. The
Worker's own check is in tests/p2p/test_worker_limits.py."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import psycopg2
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import db_pool  # noqa: E402
import master_node as backend_master  # noqa: E402
import p2p_identity  # noqa: E402
from config import settings  # noqa: E402
from desktop import node_identity  # noqa: E402
from desktop.p2p import chat_wire, identity_registry, master_node  # noqa: E402
from desktop.p2p.chat_service import ChatService  # noqa: E402


def _who(name):
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes_raw()
    return SimpleNamespace(key=key, pub=raw.hex(), name=name,
                           invite=node_identity.make_invite_code(name, raw))


MASTER = _who("Sautium")
GOOD, BLOCKED, NO_RIGHT, UNVERIFIED, BANNED, STRANGER = (
    _who(n) for n in ("good", "blocked", "noright", "unverified", "banned", "stranger"))


def _verify(pubkey, message, sig):
    return node_identity.verify_signature(message, sig, pubkey)


@pytest.fixture
def master_db(scratch_dsn, monkeypatch):
    for mod in (master_node, backend_master):
        monkeypatch.setattr(mod, "MASTER_PUBKEY_HEX", MASTER.pub)
        monkeypatch.setattr(mod, "MASTER_INVITE_CODE", MASTER.invite)
    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port,
                           postgres_user=url.username, postgres_password=url.password,
                           postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    db_pool.close_pool()
    monkeypatch.setattr(p2p_identity, "_signing_key_cache", MASTER.key)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("TRUNCATE friends, p2p_identities, p2p_node_bans, mail_vouchers CASCADE")
        for who, source, blocked in ((GOOD, "manual", False), (BLOCKED, "manual", True),
                                     (NO_RIGHT, "token", False), (UNVERIFIED, "manual", False),
                                     (BANNED, "manual", False)):
            cur.execute("INSERT INTO friends (username, public_key_hex, invite_code, source, is_blocked)"
                        " VALUES (%s, %s, %s, %s::friend_source, %s)",
                        (who.name, who.pub, who.invite, source, blocked))
        for who in (GOOD, BLOCKED, NO_RIGHT, BANNED, STRANGER):
            _verified(cur, who.pub)
        cur.execute("INSERT INTO p2p_node_bans (pubkey, reason) VALUES (%s, 'test')", (BANNED.pub,))
    master_node.decide(MASTER.pub)
    yield conn
    master_node.decide(None)
    conn.close()
    db_pool.close_pool()


def _verified(cur, pubkey):
    cur.execute("""INSERT INTO p2p_identities (pubkey, cert_v, method, issued_at, difficulty,
                                               params_version, issuer, cert_sig, status, verified_at)
                   VALUES (%s, 4, 'pow', now(), 1, 1, 'test', 'sig', 'verified', now())""", (pubkey,))


def test_the_master_vouchers_only_a_key_it_takes_mail_from(master_db):
    from routers import peer_chat
    voucher = peer_chat._mail_voucher(GOOD.pub)
    assert chat_wire.mail_voucher_holds(voucher, GOOD.pub, MASTER.pub, _verify)
    assert voucher["expires_at"] - voucher["issued_at"] == chat_wire.MAIL_VOUCHER_LIFE_S
    for who in (BLOCKED, NO_RIGHT, UNVERIFIED, BANNED, STRANGER):
        assert peer_chat._mail_voucher(who.pub) is None, who.name


def test_no_other_node_vouchers(master_db):
    from routers import peer_chat
    master_node.decide(GOOD.pub)
    assert peer_chat._mail_voucher(GOOD.pub) is None


def test_the_handshakes_answer_carries_it(master_db):
    from routers import peer_chat
    ident = {"public_key_hex": MASTER.pub, "username": "Sautium", "invite_code": MASTER.invite}
    body = json.loads(peer_chat._accepted(ident, GOOD.pub).body)
    assert chat_wire.mail_voucher_holds(body["mail_voucher"], GOOD.pub, MASTER.pub, _verify)
    assert "mail_voucher" not in json.loads(peer_chat._accepted(ident, UNVERIFIED.pub).body)


class _Gate:
    """The identity gate as a stream's connect meets it: what it is asked,
    and its verdict — a verified one recorded as the real gate records it."""
    def __init__(self, conn):
        self.conn, self.asked = conn, []

    async def admit(self, pubkey, cert, proof, addr):
        self.asked.append(pubkey)
        with self.conn.cursor() as cur:
            _verified(cur, pubkey)
        return identity_registry.Admission("verified")


def _request(bundle):
    from desktop.p2p import peer_auth
    headers = {peer_auth.HDR_CERT: peer_auth.encode_cert_bundle(*bundle)} if bundle else {}
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host="198.51.100.9"), remote="198.51.100.9")


def test_a_streams_connect_carries_it_and_a_key_new_here_is_checked_once(master_db, monkeypatch):
    from routers import peer_chat
    gate = _Gate(master_db)
    monkeypatch.setattr(peer_chat, "_identity_gate", lambda: gate)

    async def scene():
        known = await peer_chat._stream_mail_voucher(_request(None), GOOD.pub)
        unchecked = await peer_chat._stream_mail_voucher(_request(None), UNVERIFIED.pub)
        checked = await peer_chat._stream_mail_voucher(
            _request(({"pubkey": UNVERIFIED.pub}, None)), UNVERIFIED.pub)
        again = await peer_chat._stream_mail_voucher(
            _request(({"pubkey": UNVERIFIED.pub}, None)), UNVERIFIED.pub)
        return known, unchecked, checked, again
    known, unchecked, checked, again = asyncio.run(scene())
    assert chat_wire.mail_voucher_holds(known, GOOD.pub, MASTER.pub, _verify)
    assert unchecked is None
    assert chat_wire.mail_voucher_holds(checked, UNVERIFIED.pub, MASTER.pub, _verify)
    assert chat_wire.mail_voucher_holds(again, UNVERIFIED.pub, MASTER.pub, _verify)
    assert gate.asked == [UNVERIFIED.pub]                     # GOOD never; UNVERIFIED once


def test_the_launchers_surface_vouchers_alike(master_db, scratch_dsn, monkeypatch):
    from desktop.p2p import sync_server
    store = ChatService(scratch_dsn, MASTER.key.private_bytes_raw(), MASTER.pub)
    monkeypatch.setattr(node_identity, "get_private_key_raw", lambda: MASTER.key.private_bytes_raw())
    server = SimpleNamespace(_chat_service=store)
    voucher = sync_server.SyncServer._mail_voucher(server, GOOD.pub)
    assert chat_wire.mail_voucher_holds(voucher, GOOD.pub, MASTER.pub, _verify)
    for who in (BLOCKED, NO_RIGHT, UNVERIFIED, BANNED, STRANGER):
        assert sync_server.SyncServer._mail_voucher(server, who.pub) is None, who.name


def test_the_launchers_stream_connect_carries_it_and_checks_a_key_new_here_once(master_db, scratch_dsn,
                                                                                monkeypatch):
    """Review round 2: the launcher's mirror of the stream connect's voucher."""
    from desktop.p2p import sync_server
    store = ChatService(scratch_dsn, MASTER.key.private_bytes_raw(), MASTER.pub)
    monkeypatch.setattr(node_identity, "get_private_key_raw", lambda: MASTER.key.private_bytes_raw())
    gate = _Gate(master_db)
    server = SimpleNamespace(_chat_service=store, _identity_gate=lambda: gate)
    server._mail_voucher = lambda pk: sync_server.SyncServer._mail_voucher(server, pk)
    stream = sync_server.SyncServer._stream_mail_voucher

    async def scene():
        known = await stream(server, _request(None), GOOD.pub)
        unchecked = await stream(server, _request(None), UNVERIFIED.pub)
        checked = await stream(server, _request(({"pubkey": UNVERIFIED.pub}, None)), UNVERIFIED.pub)
        again = await stream(server, _request(({"pubkey": UNVERIFIED.pub}, None)), UNVERIFIED.pub)
        return known, unchecked, checked, again
    known, unchecked, checked, again = asyncio.run(scene())
    assert chat_wire.mail_voucher_holds(known, GOOD.pub, MASTER.pub, _verify)
    assert unchecked is None
    assert chat_wire.mail_voucher_holds(checked, UNVERIFIED.pub, MASTER.pub, _verify)
    assert chat_wire.mail_voucher_holds(again, UNVERIFIED.pub, MASTER.pub, _verify)
    assert gate.asked == [UNVERIFIED.pub]


def test_a_node_keeps_the_newest_voucher_for_its_key_and_its_drop_carries_that_one(master_db,
                                                                                   scratch_dsn):
    store = ChatService(scratch_dsn, GOOD.key.private_bytes_raw(), GOOD.pub)
    older = chat_wire.make_mail_voucher(MASTER.key.sign, GOOD.pub, now=1_000_000_000)
    newer = chat_wire.make_mail_voucher(MASTER.key.sign, GOOD.pub)
    store.keep_mail_voucher(newer)
    store.keep_mail_voucher(older)                            # an older one does not replace it
    assert store.mail_voucher(GOOD.pub) == newer
    assert store.mail_voucher(BLOCKED.pub) is None            # a key it holds none for (a retired one)
    with master_db.cursor() as cur:
        cur.execute("UPDATE mail_vouchers SET expires_at = now() - interval '1 second'")
    assert store.mail_voucher(GOOD.pub) is None               # past its life, no voucher


def test_a_node_keeps_a_voucher_only_when_it_is_the_masters_for_its_key(master_db, scratch_dsn):
    from desktop.p2p import chat_client as cc
    store = ChatService(scratch_dsn, GOOD.key.private_bytes_raw(), GOOD.pub)
    client = cc.ChatClient(store, identity=lambda: None, announce_port=lambda: 1)
    forged = chat_wire.make_mail_voucher(STRANGER.key.sign, GOOD.pub)
    another = chat_wire.make_mail_voucher(MASTER.key.sign, BLOCKED.pub)

    async def scene():
        for v in (forged, another, None, {"v": 1}):
            await client._keep_mail_voucher(v, GOOD.pub)
    asyncio.run(scene())
    assert store.mail_voucher(GOOD.pub) is None


def test_the_table_is_in_the_baseline_and_its_delta_alike(master_db):
    migrations = ROOT / "desktop" / "migrations"
    baseline = (migrations / "001_initial.sql").read_text(encoding="utf-8")
    [delta_file] = migrations.glob("*_mail_vouchers.sql")
    delta = delta_file.read_text(encoding="utf-8")
    ddl = delta[delta.index("CREATE TABLE IF NOT EXISTS mail_vouchers"):].strip()
    assert ddl in baseline
    with master_db.cursor() as cur:
        cur.execute(delta)                                    # again, on the schema that has it
        cur.execute("SELECT count(*) FROM mail_vouchers")
        assert cur.fetchone()[0] == 0
