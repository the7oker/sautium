"""An invite's whole path through the REAL Worker code (tests/p2p/worker_harness.mjs
in serve mode, a throwaway authority) and the backend's own routes
(backend/routers/p2p.py), on a real PostgreSQL built from the migrations: an
inviter's invite mailed with its string and kept under its id; the string
pasted into Add on the acceptor's node, spent, and the contact bound to the
inviter's own key and armed with its one nudge; the inviter's pickup adding
the acceptor as a contact bound to the acceptor's key, once; the same string
again refused as used and kept as an add by code; a plain code's add keeping
nothing on the Worker and mailing its owner the username only; an invite the
Worker refuses this node for, or does not answer, telling nobody; the
master's code waiting for the master's own key in any form.

The two nodes share this module's database: each side's rows are under the
other side's code. Skipped without a cluster or without node."""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
nacl_signing = pytest.importorskip("nacl.signing")
pytest.importorskip("fastapi")

import db_pool  # noqa: E402
from config import settings  # noqa: E402
from desktop import node_identity  # noqa: E402
from desktop.p2p import chat_wire, email_verify  # noqa: E402
from routers import p2p  # noqa: E402

HARNESS = Path(__file__).resolve().parent / "p2p" / "worker_harness.mjs"


class _Node:
    def __init__(self, name, seed):
        self.name, self.key = name, nacl_signing.SigningKey(seed)
        self.pub = self.key.verify_key.encode().hex()
        self.code = node_identity.make_invite_code(name, bytes.fromhex(self.pub))
        self.identity = {"node_id": self.pub, "public_key_hex": self.pub, "username": name,
                         "invite_code": self.code, "email": f"{name}@example.org"}

    def sign(self, message: bytes) -> bytes:
        return self.key.sign(message).signature


MASHA, SASHA, PASHA = _Node("masha", b"\x31" * 32), _Node("sasha", b"\x32" * 32), _Node("pasha", b"\x33" * 32)
_VERIFIED: set = set()


