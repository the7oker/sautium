"""A message to Support while Support is away (B-91, B-92): the chat client
both runtimes run (desktop/p2p/chat_client.py) on a node's own database, the
master as a Docker peer surface (p2p_app.app over TLS bound to its key, on a
database of its own) and the REAL Worker code (tests/p2p/worker_harness.mjs,
the test master's key swapped in) as the mailbox between them. Skipped
without a cluster or node.

Checked: a message the master cannot take now is kept in its mailbox — the
row is `kept` (parked, not delivered) — and the master's drain stores it
once and pings the sender's stream, whose pull makes the row delivered; a
sender offline at the drain learns at its connect, and what the drain
missed is pushed then; a kept message waits for the master's stream — the
pass and a push leave it alone, and it is not deposited twice; a mailbox
that refuses (a sender without a certificate) leaves the row waiting; a
`pending:` Support contact gets nothing deposited."""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

import db_pool  # noqa: E402
import master_node as backend_master  # noqa: E402
import p2p_identity  # noqa: E402
from config import settings  # noqa: E402
from desktop.p2p import chat_client as cc, mailbox_client, master_hint, master_node  # noqa: E402
from desktop.p2p.chat_service import ChatService  # noqa: E402
from tests.test_two_node_sync import _Docker, _Node  # noqa: E402

HARNESS = Path(__file__).resolve().parent / "p2p" / "worker_harness.mjs"
MASTER, ME = _Node("Sautium"), _Node("me")


@pytest.fixture(scope="module")
def worker():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    proc = subprocess.Popen(["node", str(HARNESS), os.urandom(32).hex(), "test-email-pepper", "--serve",
                             "--master-seed", MASTER.key.private_bytes_raw().hex(),
                             "--mail-per-sender", "30"],                   # one sender, every test
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    info = json.loads(proc.stdout.readline())
    yield info["url"]
    proc.terminate()
    proc.wait(10)


@pytest.fixture(scope="module")
def sender_dsn(scratch_dsn):
    """The sender's own database (the module's is the master's)."""
    from desktop import db_init
    from tests.conftest import PG, _recreate
    name = "sautium_test_chat_mailbox_sender"
    assert _recreate(name, create=True)
    conn = psycopg2.connect(dbname=name, **PG)
    db_init.apply_migrations(conn)
    conn.commit()
    conn.close()
    yield f"postgresql://{PG['user']}:{PG['password']}@{PG['host']}:{PG['port']}/{name}"
    _recreate(name, create=False)


def _connect(dsn):
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    return conn


def _born(worker_url, who):
    req = urllib.request.Request(worker_url + "/birth-certificate", method="POST", data=json.dumps({
        "pubkey_hex": who.pub, "signature": who.key.sign(f"birth:{who.pub}".encode()).hex()}).encode(),
        headers={"Content-Type": "application/json"})
    assert json.loads(urllib.request.urlopen(req, timeout=10).read())["pubkey"] == who.pub


@pytest.fixture
def world(worker, scratch_dsn, sender_dsn, monkeypatch, tmp_path):
    for mod in (master_node, backend_master):
        monkeypatch.setattr(mod, "MASTER_PUBKEY_HEX", MASTER.pub)
        monkeypatch.setattr(mod, "MASTER_INVITE_CODE", MASTER.info["invite_code"])
    monkeypatch.setattr(mailbox_client, "WORKER_URL", worker)
    where = {"master": None}
    monkeypatch.setattr(master_hint, "fetch", lambda *a, **k: where["master"])

    # The master: the Docker surface on the module's database, as MASTER.
    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port,
                           postgres_user=url.username, postgres_password=url.password,
                           postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    db_pool.close_pool()
    import p2p_chat
    monkeypatch.setattr(settings, "p2p_identity_dir", str(tmp_path / "master"))
    monkeypatch.setattr(p2p_identity, "_signing_key_cache", MASTER.key)
    monkeypatch.setattr(p2p_identity, "_witness", None)
    monkeypatch.setattr(p2p_identity, "resolve_identity", lambda s: MASTER.info)
    monkeypatch.setattr(p2p_chat, "_service", None)
    master_db, sender_db = _connect(scratch_dsn), _connect(sender_dsn)
    for conn in (master_db, sender_db):
        with conn.cursor() as cur:
            cur.execute("TRUNCATE friends CASCADE")
    with master_db.cursor() as cur:                      # the master holds the sender as a friend
        cur.execute("INSERT INTO friends (username, public_key_hex, invite_code, source) "
                    "VALUES ('me', %s, %s, 'manual')", (ME.pub, ME.info["invite_code"]))

    # The sender: its client on its own database, Support resolved.
    _born(worker, ME)
    store = ChatService(sender_dsn, ME.key.private_bytes_raw(), ME.pub)
    support = store.add_friend(public_key_hex=MASTER.pub, invite_code=MASTER.info["invite_code"],
                               username="Sautium", source="master")
    ident = cc.ClientIdentity(public_key_hex=ME.pub, username="me", invite_code=ME.info["invite_code"],
                              sign=ME.key.sign, cert_bundle=lambda: None)
    client = cc.ChatClient(store, identity=lambda: ident, announce_port=lambda: 1)
    client.running = True
    served = []

    def master_up():
        served.append(_Docker(MASTER, tmp_path / "tls"))
        host, port = urlparse(served[-1].url).hostname, urlparse(served[-1].url).port
        where["master"] = (host, port)
        return host, port

    def drain():
        box = mailbox_client.MasterMailbox(MASTER.pub, MASTER.key.sign,
                                           __import__("routers.peer_chat", fromlist=["x"]).mailbox_import,
                                           worker_url=worker)

        async def run():
            import aiohttp
            async with aiohttp.ClientSession() as session:
                return await box.drain(session)
        return run()
    try:
        yield SimpleNamespace(client=client, store=store, support=support, master_db=master_db,
                              sender_db=sender_db, master_up=master_up, drain=drain, where=where)
    finally:
        for s in served:
            s.close()
        master_db.close()
        sender_db.close()
        db_pool.close_pool()


def _state(conn, msg_uuid):
    with conn.cursor() as cur:
        cur.execute("SELECT delivered, parked_at IS NOT NULL FROM p2p_messages WHERE message_uuid = %s",
                    (msg_uuid,))
        return cur.fetchone()


def _held(conn, msg_uuid):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM p2p_messages WHERE message_uuid = %s", (msg_uuid,))
        return cur.fetchone()[0]


async def _until(check, seconds=15):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.05)


