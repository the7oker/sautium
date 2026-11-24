"""Support from a node's first minute (SUPPORT-FIRST, BACKLOG B-116), on the
chat client both runtimes run and a real PostgreSQL built from the
migrations — the network faked where it is the other end: a node born while
the master is away writes to Support at once and its message waits on it,
never in the master's mailbox before the master's first handshake; the
handshake is the event that sends it; a node whose current key the master has
not accepted yet deposits nothing; the delivery path's state is said once per
change (L3); the master's stream is dialled again sooner while a message waits
kept for Support (L4)."""

import asyncio
import logging
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import psycopg2
import pytest

REPO = Path(__file__).resolve().parents[2]
for p in (REPO, REPO / "backend"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from desktop import node_identity  # noqa: E402
from desktop.p2p import chat_client as cc, mailbox_client, master_node  # noqa: E402
from desktop.p2p.chat_service import ChatService  # noqa: E402

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")


def _who(name):
    key = ed25519.Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes_raw()
    return SimpleNamespace(key=key, pub=raw.hex(), name=name,
                           invite=node_identity.make_invite_code(name, raw))


ME, OLD_ME, MASTER = _who("me"), _who("me"), _who("Sautium")


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
    import master_node as backend_master
    invite = node_identity.make_invite_code("Sautium", bytes.fromhex(MASTER.pub))
    for mod in (master_node, backend_master):
        monkeypatch.setattr(mod, "MASTER_PUBKEY_HEX", MASTER.pub)
        monkeypatch.setattr(mod, "MASTER_INVITE_CODE", invite)
    master_node.decide(ME.pub)
    return invite


@pytest.fixture
def drops(monkeypatch):
    """The Worker mailbox as the client meets it: every deposit recorded and
    answered `answer` (kept, unless a test changes it)."""
    made = []
    state = SimpleNamespace(made=made, answer=mailbox_client.Deposit(True, {"stored": True}, None))

    async def deposit(session, **kw):
        made.append(kw)
        return state.answer
    monkeypatch.setattr(mailbox_client, "deposit", deposit)
    return state


def _client(dsn, who=ME):
    store = ChatService(dsn, who.key.private_bytes_raw(), who.pub)
    ident = cc.ClientIdentity(public_key_hex=who.pub, username=who.name, invite_code=who.invite,
                              sign=who.key.sign, cert_bundle=lambda: None)
    client = cc.ChatClient(store, identity=lambda: ident, announce_port=lambda: 8801)
    client.running = True
    return client


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def _write(conn, friend_id: int, content: str = "hello Support") -> str:
    """What POST /friends/{id}/send stores (routers/p2p.send_message)."""
    message_uuid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO p2p_messages (friend_id, direction, content, timestamp,
                                                 delivered, message_uuid)
                       VALUES (%s, 'out', %s, now(), FALSE, %s)""", (friend_id, content, message_uuid))
    return message_uuid


def _support(conn):
    return _rows(conn, "SELECT id, public_key_hex FROM friends WHERE source = 'master'")[0]


async def _nowhere(friend, refresh=False, untargeted=True):
    return []


def test_a_node_born_with_the_master_away_writes_to_support_and_nothing_is_deposited(
        conn, scratch_dsn, master, drops):
    client = _client(scratch_dsn)
    client.find_friend_peers = _nowhere
    asyncio.run(client._ensure_master_contact())
    fid, key = _support(conn)
    assert key == f"pending:{master}"
    _write(conn, fid)

    async def scene():
        await client._pass_once()
        await client._push_to(fid)
    asyncio.run(scene())
    assert drops.made == []
    assert _rows(conn, "SELECT delivered, parked_at FROM p2p_messages") == [(False, None)]


def test_the_masters_first_handshake_sends_what_waits_at_once(conn, scratch_dsn, master, drops):
    client = _client(scratch_dsn)
    asyncio.run(client._ensure_master_contact())
    fid, _ = _support(conn)
    sent = _write(conn, fid)
    pushed = []

    async def push(friend_id, pubkey, messages, peers):
        pushed.append((friend_id, pubkey, [m["message_uuid"] for m in messages], peers))

    async def no_history(friend, peers):
        return {}
    client._push_pending_messages = push
    client._sync_chat_history = no_history

    async def scene():
        await client._handshake_accepted(MASTER.pub, master, {"username": "Sautium"},
                                         client._identity(), "198.51.100.9", 8801)
        return client._master_accepted.is_set()
    assert asyncio.run(scene()) is True
    assert _support(conn) == (fid, MASTER.pub)                 # the same row: the message is its own
    assert pushed == [(fid, MASTER.pub, [sent], [("198.51.100.9", 8801)])]
    assert drops.made == []


def _resolved_master(conn, bound) -> int:
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO friends (username, public_key_hex, invite_code, source, bound_identity)
                       VALUES ('Sautium', %s, %s, 'master', %s) RETURNING id""",
                    (MASTER.pub, master_node.MASTER_INVITE_CODE, bound))
        return cur.fetchone()[0]


