"""An invite's whole path through the REAL Worker code (tests/p2p/worker_harness.mjs
in serve mode, a throwaway authority) and the backend's own routes
(backend/routers/p2p.py), on a real PostgreSQL built from the migrations: an
inviter's invite mailed with its string and kept under its id; the string
pasted into Add on the acceptor's node, spent, and the contact bound to the
inviter's own key; the inviter's pickup adding the acceptor as a contact bound
to the acceptor's key, once; the same string again refused as used and kept as
an add by code; a plain code's add keeping nothing on the Worker and mailing
its owner the username only.

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
    monkeypatch.setattr(p2p, "email_status", verified)
    with psycopg2.connect(scratch_dsn) as c, c.cursor() as cur:
        cur.execute("TRUNCATE friends, sent_invites CASCADE")
    for n in (MASHA, SASHA, PASHA):
        assert _post(worker, "/birth-certificate", {
            "pubkey_hex": n.pub, "signature": n.sign(f"birth:{n.pub}".encode()).hex()})["pubkey"] == n.pub
    email = "masha@example.org"
    assert _post(worker, "/send-verification", {
        "to": email, "invite_code": MASHA.code, "public_key_hex": MASHA.pub,
        "signature": MASHA.sign(f"sendcode:{MASHA.code}:{email}".encode()).hex()})["status"] == "sent"
    code = re.search(r'monospace;">([A-Z0-9]{6})<', _mails(worker)[-1]["html"]).group(1)
    cert = json.loads(urllib.request.urlopen(f"{worker}/birth-certificate?pubkey={MASHA.pub}").read())
    registered = _post(worker, "/register-email", {
        "invite_code": MASHA.code, "email": email, "public_key_hex": MASHA.pub, "code": code,
        "signature": MASHA.sign(f"register:{MASHA.code}:{email}".encode()).hex(), "birth_cert": cert})
    if registered.get("status") != "registered":     # already, from an earlier test of the module
        assert registered.get("status") in (None, 403), registered

    def be(n):
        class Key:
            sign = staticmethod(n.sign)
        monkeypatch.setattr(p2p, "_get_identity", lambda: n.identity)
        monkeypatch.setattr(p2p, "_get_private_key", lambda: Key)
    yield SimpleNamespace(url=worker, dsn=scratch_dsn, be=be)
    db_pool.close_pool()


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
    stub = _row(nodes.dsn, "SELECT public_key_hex, encode(key_fingerprint, 'hex') FROM friends"
                           " WHERE invite_code = %s", MASHA.code)
    assert stub == (f"pending:{MASHA.code}", chat_wire.key_fingerprint(MASHA.pub).lower())

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


def test_an_add_by_plain_code_keeps_nothing_and_adds_nobody_on_the_other_side(nodes):
    nodes.be(SASHA)
    added = _run(p2p.add_friend(p2p.AddFriendRequest(invite_code=MASHA.code)))
    assert added["invite"] is None
    nodes.be(MASHA)
    assert _run(p2p.pending_accepts()) == {"added": []}
    assert _row(nodes.dsn, "SELECT count(*) FROM friends WHERE invite_code = %s", SASHA.code) == (0,)


def test_ones_own_invite_is_refused_before_any_request(nodes):
    from fastapi import HTTPException
    nodes.be(MASHA)
    with pytest.raises(HTTPException) as refused:
        _run(p2p.add_friend(p2p.AddFriendRequest(invite_code=chat_wire.long_code("masha", MASHA.pub))))
    assert refused.value.status_code == 400