@pytest.fixture(scope="module")
def worker():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    proc = subprocess.Popen(["node", str(HARNESS), os.urandom(32).hex(), "test-email-pepper", "--serve"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    info = json.loads(proc.stdout.readline())
    yield info["url"]
    proc.terminate()
    proc.wait(10)


def _post(url, path, body):
    req = urllib.request.Request(url + path, method="POST", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=10).read())
    except urllib.error.HTTPError as e:
        return {"status": e.code, **json.loads(e.read())}


def _mails(url):
    return json.loads(urllib.request.urlopen(url + "/__harness/mails", timeout=10).read())


@pytest.fixture
def nodes(worker, scratch_dsn, monkeypatch):
    """Every node born on the Worker, masha's mailbox verified there; the
    backend's routes and pickup speaking to it, on this module's database.
    `be(node)` makes the routes speak as that node."""
    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port, postgres_user=url.username,
                           postgres_password=url.password, postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    db_pool.close_pool()
    monkeypatch.setattr(p2p, "VERIFY_WORKER_URL", worker)
    monkeypatch.setattr(email_verify, "VERIFY_WORKER_URL", worker)

    async def verified():
        return {"email": "masha@example.org", "verified": True}

    async def born():                 # every node is born on this Worker below
        return True
    monkeypatch.setattr(p2p, "email_status", verified)
    monkeypatch.setattr(p2p, "_born", born)
    monkeypatch.setattr(p2p, "_screen_pickups", deque())
    monkeypatch.setattr(p2p, "SCREEN_PICKUP_INTERVAL_S", 0)
    with psycopg2.connect(scratch_dsn) as c, c.cursor() as cur:
        cur.execute("TRUNCATE friends, sent_invites CASCADE")
    for n in (MASHA, SASHA, PASHA):
        assert _post(worker, "/birth-certificate", {
            "pubkey_hex": n.pub, "signature": n.sign(f"birth:{n.pub}".encode()).hex()})["pubkey"] == n.pub
    if worker not in _VERIFIED:       # once a module: the Worker's day of codes, all from one address
        email = "masha@example.org"
        assert _post(worker, "/send-verification", {
            "to": email, "invite_code": MASHA.code, "public_key_hex": MASHA.pub,
            "signature": MASHA.sign(f"sendcode:{MASHA.code}:{email}".encode()).hex()})["status"] == "sent"
        code = re.search(r'monospace;">([A-Z0-9]{6})<', _mails(worker)[-1]["html"]).group(1)
        cert = json.loads(urllib.request.urlopen(f"{worker}/birth-certificate?pubkey={MASHA.pub}").read())
        assert _post(worker, "/register-email", {
            "invite_code": MASHA.code, "email": email, "public_key_hex": MASHA.pub, "code": code,
            "signature": MASHA.sign(f"register:{MASHA.code}:{email}".encode()).hex(),
            "birth_cert": cert})["status"] == "registered"
        _VERIFIED.add(worker)

    def be(n):
        class Key:
            sign = staticmethod(n.sign)
        monkeypatch.setattr(p2p, "_get_identity", lambda: n.identity)
        monkeypatch.setattr(p2p, "_get_private_key", lambda: Key)
    yield SimpleNamespace(url=worker, dsn=scratch_dsn, be=be)
    db_pool.close_pool()


from collections import deque  # noqa: E402
from types import SimpleNamespace  # noqa: E402


def _run(coro):
    """A route's coroutine, and the tasks it left (a fire-and-forget call to
    the Worker) awaited before the loop closes."""
    async def main():
        result = await coro
        others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.gather(*others)
        return result
    return asyncio.run(main())


def _row(dsn, sql, *params):
    with psycopg2.connect(dsn) as c, c.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def test_an_invite_mailed_spent_picked_up_and_bound_on_both_sides(nodes):
    nodes.be(MASHA)
    assert _run(p2p.invite_by_email(p2p.InviteByEmailRequest(to_email="sasha@example.net",
                                                             message="come and listen")))["status"] == "sent"
    [invite_id] = _row(nodes.dsn, "SELECT invite_id::text FROM sent_invites")
    [mail] = [m for m in _mails(nodes.url) if m["to"] == ["sasha@example.net"]]
    string = re.search(r"([A-Za-z0-9_-]+#[0-9A-F-]{39}#i-[0-9a-f]{32})", mail["html"]).group(1)
    assert string.startswith(chat_wire.long_code("masha", MASHA.pub) + "#i-")
    assert chat_wire.invite_id(chat_wire.parse_share(string).invite_token) == invite_id

    nodes.be(SASHA)
    added = _run(p2p.add_friend(p2p.AddFriendRequest(invite_code=string)))
    assert added["invite"] == "accepted" and added["invite_code"] == MASHA.code
    stub = _row(nodes.dsn, "SELECT public_key_hex, encode(key_fingerprint, 'hex'), accepted_invite_id::text"
                           " FROM friends WHERE invite_code = %s", MASHA.code)
    assert stub == (f"pending:{MASHA.code}", chat_wire.key_fingerprint(MASHA.pub).lower(), invite_id)

    nodes.be(MASHA)
    assert _run(p2p.pending_accepts()) == {"added": [{"invite_code": SASHA.code}]}
    stub = _row(nodes.dsn, "SELECT public_key_hex, encode(key_fingerprint, 'hex') FROM friends"
                           " WHERE invite_code = %s", SASHA.code)
    assert stub == (f"pending:{SASHA.code}", chat_wire.key_fingerprint(SASHA.pub).lower())
    assert _row(nodes.dsn, "SELECT taken_at IS NOT NULL FROM sent_invites") == (True,)
    assert _run(p2p.pending_accepts()) == {"added": []}             # taken once

    # The same string again, by another: used — and kept as an add by code,
    # which tells the inviter by its username alone.
    nodes.be(PASHA)
    sent = len(_mails(nodes.url))
    again = _run(p2p.add_friend(p2p.AddFriendRequest(invite_code=string)))
    assert again["invite"] == "used"
    told = [m for m in _mails(nodes.url)[sent:] if m["to"] == ["masha@example.org"]]
    assert [m["subject"] for m in told] == ["pasha added your Sautium invite code"]
    assert "pasha@example.org" not in told[0]["html"]


def test_an_add_by_plain_code_keeps_nothing_and_adds_nobody_on_the_other_side(nodes, monkeypatch):
    nodes.be(MASHA)                    # an invite of masha's open, so her pickup asks the Worker
    assert _run(p2p.invite_by_email(p2p.InviteByEmailRequest(to_email="kasia@example.net")))["status"] == "sent"
    nodes.be(SASHA)
    sent = len(_mails(nodes.url))
    added = _run(p2p.add_friend(p2p.AddFriendRequest(invite_code=MASHA.code)))
    assert added["invite"] is None
    told = [m for m in _mails(nodes.url)[sent:] if m["to"] == ["masha@example.org"]]
    assert [m["subject"] for m in told] == ["sasha added your Sautium invite code"]
    assert "sasha@example.org" not in told[0]["html"]
    asked, real = [], email_verify._post_worker
    monkeypatch.setattr(email_verify, "_post_worker", lambda path, body: asked.append(path) or real(path, body))
    nodes.be(MASHA)
    assert _run(p2p.pending_accepts()) == {"added": []}
    assert asked == ["/pending-accepts"]
    assert _row(nodes.dsn, "SELECT count(*) FROM friends WHERE invite_code = %s", SASHA.code) == (0,)


def _mailed_invite(nodes, to):
    """masha's invite to `to`, as the string its mail prints."""
    nodes.be(MASHA)
    assert _run(p2p.invite_by_email(p2p.InviteByEmailRequest(to_email=to)))["status"] == "sent"
    [mail] = [m for m in _mails(nodes.url) if m["to"] == [to]]
    return re.search(r"([A-Za-z0-9_-]+#[0-9A-F-]{39}#i-[0-9a-f]{32})", mail["html"]).group(1)


def test_an_invite_the_worker_refuses_this_node_for_or_never_answers_tells_nobody(nodes, monkeypatch):
    told = []

    async def tell(code):
        told.append(code)
    monkeypatch.setattr(p2p, "_tell_code_added", tell)
    string = _mailed_invite(nodes, "lena@example.net")
    nodes.be(SASHA)

    class Late(datetime):             # this node's clock an hour behind
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) - timedelta(hours=1)
    monkeypatch.setattr(p2p, "datetime", Late)
    assert _run(p2p.add_friend(p2p.AddFriendRequest(invite_code=string)))["invite"] == "clock"
    monkeypatch.setattr(p2p, "datetime", datetime)
    monkeypatch.setattr(p2p, "VERIFY_WORKER_URL", "http://127.0.0.1:9")     # nobody answers there
    assert _run(p2p.add_friend(p2p.AddFriendRequest(invite_code=string)))["invite"] == "unreachable"
    assert told == []
    # the same string, the Worker reachable and the clock right: accepted
    monkeypatch.setattr(p2p, "VERIFY_WORKER_URL", nodes.url)
    assert _run(p2p.add_friend(p2p.AddFriendRequest(invite_code=string)))["invite"] == "accepted"
    assert told == []


