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


@pytest.mark.parametrize("text", ["mas\u0301ha#10BA-682C-8AD1", "masha\u20dd#10BA-682C-8AD1"])
def test_a_mark_in_a_name_refuses_the_string_a_name_being_ascii(text):
    with pytest.raises(ValueError):
        chat_wire.parse_share(text)


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


def test_a_nudges_answer_is_the_same_when_its_pickup_fails_and_past_the_cap_asks_nothing(caplog):
    from aiohttp import web
    from aiohttp.test_utils import TestClient as AioClient, TestServer
    from desktop.p2p import sync_server as ss
    OPEN, asked = str(uuid.UUID(int=6)), []

    async def failing():
        raise RuntimeError("the Worker's answer did not parse")

    def is_open(inviteid):
        asked.append(inviteid)
        return inviteid == OPEN

    async def run():
        server = ss.SyncServer("postgresql://unused", account_info={
            "public_key_hex": HOST.pub, "username": HOST.name, "invite_code": HOST.invite})
        server._chat_service = _Chat([])
        server.set_pickup(failing)
        server._is_open_invite = is_open
        app = web.Application()
        app.router.add_post("/api/chat/invite-accepted", server.handle_invite_accepted)
        async with AioClient(TestServer(app)) as client:
            out = []
            for _ in range(ss.NUDGE_PICKUPS_PER_INVITE + 2):
                r = await client.post("/api/chat/invite-accepted", json={**_signed(FRIEND), "invite_id": OPEN})
                out.append((r.status, await r.json()))
            return out
    assert asyncio.run(run()) == [(200, {"ok": True})] * (ss.NUDGE_PICKUPS_PER_INVITE + 2)
    assert len(asked) == ss.NUDGE_PICKUPS_PER_INVITE                  # past its three, no query at all
    assert any("pickup failed" in r.getMessage() and r.exc_info for r in caplog.records)


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


def test_a_pickup_asked_for_while_one_runs_gets_one_that_began_after_it():
    began = []

    def blocking():
        began.append(time.monotonic())
        time.sleep(0.15)
        return [len(began)]

    async def run():
        m = _manager()
        m._pick_up_blocking = blocking
        first = asyncio.ensure_future(m._pick_up())
        await asyncio.sleep(0.05)                       # the first is running now
        asked = time.monotonic()
        second = await m._pick_up()
        return await first, second, asked
    first, second, asked = asyncio.run(run())
    assert first == [1] and second == [2] and began[1] > asked


def test_a_joiner_given_up_on_takes_the_pickup_from_nobody():
    def blocking():
        time.sleep(0.1)
        return ["someone#AAAA-BBBB-CCCC"]

    async def run():
        m = _manager()
        m._pick_up_blocking = blocking
        await m._pickup_lock.acquire()                  # a pickup running: the two below wait together
        one, two = asyncio.ensure_future(m._pick_up()), asyncio.ensure_future(m._pick_up())
        await asyncio.sleep(0.01)
        one.cancel()
        m._pickup_lock.release()
        return await two
    assert asyncio.run(run()) == ["someone#AAAA-BBBB-CCCC"]


def _armed(fid=7):
    return {"id": fid, "accepted_invite_id": str(uuid.UUID(int=5)),
            "key_fingerprint": chat_wire.key_fingerprint(FRIEND.pub), "join_token_id": None}


@pytest.mark.parametrize("status, outcome", [
    (200, "taken"), (404, "answered"), (400, "answered"),             # a node with no nudge route
    (403, None), (429, None), (500, None), (503, None), (None, None)])  # busy, or no answer: armed still
def test_a_refused_handshakes_nudge_and_what_each_answer_makes_of_it(status, outcome):
    spents, resolved = [], []

    async def run():
        m = _manager()
        m._chat_service = SimpleNamespace(nudged=lambda fid: spents.append(fid))
        m._resolve_soon = lambda: resolved.append(1)

        async def answer(ip, port, invite, body, session):
            return status
        m._try_nudge = answer
        return await m._nudge_refused(_armed(), "192.0.2.1", 1, FRIEND.invite, {}, None, FRIEND.pub, 100.0)
    assert asyncio.run(run()) == outcome
    assert spents == ([7] if outcome == "taken" else []) and resolved == ([1] if outcome == "taken" else [])


