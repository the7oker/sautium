"""The invite wire on the nodes (desktop/p2p/chat_wire.py), against the
vectors the Worker's harness asserts too (tests/p2p/vectors/invite_wire_v1.json):
the codes, the invite's id, the four texts and their signatures; the parse of
what is pasted into Add; a contact bound to a long code's fingerprint; a
message's signature over who wrote it to whom, which, when and what; and a
classic handshake that a contact bound to a fingerprint answers for its own
key alone, on both surfaces."""

import asyncio
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parent.parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from desktop import node_identity  # noqa: E402
from desktop.p2p import chat_wire  # noqa: E402

nacl_signing = pytest.importorskip("nacl.signing")
V = json.loads((Path(__file__).parent / "vectors" / "invite_wire_v1.json").read_text())


def _key(seed_hex):
    return nacl_signing.SigningKey(bytes.fromhex(seed_hex))


def _pub(key):
    return key.verify_key.encode().hex()


INVITER, ACCEPTOR = _key(V["inviter_seed"]), _key(V["acceptor_seed"])


# --- the vectors ---------------------------------------------------------------

def test_the_codes_and_the_invite_id_are_the_vectors():
    for who, key in (("inviter", INVITER), ("acceptor", ACCEPTOR)):
        v = V[who]
        assert _pub(key) == v["pubkey"]
        assert node_identity.make_invite_code(v["name"], bytes.fromhex(v["pubkey"])) == v["short_code"]
        assert chat_wire.long_code(v["name"], v["pubkey"]) == v["long_code"]
        assert v["long_code"].startswith(v["short_code"] + "-")     # a person recognises it
    assert chat_wire.invite_id(V["token"]) == V["invite_id"]
    assert V["invite_string"] == f"{V['inviter']['long_code']}#i-{V['token']}"


def test_the_four_texts_and_their_signatures_are_the_vectors():
    inviter, acceptor = V["inviter"], V["acceptor"]
    texts = {
        "send": chat_wire.send_text(V["ts"], inviter["short_code"], V["to"], V["message"]),
        "accept": chat_wire.accept_text(V["ts"], V["invite_id"], inviter["short_code"],
                                        acceptor["short_code"], acceptor["pubkey"]),
        "add": chat_wire.add_text(V["ts"], inviter["short_code"], acceptor["short_code"], acceptor["pubkey"]),
        "pickup": chat_wire.pickup_text(V["ts"], inviter["short_code"], inviter["pubkey"]),
    }
    assert texts == V["texts"]
    assert V["texts"]["send"].endswith(V["message_sha256"])
    for name, text in texts.items():
        signer = INVITER if V["signed_by"][name] == "inviter" else ACCEPTOR
        assert signer.sign(text.encode()).signature.hex() == V["signatures"][name], name
        assert chat_wire.verify(_pub(signer), text.encode(), V["signatures"][name])
    # no field holds ':', so a text splits one way
    assert all(len(t.split(":")) == {"send": 6, "accept": 7, "add": 6, "pickup": 5}[n] for n, t in texts.items())


