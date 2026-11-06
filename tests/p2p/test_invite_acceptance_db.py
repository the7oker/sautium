"""The inviter's side of an invite and the contacts it leaves
(desktop/p2p/chat_wire.take_acceptance, email_verify.pick_up_acceptances,
the chat services' bound contacts and signed messages), on a real
PostgreSQL built from the migrations — skipped without a cluster:

- an acceptance is added once, for an invite this node sent, under its
  acceptor's own signed statement, as a contact bound to the acceptor's key;
  a held key, another node's invite, an unbound code, a statement for another
  invite or node, and this node's own key add nothing; two pickups at once
  add it once;
- the pickup asks the Worker only while an invite is open, and deletes none;
- a contact bound to a fingerprint is resolved only by the key it names, on
  both surfaces; an unbound friendship goes back bound to the key it held;
- a received message keeps its sender's signature and the time it signed; an
  unsigned one is refused; a history import takes a line as ours only under
  our own signature, theirs under theirs when it carries one; an export
  gives each row back with its signature."""

import base64
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest

REPO = Path(__file__).resolve().parents[2]
for p in (REPO, REPO / "backend"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from desktop import node_identity  # noqa: E402
from desktop.p2p import chat_wire, email_verify  # noqa: E402
from desktop.p2p.chat_service import ChatService  # noqa: E402

nacl_signing = pytest.importorskip("nacl.signing")


class _Who:
    def __init__(self, name, seed):
        self.key = nacl_signing.SigningKey(seed)
        self.pub = self.key.verify_key.encode().hex()
        self.name, self.code = name, node_identity.make_invite_code(name, bytes.fromhex(self.pub))

    def sign(self, message: bytes) -> bytes:
        return self.key.sign(message).signature


ME, FRIEND, OTHER = _Who("me", b"\x41" * 32), _Who("friend", b"\x42" * 32), _Who("other", b"\x43" * 32)
INVITE = str(uuid.UUID(int=77))


@pytest.fixture
def conn(scratch_dsn):
    c = psycopg2.connect(scratch_dsn)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("TRUNCATE friends, sent_invites CASCADE")
        cur.execute("INSERT INTO sent_invites (to_email, invite_id) VALUES ('friend@example.net', %s)", (INVITE,))
    yield c
    c.close()


def _acceptance(who=FRIEND, inviteid=INVITE, for_code=ME.code, ts=1763000000, **over):
    statement = chat_wire.accept_text(ts, inviteid, for_code, who.code, who.pub)
    return {"invite_id": inviteid, "accepted_at": "2026-11-07T00:00:00.000Z",
            "acceptor": {"invite_code": who.code, "public_key_hex": who.pub, "ts": ts,
                         "signature": who.sign(statement.encode()).hex(), **over}}


def _take(conn, accept):
    return email_verify._in_transaction(conn, lambda: chat_wire.take_acceptance(conn, accept, ME.code, ME.pub))


def _row(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def test_an_acceptance_is_added_once_bound_to_its_acceptors_key(conn):
    assert _take(conn, _acceptance()) == "added"
    assert _row(conn, "SELECT public_key_hex, encode(key_fingerprint, 'hex') FROM friends WHERE invite_code = %s",
                FRIEND.code) == (f"pending:{FRIEND.code}", chat_wire.key_fingerprint(FRIEND.pub).lower())
    assert _row(conn, "SELECT taken_at IS NOT NULL FROM sent_invites") == (True,)
    assert _take(conn, _acceptance()) == "taken"
    assert _row(conn, "SELECT count(*) FROM friends") == (1,)


@pytest.mark.parametrize("accept, why", [
    (lambda: _acceptance(inviteid=str(uuid.UUID(int=78))), "not an invite of this node"),
    (lambda: _acceptance(for_code=OTHER.code), "statement does not verify"),
    (lambda: {**_acceptance(), "invite_id": str(uuid.UUID(int=78))}, "statement does not verify"),
    (lambda: _acceptance(invite_code=OTHER.code), "code does not bind the key"),
    (lambda: _acceptance(signature="00" * 64), "statement does not verify"),
    (lambda: _acceptance(who=ME), "own key"),
    (lambda: {"invite_id": INVITE}, "malformed"),
])
def test_an_acceptance_that_does_not_hold_adds_nothing(conn, accept, why):
    assert _take(conn, accept()) == why
    assert _row(conn, "SELECT count(*) FROM friends") == (0,)
    assert _row(conn, "SELECT taken_at FROM sent_invites") == (None,)


def test_an_acceptance_by_a_friend_already_held_takes_the_invite_and_adds_nobody(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO friends (username, public_key_hex, invite_code) VALUES ('friend', %s, %s)",
                    (FRIEND.pub, FRIEND.code))
    assert _take(conn, _acceptance()) == "held"
    assert _row(conn, "SELECT count(*) FROM friends") == (1,)
    assert _row(conn, "SELECT taken_at IS NOT NULL FROM sent_invites") == (True,)


def test_two_pickups_at_once_add_an_acceptance_once(scratch_dsn, conn):
    outcomes, start = [], threading.Barrier(2)

    def take():
        c = psycopg2.connect(scratch_dsn)
        try:
            start.wait()
            outcomes.append(_take(c, _acceptance()))
        finally:
            c.close()
    threads = [threading.Thread(target=take) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(outcomes) == ["added", "taken"]
    assert _row(conn, "SELECT count(*) FROM friends") == (1,)


def test_the_pickup_asks_only_while_an_invite_is_open(conn, monkeypatch):
    asked = []
    monkeypatch.setattr(email_verify, "_post_worker", lambda path, body: asked.append((path, body)) or {
        "accepts": [_acceptance()]})
    assert email_verify.pick_up_acceptances(conn, ME.code, ME.pub, ME.sign) == [FRIEND.code]
    [(path, body)] = asked
    assert path == "/pending-accepts"
    assert chat_wire.verify(ME.pub, chat_wire.pickup_text(body["ts"], ME.code, ME.pub).encode(), body["signature"])
    # taken: nothing open, no request
    assert email_verify.pick_up_acceptances(conn, ME.code, ME.pub, ME.sign) == []
    assert len(asked) == 1
    with conn.cursor() as cur:
        cur.execute("UPDATE sent_invites SET taken_at = NULL, sent_at = now() - interval '61 days'")
    assert email_verify.pick_up_acceptances(conn, ME.code, ME.pub, ME.sign) == [] and len(asked) == 1


# --- contacts bound to a fingerprint ---------------------------------------------

def _service(dsn, who):
    return ChatService(dsn, bytes(who.key), who.pub)


def test_a_bound_contact_is_resolved_only_by_the_key_it_names(scratch_dsn, conn, monkeypatch):
    me = _service(scratch_dsn, ME)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO friends (username, public_key_hex, invite_code, key_fingerprint) "
                    "VALUES ('friend', %s, %s, %s)",
                    (f"pending:{FRIEND.code}", FRIEND.code, bytes.fromhex(chat_wire.key_fingerprint(FRIEND.pub))))
    # a key sharing the short code's 48 bits (the check made to agree) is not the one bound
    monkeypatch.setattr(node_identity, "verify_invite_code", lambda code, pk: True)
    assert me.friend_for_key(OTHER.pub) is None
    assert me.add_friend(public_key_hex=OTHER.pub, invite_code=FRIEND.code) is None
    assert _row(conn, "SELECT count(*) FROM friends") == (1,)
    assert me.friend_for_key(FRIEND.pub)["public_key_hex"] == FRIEND.pub


def test_the_docker_service_resolves_a_bound_contact_only_by_its_key(scratch_dsn, conn, monkeypatch):
    import db_pool
    import p2p_identity
    from config import settings
    from urllib.parse import urlparse
    from p2p_chat import PeerChatService
    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port, postgres_user=url.username,
                           postgres_password=url.password, postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    db_pool.close_pool()
    try:
        svc = PeerChatService(bytes(ME.key), ME.pub)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO friends (username, public_key_hex, invite_code, key_fingerprint) "
                        "VALUES ('friend', %s, %s, %s)",
                        (f"pending:{FRIEND.code}", FRIEND.code,
                         bytes.fromhex(chat_wire.key_fingerprint(FRIEND.pub))))
        monkeypatch.setattr(p2p_identity, "verify_invite_code", lambda code, pk: True)
        assert svc.friend_for_key(OTHER.pub) is None
        assert svc.add_friend(OTHER.pub, FRIEND.code) is None
        assert svc.friend_for_key(FRIEND.pub)["public_key_hex"] == FRIEND.pub
    finally:
        db_pool.close_pool()


def test_an_unbound_friendship_waits_for_the_key_it_held(scratch_dsn, conn):
    me = _service(scratch_dsn, ME)
    fid = me.add_friend(public_key_hex=FRIEND.pub, invite_code=FRIEND.code)
    me.unbind_friend(fid)
    assert _row(conn, "SELECT public_key_hex, encode(key_fingerprint, 'hex') FROM friends") == (
        f"pending:{FRIEND.code}", chat_wire.key_fingerprint(FRIEND.pub).lower())


# --- signed messages ---------------------------------------------------------------

def test_a_message_keeps_its_senders_signature_and_an_unsigned_one_is_refused(scratch_dsn, conn):
    me, friend = _service(scratch_dsn, ME), _service(scratch_dsn, FRIEND)
    me.add_friend(public_key_hex=FRIEND.pub, invite_code=FRIEND.code)
    u = str(uuid.uuid4())
    sent = datetime.now(timezone.utc) - timedelta(seconds=5)
    assert me.handle_incoming(FRIEND.pub, friend.encrypt_envelope("hello", u, sent.isoformat(), ME.pub),
                              sent.isoformat(), message_uuid=u)["content"] == "hello"
    sig, sent_at = _row(conn, "SELECT sender_sig, sent_at FROM p2p_messages WHERE message_uuid = %s", u)
    assert sent_at == sent
    assert chat_wire.verify(FRIEND.pub, chat_wire.message_statement(FRIEND.pub, ME.pub, u, sent, "hello"),
                            bytes(sig).hex())
    # boxed by the friend, signed by nobody — or by someone else
    plain = lambda sig: friend._box(ME.pub, chat_wire.envelope("x", str(uuid.uuid4()), sent.isoformat(), sig))
    assert me.handle_incoming(FRIEND.pub, plain(None), sent.isoformat()) is None
    assert me.handle_incoming(FRIEND.pub, plain("ab" * 64), sent.isoformat()) is None
    assert _row(conn, "SELECT count(*) FROM p2p_messages") == (1,)


def test_a_history_import_takes_a_line_as_ours_only_under_our_signature(scratch_dsn, conn):
    me = _service(scratch_dsn, ME)
    fid = me.add_friend(public_key_hex=FRIEND.pub, invite_code=FRIEND.code)
    at = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)

    def line(direction, signer, text, sig=True):
        u = str(uuid.uuid4())
        recipient = FRIEND.pub if signer is ME else ME.pub
        s = signer.sign(chat_wire.message_statement(signer.pub, recipient, u, at, text)).hex() if sig else None
        return {"message_uuid": u, "direction": direction, "content": text, "timestamp": at.isoformat(), "sig": s}
    lines = [
        line("in", ME, "mine, signed"),                  # the friend got it from us
        line("in", FRIEND, "mine, but the friend signed"),
        line("in", ME, "mine, unsigned", sig=False),
        line("out", FRIEND, "theirs, signed"),
        line("out", FRIEND, "theirs, from before", sig=False),
        line("out", OTHER, "theirs, signed by another"),
    ]
    stats = me.import_history(fid, lines)
    assert stats["imported"] == 3 and stats["skipped"] == 3
    with conn.cursor() as cur:
        cur.execute("SELECT direction::text, content, sender_sig IS NOT NULL FROM p2p_messages ORDER BY content")
        assert cur.fetchall() == [("out", "mine, signed", False), ("in", "theirs, from before", False),
                                  ("in", "theirs, signed", True)]


