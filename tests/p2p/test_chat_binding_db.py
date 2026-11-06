"""A chat message is bound to its uuid and time inside its box, and a uuid
to one message of one thread (desktop/p2p/chat_service.py,
backend/p2p_chat.py), on a real PostgreSQL built from the migrations —
skipped without a cluster: an earlier ciphertext under a new uuid is
refused; the same message again is a duplicate, anything else under its
uuid is refused; a history import touches its own thread only; a history
page marks delivered only the messages it carries, and the next page starts
after the last one's row, even within one second; a page carries this node's
undelivered rows behind the cursor too, and its cursor never steps back; one
message arriving twice at once is stored once; a friend's retired key speaks
for nobody on the Docker surface; the token hour's ceiling is checked before
a use is spent."""

import base64
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import pytest

REPO = Path(__file__).resolve().parents[2]
for p in (REPO, REPO / "backend"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from desktop import node_identity  # noqa: E402
from desktop.p2p import chat_wire  # noqa: E402
from desktop.p2p.chat_service import ChatService  # noqa: E402

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")


def _who(name):
    key = ed25519.Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes_raw()
    return key, raw.hex(), node_identity.make_invite_code(name, raw)


ME, FRIEND, OTHER = _who("me"), _who("friend"), _who("other")


@pytest.fixture
def conn(scratch_dsn):
    c = psycopg2.connect(scratch_dsn)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("TRUNCATE friends CASCADE")
    yield c
    c.close()


def _service(scratch_dsn, who):
    return ChatService(scratch_dsn, who[0].private_bytes_raw(), who[1])


def test_a_message_is_bound_to_its_uuid_its_thread_and_its_text(conn, scratch_dsn):
    me, friend = _service(scratch_dsn, ME), _service(scratch_dsn, FRIEND)
    fid = me.add_friend(public_key_hex=FRIEND[1], invite_code=FRIEND[2], username="friend")
    oid = me.add_friend(public_key_hex=OTHER[1], invite_code=OTHER[2], username="other")
    u1, u2 = str(uuid.uuid4()), str(uuid.uuid4())
    sent = datetime.now(timezone.utc).isoformat()
    box1 = friend.encrypt_envelope("hello", u1, sent, ME[1])
    assert me.handle_incoming(FRIEND[1], box1, sent, message_uuid=u1)["content"] == "hello"
    assert me.handle_incoming(FRIEND[1], box1, sent, message_uuid=u2) is None     # replayed under a new uuid
    assert me.handle_incoming(FRIEND[1], box1, sent, message_uuid=u1)["duplicate"]
    other_text = friend.encrypt_envelope("not hello", u1, sent, ME[1])
    assert me.handle_incoming(FRIEND[1], other_text, sent, message_uuid=u1) is None
    other = _service(scratch_dsn, OTHER)
    assert me.handle_incoming(OTHER[1], other.encrypt_envelope("hello", u1, sent, ME[1]),
                              sent, message_uuid=u1) is None                     # another thread's uuid
    plain = base64.b64encode(friend._get_box(ME[1]).encrypt(b"no envelope")).decode()
    assert me.handle_incoming(FRIEND[1], plain, sent) is None
    with conn.cursor() as cur:
        cur.execute("SELECT friend_id, content FROM p2p_messages")
        assert cur.fetchall() == [(fid, "hello")]
    # A history export's word about another thread's uuid changes nothing there.
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO p2p_messages (friend_id, direction, content, timestamp,
                                                 delivered, message_uuid)
                       VALUES (%s, 'out', 'to other', now(), FALSE, %s)""", (oid, u2))
    stats = me.import_history(fid, [{"message_uuid": u2, "direction": "in",
                                     "content": "to other", "timestamp": sent}])
    assert stats["delivered"] == 0
    with conn.cursor() as cur:
        cur.execute("SELECT delivered FROM p2p_messages WHERE message_uuid = %s", (u2,))
        assert cur.fetchone() == (False,)


def test_a_history_page_marks_delivered_only_what_it_carries(conn, scratch_dsn, monkeypatch):
    from datetime import timedelta
    me = _service(scratch_dsn, ME)
    fid = me.add_friend(public_key_hex=FRIEND[1], invite_code=FRIEND[2], username="friend")
    t0 = datetime(2026, 11, 3, tzinfo=timezone.utc)
    stamps = [t0, t0 + timedelta(seconds=1), t0 + timedelta(seconds=1), t0 + timedelta(seconds=2)]
    with conn.cursor() as cur:
        for n, stamp in enumerate(stamps):
            cur.execute("""INSERT INTO p2p_messages (friend_id, direction, content, timestamp,
                                                     delivered, message_uuid)
                           VALUES (%s, 'out', %s, %s, FALSE, %s)""",
                        (fid, f"m{n}", stamp, str(uuid.uuid4())))
    monkeypatch.setattr(chat_wire, "HISTORY_EXPORT_MAX", 2)
    seal = lambda row: row["content"]
    seen = []
    after = None
    while True:
        rows = chat_wire.export_rows(conn, fid, chat_wire.history_cursor(after))
        page, out_ids, after, more = chat_wire.export_page(rows, seal)
        chat_wire.mark_exported(conn, fid, out_ids)
        seen += [m["encrypted"] for m in page]
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM p2p_messages WHERE delivered")
            assert cur.fetchone()[0] == len(seen)          # what the pages carried, and no more
        if not more:
            break
    assert seen == ["m0", "m1", "m2", "m3"]                 # two in one second, both read


def test_a_page_carries_this_nodes_undelivered_rows_behind_the_cursor(conn, scratch_dsn):
    from datetime import timedelta
    me = _service(scratch_dsn, ME)
    fid = me.add_friend(public_key_hex=FRIEND[1], invite_code=FRIEND[2], username="friend")
    t0 = datetime(2026, 11, 3, tzinfo=timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO p2p_messages (friend_id, direction, content, timestamp,
                                                 delivered, message_uuid)
                       VALUES (%s, 'out', 'older, never delivered', %s, FALSE, %s),
                              (%s, 'in', 'older, theirs', %s, TRUE, %s),
                              (%s, 'out', 'newer', %s, FALSE, %s)
                       RETURNING id""",
                    (fid, t0, str(uuid.uuid4()), fid, t0, str(uuid.uuid4()),
                     fid, t0 + timedelta(hours=1), str(uuid.uuid4())))
        ids = [r[0] for r in cur.fetchall()]
    ahead = (t0 + timedelta(minutes=30), 10 ** 9)      # a cursor another node of the account gave
    rows = chat_wire.export_rows(conn, fid, ahead)
    page, out_ids, after, more = chat_wire.export_page(rows, lambda row: row["content"], ahead)
    assert [m["encrypted"] for m in page] == ["older, never delivered", "newer"]
    assert out_ids == [ids[0], ids[2]] and not more
    assert chat_wire.history_cursor(after) == (t0 + timedelta(hours=1), ids[2])
    assert chat_wire.history_cursor(chat_wire.export_page([], lambda row: row["content"], ahead)[2]) == ahead