def test_an_empty_message_signs_the_hash_of_nothing():
    assert chat_wire.send_text(1, "a#B", "c@d.e", "").endswith(
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")


# --- the parse -------------------------------------------------------------------

def test_the_parse_takes_a_wrapped_invite_and_every_spelling_of_its_token():
    inviter = V["inviter"]
    share = chat_wire.parse_share(V["parse"]["wrapped"])
    assert share == chat_wire.parse_share(V["invite_string"])
    assert share.code == inviter["short_code"]                       # what every consumer keys by
    assert share.fingerprint == inviter["long_code"].split("#")[1].replace("-", "")
    assert share.invite_token == V["token"] and share.link_token is None
    for prefix in V["parse"]["token_prefixes"]:
        assert chat_wire.parse_share(f"{inviter['long_code']}#{prefix}{V['token'].upper()}").invite_token \
            == V["token"]
    assert chat_wire.parse_share("masha#10ba682c8ad1").code == inviter["short_code"]   # any case, no dashes
    assert chat_wire.parse_share(inviter["short_code"]).fingerprint is None


def test_an_invite_link_keeps_its_uuid_token():
    token = str(uuid.uuid4())
    share = chat_wire.parse_share(f"{V['inviter']['long_code']}#{token}")
    assert share.link_token == token and share.invite_token is None
    assert chat_wire.parse_share(f"{V['inviter']['long_code']}#{token.upper()}").link_token == token


def test_the_parse_drops_what_a_mail_client_wraps_a_string_with():
    s = V["invite_string"]
    soft = "\u00ad".join(s[i:i + 9] for i in range(0, len(s), 9))
    for text in ("\ufeff" + s, s.replace("#i-", "#\u200bi-\u2060"), soft, s + "\u200d",
                 "\u200e" + s + "\u200f", "\u202a" + s + "\u202c", "\u2066" + s + "\u2069",   # right-to-left clients
                 "\u061c" + s, s.replace("-", "\u034f-"), s.replace("#i", "#\u2061i"), s + "\u180e"):
        assert chat_wire.parse_share(text) == chat_wire.parse_share(s)


@pytest.mark.parametrize("tail", ["000102030405060708090a0b0c0d0e0f",          # a token without its i-
                                  "{12345678-1234-5678-1234-567812345678}",
                                  "urn:uuid:12345678-1234-5678-1234-567812345678"])
def test_an_invite_links_token_is_its_dashed_uuid_alone(tail):
    with pytest.raises(ValueError):
        chat_wire.parse_share(f"{V['inviter']['long_code']}#{tail}")


@pytest.mark.parametrize("text", [
    "masha", "masha#10BA-682C-8AD", "masha#10BA-682C-8AD1-3513", "ma#10BA-682C-8AD1",
    "masha#10BA-682C-8ADZ", "masha#10BA-682C-8AD1#i-0001", "masha#10BA-682C-8AD1#nonsense",
    "masha#10BA-682C-8AD1#i-000102030405060708090a0b0c0d0e0f#x", "a b#10BA-682C-8AD1", "",
    "masha#10BA-682C-8AD1#i-" + "g" * 32,
])
def test_the_parse_refuses_anything_else(text):
    with pytest.raises(ValueError):
        chat_wire.parse_share(text)


# --- the fingerprint ---------------------------------------------------------------

def test_a_contact_bound_to_a_fingerprint_answers_to_its_own_key_alone():
    fingerprint = chat_wire.key_fingerprint(V["inviter"]["pubkey"])
    assert chat_wire.binds(fingerprint, V["inviter"]["pubkey"])
    assert not chat_wire.binds(fingerprint, V["acceptor"]["pubkey"])
    assert chat_wire.binds(None, V["acceptor"]["pubkey"])            # a short code's contact: its code decides
    assert not chat_wire.binds(fingerprint, "zz")


def test_a_box_names_its_uuid_in_the_dashed_form_the_database_reads():
    at = "2026-11-07T00:00:00+00:00"
    box = lambda u: json.dumps({"v": chat_wire.ENVELOPE_VERSION, "uuid": u, "ts": at, "text": "x", "sig": None}).encode()
    u = uuid.uuid4()
    assert chat_wire.open_envelope(box(str(u).upper()))["uuid"] == str(u)
    for spelling in ("urn:uuid:" + str(u), "{%s}" % u, u.hex):
        with pytest.raises(ValueError):
            chat_wire.open_envelope(box(spelling))


def test_a_message_statement_names_its_uuid_in_the_form_rows_store():
    at = datetime(2026, 11, 7, tzinfo=timezone.utc)
    u = uuid.uuid4()
    canonical = chat_wire.message_statement("ab" * 32, "cd" * 32, str(u), at, "x")
    for spelling in (str(u).upper(), "{%s}" % u, u.hex):
        assert chat_wire.message_statement("ab" * 32, "cd" * 32, spelling, at, "x") == canonical


def test_a_small_order_key_verifies_nothing():
    assert not chat_wire.verify("01" + "00" * 31, b"anything", "01" + "00" * 63)


# --- a message's signature -----------------------------------------------------------

def test_a_message_is_signed_for_its_author_recipient_uuid_time_and_text():
    me, friend = _key("33" * 32), _key("44" * 32)
    u, at = str(uuid.uuid4()), datetime(2026, 11, 7, 1, 2, 3, 456789, tzinfo=timezone.utc)
    sig = me.sign(chat_wire.message_statement(_pub(me), _pub(friend), u, at, "hello")).signature.hex()
    ok = lambda **over: chat_wire.signed_by(
        over.get("sig", sig), over.get("senders", (_pub(me),)), over.get("recipients", (_pub(friend),)),
        over.get("u", u), over.get("at", at), over.get("text", "hello"))
    assert ok()
    assert ok(at=at.astimezone(timezone(timedelta(hours=2))))        # one instant, any offset
    assert not ok(text="hello!") and not ok(u=str(uuid.uuid4()))
    assert not ok(at=at + timedelta(microseconds=1))
    assert not ok(senders=(_pub(friend),)) and not ok(recipients=(_pub(me),))
    assert ok(senders=("", _pub(friend), _pub(me)), recipients=(None, _pub(friend)))   # one of each
    assert not ok(sig=None)


def test_an_envelope_carries_its_signature_and_refuses_the_last_version():
    env = chat_wire.open_envelope(chat_wire.envelope("t", str(uuid.UUID(int=1)), "2026-11-07T00:00:00+00:00",
                                                     "ab" * 64))
    assert env["sig"] == "ab" * 64
    assert chat_wire.open_envelope(chat_wire.envelope("t", str(uuid.UUID(int=1)), "x", None))["sig"] is None
    with pytest.raises(ValueError):
        chat_wire.open_envelope(json.dumps({"v": 2, "uuid": str(uuid.UUID(int=1)), "ts": "x",
                                            "text": "t"}).encode())
    with pytest.raises(ValueError):
        chat_wire.open_envelope(chat_wire.envelope("t", str(uuid.UUID(int=1)), "x", "AB" * 64))
    with pytest.raises(ValueError):
        chat_wire.sent_time("2026-11-07T00:00:00")                    # no offset: no instant


# --- the classic handshake and a bound contact ------------------------------------------

def _who(name, seed):
    key = _key(seed)
    return SimpleNamespace(key=key, pub=_pub(key), name=name,
                           invite=node_identity.make_invite_code(name, bytes.fromhex(_pub(key))))


HOST, FRIEND, COLLIDER = _who("host", "55" * 32), _who("friend", "66" * 32), _who("friend", "77" * 32)


def _signed(who):
    # The collider speaks under the friend's code: a key sharing its 48 bits
    # (verify_invite_code is made to agree below).
    invite = FRIEND.invite if who is COLLIDER else who.invite
    return chat_wire.sign_handshake(lambda m: who.key.sign(m).signature,
                                    {"public_key_hex": who.pub, "username": who.name,
                                     "invite_code": invite}, HOST.invite)


class _Chat:
    def __init__(self, friends):
        self.friends, self.writes = friends, []

    def get_friends(self):
        return list(self.friends)

    def add_friend(self, **kw):
        self.writes.append(("add", kw["public_key_hex"]))
        return 1

    def update_friend_last_seen(self, pk):
        self.writes.append(("seen", pk))

    def db_conn(self):
        return None


def _stub(fingerprint, **over):
    return {"id": 7, "username": FRIEND.name, "public_key_hex": f"pending:{FRIEND.invite}",
            "invite_code": FRIEND.invite, "is_blocked": False, "source": "manual",
            "join_token_id": None, "key_fingerprint": fingerprint, **over}


def test_the_launchers_handshake_resolves_a_bound_contact_for_its_key_alone(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient as AioClient, TestServer
    from desktop.p2p.sync_server import SyncServer

    async def run(chat, who):
        server = SyncServer("postgresql://unused", account_info={
            "public_key_hex": HOST.pub, "username": HOST.name, "invite_code": HOST.invite})
        server._chat_service = chat
        app = web.Application()
        app.router.add_post("/api/chat/handshake", server.handle_chat_handshake)
        async with AioClient(TestServer(app)) as client:
            r = await client.post("/api/chat/handshake", json=_signed(who))
            return r.status
    # A key that shares the short code's 48 bits is not the one the long code named.
    monkeypatch.setattr(node_identity, "verify_invite_code", lambda code, pk: True)
    bound = _Chat([_stub(chat_wire.key_fingerprint(FRIEND.pub))])
    assert asyncio.run(run(bound, COLLIDER)) == 403 and bound.writes == []
    assert asyncio.run(run(bound, FRIEND)) == 200 and ("add", FRIEND.pub) in bound.writes
    unbound = _Chat([_stub(None)])
    assert asyncio.run(run(unbound, COLLIDER)) == 200                 # a short code's contact: as before


def test_the_docker_handshake_resolves_a_bound_contact_for_its_key_alone(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import peer_chat

    class Svc:
        def __init__(self):
            self.writes = []

        def add_friend(self, *a, **kw):
            self.writes.append(("add", a[0]))
            return 1

        def update_friend_last_seen(self, pk):
            self.writes.append(("seen", pk))

    def app(svc, match):
        monkeypatch.setattr(peer_chat, "get_peer_chat", lambda: svc)
        monkeypatch.setattr(peer_chat, "resolve_identity", lambda s: {
            "public_key_hex": HOST.pub, "username": HOST.name, "invite_code": HOST.invite})
        monkeypatch.setattr(peer_chat, "_find_friend_for_handshake", lambda invite, pk: match)
        monkeypatch.setattr(peer_chat, "verify_invite_code", lambda code, pk: True)
        a = FastAPI()
        a.include_router(peer_chat.chat_router)
        return a

    svc = Svc()
    with TestClient(app(svc, _stub(chat_wire.key_fingerprint(FRIEND.pub)))) as http:
        assert http.post("/api/chat/handshake", json=_signed(COLLIDER)).status_code == 403
        assert svc.writes == []
        assert http.post("/api/chat/handshake", json=_signed(FRIEND)).status_code == 200
        assert ("add", FRIEND.pub) in svc.writes


def test_the_initiator_takes_only_the_key_its_contact_is_bound_to():
    from desktop.p2p.p2p_manager import P2PManager
    fingerprint = chat_wire.key_fingerprint(FRIEND.pub)
    answer = lambda who: {"public_key_hex": who.pub}
    assert P2PManager._accepted_pubkey(answer(FRIEND), FRIEND.invite, fingerprint=fingerprint) == FRIEND.pub
    assert P2PManager._accepted_pubkey(answer(COLLIDER), COLLIDER.invite, fingerprint=fingerprint) is None


# --- the nudge ---------------------------------------------------------------------------

def test_a_nudge_runs_the_pickup_only_for_an_open_invite_and_answers_every_nudge_alike(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient as AioClient, TestServer
    from desktop.p2p import sync_server as ss

    ran = []
    OPEN, SHUT = str(uuid.UUID(int=1)), str(uuid.UUID(int=2))

    async def pickup():
        ran.append(time.monotonic())
        return ["someone#AAAA-BBBB-CCCC"]

    async def run(nudges):
        server = ss.SyncServer("postgresql://unused", account_info={
            "public_key_hex": HOST.pub, "username": HOST.name, "invite_code": HOST.invite})
        server._chat_service = _Chat([])
        server.set_pickup(pickup)
        server._is_open_invite = lambda inviteid: inviteid == OPEN
        app = web.Application()
        app.router.add_post("/api/chat/invite-accepted", server.handle_invite_accepted)
        async with AioClient(TestServer(app)) as client:
            out = []
            for body in nudges:
                r = await client.post("/api/chat/invite-accepted", json=body)
                out.append((r.status, await r.json()))
            return out, server._chat_service.writes

    unsigned = {"public_key_hex": FRIEND.pub, "username": FRIEND.name, "invite_code": FRIEND.invite,
                "invite_id": OPEN}
    naming = lambda inviteid: {**_signed(FRIEND), "invite_id": inviteid}
    answers, writes = asyncio.run(run([unsigned, _signed(FRIEND), naming(SHUT), naming("not an id"),
                                       naming(OPEN), naming(OPEN.upper())]))
    assert answers[0][0] == 403                                       # signed, as the handshake it follows
    assert answers[1:] == [(200, {"ok": True})] * 5                   # one answer, a pickup run or not
    assert len(ran) == 2                                              # only the open invite's, in either spelling
    assert writes == []                                               # it adds nobody itself
    answers, _ = asyncio.run(run([naming(OPEN)] * (ss.NUDGE_PICKUPS_PER_INVITE + 2)))
    assert answers == [(200, {"ok": True})] * (ss.NUDGE_PICKUPS_PER_INVITE + 2)
    assert len(ran) == 2 + ss.NUDGE_PICKUPS_PER_INVITE                # a few pickups an invite, whoever nudges


def test_nudges_at_once_share_their_invites_three_pickups():
    from aiohttp import web
    from aiohttp.test_utils import TestClient as AioClient, TestServer
    from desktop.p2p import sync_server as ss
    OPEN, ran = str(uuid.UUID(int=4)), []

    async def pickup():
        ran.append(1)
        await asyncio.sleep(0.01)
        return []

    def slow_open(inviteid):              # the database's answer takes a while
        time.sleep(0.01)
        return inviteid == OPEN

    async def run():
        server = ss.SyncServer("postgresql://unused", account_info={
            "public_key_hex": HOST.pub, "username": HOST.name, "invite_code": HOST.invite})
        server._chat_service = _Chat([])
        server.set_pickup(pickup)
        server._is_open_invite = slow_open
        app = web.Application()
        app.router.add_post("/api/chat/invite-accepted", server.handle_invite_accepted)
        async with AioClient(TestServer(app)) as client:
            body = {**_signed(FRIEND), "invite_id": OPEN}
            answers = await asyncio.gather(*[client.post("/api/chat/invite-accepted", json=body) for _ in range(20)])
            return [a.status for a in answers]
    assert asyncio.run(run()) == [200] * 20
    assert len(ran) == ss.NUDGE_PICKUPS_PER_INVITE


def _manager():
    from desktop.p2p.p2p_manager import P2PManager
    m = P2PManager.__new__(P2PManager)
    m._pickup_lock, m._pickup_waiting, m._nudged_at = asyncio.Lock(), None, {}
    return m


def test_pickups_asked_for_at_once_join_the_one_waiting_for_its_turn():
    runs = []

    def blocking():
        time.sleep(0.05)
        runs.append(1)
        return ["someone#AAAA-BBBB-CCCC"]

    async def run():
        m = _manager()
        m._pick_up_blocking = blocking
        m._pickup_lock = asyncio.Lock()
        return await asyncio.gather(*[m._pick_up() for _ in range(10)])
    assert asyncio.run(run()) == [["someone#AAAA-BBBB-CCCC"]] * 10
    assert len(runs) <= 2                     # one running, one that every later request joined


@pytest.mark.parametrize("status, spent, resolves", [
    (200, True, True), (404, True, False), (403, True, False),       # a Docker inviter: no nudge route
    (429, False, False), (503, False, False), (None, False, False)])  # busy, or no answer: armed still
def test_a_refused_handshake_nudges_once_in_a_while_and_an_answer_spends_it(status, spent, resolves):
    from desktop.p2p import p2p_manager as pm
    inviteid = str(uuid.UUID(int=5))
    friend = {"id": 7, "accepted_invite_id": inviteid, "key_fingerprint": chat_wire.key_fingerprint(FRIEND.pub),
              "join_token_id": None}
    sent, spents, resolved = [], [], []

    async def run():
        m = _manager()
        m._chat_service = SimpleNamespace(nudged=lambda fid: spents.append(fid))
        m._resolve_soon = lambda: resolved.append(1)

        async def answer(ip, port, invite, body, session):
            sent.append(body["invite_id"])
            return status
        m._try_nudge = answer
        first = await m._nudge_refused(friend, "192.0.2.1", 1, FRIEND.invite, {"ts": 1}, None, FRIEND.pub)
        again = await m._nudge_refused(friend, "192.0.2.1", 1, FRIEND.invite, {"ts": 2}, None, FRIEND.pub)
        return first, again
    first, again = asyncio.run(run())
    assert first is resolves and again is False
    assert sent == [inviteid]                                         # once in NUDGE_RETRY_S
    assert spents == ([7] if spent else []) and resolved == ([1] if resolves else [])
    assert pm.NUDGE_RETRY_S >= 60


def test_a_contact_nudges_only_its_inviters_own_key_and_only_once_armed():
    from desktop.p2p.p2p_manager import P2PManager
    fingerprint, inviteid = chat_wire.key_fingerprint(FRIEND.pub), str(uuid.UUID(int=3))
    armed = {"accepted_invite_id": inviteid, "key_fingerprint": fingerprint, "join_token_id": None}
    assert P2PManager._nudge_invite(armed, FRIEND.pub) == inviteid
    assert P2PManager._nudge_invite(armed, COLLIDER.pub) is None      # a stranger at its address hears nothing
    assert P2PManager._nudge_invite(armed, None) is None
    assert P2PManager._nudge_invite({**armed, "accepted_invite_id": None}, FRIEND.pub) is None
    assert P2PManager._nudge_invite({**armed, "key_fingerprint": None}, FRIEND.pub) is None
    assert P2PManager._nudge_invite({**armed, "join_token_id": str(uuid.uuid4())}, FRIEND.pub) is None
