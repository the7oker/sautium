"""The doors that make or change an account hold the password estimate's
edge (SEC-S2): refused under an hour of one GPU, kept under a year only on
the person's word, nothing asked above — the Web UI's setup (and its restore,
which brings the identity of record back whatever its password), an identity
change, a start that makes its account from .env. An existing account is told
its time once at a sign-in, and only the key estimated is kept. The pairing
code is 140 bits, never burned, a wrong one told. Every box is padded, and no
log holds a password. On a real database; made-up passwords only."""

import asyncio
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import psycopg2
import psycopg2.pool
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nacl.public import Box, PrivateKey, PublicKey

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import config  # noqa: E402
import db_pool  # noqa: E402
import device_auth  # noqa: E402
import p2p_identity  # noqa: E402
from desktop import node_identity, password_strength  # noqa: E402

REFUSED = "sunshine"                     # 47 guesses
WORD = "qmzvhwkt"                        # 10⁸: about 4 days of one GPU today
CLEAR = "tangerine volcano quiet"        # about 2 centuries
SENTINEL = "Qz7vXk2Lp9WmTr4"             # the password a log must never hold


class _Key:
    def __init__(self, username: str):
        self.key = Ed25519PrivateKey.generate()
        raw = self.key.public_key().public_bytes_raw()
        self.pub = raw.hex()
        self.username = username
        self.code = node_identity.make_invite_code(username, raw)

    def info(self) -> dict:
        return {"node_id": self.pub, "public_key_hex": self.pub, "username": self.username,
                "invite_code": self.code, "email": ""}


@pytest.fixture
def db(scratch_dsn, monkeypatch):
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM user_settings WHERE key IN (%s, 'p2p.bound_identity', %s, %s)",
                    (node_identity.SIGNING_KEY_SETTING, device_auth.PASSWORD_ESTIMATED_KEY,
                     device_auth.PIN_FAILURES_KEY))
    p2p_identity.forget_cached()
    monkeypatch.setattr(device_auth, "_pin_recorded", 0)
    monkeypatch.setattr(device_auth, "_pin_unrecorded", 0)
    yield conn
    p2p_identity.forget_cached()
    conn.close()
    pool.closeall()


def _record(conn, pubkey: str) -> None:
    with conn.cursor() as cur:
        cur.execute(node_identity.RECORD_SIGNING_KEY_SQL, {"key": json.dumps(pubkey)})


def _setting(conn, key: str):
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM user_settings WHERE key = %s", (key,))
        row = cur.fetchone()
    return row[0] if row else None


@pytest.fixture
def derive(monkeypatch):
    calls = []
    state = SimpleNamespace(key=None, calls=calls)

    def identity(username, password, email=""):
        calls.append(username)
        return {**state.key.info(), "username": username}

    monkeypatch.setattr(p2p_identity, "derive_identity", identity)
    monkeypatch.setattr(p2p_identity, "derive_private_key", lambda u, p: state.key.key)
    monkeypatch.setattr(device_auth, "_record_signing_key", lambda _pubkey: None)
    return state


def _settings(monkeypatch, **kw):
    for k, v in {"sautium_master": False, "p2p_username": "", "p2p_password": "", "p2p_email": "",
                 "p2p_identity_dir": "", **kw}.items():
        monkeypatch.setattr(config.settings, k, v)


# --- the Web UI's setup: a new identity -----------------------------------------

def test_a_password_under_an_hour_is_refused_before_any_derivation(db, derive, tmp_path, monkeypatch):
    _settings(monkeypatch)
    derive.key = _Key("alice")
    with pytest.raises(device_auth.WeakPassword) as e:
        device_auth._write_identity(tmp_path, "alice", REFUSED)
    assert e.value.verdict["zone"] == "refused"
    assert derive.calls == [] and not (tmp_path / "node_ed25519.key").exists()


def test_a_password_under_a_year_is_made_only_on_the_person_s_word(db, derive, tmp_path, monkeypatch):
    _settings(monkeypatch)
    derive.key = _Key("alice")
    with pytest.raises(device_auth.WeakPassword) as e:
        device_auth._write_identity(tmp_path, "alice", WORD)
    assert e.value.verdict["zone"] == "word" and e.value.verdict["words"] == "about 3 days"
    assert derive.calls == []
    info = device_auth._write_identity(tmp_path, "alice", WORD, keep=True)
    assert info["public_key_hex"] == derive.key.pub


