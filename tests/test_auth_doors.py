"""The doors a browser signs in through (device_auth, routers/auth.py,
auth.js): a password login on an anonymous account is refused before any
derivation; the derivations in flight are bounded, a request past the bound
is told the node is busy; a box the node could not open says so, and the page
seals it again once, to a fresh handshake."""

import asyncio
import base64
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nacl.public import Box, PrivateKey, PublicKey

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import device_auth  # noqa: E402
import p2p_identity  # noqa: E402


@pytest.fixture(autouse=True)
def _no_identity(monkeypatch):
    monkeypatch.setattr(p2p_identity, "load_signing_key", lambda settings: None)
    monkeypatch.setattr(device_auth, "_derive_in_flight", 0)
    device_auth._channels.clear()


def _account(monkeypatch, *, anonymous: bool):
    monkeypatch.setattr(device_auth, "_expected_pubkey", lambda: "ab" * 32)
    monkeypatch.setattr(device_auth, "account_username",
                        lambda: "anonymous-1a2b" if anonymous else "owner")
    monkeypatch.setattr(device_auth, "account_anonymous", lambda: anonymous)


def test_a_password_login_on_an_anonymous_account_derives_nothing(monkeypatch):
    _account(monkeypatch, anonymous=True)
    derived = []
    monkeypatch.setattr(p2p_identity, "derive_identity",
                        lambda *a: derived.append(a) or {"public_key_hex": "ab" * 32})
    assert asyncio.run(device_auth.verify_password("any password at all")) is False
    assert derived == []


def test_the_derivations_in_flight_are_bounded_and_the_last_place_is_a_signed_callers(monkeypatch):
    monkeypatch.setattr(device_auth, "_derive_semaphore", asyncio.Semaphore(2))
    gate = threading.Event()

    def derive():
        gate.wait(10)
        return "derived"

    async def scenario():
        unsigned = [asyncio.ensure_future(device_auth.derive_guarded(derive))
                    for _ in range(device_auth.DERIVE_QUEUE - 1)]
        await asyncio.sleep(0.05)
        with pytest.raises(device_auth.Busy):
            await device_auth.derive_guarded(derive)                  # the login door is full
        signed = asyncio.ensure_future(device_auth.derive_guarded(derive, signed=True))
        await asyncio.sleep(0.05)
        with pytest.raises(device_auth.Busy):
            await device_auth.derive_guarded(derive, signed=True)
        # a caller that goes leaves its derivation counted until its thread ends
        unsigned[0].cancel()
        await asyncio.sleep(0.05)
        assert device_auth._derive_in_flight == device_auth.DERIVE_QUEUE
        gate.set()
        results = await asyncio.gather(*unsigned[1:], signed)
        while device_auth._derive_in_flight:
            await asyncio.sleep(0.01)
        return results
    assert asyncio.run(scenario()) == ["derived"] * (device_auth.DERIVE_QUEUE - 1)
    assert device_auth._derive_in_flight == 0


def test_a_derivation_whose_caller_went_has_its_failure_said(caplog, monkeypatch):
    monkeypatch.setattr(device_auth, "_derive_semaphore", asyncio.Semaphore(2))
    gate = threading.Event()

    def derive():
        gate.wait(10)
        raise ValueError("the derivation failed here")

    async def scenario():
        caller = asyncio.ensure_future(device_auth.derive_guarded(derive))
        await asyncio.sleep(0.05)
        caller.cancel()
        await asyncio.sleep(0.01)
        gate.set()
        while device_auth._derive_in_flight:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.01)
    with caplog.at_level("WARNING", logger="device_auth"):
        asyncio.run(scenario())
    assert "the derivation failed here" in caplog.text


def _seal(handshake: dict, payload: dict) -> dict:
    client = PrivateKey.generate()
    sealed = Box(client, PublicKey(base64.b64decode(handshake["eph"]))).encrypt(
        json.dumps(payload).encode())
    b64 = lambda raw: base64.b64encode(raw).decode()  # noqa: E731
    return {"eph": handshake["eph"], "client": b64(bytes(client.public_key)),
            "nonce": b64(sealed.nonce), "box": b64(sealed.ciphertext)}


@pytest.fixture
def doors():
    from routers import auth
    app = FastAPI()
    app.include_router(auth.router)
    return TestClient(app)


def test_a_busy_node_says_so_at_the_password_door(doors, monkeypatch):
    _account(monkeypatch, anonymous=False)
    monkeypatch.setattr(device_auth, "_doors", {name: {"failures": 0, "shut_until": 0.0, "in_flight": 0}
                                              for name in ("login", "signed")})
    async def busy(_password, **_kw):
        raise device_auth.Busy()
    monkeypatch.setattr(device_auth, "verify_password", busy)
    r = doors.post("/api/auth/login", json=_seal(doors.get("/api/auth/handshake").json(),
                                                 {"password": "hunter22"}))
    assert r.status_code == 503 and r.headers["retry-after"]


def test_a_box_the_node_cannot_open_is_marked_as_the_channels(doors):
    envelope = _seal(doors.get("/api/auth/handshake").json(), {"password": "hunter22"})
    device_auth._channels.clear()                     # its handshake let go meanwhile
    r = doors.post("/api/auth/login", json=envelope)
    assert r.status_code == 400 and r.headers["x-sautium-auth-error"] == "channel"