def test_a_page_reads_no_text_past_its_bytes(conn, scratch_dsn, monkeypatch):
    from datetime import timedelta
    me = _service(scratch_dsn, ME)
    fid = me.add_friend(public_key_hex=FRIEND[1], invite_code=FRIEND[2], username="friend")
    t0 = datetime(2026, 11, 3, tzinfo=timezone.utc)
    with conn.cursor() as cur:
        for n in range(4):
            cur.execute("""INSERT INTO p2p_messages (friend_id, direction, content, timestamp,
                                                     delivered, message_uuid)
                           VALUES (%s, 'in', %s, %s, TRUE, %s)""",
                        (fid, "x" * 20, t0 + timedelta(seconds=n), str(uuid.uuid4())))
    monkeypatch.setattr(chat_wire, "HISTORY_EXPORT_BYTES", 30)
    rows = chat_wire.export_rows(conn, fid, None)
    assert [r["content"] is not None for r in rows] == [True, True, False, False]
    page, _, after, more = chat_wire.export_page(rows, lambda row: row["content"])
    assert len(page) == 2 and more


def test_one_message_arriving_twice_at_once_is_stored_once(conn, scratch_dsn, docker_db):
    from p2p_chat import PeerChatService
    svc = PeerChatService(ME[0].private_bytes_raw(), ME[1], ())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO friends (public_key_hex, invite_code, username) VALUES (%s, %s, 'friend')",
                    (FRIEND[1], FRIEND[2]))
    friend = _service(scratch_dsn, FRIEND)
    u, sent = str(uuid.uuid4()), datetime.now(timezone.utc).isoformat()
    box = friend.encrypt_envelope("hello", u, sent, ME[1])
    assert svc.handle_incoming(FRIEND[1], box, sent, message_uuid=u)["content"] == "hello"
    real = svc._stored_message
    calls = []
    svc._stored_message = lambda m: (calls.append(m), None if len(calls) == 1 else real(m))[1]
    again = svc.handle_incoming(FRIEND[1], box, sent, message_uuid=u)   # its twin landed first
    assert again["duplicate"] and len(calls) == 2
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM p2p_messages WHERE message_uuid = %s", (u,))
        assert cur.fetchone()[0] == 1


@pytest.fixture
def docker_db(scratch_dsn, monkeypatch):
    from config import settings
    import db_pool
    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port,
                           postgres_user=url.username, postgres_password=url.password,
                           postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


def test_a_friends_retired_key_speaks_for_nobody_on_docker(conn, docker_db):
    from p2p_chat import PeerChatService
    svc = PeerChatService(ME[0].private_bytes_raw(), ME[1], ())
    old = _who("friend")
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO friends (public_key_hex, invite_code, username, previous_public_key_hex)
                       VALUES (%s, %s, 'friend', %s)""", (FRIEND[1], FRIEND[2], old[1]))
    assert svc.get_friend_by_public_key(FRIEND[1]) is not None
    assert svc.get_friend_by_public_key(old[1]) is None
    assert svc.friend_for_key(old[1]) is None


def test_the_token_hours_ceiling_spends_no_use(conn, docker_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from p2p_chat import PeerChatService
    from routers import peer_chat
    svc = PeerChatService(ME[0].private_bytes_raw(), ME[1], ())
    monkeypatch.setattr(peer_chat, "get_peer_chat", lambda: svc)
    monkeypatch.setattr(peer_chat, "resolve_identity", lambda s: {
        "public_key_hex": ME[1], "username": "me", "invite_code": ME[2]})
    monkeypatch.setattr(peer_chat, "TOKEN_FRIENDS_PER_HOUR", 0)
    tid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO invite_tokens (id, label, max_uses) VALUES (%s, 'test', 1)", (tid,))
    import time
    ts = int(time.time())
    body = {"public_key_hex": FRIEND[1], "username": "friend", "invite_code": FRIEND[2],
            "token_id": tid, "ts": ts,
            "signature": FRIEND[0].sign(f"token_handshake:{ts}:{tid}:{ME[2]}".encode()).hex()}
    app = FastAPI()
    app.include_router(peer_chat.chat_router)
    with TestClient(app) as http:
        assert http.post("/api/chat/handshake", json=body).status_code == 429
    with conn.cursor() as cur:
        cur.execute("SELECT use_count FROM invite_tokens WHERE id = %s", (tid,))
        assert cur.fetchone() == (0,)