def test_a_password_above_a_year_asks_nothing(db, derive, tmp_path, monkeypatch):
    _settings(monkeypatch)
    derive.key = _Key("alice")
    assert device_auth._write_identity(tmp_path, "alice", CLEAR)["username"] == "alice"


def test_the_nickname_and_the_email_typed_are_tried_first(db, derive, tmp_path, monkeypatch):
    _settings(monkeypatch)
    derive.key = _Key("zorbulon")
    assert password_strength.verdict("zorbulon1999")["zone"] == "clear"
    with pytest.raises(device_auth.WeakPassword) as e:
        device_auth._write_identity(tmp_path, "zorbulon", "zorbulon1999")
    assert e.value.verdict["zone"] == "refused"
    assert password_strength.verdict("quixotic1999")["zone"] == "word"
    with pytest.raises(device_auth.WeakPassword) as e:
        device_auth._write_identity(tmp_path, "someone", "quixotic1999", keep=True,
                                    email="quixotic@example.org")
    assert e.value.verdict["zone"] == "refused"


# --- the Web UI's setup: bringing the identity of record back -------------------

def test_the_identity_of_record_comes_back_whatever_its_password(db, derive, tmp_path, monkeypatch):
    _settings(monkeypatch)
    mine = _Key("alice")
    _record(db, mine.pub)
    derive.key = mine
    info = device_auth._write_identity(tmp_path, "alice", REFUSED)
    assert info["public_key_hex"] == mine.pub


def test_another_identity_over_the_records_is_gated_after_its_derivation(db, derive, tmp_path,
                                                                         monkeypatch):
    _settings(monkeypatch)
    _record(db, _Key("alice").pub)
    derive.key = _Key("alice")                    # the pair makes another key
    with pytest.raises(device_auth.AnotherIdentity):
        device_auth._write_identity(tmp_path, "alice", REFUSED)
    with pytest.raises(device_auth.WeakPassword):
        device_auth._write_identity(tmp_path, "alice", REFUSED, new_identity=True)
    assert derive.calls == ["alice", "alice"] and not (tmp_path / "node_ed25519.key").exists()
    assert device_auth._write_identity(tmp_path, "alice", CLEAR, new_identity=True)


# --- the route: the refusal sealed, a phrase beside it, the setup code kept ----

def _seal(handshake: dict, payload: dict):
    client = PrivateKey.generate()
    box = Box(client, PublicKey(base64.b64decode(handshake["eph"])))
    sealed = box.encrypt(json.dumps(payload).encode())
    b64 = lambda raw: base64.b64encode(raw).decode()  # noqa: E731
    return box, {"eph": handshake["eph"], "client": b64(bytes(client.public_key)),
                 "nonce": b64(sealed.nonce), "box": b64(sealed.ciphertext)}


def _call(client, path, payload):
    box, envelope = _seal(client.get("/api/auth/handshake").json(), payload)
    r = client.post(path, json=envelope)
    if r.status_code != 200:
        return r.status_code, None, r
    env = r.json()
    plain = box.decrypt(base64.b64decode(env["box"]), base64.b64decode(env["nonce"]))
    return 200, json.loads(plain), r


@pytest.fixture
def doors(monkeypatch):
    from routers import auth
    monkeypatch.setattr(p2p_identity, "load_signing_key", lambda settings: None)
    device_auth._channels.clear()
    app = FastAPI()
    app.include_router(auth.router)
    return TestClient(app)