def test_a_busy_nudge_waits_its_interval_and_one_pass_reaches_every_address():
    from desktop.p2p import p2p_manager as pm
    sent = []

    async def run():
        m = _manager()
        m._chat_service = SimpleNamespace(nudged=lambda fid: None)
        m._resolve_soon = lambda: None

        async def answer(ip, port, invite, body, session):
            sent.append((ip, body["invite_id"]))
            return 503
        m._try_nudge = answer
        for address, pass_at in (("192.0.2.1", 0.0), ("192.0.2.2", 0.0),            # one pass, two addresses
                                 ("192.0.2.1", pm.NUDGE_RETRY_S - 1.0),             # the next passes wait
                                 ("192.0.2.1", pm.NUDGE_RETRY_S + 1.0)):            # past the interval
            await m._nudge_refused(_armed(), address, 1, FRIEND.invite, {}, None, FRIEND.pub, pass_at)
    asyncio.run(run())
    assert [ip for ip, _ in sent] == ["192.0.2.1", "192.0.2.2", "192.0.2.1"]


class _FakeContent:
    def __init__(self, data):
        self.data = data

    async def read(self, n):
        chunk, self.data = self.data[:n], self.data[n:]
        return chunk


class _FakeResponse:
    def __init__(self, status, body):
        self.status, self.content = status, _FakeContent(json.dumps(body).encode())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _resolver(monkeypatch, nudge_answers):
    """A manager whose resolver pass meets the inviter at the addresses of
    `nudge_answers` ({ip: status of its nudge}), each refusing the handshake;
    → (manager, the requests it made, the contacts it spent)."""
    import aiohttp
    from desktop import node_identity
    from desktop.p2p import birth_cert, p2p_manager as pm
    requests, spent = [], []
    acceptor = _who("ada", "88" * 32)

    class Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, json=None, timeout=None):
            ip = url.split("//", 1)[1].split(":", 1)[0]
            requests.append((ip, url.rsplit("/", 1)[1], json))
            if url.endswith("/handshake"):
                return _FakeResponse(403, {"error": "not accepted"})
            return _FakeResponse(nudge_answers[ip], {"ok": True})
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    monkeypatch.setattr(aiohttp, "TCPConnector", lambda **k: None)
    monkeypatch.setattr(pm, "pinned_ssl_context", lambda: object())
    monkeypatch.setattr(pm, "pinned_pubkey", lambda ctx: FRIEND.pub)       # each channel: the inviter's key
    monkeypatch.setattr(node_identity, "get_account_info", lambda: {
        "public_key_hex": acceptor.pub, "username": acceptor.name, "invite_code": acceptor.invite})
    monkeypatch.setattr(node_identity, "sign_message", lambda m: acceptor.key.sign(m).signature)
    monkeypatch.setattr(birth_cert, "load_certificate", lambda: None)
    monkeypatch.setattr(birth_cert, "load_proof", lambda: None)
    m = _manager()
    friend = {**_armed(), "public_key_hex": f"pending:{FRIEND.invite}", "invite_code": FRIEND.invite,
              "username": FRIEND.name, "source": "manual"}
    m._chat_service = SimpleNamespace(get_friends=lambda: [friend], nudged=lambda fid: spent.append(fid),
                                      remove_friend=lambda fid: None)
    m._proof_wait_invites = set()
    m._resolve_soon = lambda: None

    async def nothing():
        return None

    async def peers(f):
        return [(ip, 24001) for ip in nudge_answers]
    m._deliver_rotation_notices, m._find_friend_peers = nothing, peers
    return m, requests, spent


def test_a_resolver_pass_nudges_each_address_of_the_inviters_key_and_one_that_takes_it_spends_it(monkeypatch):
    # a node of the inviter's account without the route first, the one that sent the invite next
    m, requests, spent = _resolver(monkeypatch, {"192.0.2.1": 404, "192.0.2.2": 200})
    asyncio.run(m._do_resolve_pending())
    nudges = [(ip, body["invite_id"]) for ip, path, body in requests if path == "invite-accepted"]
    assert nudges == [("192.0.2.1", str(uuid.UUID(int=5))), ("192.0.2.2", str(uuid.UUID(int=5)))]
    assert spent == [7]


def test_a_resolver_pass_whose_nudge_no_address_could_take_spends_it_at_its_end(monkeypatch):
    m, requests, spent = _resolver(monkeypatch, {"192.0.2.1": 404})
    asyncio.run(m._do_resolve_pending())
    assert [path for _, path, _ in requests] == ["handshake", "invite-accepted"] and spent == [7]
    m, requests, spent = _resolver(monkeypatch, {"192.0.2.1": 503})
    asyncio.run(m._do_resolve_pending())
    assert spent == []                                                # busy: armed for a later pass


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