_PAGE_DRIVER = r"""
const fs = require('node:fs'), vm = require('node:vm');
const [naclPath, shaPath, authPath, origin, setup] = process.argv.slice(-5);
const cfg = JSON.parse(setup);
const store = Object.assign({}, cfg.store || {});
const asked = [], sent = {};
const ctx = {
  window: { addEventListener() {}, dispatchEvent() {} },
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  },
  location: { origin, hash: cfg.hash || '', pathname: '/', search: '' },
  document: { readyState: 'complete', addEventListener() {}, getElementById: () => null },
  history: { replaceState() {} },
  crypto: globalThis.crypto, Headers, URL, TextEncoder, TextDecoder, console, setTimeout, clearTimeout,
  Request: class extends Request { constructor(input, init) { super(new URL(String(input), origin), init); } },
  btoa, atob, Uint8Array, BigInt, DataView, JSON,
};
ctx.self = ctx; ctx.window.crypto = globalThis.crypto;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(naclPath, 'utf8'), ctx);
const nacl = ctx.nacl = ctx.nacl || ctx.window.nacl;
const server = nacl.box.keyPair();
const identity = nacl.sign.keyPair();
const hex = (b) => Buffer.from(b).toString('hex');
ctx.window.fetch = async (input, init) => {
  const path = String(input);
  asked.push(path);
  if (path.startsWith('/api/auth/handshake')) {
    const expires = 4102444800;
    const eb = Buffer.alloc(8); eb.writeBigUInt64BE(BigInt(expires));
    const signed = Buffer.concat([Buffer.from('sautium-pair:v1'), Buffer.from(server.publicKey), eb]);
    return new Response(JSON.stringify({
      eph: Buffer.from(server.publicKey).toString('base64'), expires,
      node_pubkey: cfg.unsigned ? '' : hex(identity.publicKey),
      sig: cfg.unsigned ? '' : hex(nacl.sign.detached(signed, identity.secretKey)) }));
  }
  const queue = (cfg.replies || {})[path];
  if (!queue) return new Response('{}');
  const env = JSON.parse(init.body);
  const client = Uint8Array.from(Buffer.from(env.client, 'base64'));
  const opened = nacl.box.open(Uint8Array.from(Buffer.from(env.box, 'base64')),
                               Uint8Array.from(Buffer.from(env.nonce, 'base64')), client, server.secretKey);
  (sent[path] = sent[path] || []).push(JSON.parse(Buffer.from(opened).toString()));
  const [status, headers] = queue.shift();
  if (status !== 200) return new Response('{"detail":"x"}', { status, headers: headers || {} });
  const nonce = nacl.randomBytes(24);
  const reply = nacl.box(Buffer.from(JSON.stringify({ token: 'fresh token', public_key_hex: hex(identity.publicKey) })),
                         nonce, client, server.secretKey);
  return new Response(JSON.stringify({ nonce: Buffer.from(nonce).toString('base64'),
                                       box: Buffer.from(reply).toString('base64') }));
};
ctx.window.Sautium = {};
vm.runInContext(fs.readFileSync(shaPath, 'utf8'), ctx);
vm.runInContext(fs.readFileSync(authPath, 'utf8'), ctx);
ctx.fetch = ctx.window.fetch;      // the page's global is its window
(async () => {
  // the boot settled first: a signed request waits for it
  await ctx.window.fetch('/api/settled');
  let result, thrown = null;
  try {
    result = await vm.runInContext(`(async () => { ${cfg.scenario} })()`, ctx);
  } catch (e) {
    thrown = e.constructor.name + ': ' + e.message;
  }
  process.stdout.write(JSON.stringify({ result, thrown, asked, sent, store, node: hex(identity.publicKey) }));
})();
"""


def _page(scenario: str, **cfg):
    """auth.js run in node against a node the driver plays: its handshake
    signed by an identity of its own (`unsigned`: none), each credential
    route answering from `replies` (path → [[status, headers], …]; 200 seals a
    token and the identity's key), `store` as localStorage and `hash` as the
    page's fragment. Returns what `scenario` (an async body) returned or
    threw, the paths asked, the payloads opened per path and the storage."""
    static = ROOT / "backend" / "static"
    proc = subprocess.run(["node", "-e", _PAGE_DRIVER, "--", str(static / "vendor" / "nacl-fast.min.js"),
                           str(static / "sha256.js"), str(static / "auth.js"), "http://node.test:8800",
                           json.dumps({"scenario": scenario, **cfg})],
                          capture_output=True, check=True, timeout=30)
    return json.loads(proc.stdout)


_SIGNED_IN = {"sautium.device_token": "old token"}


def _gate(replies):
    out = _page("return await window.Sautium.auth.login('hunter22')", store=dict(_SIGNED_IN),
                replies={"/api/auth/login": [[status, {"X-Sautium-Auth-Error": kind} if kind else {}]
                                             for status, kind in replies]})
    out["asked"] = [p for p in out["asked"] if p != "/api/settled"]
    return out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_page_seals_a_box_again_once_to_a_fresh_handshake_and_says_busy():
    out = _gate([[400, "channel"], [503, None]])
    assert out["result"] == "The node is busy — try again in a moment."
    assert out["asked"] == ["/api/auth/handshake", "/api/auth/login"] * 2
    out = _gate([[400, "channel"], [400, "channel"]])
    assert out["asked"].count("/api/auth/login") == 2
    out = _gate([[401, None]])
    assert out["result"] is False and out["asked"].count("/api/auth/login") == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_only_a_refused_password_is_said_to_be_wrong():
    for replies in ([[400, "channel"], [400, "channel"]], [[422, None]], [[500, None]]):
        assert _gate(replies)["result"] == "The sign-in did not finish — try again.", replies