def test_the_setup_door_seals_its_refusal_with_a_phrase_and_keeps_the_code(db, derive, doors,
                                                                           tmp_path, monkeypatch):
    _settings(monkeypatch, p2p_identity_dir=str(tmp_path))
    device_auth.prepare_setup()
    code = device_auth._setup_code
    status, said, r = _call(doors, "/api/auth/create-account",
                            {"username": "alice", "password": REFUSED, "setup_code": code})
    assert status == 200 and said["password"]["zone"] == "refused"
    offer = said["password"]["offer"]
    assert len(offer["phrase"].split("-")) == 4 and offer["guesses"] == 7776 ** 4
    # in the clear: an envelope, nothing that marks a refusal
    assert set(r.json()) == {"nonce", "box"} and "x-sautium-auth-error" not in r.headers
    assert device_auth._setup_code == code and device_auth._doors["setup"]["failures"] == 0
    status, said, _ = _call(doors, "/api/auth/create-account",
                            {"username": "alice", "password": WORD, "setup_code": code})
    assert said == {"password": {**password_strength.verdict(WORD, ("alice", "")),
                                 "zone": "word"}, "_pad": said["_pad"]}
    assert not (tmp_path / "node_ed25519.key").exists()


def test_the_setup_meter_answers_only_while_the_node_has_no_account(db, doors, tmp_path, monkeypatch):
    _settings(monkeypatch, p2p_identity_dir=str(tmp_path))
    status, said, _ = _call(doors, "/api/auth/setup-estimate",
                            {"password": "zorbulon1999", "username": "zorbulon", "email": ""})
    assert status == 200 and said["zone"] == "refused"
    status, said, _ = _call(doors, "/api/auth/setup-phrase", {})
    assert status == 200 and len(said["phrase"].split("-")) == 4
    monkeypatch.setattr(device_auth, "account_configured", lambda: True)
    assert _call(doors, "/api/auth/setup-estimate", {"password": "x"})[0] == 409
    assert _call(doors, "/api/auth/setup-phrase", {})[0] == 409


def test_one_setup_estimate_at_a_time(db, doors, tmp_path, monkeypatch):
    from routers import auth
    _settings(monkeypatch, p2p_identity_dir=str(tmp_path))
    monkeypatch.setattr(auth, "_setup_estimates", 1)
    assert _call(doors, "/api/auth/setup-estimate", {"password": "x"})[0] == 503


def test_the_signed_meter_tries_the_account_s_email(db, doors, monkeypatch):
    monkeypatch.setattr(device_auth, "account_email", lambda: "quixotic@example.org")
    _, said, _ = _call(doors, "/api/auth/estimate", {"password": "quixotic1999", "username": "x"})
    assert said["zone"] == "refused"
    monkeypatch.setattr(device_auth, "account_email", lambda: "")
    _, said, _ = _call(doors, "/api/auth/estimate", {"password": "quixotic1999", "username": "x"})
    assert said["zone"] == "word"


def test_every_box_the_node_seals_is_a_whole_bucket(db, doors, tmp_path, monkeypatch):
    _settings(monkeypatch, p2p_identity_dir=str(tmp_path))
    sizes = {len(base64.b64decode(_call(doors, "/api/auth/setup-estimate",
                                        {"password": p, "username": "alice"})[2].json()["box"]))
             for p in ("a", "Sunshine1!", CLEAR, "x" * 200)}
    assert sizes == {device_auth.BOX_BUCKET + 16}
    assert len(device_auth.padded({"token": "ab" * 32})) % device_auth.BOX_BUCKET == 0
    assert len(device_auth.padded({"x": "é" * 3000})) % device_auth.BOX_BUCKET == 0


def test_the_page_pads_what_it_seals_to_the_same_bucket():
    js = (BACKEND / "static" / "auth.js").read_text(encoding="utf-8")
    assert "const BOX_BUCKET = 1024;" in js
    assert "nacl.box(padded(payload), nonce, hs.eph, kp.secretKey)" in js


# --- an identity change ----------------------------------------------------------