def test_a_message_kept_while_support_is_away_is_delivered_when_its_drain_pings_the_sender(world):
    w = world

    async def no_push(master, ip, port):
        pass                                     # the pull alone must make it delivered
    w.client._push_master_outbox = no_push

    async def scene():
        host, port = await asyncio.to_thread(w.master_up)
        sub = asyncio.create_task(w.client._relay_subscription_loop(
            MASTER.pub, lambda: _resolved(w, host, port), is_master=True))
        await _until(lambda: w.client._friend_peer_cache.get(w.support))      # the stream is up
        # The direct path fails now (a dead address), the mailbox takes it.
        w.client._friend_peer_cache[w.support] = [("127.0.0.1", 9)]
        w.where["master"] = None
        msg = await w.client._db(w.store.prepare_outgoing, w.support, "help, please")
        await w.client._push_to(w.support)
        kept = _state(w.sender_db, msg["message_uuid"])
        assert await w.drain() == 1
        await _until(lambda: _state(w.sender_db, msg["message_uuid"])[0])
        sub.cancel()
        return msg["message_uuid"], kept

    msg_uuid, kept = asyncio.run(scene())
    assert kept == (False, True)                              # kept, not delivered
    assert _state(w.sender_db, msg_uuid) == (True, True)      # delivered; when it was kept stays a fact
    assert _held(w.master_db, msg_uuid) == 1


async def _resolved(w, host, port):
    return (await w.client.master_contact(), (host, port))


def test_a_sender_away_at_the_drain_learns_at_its_connect_and_the_rest_is_pushed_then(world):
    w = world

    async def scene():
        first = await w.client._db(w.store.prepare_outgoing, w.support, "first")
        await w.client._push_to(w.support)                    # Support away: kept
        assert _state(w.sender_db, first["message_uuid"]) == (False, True)
        host, port = await asyncio.to_thread(w.master_up)
        assert await w.drain() == 1                           # nobody to ping
        assert _state(w.sender_db, first["message_uuid"]) == (False, True)
        # A second message kept the drain never saw (its row kept by hand —
        # the deposit's own path is the first test's).
        second = await w.client._db(w.store.prepare_outgoing, w.support, "second")
        with w.sender_db.cursor() as cur:
            cur.execute("UPDATE p2p_messages SET parked_at = now() WHERE message_uuid = %s",
                        (second["message_uuid"],))
        sub = asyncio.create_task(w.client._relay_subscription_loop(
            MASTER.pub, lambda: _resolved(w, host, port), is_master=True))
        await _until(lambda: all(_state(w.sender_db, m["message_uuid"])[0] for m in (first, second)))
        sub.cancel()
        return first["message_uuid"], second["message_uuid"]

    first, second = asyncio.run(scene())
    assert _held(w.master_db, first) == 1 and _held(w.master_db, second) == 1