@pytest.fixture
def rotations(doors, tmp_path, monkeypatch):
    """The change-identity door over a node with an identity file; the
    rotation's own work — the key's, the witness drain's, what the node is
    told after — recorded or stubbed, not run; the current password is "the
    right one" until a rotation sets another."""
    import db_pool
    import notary
    import p2p_chat
    import p2p_identity
    from desktop import node_identity
    from routers import auth
    from routers import p2p as p2p_router
    done, state = [], {"password": "the right one"}
    (tmp_path / "node_info.json").write_text("{}")
    monkeypatch.setattr(device_auth, "_identity_dir", lambda: tmp_path)
    monkeypatch.setattr(device_auth, "account_configured", lambda: True)
    monkeypatch.setattr(device_auth, "_create_lock", asyncio.Lock())
    monkeypatch.setattr(device_auth, "pin_door", lambda: True)
    monkeypatch.setattr(device_auth, "_doors", {name: {"failures": 0, "shut_until": 0.0, "in_flight": 0}
                                              for name in ("login", "signed")})
    monkeypatch.setattr(device_auth, "_record_failure", lambda: None)
    monkeypatch.setattr(device_auth, "_pin", None)
    monkeypatch.setattr(device_auth, "_pin_prev", None)
    monkeypatch.setattr(device_auth, "_pin_lock", asyncio.Lock())

    def rotate(_d, username, password, anonymous):
        time.sleep(0.05)
        done.append((username, password))
        state["password"] = password
        return {"info": {"username": username, "invite_code": "x", "public_key_hex": "cd" * 32,
                         "anonymous": anonymous}, "rotation": {}}
    monkeypatch.setattr(node_identity, "rotate_identity", rotate)
    monkeypatch.setattr(notary, "drain_witness", lambda: None)
    for module, name in ((p2p_identity, "forget_cached"), (p2p_router, "forget_identity"),
                         (p2p_chat, "forget_service"), (device_auth, "bump_epoch")):
        monkeypatch.setattr(module, name, lambda: None)
    monkeypatch.setattr(db_pool, "db_execute", lambda *a, **k: None)
    monkeypatch.setattr(auth, "_secret", lambda: b"node secret")
    monkeypatch.setattr(device_auth, "current_token", lambda _s: "fresh token")
    asked = []

    async def verify(password, **kw):
        asked.append(kw.get("signed", False))
        await asyncio.sleep(0.01)
        return password == state["password"]
    monkeypatch.setattr(device_auth, "verify_password", verify)

    def change(**payload):
        return doors.post("/api/auth/change-identity",
                          json=_seal(doors.get("/api/auth/handshake").json(), payload))
    change.asked = asked
    return change, done


def test_two_changes_at_once_each_need_the_password_of_the_identity_they_change(rotations, doors,
                                                                                 monkeypatch):
    import httpx
    _change, done = rotations
    monkeypatch.setattr(device_auth, "account_anonymous", lambda: False)

    async def both():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=doors.app),
                                     base_url="http://node") as client:
            async def change(username, password):
                handshake = (await client.get("/api/auth/handshake")).json()
                return (await client.post("/api/auth/change-identity", json=_seal(handshake, {
                    "username": username, "password": password,
                    "current_password": "the right one"}))).status_code
            return await asyncio.gather(change("second", "the second one"),
                                        change("third", "the third one"))
    assert sorted(asyncio.run(both())) == [200, 403]
    assert len(done) == 1