def test_a_new_password_at_an_identity_change_passes_the_same_edge(db, tmp_path, monkeypatch):
    d = tmp_path / "id"
    d.mkdir()
    (d / "node_info.json").write_text(json.dumps({"username": "alice", "email": "quixotic@example.org"}))
    _settings(monkeypatch, p2p_identity_dir=str(d))
    monkeypatch.setattr(device_auth, "account_configured", lambda: True)
    rotated = []

    async def stepped(password, pin):
        return None

    async def rotate(d_, username, password):
        rotated.append(password)
        return {"info": {"public_key_hex": "ab" * 32}}
    monkeypatch.setattr(device_auth, "step_up", stepped)
    monkeypatch.setattr(device_auth, "_rotate_locked", rotate)
    monkeypatch.setattr(device_auth, "_after_rotation", lambda pubkey: None)
    for password, keep in ((REFUSED, True), (WORD, False), ("alice1999", True), ("quixotic1999", True)):
        with pytest.raises(device_auth.WeakPassword):
            asyncio.run(device_auth.rotate_identity("alice", password, "current", keep=keep))
    assert rotated == []
    asyncio.run(device_auth.rotate_identity("alice", WORD, "current", keep=True))
    asyncio.run(device_auth.rotate_identity("alice", "", "current"))          # minted: no edge
    assert rotated == [WORD, ""]


# --- an existing account, told once ----------------------------------------------

def test_a_sign_in_tells_a_weak_password_s_time_once_and_keeps_only_the_key(db, monkeypatch):
    key = {"pub": "ab" * 32}
    monkeypatch.setattr(device_auth, "_expected_pubkey", lambda: key["pub"])
    monkeypatch.setattr(device_auth, "account_username", lambda: "alice")
    monkeypatch.setattr(device_auth, "account_email", lambda: "")
    said = device_auth.told_once(WORD)
    assert said["zone"] == "word" and said["words"] == "about 3 days"
    assert _setting(db, device_auth.PASSWORD_ESTIMATED_KEY) == key["pub"]
    assert device_auth.told_once(WORD) is None
    key["pub"] = "cd" * 32                                   # a new password is a new key
    assert device_auth.told_once(REFUSED)["zone"] == "refused"
    key["pub"] = "ef" * 32
    assert device_auth.told_once(CLEAR) is None
    assert _setting(db, device_auth.PASSWORD_ESTIMATED_KEY) == key["pub"]   # strong: the same record


# --- a start that makes its account from .env -----------------------------------

def test_a_start_making_its_account_from_a_weak_env_password_refuses(db, tmp_path, monkeypatch):
    _settings(monkeypatch, p2p_username="alice", p2p_password=REFUSED, p2p_identity_dir=str(tmp_path))
    refusal = device_auth.env_password_refusal()
    assert refusal.startswith("P2P_PASSWORD would be found within an hour") and REFUSED not in refusal
    _settings(monkeypatch, p2p_username="alice", p2p_password=WORD, p2p_identity_dir=str(tmp_path))
    assert device_auth.env_password_refusal() is None          # the operator's .env is its word


def test_a_node_its_env_made_before_keeps_starting(db, tmp_path, monkeypatch):
    _settings(monkeypatch, p2p_username="alice", p2p_password=REFUSED, p2p_identity_dir=str(tmp_path))
    _record(db, _Key("alice").pub)
    assert device_auth.env_password_refusal() is None
    with db.cursor() as cur:
        cur.execute("DELETE FROM user_settings WHERE key = %s", (node_identity.SIGNING_KEY_SETTING,))
    (tmp_path / "node_ed25519.key").write_text("a key")
    assert device_auth.env_password_refusal() is None


# --- the pairing code (A3) -------------------------------------------------------

def test_the_pairing_code_is_never_burned_and_a_wrong_one_is_told(db, monkeypatch):
    monkeypatch.setattr(device_auth, "_pin", None)
    monkeypatch.setattr(device_auth, "_pin_prev", None)
    code = device_auth.current_pin()["code"]
    assert len(code.replace("-", "")) == 28                  # 140 bits
    for i in range(50):
        assert asyncio.run(device_auth.redeem_pin(f"WRONG-{i:04d}")) is False
    assert device_auth.current_pin()["code"] == code
    assert asyncio.run(device_auth.redeem_pin(code.lower())) is True
    assert 25 <= _setting(db, device_auth.PIN_FAILURES_KEY)["count"] <= 50    # written as it doubles