def test_a_master_that_knows_an_older_key_of_this_node_gets_no_drop(conn, scratch_dsn, master, drops,
                                                                    caplog):
    fid = _resolved_master(conn, OLD_ME.pub)
    _write(conn, fid)
    client = _client(scratch_dsn)
    caplog.set_level(logging.INFO, logger=cc.__name__)

    async def scene():
        await client._relay_forward(fid, MASTER.pub, await client._pending_for(fid))
    asyncio.run(scene())
    assert drops.made == []
    assert (f"Delivery to friend {fid}: no direct route; waiting for Support to accept this "
            "node's current key") in caplog.text


@pytest.mark.parametrize("bound", ["mine", None])
def test_a_master_that_accepted_this_key_takes_the_drop(conn, scratch_dsn, master, drops, bound):
    fid = _resolved_master(conn, ME.pub if bound == "mine" else None)    # None: a row from before 014
    _write(conn, fid)
    client = _client(scratch_dsn)

    async def scene():
        await client._mailbox_deposit(fid, await client._pending_for(fid))
    asyncio.run(scene())
    assert len(drops.made) == 1 and drops.made[0]["sender_pubkey"] == ME.pub
    assert _rows(conn, "SELECT parked_at IS NOT NULL FROM p2p_messages") == [(True,)]


def test_the_delivery_paths_state_is_said_once_per_change(conn, scratch_dsn, master, drops, caplog):
    fid = _resolved_master(conn, ME.pub)
    _write(conn, fid, "one")
    _write(conn, fid, "two")
    client = _client(scratch_dsn)
    caplog.set_level(logging.INFO, logger=cc.__name__)
    drops.answer = mailbox_client.Deposit(False, None, None)         # the Worker did not answer

    async def scene():
        for _ in range(3):
            await client._push_pending_messages(fid, MASTER.pub, await client._pending_for(fid), [])
    asyncio.run(scene())
    said = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert said == [f"Delivery to friend {fid}: no direct route; the master's mailbox is unreachable"]
    drops.answer = mailbox_client.Deposit(False, None, "mailbox busy")
    caplog.clear()
    asyncio.run(scene())
    said = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert said == [f"Delivery to friend {fid}: no direct route; the master's mailbox refused it: "
                    "mailbox busy"]


def test_the_masters_stream_is_dialled_again_sooner_while_a_message_waits_kept(conn, scratch_dsn,
                                                                                master):
    fid = _resolved_master(conn, ME.pub)
    client = _client(scratch_dsn)
    assert asyncio.run(client._reconnect_cap(is_master=True)) == cc.RECONNECT_CAP_S
    _write(conn, fid)
    with conn.cursor() as cur:
        cur.execute("UPDATE p2p_messages SET parked_at = now()")
    assert asyncio.run(client._reconnect_cap(is_master=True)) == cc.KEPT_RECONNECT_CAP_S
    assert asyncio.run(client._reconnect_cap(is_master=False)) == cc.RECONNECT_CAP_S


def test_a_drop_carries_the_masters_voucher_for_this_nodes_key(conn, scratch_dsn, master, drops):
    from desktop.p2p import chat_wire
    fid = _resolved_master(conn, ME.pub)
    _write(conn, fid)
    client = _client(scratch_dsn)
    voucher = chat_wire.make_mail_voucher(MASTER.key.sign, ME.pub)
    old_key_voucher = chat_wire.make_mail_voucher(MASTER.key.sign, OLD_ME.pub)

    async def scene():
        await client._keep_mail_voucher(old_key_voucher, OLD_ME.pub)
        await client._mailbox_deposit(fid, await client._pending_for(fid))
        await client._keep_mail_voucher(voucher, ME.pub)
        with conn.cursor() as cur:
            cur.execute("UPDATE p2p_messages SET parked_at = NULL")
        await client._mailbox_deposit(fid, await client._pending_for(fid))
    asyncio.run(scene())
    assert [d["voucher"] for d in drops.made] == [None, voucher]