def test_a_docker_account_says_it_has_a_password(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    monkeypatch.setattr(device_auth, "_identity_dir", lambda: tmp_path)
    info = device_auth._write_identity(tmp_path, "anonymous-1a2b", "a chosen password")
    assert info["anonymous"] is False
    assert json.loads((tmp_path / "node_info.json").read_text())["anonymous"] is False
    assert device_auth.account_anonymous() is False


def test_a_named_account_changes_only_with_its_current_password(rotations, monkeypatch):
    change, done = rotations
    monkeypatch.setattr(device_auth, "account_anonymous", lambda: False)
    for wrong in ({}, {"current_password": ""}, {"current_password": "a guess"}):
        r = change(username="second", password="new password", **wrong)
        assert r.status_code == 403, wrong
    assert done == []
    assert change(username="second", password="new password",
                  current_password="the right one").status_code == 200
    assert done == [("second", "new password")]


def test_an_account_without_a_password_changes_on_the_code_its_host_shows(rotations, monkeypatch):
    """An anonymous account has no password anyone knows: its host vouches
    for the change by the pairing code it shows now, carried in the box."""
    change, done = rotations
    monkeypatch.setattr(device_auth, "account_anonymous", lambda: True)
    code = device_auth.issue_pin()
    for wrong in ({}, {"pin": ""}, {"pin": "AAAA-AAAA"}, {"current_password": "the right one"}):
        assert change(username="named", password="a password now", **wrong).status_code == 403, wrong
    assert done == [] and change.asked == []
    assert change(username="named", password="a password now", pin=code).status_code == 200
    assert done == [("named", "a password now")]


def test_a_node_whose_host_shows_no_code_keeps_a_password(rotations, doors, monkeypatch):
    """A node without a pairing code (Docker) is signed into by its password:
    a change to none is refused before anything is derived, and an account
    is never created without one."""
    change, done = rotations
    monkeypatch.setattr(device_auth, "account_anonymous", lambda: False)
    monkeypatch.setattr(device_auth, "pin_door", lambda: False)
    r = change(username="second", password="", current_password="the right one")
    assert r.status_code == 422 and done == [] and change.asked == []
    assert change(username="second", password="new password",
                  current_password="the right one").status_code == 200
    r = doors.post("/api/auth/create-account", json=_seal(doors.get("/api/auth/handshake").json(),
                                                          {"username": "owner", "password": "",
                                                           "setup_code": "x"}))
    assert r.status_code == 422


def test_a_busy_node_says_so_at_the_change(rotations, monkeypatch):
    change, done = rotations
    monkeypatch.setattr(device_auth, "account_anonymous", lambda: False)

    async def busy(_password, *, signed=False):
        raise device_auth.Busy()
    monkeypatch.setattr(device_auth, "verify_password", busy)
    assert change(username="second", current_password="the right one").status_code == 503
    assert done == []


def test_the_change_asks_its_derivation_as_a_signed_caller(rotations, monkeypatch):
    change, _done = rotations
    monkeypatch.setattr(device_auth, "account_anonymous", lambda: False)
    assert change(username="second", password="new password",
                  current_password="the right one").status_code == 200
    assert change.asked == [True]


def test_an_identity_from_the_environment_is_changed_there_before_any_password_is_derived(
        rotations, tmp_path, monkeypatch):
    change, done = rotations
    monkeypatch.setattr(device_auth, "account_anonymous", lambda: False)
    (tmp_path / "node_info.json").unlink()
    r = change(username="second", password="new password", current_password="a guess")
    assert r.status_code == 409 and "environment" in r.json()["detail"]
    assert change.asked == [] and done == []


@pytest.fixture
def fresh_node(tmp_path, monkeypatch):
    """A node with no account: its identity folder `tmp_path`, empty."""
    from config import settings
    monkeypatch.setattr(settings, "p2p_identity_dir", str(tmp_path))
    monkeypatch.setattr(settings, "p2p_username", "")
    monkeypatch.setattr(device_auth, "_setup_code", None)
    monkeypatch.setattr(device_auth, "_create_lock", asyncio.Lock())
    monkeypatch.setattr(device_auth, "_expected_pubkey",
                        lambda: "ab" * 32 if (tmp_path / "node_ed25519.key").exists() else None)
    monkeypatch.setattr(device_auth, "_identity_dir", lambda: tmp_path)
    return tmp_path


def test_a_fresh_node_is_set_up_only_with_the_code_it_wrote_at_its_start(fresh_node, monkeypatch, caplog):
    """Its log and its identity folder hold the code — the folder's file for
    the node's user alone — and nothing is created without it; the code
    dies with the account."""
    written = []
    monkeypatch.setattr(device_auth, "_write_identity", lambda d, u, p: (
        written.append(u), (d / "node_ed25519.key").write_text("k"),
        (d / "node_info.json").write_text(json.dumps({"username": u})), {"username": u})[3])
    with caplog.at_level("INFO", logger="device_auth"):
        device_auth.prepare_setup()
    path = fresh_node / device_auth.SETUP_CODE_FILE
    code = path.read_text().strip()
    assert code and code in caplog.text and path.stat().st_mode & 0o077 == 0
    for wrong in ("", "AAAA-AAAA-AAAA", code[:-1]):
        with pytest.raises(device_auth.WrongSetupCode):
            asyncio.run(device_auth.create_account("owner", "a password", wrong))
    assert written == []
    asyncio.run(device_auth.create_account("owner", "a password", code.lower()))
    assert written == ["owner"] and not path.exists()
    with pytest.raises(device_auth.AccountExists):
        asyncio.run(device_auth.create_account("other", "a password", code))
    for name in ("node_ed25519.key", "node_info.json"):   # the folder emptied while it runs
        (fresh_node / name).unlink()
    with pytest.raises(device_auth.WrongSetupCode):
        asyncio.run(device_auth.create_account("other", "a password", code))
    (fresh_node / "node_ed25519.key").write_text("k")     # a key with no name is an identity
    with caplog.at_level("WARNING", logger="device_auth"):
        device_auth.prepare_setup()
    assert not path.exists() and "no account can be created over it" in caplog.text
    assert device_auth.identity_key_on_disk()
    with pytest.raises(device_auth.AccountExists):
        asyncio.run(device_auth.create_account("other", "a password", code))


def test_the_setup_code_is_read_out_of_what_was_pasted_around_it(fresh_node, monkeypatch):
    """A line of the log, spaces, another layout's letters: the code inside
    is taken, nothing else is, and nothing is a server error."""
    monkeypatch.setattr(device_auth, "_write_identity", lambda d, u, p: {"username": u})
    device_auth.prepare_setup()
    code = device_auth._setup_code
    for given in ("\u0410\u0412\u0421\u0415-\u041d\u041a\u041c\u0420-\u0422\u0425\u0410\u0412", "", " - "):
        with pytest.raises(device_auth.WrongSetupCode):
            asyncio.run(device_auth.create_account("owner", "a password", given))
    pasted = f"backend-1 | WARNING No account yet. The setup code to create it is {code} (also in …)"
    assert asyncio.run(device_auth.create_account("owner", "a password", pasted)) == {"username": "owner"}


def test_the_set_up_door_refuses_a_code_that_is_not_the_nodes(fresh_node, doors, monkeypatch):
    monkeypatch.setattr(device_auth, "_write_identity", lambda *a: pytest.fail("created"))
    device_auth.prepare_setup()
    r = doors.post("/api/auth/create-account", json=_seal(doors.get("/api/auth/handshake").json(), {
        "username": "owner", "password": "a password", "setup_code": "AAAA-AAAA-AAAA"}))
    assert r.status_code == 403


@pytest.fixture
def login_door(doors, monkeypatch):
    """The password door with its clock in hand (`clock[0]`), its count
    fresh, the owner's record in memory, and every derivation counted."""
    import db_pool
    import routers.settings as settings_router
    from routers import auth
    clock, derived, store, woken = [1000.0], [], {}, []
    monkeypatch.setattr(auth, "_secret", lambda: b"node secret")
    monkeypatch.setattr(device_auth, "current_token", lambda _s: "fresh token")
    monkeypatch.setattr(device_auth, "account_configured", lambda: True)
    monkeypatch.setattr(device_auth, "account_anonymous", lambda: False)
    monkeypatch.setattr(device_auth, "_doors", {name: {"failures": 0, "shut_until": 0.0, "in_flight": 0}
                                              for name in ("login", "signed")})
    monkeypatch.setattr(device_auth.time, "time", lambda: clock[0])
    monkeypatch.setattr(settings_router, "_read", lambda key: store.get(key))
    monkeypatch.setattr(settings_router, "_write", lambda key, value: store.__setitem__(key, value))
    monkeypatch.setattr(db_pool, "db_execute", lambda sql, *a: woken.append(sql))

    async def verify(password, **_kw):
        derived.append(password)
        await asyncio.sleep(0.05)                     # a derivation takes its time
        return password == "the right one"
    monkeypatch.setattr(device_auth, "verify_password", verify)

    def login(password):
        return doors.post("/api/auth/login", json=_seal(doors.get("/api/auth/handshake").json(),
                                                         {"password": password}))
    return login, clock, derived, store, woken


def test_wrong_passwords_shut_the_login_door_for_a_growing_delay(login_door):
    """The free wrong passwords are told as wrong; past them the door is
    shut, a try meanwhile refused before anything is derived and told how
    long to wait, a longer wait after each further wrong one; a right
    password clears the count."""
    login, clock, derived, _store, _woken = login_door
    for _ in range(device_auth.LOGIN_FREE_FAILURES):
        assert login("a guess").status_code == 401
    assert login("a guess").status_code == 401           # one past the free ones: the door shuts
    r = login("the right one")
    assert r.status_code == 429 and r.headers["retry-after"] == "1"
    assert len(derived) == device_auth.LOGIN_FREE_FAILURES + 1
    clock[0] += 1.5
    assert login("a guess").status_code == 401
    r = login("a guess")
    assert r.status_code == 429 and r.headers["retry-after"] == "2"
    clock[0] += 2.5
    assert login("the right one").status_code == 200
    assert login("a guess").status_code == 401 and login("a guess").status_code == 401


def test_the_owner_is_told_of_wrong_passwords_since_the_notice_was_seen(login_door):
    login, clock, _derived, store, woken = login_door
    for _ in range(3):
        login("a guess")
    record = store[device_auth.LOGIN_FAILURES_KEY]
    assert record["count"] == 3 and woken == ["NOTIFY sautium_notices"] * 3
    store["notice.seen"] = {"auth.login_failures": record["last"]}
    clock[0] += 60
    login("a guess")
    fresh = store[device_auth.LOGIN_FAILURES_KEY]
    assert fresh["count"] == 1 and fresh["since"] != record["since"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_page_says_how_long_a_shut_door_waits():
    out = _gate([[429, None]])
    assert out["result"] is not False and "wrong password" in out["result"]
    out = _page("return await window.Sautium.auth.login('x')", store=dict(_SIGNED_IN),
                replies={"/api/auth/login": [[429, {"Retry-After": "300"}]]})
    assert out["result"] == "Too many wrong passwords — the next try is taken in 5 min."


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_page_opened_from_the_host_carries_its_code_into_an_identity_change():
    """The pairing code the host opened the page with signs it in and, for
    this load, is what a change of an account with no password carries."""
    out = _page("return await window.Sautium.auth.changeIdentity('named', 'a password now', '')",
                hash="#pair=ABCD-EFGH",
                replies={"/api/auth/pair": [[200]], "/api/auth/change-identity": [[200]]})
    assert out["thrown"] is None and out["result"]["ok"] is True
    assert out["sent"]["/api/auth/pair"] == [{"code": "ABCD-EFGH"}]
    assert out["sent"]["/api/auth/change-identity"][0]["pin"] == "ABCD-EFGH"
    out = _page("return await window.Sautium.auth.changeIdentity('named', 'a password now', '')",
                store=dict(_SIGNED_IN), replies={"/api/auth/change-identity": [[200]]})
    assert out["sent"]["/api/auth/change-identity"][0]["pin"] == ""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_node_with_no_identity_signs_no_browser_in_and_pins_nothing():
    """An answer without an identity is refused at the password and pairing
    doors before anything is sent; creating an account takes it — asking
    first where this browser knew another node, and pinning the new
    account's key once it is made."""
    for scenario, door in (("return await window.Sautium.auth.login('x')", "/api/auth/login"),
                           ("return await window.Sautium.auth.pair('ABCD-EFGH')", "/api/auth/pair")):
        out = _page(scenario, unsigned=True, store=dict(_SIGNED_IN), replies={door: [[200]]})
        assert out["thrown"] == "NodeUnset: This node presents no identity — it is not set up yet.", out
        assert door not in out["asked"] and "sautium.node_pubkey" not in out["store"]
    create = "return await window.Sautium.auth.createAccount('owner', 'a password', 'AAAA-BBBB-CCCC')"
    out = _page(create, unsigned=True, store=dict(_SIGNED_IN), replies={"/api/auth/create-account": [[200]]})
    assert out["result"] is True and out["store"]["sautium.node_pubkey"] == out["node"]
    assert out["sent"]["/api/auth/create-account"][0]["setup_code"] == "AAAA-BBBB-CCCC"
    known = {**_SIGNED_IN, "sautium.node_pubkey": "cd" * 32}
    out = _page(create, unsigned=True, store=known, replies={"/api/auth/create-account": [[200]]})
    assert out["thrown"] == "NodeChanged: node identity changed"
    assert "/api/auth/create-account" not in out["asked"]
    out = _page("window.Sautium.auth.acceptNode(''); " + create, unsigned=True, store=known,
                replies={"/api/auth/create-account": [[200]]})
    assert out["result"] is True and out["store"]["sautium.node_pubkey"] == out["node"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_pin_is_a_key_or_nothing():
    """A stored value of another shape pins no node, and no other shape is
    ever written."""
    out = _page("return await window.Sautium.auth.login('x')",
                store={**_SIGNED_IN, "sautium.node_pubkey": "not a key"},
                replies={"/api/auth/login": [[200]]})
    assert out["result"] is True and out["store"]["sautium.node_pubkey"] == out["node"]
    out = _page("window.Sautium.auth.acceptNode('not a key'); return null",
                store={**_SIGNED_IN, "sautium.node_pubkey": "cd" * 32})
    assert "sautium.node_pubkey" not in out["store"]


def test_a_refusal_at_the_credential_doors_names_the_field_never_its_value(doors):
    r = doors.post("/api/auth/create-account", json=_seal(doors.get("/api/auth/handshake").json(), {
        "username": "owner", "password": "pw4321", "setup_code": "x"}))
    assert r.status_code == 422 and "pw4321" not in r.text


def test_the_pairing_code_is_the_hosts_to_read(tmp_path, monkeypatch):
    """A request a browser's device token signed is refused the code — the
    code vouches for a browser, and for an account's identity change — and
    the host's own signature reads it."""
    import collections

    import auth_hmac
    from routers import auth
    from test_request_signing import TOKEN, signed
    monkeypatch.setattr(device_auth, "current_token", lambda secret: TOKEN)
    monkeypatch.setattr(auth_hmac, "_allowed_hosts", {"testserver"})
    monkeypatch.setattr(auth_hmac, "_seen", type(auth_hmac._seen)())
    monkeypatch.setattr(auth_hmac, "_seen_wall", [])
    monkeypatch.setattr(auth_hmac, "_seen_order", collections.deque())
    monkeypatch.setattr(device_auth, "_pin", None)
    app = FastAPI()
    app.include_router(auth.router)
    app.add_middleware(auth_hmac.HMACAuthMiddleware, secret_path=tmp_path / ".api_secret")
    client = TestClient(app)
    secret = auth_hmac.ensure_secret(tmp_path / ".api_secret")
    assert client.get("/api/auth/pin", headers=signed("GET", "/api/auth/pin")).status_code == 403
    assert client.get("/api/auth/pin/stream", headers=signed("GET", "/api/auth/pin/stream")).status_code == 403
    assert device_auth._pin is None                   # the refused read minted nothing
    r = client.get("/api/auth/pin", headers=signed("GET", "/api/auth/pin", key=secret))
    assert r.status_code == 200 and r.json()["code"] == device_auth._pin["code"]


def test_tries_at_once_past_the_free_failures_are_let_in_one_at_a_time(login_door, doors):
    import httpx
    _login, _clock, derived, _store, _woken = login_door
    device_auth._doors["login"]["failures"] = device_auth.LOGIN_FREE_FAILURES

    async def at_once():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=doors.app),
                                     base_url="http://node") as client:
            async def guess():
                handshake = (await client.get("/api/auth/handshake")).json()
                return (await client.post("/api/auth/login",
                                          json=_seal(handshake, {"password": "a guess"}))).status_code
            return await asyncio.gather(*(guess() for _ in range(3)))
    assert sorted(asyncio.run(at_once())) == [401, 429, 429]
    assert len(derived) == 1


def test_only_a_derivation_that_did_not_match_is_a_wrong_password(login_door, monkeypatch):
    """An empty password, or a node with no password door, is refused for
    nothing; a derivation that failed is a busy node — none is counted."""
    login, _clock, derived, store, _woken = login_door
    assert login("").status_code == 401 and derived == []
    monkeypatch.setattr(device_auth, "account_anonymous", lambda: True)
    assert login("a guess").status_code == 401 and derived == []
    monkeypatch.setattr(device_auth, "account_anonymous", lambda: False)

    async def failing(password, **_kw):
        raise device_auth.Busy()
    monkeypatch.setattr(device_auth, "verify_password", failing)
    assert login("the right one").status_code == 503
    assert device_auth._doors["login"] == {"failures": 0, "shut_until": 0.0, "in_flight": 0}
    assert device_auth.LOGIN_FAILURES_KEY not in store


def test_a_derivation_that_fails_says_the_node_is_busy(monkeypatch):
    _account(monkeypatch, anonymous=False)

    def failing(*_a):
        raise MemoryError("no room for the derivation")
    monkeypatch.setattr(p2p_identity, "derive_identity", failing)
    with pytest.raises(device_auth.Busy):
        asyncio.run(device_auth.verify_password("the right one"))


def test_a_crowd_at_the_login_door_never_turns_a_signed_change_away(rotations, doors, monkeypatch):
    """Wrong passwords at the login shut the login's door; an identity
    change's current password passes a door of its own, under the same rule:
    its own wrong ones shut it, and the login's crowd never does."""
    change, done = rotations
    monkeypatch.setattr(device_auth, "account_anonymous", lambda: False)
    monkeypatch.setattr(device_auth.time, "time", lambda: 1000.0)
    for _ in range(device_auth.LOGIN_FREE_FAILURES + 1):
        doors.post("/api/auth/login", json=_seal(doors.get("/api/auth/handshake").json(),
                                                 {"password": "a guess"}))
    shut = doors.post("/api/auth/login", json=_seal(doors.get("/api/auth/handshake").json(),
                                                    {"password": "the right one"}))
    assert shut.status_code == 429                    # the login's door is shut
    assert change(username="second", password="new password",
                  current_password="the right one").status_code == 200
    for _ in range(device_auth.LOGIN_FREE_FAILURES + 1):
        assert change(username="third", password="new password", current_password="a guess").status_code == 403
    r = change(username="third", password="new password", current_password="new password")
    assert r.status_code == 429 and r.headers["retry-after"] == "1" and done == [("second", "new password")]


def test_a_try_let_in_holds_the_door_until_its_answer_however_long_it_takes(login_door, monkeypatch):
    """Past the free failures one try at a time: the next waits for the
    first's answer, not for a clock the first outlives — and one the node
    could not check holds nothing after it."""
    _login, clock, derived, _store, _woken = login_door
    device_auth._doors["login"]["failures"] = device_auth.LOGIN_FREE_FAILURES
    release = []

    async def slow(password, **_kw):
        derived.append(password)
        gate = asyncio.Event()
        release.append(gate)
        await gate.wait()
        raise device_auth.Busy()
    monkeypatch.setattr(device_auth, "verify_password", slow)

    async def scenario():
        first = asyncio.ensure_future(device_auth.password_tried("a guess"))
        await asyncio.sleep(0.01)
        clock[0] += 3600                                  # longer than any hold the clock could give
        with pytest.raises(device_auth.Shut):
            await asyncio.wait_for(device_auth.password_tried("another guess"), 2)
        release[0].set()
        with pytest.raises(device_auth.Busy):
            await first
        second = asyncio.ensure_future(device_auth.password_tried("another guess"))
        await asyncio.sleep(0.01)
        release[1].set()
        with pytest.raises(device_auth.Busy):
            await asyncio.wait_for(second, 2)
    asyncio.run(scenario())
    assert derived == ["a guess", "another guess"]


def test_a_change_without_the_hosts_code_costs_its_code_nothing(rotations, monkeypatch):
    change, _done = rotations
    monkeypatch.setattr(device_auth, "account_anonymous", lambda: True)
    device_auth.issue_pin()
    left = device_auth.pin_state()["attempts_left"]
    assert change(username="named", password="a password now").status_code == 403
    assert device_auth.pin_state()["attempts_left"] == left
    assert change(username="named", password="a password now", pin="AAAA-AAAA").status_code == 403
    assert device_auth.pin_state()["attempts_left"] == left - 1


def test_the_owners_notice_of_wrong_passwords_is_dropped_once_seen(monkeypatch):
    import routers.settings as settings_router
    store = {device_auth.LOGIN_FAILURES_KEY: {"count": 2, "since": "2026-11-05T10:00:00+00:00",
                                              "last": "2026-11-05T10:05:00+00:00"}}
    monkeypatch.setattr(settings_router, "_read", lambda key: store.get(key))
    notice = settings_router._login_failures_notice()
    assert notice["since"] == "2026-11-05T10:05:00+00:00" and notice["data"]["count"] == 2
    monkeypatch.setattr(settings_router, "_notice_items", lambda: [{**notice, "seen": False}])
    assert [n["key"] for n in settings_router._notices_state()["items"]] == ["auth.login_failures"]
    monkeypatch.setattr(settings_router, "_notice_items", lambda: [{**notice, "seen": True}])
    assert settings_router._notices_state()["items"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_setting_up_a_node_keeps_the_pin_until_the_account_is_made():
    """"Set it up" on a node with no identity: a creation the node refuses
    leaves the browser pinned to the node it knew."""
    known = {**_SIGNED_IN, "sautium.node_pubkey": "cd" * 32}
    create = "return await window.Sautium.auth.createAccount('owner', 'a password', 'AAAA-BBBB-CCCC')"
    out = _page("window.Sautium.auth.acceptNode(''); " + create, unsigned=True, store=known,
                replies={"/api/auth/create-account": [[403]]})
    assert out["result"] != True and out["store"]["sautium.node_pubkey"] == "cd" * 32  # noqa: E712


def test_a_free_try_answered_never_lets_two_in_past_the_free_failures(login_door, monkeypatch):
    """Two free tries in flight as the count reaches the free ones: after
    the first's answer no try is let in beside the second, and the one let in
    after the second's answer is the only one in — the clock moved past any
    shut, the next still waits."""
    _login, clock, derived, _store, _woken = login_door
    device_auth._doors["login"]["failures"] = device_auth.LOGIN_FREE_FAILURES - 1
    gates = []

    async def slow(password, **_kw):
        derived.append(password)
        gate = asyncio.Event()
        gates.append(gate)
        await gate.wait()
        return False
    monkeypatch.setattr(device_auth, "verify_password", slow)

    async def scenario():
        a = asyncio.ensure_future(device_auth.password_tried("a"))
        b = asyncio.ensure_future(device_auth.password_tried("b"))
        await asyncio.sleep(0.01)
        gates[0].set()
        await asyncio.wait_for(a, 2)                      # the count reaches the free ones
        with pytest.raises(device_auth.Shut):             # b is still in flight
            await asyncio.wait_for(device_auth.password_tried("c"), 2)
        gates[1].set()
        await asyncio.wait_for(b, 2)
        clock[0] += 3600
        c = asyncio.ensure_future(device_auth.password_tried("c"))
        await asyncio.sleep(0.01)
        clock[0] += 3600
        with pytest.raises(device_auth.Shut):             # c is in flight
            await asyncio.wait_for(device_auth.password_tried("d"), 2)
        gates[2].set()
        await asyncio.wait_for(c, 2)
    asyncio.run(scenario())
    assert derived == ["a", "b", "c"]


def test_no_setup_is_offered_over_a_key_without_an_account(fresh_node, doors):
    (fresh_node / "node_ed25519.key").write_text("k")
    status = doors.get("/api/auth/status").json()
    assert status["onboarding"] is False and status["unnamed_key"] is True


_GATE_PAGE = r"""
const fs = require('node:fs'), vm = require('node:vm');
const [naclPath, shaPath, authPath, status] = process.argv.slice(-4);
const made = {};
function el(tag) {
  const e = { tag, hidden: false, textContent: '', innerHTML: '', disabled: false, children: [],
              listeners: {}, value: '',
              addEventListener(k, f) { this.listeners[k] = f; }, focus() {}, showModal() {},
              appendChild(c) { this.children.push(c); } };
  e.querySelector = (sel) => {
    const id = sel.replace(/^#/, '');
    if (!made[id]) made[id] = el('x');
    return made[id];
  };
  return e;
}
const ctx = {
  window: { addEventListener() {}, dispatchEvent() {} },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  location: { origin: 'http://node.test', hash: '', pathname: '/', search: '' },
  document: { readyState: 'loading', addEventListener() {}, getElementById: () => null,
              createElement: (t) => el(t), body: el('body') },
  history: { replaceState() {} }, crypto: globalThis.crypto, Headers, URL, TextEncoder, TextDecoder,
  console, setTimeout, clearTimeout, Request, btoa, atob, Uint8Array, BigInt, DataView, JSON,
};
ctx.self = ctx; ctx.window.crypto = globalThis.crypto;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(naclPath, 'utf8'), ctx);
ctx.nacl = ctx.nacl || ctx.window.nacl;
ctx.window.fetch = async () => new Response(status);
ctx.window.Sautium = {};
vm.runInContext(fs.readFileSync(shaPath, 'utf8'), ctx);
vm.runInContext(fs.readFileSync(authPath, 'utf8'), ctx);
ctx.window.Sautium.auth.showLoginGate();
setTimeout(async () => {
  const msg = made['auth-gate-msg'].textContent;
  await made['auth-gate-submit'].listeners.click?.();
  process.stdout.write(JSON.stringify({ msg, after: made['auth-gate-msg'].textContent,
                                        actions: made['auth-gate-form-actions'].hidden }));
}, 50);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("status, says", [
    ({"onboarding": False, "password_login": False, "pin_door": False, "unnamed_key": True,
      "username": None}, "holds a key without its account"),
    ({"onboarding": False, "password_login": False, "pin_door": False, "unnamed_key": False,
      "username": "anonymous-1a2b"}, "loses this identity"),
])
def test_a_node_no_browser_can_sign_in_to_says_why_and_offers_no_form(status, says):
    """A key with no account, or an account with no password where no host
    shows a code: the gate says which, what starting over costs, and offers
    nothing to submit — a press changes nothing."""
    static = ROOT / "backend" / "static"
    proc = subprocess.run(["node", "-e", _GATE_PAGE, "--", str(static / "vendor" / "nacl-fast.min.js"),
                           str(static / "sha256.js"), str(static / "auth.js"), json.dumps(status)],
                          capture_output=True, check=True, timeout=30)
    out = json.loads(proc.stdout)
    assert says in out["msg"] and out["after"] == out["msg"] and out["actions"] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_change_sheet_says_how_long_a_shut_door_waits():
    out = _page("return await window.Sautium.auth.changeIdentity('second', 'new password', 'a guess')",
                store=dict(_SIGNED_IN), replies={"/api/auth/change-identity": [[429, {"Retry-After": "4"}]]})
    assert out["result"] == {"ok": False, "error": "Too many wrong passwords — the next try is taken in 4 s."}