def test_the_masters_code_waits_for_the_masters_own_key_in_any_form(nodes):
    from master_node import MASTER_INVITE_CODE, MASTER_PUBKEY_HEX
    if not (MASTER_INVITE_CODE and MASTER_PUBKEY_HEX):
        pytest.skip("no master pinned")
    nodes.be(SASHA)
    name = MASTER_INVITE_CODE.split("#")[0]
    for form in (MASTER_INVITE_CODE, chat_wire.long_code(name, MASTER_PUBKEY_HEX), _another(name, MASTER_PUBKEY_HEX)):
        _run(p2p.add_friend(p2p.AddFriendRequest(invite_code=form)))
        assert _row(nodes.dsn, "SELECT encode(key_fingerprint, 'hex') FROM friends WHERE invite_code = %s",
                    MASTER_INVITE_CODE) == (chat_wire.key_fingerprint(MASTER_PUBKEY_HEX).lower(),), form


def _another(name, pubkey):
    """A long code with this key's 48 bits and another key's 128."""
    real = chat_wire.long_code(name, pubkey)
    return real[:-4] + ("0000" if not real.endswith("0000") else "1111")


def _told(monkeypatch) -> list:
    told = []

    async def tell(code):
        told.append(code)
    monkeypatch.setattr(p2p, "_tell_code_added", tell)
    return told


def test_an_invite_the_worker_does_not_know_is_an_add_by_code_and_a_node_not_born_asks_nothing(nodes, monkeypatch):
    told = _told(monkeypatch)
    nodes.be(SASHA)
    unknown = f"{chat_wire.long_code('masha', MASHA.pub)}#i-{'5a' * 16}"
    state = SimpleNamespace(born=False)

    async def born():
        return state.born
    monkeypatch.setattr(p2p, "_born", born)
    assert _run(p2p.add_friend(p2p.AddFriendRequest(invite_code=unknown)))["invite"] == "unreachable"
    assert told == []                                       # a key not born asks nothing, tells nobody
    state.born = True
    assert _run(p2p.add_friend(p2p.AddFriendRequest(invite_code=unknown)))["invite"] == "not_found"
    assert told == [MASHA.code]


