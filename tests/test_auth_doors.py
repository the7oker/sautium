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
    monkeypatch.setattr(device_auth, "_derive_in_flight", 0, raising=False)
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
    async def busy(_password):
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


_GATE_DRIVER = r"""
const fs = require('node:fs'), vm = require('node:vm');
const [naclPath, shaPath, authPath, origin, answers] = process.argv.slice(-5);
const replies = JSON.parse(answers);
const asked = [];
const ctx = {
  window: { addEventListener() {}, dispatchEvent() {} },
  // a browser already signed in: the boot shows no gate
  localStorage: { getItem: (k) => (k === 'sautium.device_token' ? 'old token' : null), setItem() {}, removeItem() {} },
  location: { origin, hash: '', pathname: '/', search: '' },
  document: { readyState: 'complete', addEventListener() {}, getElementById: () => null },
  history: { replaceState() {} },
  crypto: globalThis.crypto, Headers, URL, TextEncoder, TextDecoder, console, setTimeout, clearTimeout,
  Request: class extends Request { constructor(input, init) { super(new URL(String(input), origin), init); } },
  btoa, atob, Uint8Array, BigInt, DataView, JSON,
};
ctx.self = ctx; ctx.window.crypto = globalThis.crypto;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(naclPath, 'utf8'), ctx);
ctx.nacl = ctx.nacl || ctx.window.nacl;
const server = ctx.nacl.box.keyPair();
ctx.window.fetch = async (input) => {
  const path = String(input);
  asked.push(path);
  if (path.startsWith('/api/auth/handshake')) {
    const eph = Buffer.from(server.publicKey).toString('base64');
    return new Response(JSON.stringify({ eph, expires: 4102444800, node_pubkey: '', sig: '' }));
  }
  const [status, kind] = replies.shift();
  return new Response('{"detail":"x"}', { status, headers: kind ? { 'X-Sautium-Auth-Error': kind } : {} });
};
ctx.window.Sautium = {};
vm.runInContext(fs.readFileSync(shaPath, 'utf8'), ctx);
vm.runInContext(fs.readFileSync(authPath, 'utf8'), ctx);
(async () => {
  const result = await ctx.window.Sautium.auth.login('hunter22');
  process.stdout.write(JSON.stringify({ result, asked }));
})();
"""


def _gate(replies):
    static = ROOT / "backend" / "static"
    proc = subprocess.run(["node", "-e", _GATE_DRIVER, "--", str(static / "vendor" / "nacl-fast.min.js"),
                           str(static / "sha256.js"), str(static / "auth.js"), "http://node.test:8800",
                           json.dumps(replies)], capture_output=True, check=True, timeout=30)
    return json.loads(proc.stdout)


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
    # an anonymous account has no password anyone knows to type again
    monkeypatch.setattr(device_auth, "account_anonymous", lambda: True)
    assert change(username="named", password="a password now").status_code == 200
    assert done[-1] == ("named", "a password now")


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