def test_a_flood_of_wrong_codes_is_a_handful_of_writes(db, monkeypatch):
    """The record is written when the codes not yet in it reach its count —
    1, 2, 4 … — one write in flight; the owner's look starts it at one again."""
    writes = []
    record = device_auth._record_failure
    monkeypatch.setattr(device_auth, "_record_failure",
                        lambda key, n=1: writes.append(n) or record(key, n))

    async def one_by_one(k):
        for i in range(k):
            await device_auth.redeem_pin(f"WRONG-{i:04d}")
    asyncio.run(one_by_one(1000))
    assert len(writes) <= 11 and writes[:4] == [1, 1, 2, 4]
    counted = _setting(db, device_auth.PIN_FAILURES_KEY)
    assert 500 <= counted["count"] <= 1000                   # "at least" what the notice says
    with db.cursor() as cur:                                 # the owner looks
        cur.execute("INSERT INTO user_settings (key, value) VALUES ('notice.seen', %s::jsonb) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (json.dumps({device_auth.PIN_FAILURES_KEY: counted["last"]}),))
    device_auth.pin_failures_seen()
    asyncio.run(one_by_one(1))
    assert _setting(db, device_auth.PIN_FAILURES_KEY)["count"] == 1


def test_a_look_during_a_write_is_not_undone_by_it(db, monkeypatch):
    """The owner's look lands while a write is in flight: the write's count
    is stale, and the next wrong code is written at once."""
    record = device_auth._record_failure

    def looked_meanwhile(key, n=1):
        count = record(key, n)
        device_auth.pin_failures_seen()
        return count
    monkeypatch.setattr(device_auth, "_record_failure", looked_meanwhile)
    asyncio.run(device_auth.redeem_pin("WRONG-0001"))
    assert device_auth._pin_recorded == 0
    with db.cursor() as cur:                                 # the look's own mark
        cur.execute("INSERT INTO user_settings (key, value) VALUES ('notice.seen', %s::jsonb) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (json.dumps({device_auth.PIN_FAILURES_KEY:
                                 _setting(db, device_auth.PIN_FAILURES_KEY)["last"]}),))
    monkeypatch.setattr(device_auth, "_record_failure", record)
    asyncio.run(device_auth.redeem_pin("WRONG-0002"))
    assert _setting(db, device_auth.PIN_FAILURES_KEY)["count"] == 1          # written at once


def test_random_characters_between_symbols_keep_their_price():
    """Every separator character is priced but the one a phrase repeats
    between its words: a run of symbols, a backtick, keep theirs."""
    for password in ("a#B3$c!D9&eF", "Xk#9$mQ!2&vL", "w%T&k!9#Zr", "m){[%%,|=,n28", "|$=2~`#r&#+c",
                     "(o-,,/;c)+;-", "a2[-Xj<]}[*|"):
        assert password_strength.estimate(password).zone == "clear", password
    for title in ("harry potter and the philosopher's stone", "the-lord-of-the-rings",
                  "make-america-great-again!", "ocean's eleven", "Schindler's List"):
        assert password_strength.estimate(title).zone == "refused", title


def test_a_code_this_node_minted_and_let_expire_is_not_told(db, monkeypatch):
    monkeypatch.setattr(device_auth, "_pin", None)
    monkeypatch.setattr(device_auth, "_pin_prev", None)
    old = device_auth.issue_pin()
    monkeypatch.setattr(device_auth, "_pin", {"code": old, "expires_at": 0})
    monkeypatch.setattr(device_auth, "_pin_prev", None)
    assert asyncio.run(device_auth.redeem_pin(old)) is False
    assert _setting(db, device_auth.PIN_FAILURES_KEY) is None


def test_the_wrong_codes_are_a_notice_of_their_own(db):
    from routers import settings as settings_router
    asyncio.run(device_auth.redeem_pin("NOT-A-CODE"))
    keys = [n["key"] for n in settings_router._notices_state()["items"]]
    assert "auth.pin_failures" in keys


# --- nothing of a password in a log ----------------------------------------------