def test_a_kept_message_waits_for_the_masters_stream_and_is_kept_once(world, monkeypatch):
    w = world
    pushed = []

    async def scene():
        msg = await w.client._db(w.store.prepare_outgoing, w.support, "once")
        await w.client._push_to(w.support)
        assert _state(w.sender_db, msg["message_uuid"]) == (False, True)
        real = w.client._push_pending_messages

        async def watched(fid, pubkey, messages, peers):
            pushed.append([m["message_uuid"] for m in messages])
            await real(fid, pubkey, messages, peers)
        w.client._push_pending_messages = watched
        await w.client._pass_once()
        await w.client._push_to(w.support)
        await w.client._mailbox_deposit(w.support, await w.client._pending_for(w.support, with_kept=True))
        return msg["message_uuid"]

    asyncio.run(scene())
    assert pushed == []                                       # nor the pass nor a push touched it
    assert asyncio.run(w.drain()) == 1                        # deposited once


def test_a_mailbox_that_refuses_leaves_the_message_waiting(world, monkeypatch):
    w = world
    stranger = _Node("stranger")                              # no certificate at the Worker
    store = ChatService(w.store.db_dsn, stranger.key.private_bytes_raw(), stranger.pub)
    with w.sender_db.cursor() as cur:
        cur.execute("TRUNCATE friends CASCADE")
    support = store.add_friend(public_key_hex=MASTER.pub, invite_code=MASTER.info["invite_code"],
                               username="Sautium", source="master")
    ident = cc.ClientIdentity(public_key_hex=stranger.pub, username="stranger",
                              invite_code=stranger.info["invite_code"], sign=stranger.key.sign,
                              cert_bundle=lambda: None)
    client = cc.ChatClient(store, identity=lambda: ident, announce_port=lambda: 1)
    client.running = True

    async def scene():
        msg = await client._db(store.prepare_outgoing, support, "refused")
        await client._push_to(support)
        return msg["message_uuid"]

    assert _state(w.sender_db, asyncio.run(scene())) == (False, False)      # waiting here


def test_a_pending_support_contact_gets_nothing_deposited(world):
    w = world
    with w.sender_db.cursor() as cur:
        cur.execute("UPDATE friends SET public_key_hex = %s WHERE id = %s",
                    (f"pending:{MASTER.info['invite_code']}", w.support))

    async def scene():
        w.store.store_message(w.support, "out", "before the handshake", delivered=False)
        await w.client._pass_once()
        await w.client._push_to(w.support)

    asyncio.run(scene())
    with w.sender_db.cursor() as cur:
        cur.execute("SELECT delivered, parked_at FROM p2p_messages")
        assert cur.fetchall() == [(False, None)]
    assert asyncio.run(w.drain()) == 0


def test_a_drop_from_a_key_the_master_never_met_is_said_and_acked_and_its_sender_keeps_it(
        world, caplog):
    """SUPPORT-FIRST §1.4: a master that does not know the sender (it forgot
    the node, or never met it) drops the drop — said at warning, acked, so the
    Worker holds it no more — and nothing marks it delivered; the sender's
    copy goes directly once the master knows its key."""
    w = world
    with w.master_db.cursor() as cur:
        cur.execute("DELETE FROM friends WHERE public_key_hex = %s", (ME.pub,))
    caplog.set_level(logging.INFO)

    async def scene():
        msg = await w.client._db(w.store.prepare_outgoing, w.support, "is anyone there?")
        await w.client._push_to(w.support)
        kept = _state(w.sender_db, msg["message_uuid"])
        drained = await w.drain(), await w.drain()
        after_drain = _state(w.sender_db, msg["message_uuid"])
        with w.master_db.cursor() as cur:          # the master meets this node again
            cur.execute("INSERT INTO friends (username, public_key_hex, invite_code, source) "
                        "VALUES ('me', %s, %s, 'manual')", (ME.pub, ME.info["invite_code"]))
        host, port = await asyncio.to_thread(w.master_up)
        await w.client._push_master_outbox(await w.client.master_contact(), host, port)
        return msg["message_uuid"], kept, drained, after_drain

    msg_uuid, kept, drained, after_drain = asyncio.run(scene())
    assert kept == (False, True)
    assert drained == (1, 0)                                   # handed over once, acked
    assert after_drain == (False, True)                        # the drain proved nothing to the sender
    said = [r for r in caplog.records if "a key this master does not know" in r.getMessage()]
    assert len(said) == 1 and said[0].levelno == logging.WARNING
    assert _state(w.sender_db, msg_uuid) == (True, True)       # delivered directly, once known
    assert _held(w.master_db, msg_uuid) == 1