def test_an_export_gives_each_row_back_with_its_signature(scratch_dsn, conn):
    me, friend = _service(scratch_dsn, ME), _service(scratch_dsn, FRIEND)
    fid = me.add_friend(public_key_hex=FRIEND.pub, invite_code=FRIEND.code)
    u_in, u_out = str(uuid.uuid4()), str(uuid.uuid4())
    sent = datetime.now(timezone.utc) - timedelta(minutes=1)
    me.handle_incoming(FRIEND.pub, friend.encrypt_envelope("theirs", u_in, sent.isoformat(), ME.pub),
                       sent.isoformat(), message_uuid=u_in)
    me.store_message(fid, "out", "ours", sent + timedelta(seconds=1), delivered=True, message_uuid=u_out)
    rows = chat_wire.export_rows(conn, fid, None)
    page, _, _, _ = chat_wire.export_page(rows, lambda row: me.seal_exported(row, FRIEND.pub))
    opened = {m["message_uuid"]: friend.decrypt_envelope(m["encrypted"], ME.pub) for m in page}
    theirs, ours = opened[u_in], opened[u_out]
    assert chat_wire.signed_by(theirs["sig"], (FRIEND.pub,), (ME.pub,), u_in,
                               chat_wire.sent_time(theirs["ts"]), "theirs")
    assert chat_wire.signed_by(ours["sig"], (ME.pub,), (FRIEND.pub,), u_out,
                               chat_wire.sent_time(ours["ts"]), "ours")