def test_no_door_logs_a_password_or_its_time(db, derive, doors, tmp_path, monkeypatch, caplog):
    from routers import auth
    _settings(monkeypatch, p2p_identity_dir=str(tmp_path), p2p_username="alice", p2p_password=SENTINEL[:6])
    derive.key = _Key("alice")
    caplog.set_level("DEBUG")
    assert device_auth.env_password_refusal()                         # the env's door, weak
    device_auth.prepare_setup()
    code = device_auth._setup_code
    for password in (SENTINEL[:6], SENTINEL):
        _call(doors, "/api/auth/setup-estimate", {"password": password, "username": "alice"})
    # refused at the door (made, it would start the node's workers: not here)
    _call(doors, "/api/auth/create-account", {"username": "alice", "password": SENTINEL[:6],
                                              "setup_code": code})
    device_auth._write_identity(tmp_path, "alice", SENTINEL)          # made
    monkeypatch.setattr(device_auth, "_expected_pubkey", lambda: "ab" * 32)
    monkeypatch.setattr(auth, "_secret", lambda: b"node secret")

    async def right(password, **_kw):
        return True
    monkeypatch.setattr(device_auth, "password_tried", right)
    _call(doors, "/api/auth/login", {"password": SENTINEL[:6]})      # told once, at the sign-in
    text = caplog.text
    assert not any(SENTINEL[i:i + 3] in text for i in range(len(SENTINEL) - 2))
    for weak in (SENTINEL[:6],):
        said = password_strength.verdict(weak, ("alice",))
        assert said["words"] not in text and said["meter"] not in text


# --- the routes, end to end -------------------------------------------------------

def test_a_sign_in_carries_the_time_once_in_its_sealed_answer(db, doors, monkeypatch):
    from routers import auth
    monkeypatch.setattr(auth, "_secret", lambda: b"node secret")
    monkeypatch.setattr(device_auth, "_expected_pubkey", lambda: "ab" * 32)
    monkeypatch.setattr(device_auth, "account_username", lambda: "alice")

    async def right(password, **_kw):
        return True
    monkeypatch.setattr(device_auth, "password_tried", right)
    _, said, _ = _call(doors, "/api/auth/login", {"password": WORD})
    assert said["token"] and said["password"]["zone"] == "word" and said["password"]["env"] is True
    _, said, _ = _call(doors, "/api/auth/login", {"password": WORD})
    assert "password" not in said


def test_a_change_s_refusal_is_sealed_and_changes_nothing(db, doors, tmp_path, monkeypatch):
    d = tmp_path / "id"
    d.mkdir()
    (d / "node_info.json").write_text(json.dumps({"username": "alice"}))
    _settings(monkeypatch, p2p_identity_dir=str(d))
    monkeypatch.setattr(device_auth, "account_configured", lambda: True)
    monkeypatch.setattr(device_auth, "pin_door", lambda: True)

    async def stepped(password, pin):
        return None

    async def rotate(*a):
        raise AssertionError("rotated")
    monkeypatch.setattr(device_auth, "step_up", stepped)
    monkeypatch.setattr(device_auth, "_rotate_locked", rotate)
    status, said, r = _call(doors, "/api/auth/change-identity",
                            {"username": "alice", "password": REFUSED, "current_password": "x"})
    assert status == 200 and said["password"]["zone"] == "refused" and said["password"]["offer"]["phrase"]
    assert set(r.json()) == {"nonce", "box"}


@pytest.mark.parametrize("password", [REFUSED, WORD, CLEAR, "Sunshine1!", "Kyiv2025", "ocean's eleven"])
def test_the_meter_and_the_door_read_one_number(db, derive, doors, tmp_path, monkeypatch, password):
    _settings(monkeypatch, p2p_identity_dir=str(tmp_path))
    _, meter, _ = _call(doors, "/api/auth/setup-estimate", {"password": password, "username": "alice"})
    try:
        device_auth.check_new_password(password, "alice", "", False)
        door = "clear"
    except device_auth.WeakPassword as e:
        door = e.verdict["zone"]
    assert meter["zone"] == door == password_strength.verdict(password, ("alice", ""))["zone"]


@pytest.mark.parametrize("path", ["/api/auth/setup-estimate", "/api/auth/estimate", "/api/auth/setup-phrase",
                                  "/api/auth/phrase"])
def test_the_new_routes_take_nothing_but_the_box(db, doors, path):
    r = doors.post(path, json={"password": SENTINEL})
    assert r.status_code == 400 and r.headers["x-sautium-auth-error"] == "unboxed" and SENTINEL not in r.text