def _count_pickups(monkeypatch) -> list:
    asked, real = [], email_verify._post_worker
    monkeypatch.setattr(email_verify, "_post_worker", lambda path, body: asked.append(path) or real(path, body))
    return asked


def test_the_friends_screen_asks_only_with_an_invite_open_a_born_key_and_its_share_left(nodes, monkeypatch):
    asked = _count_pickups(monkeypatch)
    monkeypatch.setattr(p2p, "SCREEN_PICKUP_INTERVAL_S", 60)
    births = SimpleNamespace(asked=0, born=True)

    async def born():
        births.asked += 1
        return births.born
    monkeypatch.setattr(p2p, "_born", born)
    nodes.be(MASHA)
    assert _run(p2p.pending_accepts()) == {"added": []} and asked == []
    assert births.asked == 0 and len(p2p._screen_pickups) == 0      # no invite open: no birth asked, no share spent
    with psycopg2.connect(nodes.dsn) as c, c.cursor() as cur:
        cur.execute("INSERT INTO sent_invites (to_email, invite_id) VALUES ('nina@example.net', %s)",
                    (str(uuid.UUID(int=91)),))
    births.born = False
    assert _run(p2p.pending_accepts()) == {"added": []} and asked == []      # a key not born asks nothing
    births.born = True
    monkeypatch.setattr(p2p, "_screen_pickups", deque())
    assert _run(p2p.pending_accepts()) == {"added": []} and asked == ["/pending-accepts"]
    before = births.asked
    assert _run(p2p.pending_accepts()) == {"added": []} and asked == ["/pending-accepts"]   # within its minute
    assert births.asked == before                                       # past its share, no birth asked either


def test_an_invite_is_held_to_the_key_its_contact_already_waits_for(nodes):
    string = _mailed_invite(nodes, "oleh@example.net")
    nodes.be(SASHA)
    # a long code under masha's short code that names another key: the contact waits for that one
    other = _another("masha", MASHA.pub)
    _run(p2p.add_friend(p2p.AddFriendRequest(invite_code=other)))
    bound = _row(nodes.dsn, "SELECT key_fingerprint FROM friends WHERE invite_code = %s", MASHA.code)
    short = MASHA.code + "#i-" + string.rsplit("#i-", 1)[1]             # the invite, its code in the short form
    assert _run(p2p.add_friend(p2p.AddFriendRequest(invite_code=short)))["invite"] == "unreachable"
    assert _row(nodes.dsn, "SELECT key_fingerprint FROM friends WHERE invite_code = %s", MASHA.code) == bound


def test_a_new_fingerprint_drops_the_nudge_of_the_one_before(nodes):
    nodes.be(SASHA)
    _run(p2p.add_friend(p2p.AddFriendRequest(invite_code=chat_wire.long_code("masha", MASHA.pub))))
    with psycopg2.connect(nodes.dsn) as c, c.cursor() as cur:
        cur.execute("UPDATE friends SET accepted_invite_id = %s WHERE invite_code = %s",
                    (str(uuid.UUID(int=92)), MASHA.code))
    _run(p2p.add_friend(p2p.AddFriendRequest(invite_code=MASHA.code)))             # no fingerprint: kept
    assert _row(nodes.dsn, "SELECT accepted_invite_id::text FROM friends WHERE invite_code = %s",
                MASHA.code) == (str(uuid.UUID(int=92)),)
    _run(p2p.add_friend(p2p.AddFriendRequest(invite_code=_another("masha", MASHA.pub))))   # another key's: dropped
    assert _row(nodes.dsn, "SELECT accepted_invite_id FROM friends WHERE invite_code = %s", MASHA.code) == (None,)


def test_ones_own_invite_is_refused_before_any_request(nodes):
    from fastapi import HTTPException
    nodes.be(MASHA)
    with pytest.raises(HTTPException) as refused:
        _run(p2p.add_friend(p2p.AddFriendRequest(invite_code=chat_wire.long_code("masha", MASHA.pub))))
    assert refused.value.status_code == 400
