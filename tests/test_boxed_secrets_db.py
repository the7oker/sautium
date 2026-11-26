"""B-93: every secret typed in the Web UI rides the credential box, and the
rare changes that act outside the node and outlast "Sign out everywhere" take
the account's password (routers/auth.boxed / step_up, device_auth.step_up and
its grants). Through the routes themselves, on a real PostgreSQL built from
the migrations, the password checked by a real (cheap) derivation at the
signed door, and the email flow through the REAL Worker code
(tests/p2p/worker_harness.mjs in serve mode):

- a submit with the box lands the value; the same body unboxed is refused and
  stores nothing, echoing nothing; a replayed box is refused;
- the AI key, the email, an invite token with rights and both agents'
  sign-ins refuse a valid box without the password, and with a wrong one
  store nothing; an anonymous account vouches by its host's code instead;
- the later steps of a flow (the email's code, a sign-in's code) take the
  grant the first step sealed to its browser, and nothing else; the Codex
  device code leaves the state every browser reads in the clear;
- a token patch that widens what a token with rights grants asks the
  password, decided again under the row's lock;
- no secret appears in a log line at any level."""

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
nacl_signing = pytest.importorskip("nacl.signing")
pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from nacl.public import Box, PrivateKey, PublicKey  # noqa: E402

import claude_code  # noqa: E402
import codex_cli  # noqa: E402
import db_pool  # noqa: E402
import device_auth  # noqa: E402
import p2p_identity  # noqa: E402
from config import settings  # noqa: E402
from desktop import node_identity  # noqa: E402
from routers import auth as auth_router  # noqa: E402
from routers import p2p  # noqa: E402
from routers import settings as settings_router  # noqa: E402

HARNESS = Path(__file__).resolve().parent / "p2p" / "worker_harness.mjs"

PASSWORD = "the right one"
API_KEY = "sk-ant-b93-throwaway-" + os.urandom(8).hex()
CLAUDE_CODE = "b93-oauth-code-" + os.urandom(6).hex()
DEVICE_CODE = "B93X-" + os.urandom(2).hex().upper()


# -- the box, as auth.js seals it ------------------------------------------

def _seal(client, payload):
    """A fresh handshake and `payload` sealed to it; `open(reply)` reads the
    sealed answer."""
    hs = client.get("/api/auth/handshake").json()
    key = PrivateKey.generate()
    box = Box(key, PublicKey(base64.b64decode(hs["eph"])))
    sealed = box.encrypt(json.dumps(payload).encode())
    b64 = lambda raw: base64.b64encode(raw).decode()  # noqa: E731
    envelope = {"eph": hs["eph"], "client": b64(bytes(key.public_key)),
                "nonce": b64(sealed.nonce), "box": b64(sealed.ciphertext)}

    def open_(response):
        env = response.json()
        said = json.loads(box.decrypt(base64.b64decode(env["box"]), base64.b64decode(env["nonce"])))
        said.pop("_pad")                     # the bucket's padding (device_auth.padded)
        return said
    return envelope, open_


def _send(client, method, path, payload):
    envelope, open_ = _seal(client, payload)
    r = client.request(method, path, json=envelope)
    return r, (open_(r) if r.status_code == 200 else None)


# -- the node ----------------------------------------------------------------

def _post(url, path, body):
    req = urllib.request.Request(url + path, method="POST", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=10).read())
    except urllib.error.HTTPError as e:
        return {"status": e.code, **json.loads(e.read())}


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


@pytest.fixture
def node(scratch_dsn, tmp_path, monkeypatch, caplog):
    """The auth, settings and P2P routes over this module's database; an
    account "owner" whose password is PASSWORD, checked by its sign-in hash
    (Argon2 made cheap) at the signed door; a node_info.json in a tmp
    identity folder; every log line kept at DEBUG."""
    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port, postgres_user=url.username,
                           postgres_password=url.password, postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    db_pool.close_pool()
    with psycopg2.connect(scratch_dsn) as c, c.cursor() as cur:
        cur.execute("TRUNCATE invite_tokens CASCADE")
        cur.execute("DELETE FROM user_settings WHERE key LIKE 'ai.%%' OR key = %s",
                    (device_auth.LOGIN_FAILURES_KEY,))
        cur.execute("UPDATE user_profile SET email_verified = FALSE WHERE id = 1")
        cur.execute("""INSERT INTO user_settings (key, value) VALUES ('ai.provider', '"anthropic"')""")
    # PUT /ai/key overlays the key on the process's settings, as a node does
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    from desktop import node_identity
    pubkey = "ab" * 32
    node_identity.write_account(tmp_path, {**node_identity.new_account("owner", PASSWORD, source="form"),
                                           "pubkey": pubkey})
    monkeypatch.setattr(device_auth, "_expected_pubkey", lambda: pubkey)
    monkeypatch.setattr(device_auth, "_doors", {name: {"failures": 0, "shut_until": 0.0, "in_flight": 0}
                                              for name in ("login", "signed")})
    monkeypatch.setattr(device_auth, "_derive_in_flight", 0)
    monkeypatch.setattr(device_auth, "_grants", {})
    monkeypatch.setattr(p2p_identity, "load_signing_key", lambda settings: None)
    monkeypatch.setattr(p2p, "_get_identity", lambda: None)
    device_auth._channels.clear()
    (tmp_path / "node_info.json").write_text(json.dumps({"email": "", "email_verified": False}))
    monkeypatch.setattr(settings, "p2p_identity_dir", str(tmp_path))
    caplog.set_level(logging.DEBUG)
    app = FastAPI()
    for module in (auth_router, settings_router, p2p):
        app.include_router(module.router)
    client = TestClient(app)
    yield SimpleNamespace(client=client, dsn=scratch_dsn, dir=tmp_path, caplog=caplog)
    db_pool.close_pool()


def _setting(dsn, key):
    with psycopg2.connect(dsn) as c, c.cursor() as cur:
        cur.execute("SELECT value FROM user_settings WHERE key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else None


def _tokens(dsn):
    with psycopg2.connect(dsn) as c, c.cursor() as cur:
        cur.execute("""SELECT t.label, t.max_uses,
                              ARRAY(SELECT p2p_right::text FROM invite_token_rights r
                                    WHERE r.token_id = t.id ORDER BY 1)
                       FROM invite_tokens t ORDER BY t.created_at""")
        return cur.fetchall()


def _no_secret_in(node, *texts):
    for secret in (API_KEY, PASSWORD, CLAUDE_CODE, DEVICE_CODE):
        assert secret not in node.caplog.text
        for text in texts:
            assert secret not in text


# -- the AI key ----------------------------------------------------------------

def test_the_key_lands_through_the_box_with_the_password_and_comes_back_masked(node):
    r, answer = _send(node.client, "PUT", "/api/settings/ai/key", {"api_key": API_KEY, "password": PASSWORD})
    assert r.status_code == 200
    assert _setting(node.dsn, "ai.api_key") == API_KEY
    assert answer["masked_key"].endswith(API_KEY[-4:]) and API_KEY not in json.dumps(answer)
    _no_secret_in(node, r.text)


def test_the_key_unboxed_is_refused_and_stores_nothing_echoing_nothing(node):
    r = node.client.put("/api/settings/ai/key", json={"api_key": API_KEY, "password": PASSWORD})
    assert r.status_code == 400 and r.headers["x-sautium-auth-error"] == "unboxed"
    assert _setting(node.dsn, "ai.api_key") is None
    _no_secret_in(node, r.text)


@pytest.mark.parametrize("password", ["", "the wrong one"])
def test_the_key_without_the_password_or_with_a_wrong_one_stores_nothing(node, password):
    r, _ = _send(node.client, "PUT", "/api/settings/ai/key", {"api_key": API_KEY, "password": password})
    assert r.status_code == 403 and r.headers["x-sautium-auth-error"] == "step-up"
    assert _setting(node.dsn, "ai.api_key") is None
    assert device_auth._doors["signed"]["failures"] == (1 if password else 0)
    _no_secret_in(node, r.text)


def test_a_replayed_box_is_refused(node):
    envelope, _ = _seal(node.client, {"api_key": API_KEY, "password": PASSWORD})
    assert node.client.put("/api/settings/ai/key", json=envelope).status_code == 200
    with psycopg2.connect(node.dsn) as c, c.cursor() as cur:
        cur.execute("DELETE FROM user_settings WHERE key LIKE 'ai.%%'")
    again = node.client.put("/api/settings/ai/key", json=envelope)
    assert again.status_code == 400 and again.headers["x-sautium-auth-error"] == "channel"
    assert _setting(node.dsn, "ai.api_key") is None


def test_an_account_without_a_password_vouches_by_its_hosts_code(node, monkeypatch):
    monkeypatch.setattr(device_auth, "account_anonymous", lambda: True)
    monkeypatch.setattr(device_auth, "_pin", None)
    monkeypatch.setattr(device_auth, "_pin_prev", None)
    code = device_auth.issue_pin()
    for pin in ("", "WRNG-CODE"):
        r, _ = _send(node.client, "PUT", "/api/settings/ai/key", {"api_key": API_KEY, "pin": pin})
        assert r.status_code == 403 and "computer that runs Sautium" in r.json()["detail"]
        assert _setting(node.dsn, "ai.api_key") is None
    r, _ = _send(node.client, "PUT", "/api/settings/ai/key", {"api_key": API_KEY, "pin": code})
    assert r.status_code == 200 and _setting(node.dsn, "ai.api_key") == API_KEY


@pytest.mark.parametrize("method,path", [
    ("PUT", "/api/settings/ai/key"), ("POST", "/api/settings/ai/claude/signin"),
    ("POST", "/api/settings/ai/claude/signin/code"), ("POST", "/api/settings/ai/codex/signin"),
    ("POST", "/api/settings/ai/codex/signin/code"), ("PUT", "/api/p2p/account/email"),
    ("POST", "/api/p2p/email/send-code"), ("POST", "/api/p2p/email/verify-code"),
    ("POST", "/api/p2p/tokens"), ("PATCH", "/api/p2p/tokens/00000000-0000-0000-0000-000000000000"),
    ("POST", "/api/p2p/friends/add"),
])
def test_every_boxed_route_refuses_the_plain_body_before_reading_it(node, method, path):
    r = node.client.request(method, path, json={"code": CLAUDE_CODE, "api_key": API_KEY, "password": PASSWORD})
    assert r.status_code == 400 and r.headers["x-sautium-auth-error"] == "unboxed"
    _no_secret_in(node, r.text)


# -- the agents' sign-ins ------------------------------------------------------

@pytest.fixture
def agents(monkeypatch):
    """Both CLIs installed, their sign-in drivers recorded: the Claude one
    accepting a pasted code, the Codex one in the device flow with its code
    parsed."""
    started, pasted = [], []
    snap = {"running": True, "flow": "browser", "url": "https://example.test/authorize", "code": None,
            "accepts_code": True, "started_at": 1.0, "error": None}
    monkeypatch.setattr(claude_code, "get_claude_executable", lambda: Path("/bin/claude"))
    monkeypatch.setattr(claude_code, "get_state", lambda fresh=False: "not_authed")
    monkeypatch.setattr(claude_code, "start_signin", lambda cb: started.append("claude") or dict(snap))
    monkeypatch.setattr(claude_code, "submit_signin_code", lambda code: pasted.append(code) or dict(snap))
    codex = {**snap, "flow": "device", "code": DEVICE_CODE, "accepts_code": False, "started_at": 2.0}
    monkeypatch.setattr(codex_cli, "get_codex_executable", lambda: Path("/bin/codex"))
    monkeypatch.setattr(codex_cli, "get_state", lambda fresh=False: "not_authed")
    monkeypatch.setattr(codex_cli, "start_signin",
                        lambda cb, device: started.append(("codex", device)) or dict(codex))
    monkeypatch.setattr(codex_cli, "signin_snapshot", lambda: dict(codex))
    monkeypatch.setattr(codex_cli, "codex_auth_method", lambda: None)
    monkeypatch.setattr(codex_cli.auth, "verdict", lambda: None)
    return SimpleNamespace(started=started, pasted=pasted)


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("password", ["", "the wrong one"])
def test_a_sign_in_starts_only_on_the_password(node, agents, agent, password):
    r, _ = _send(node.client, "POST", f"/api/settings/ai/{agent}/signin", {"password": password})
    assert r.status_code == 403 and r.headers["x-sautium-auth-error"] == "step-up"
    assert agents.started == []


def test_the_claude_code_goes_in_only_with_the_grant_its_start_sealed(node, agents):
    r, started = _send(node.client, "POST", "/api/settings/ai/claude/signin", {"password": PASSWORD})
    assert r.status_code == 200 and agents.started == ["claude"] and started["grant"]
    for grant in ("", "a grant of nobody's"):
        r, _ = _send(node.client, "POST", "/api/settings/ai/claude/signin/code", {"code": CLAUDE_CODE, "grant": grant})
        assert r.status_code == 403 and r.headers["x-sautium-auth-error"] == "grant"
    assert agents.pasted == []
    r, _ = _send(node.client, "POST", "/api/settings/ai/claude/signin/code",
                 {"code": CLAUDE_CODE, "grant": started["grant"]})
    assert r.status_code == 200 and agents.pasted == [CLAUDE_CODE]
    _no_secret_in(node, r.text)


def test_the_codex_device_code_is_read_sealed_with_the_grant_and_never_in_the_clear(node, agents):
    state = node.client.get("/api/settings/ai/codex/state").json()
    assert state["signin"]["code"] is None and state["signin"]["code_ready"] is True
    assert DEVICE_CODE not in json.dumps(state)
    r, started = _send(node.client, "POST", "/api/settings/ai/codex/signin", {"password": PASSWORD, "device": True})
    assert r.status_code == 200 and agents.started == [("codex", True)]
    r, _ = _send(node.client, "POST", "/api/settings/ai/codex/signin/code", {"grant": "a grant of nobody's"})
    assert r.status_code == 403
    r, answer = _send(node.client, "POST", "/api/settings/ai/codex/signin/code", {"grant": started["grant"]})
    assert r.status_code == 200 and answer == {"code": DEVICE_CODE}
    _no_secret_in(node, r.text, json.dumps(state))


# -- the account email, through the real Worker ---------------------------------

@pytest.fixture
def mailbox(node, worker, monkeypatch):
    """This node born on the Worker; the P2P routes speaking as it, its
    address read from its node_info.json as the identity does."""
    key = nacl_signing.SigningKey(os.urandom(32))
    pub = key.verify_key.encode().hex()
    code = node_identity.make_invite_code("owner", bytes.fromhex(pub))
    assert _post(worker, "/birth-certificate", {
        "pubkey_hex": pub, "signature": key.sign(f"birth:{pub}".encode()).signature.hex()})["pubkey"] == pub

    def identity():
        info = json.loads((node.dir / "node_info.json").read_text())
        return {"node_id": pub, "public_key_hex": pub, "username": "owner", "invite_code": code,
                "email": info.get("email", "")}

    class Key:
        @staticmethod
        def sign(message):
            return key.sign(message).signature

    async def born():
        return True

    async def cert(refresh=False):
        return json.loads(urllib.request.urlopen(f"{worker}/birth-certificate?pubkey={pub}").read())
    monkeypatch.setattr(p2p, "VERIFY_WORKER_URL", worker)
    monkeypatch.setattr(p2p, "_get_identity", identity)
    monkeypatch.setattr(p2p, "_get_private_key", lambda: Key)
    monkeypatch.setattr(p2p, "_born", born)
    monkeypatch.setattr(p2p, "_get_own_birth_cert", cert)

    def mailed_code(to):
        mails = json.loads(urllib.request.urlopen(worker + "/__harness/mails", timeout=10).read())
        mine = [m for m in mails if m["to"] == [to]]
        return re.search(r'monospace;">([A-Z0-9]{6})<', mine[-1]["html"]).group(1) if mine else None
    return SimpleNamespace(code=mailed_code)


def _email(node):
    return json.loads((node.dir / "node_info.json").read_text())["email"]


def test_the_email_moves_only_on_the_password_and_its_code_only_on_the_grant(node, mailbox):
    address = "owner-b93@example.org"
    for password in ("", "the wrong one"):
        r, _ = _send(node.client, "PUT", "/api/p2p/account/email", {"email": address, "password": password})
        assert r.status_code == 403 and _email(node) == ""
    r, saved = _send(node.client, "PUT", "/api/p2p/account/email", {"email": address, "password": PASSWORD})
    assert r.status_code == 200 and _email(node) == address and saved["grant"]

    r, _ = _send(node.client, "POST", "/api/p2p/email/send-code", {"grant": "a grant of nobody's"})
    assert r.status_code == 403 and mailbox.code(address) is None
    r, sent = _send(node.client, "POST", "/api/p2p/email/send-code", {"grant": saved["grant"]})
    assert r.status_code == 200 and sent["grant"]
    code = mailbox.code(address)
    assert code

    r, _ = _send(node.client, "POST", "/api/p2p/email/verify-code", {"code": code, "grant": saved["grant"]})
    assert r.status_code == 403                       # the send's grant replaced the setting's
    r, verified = _send(node.client, "POST", "/api/p2p/email/verify-code", {"code": code, "grant": sent["grant"]})
    assert r.status_code == 200 and verified["verified"] is True
    with psycopg2.connect(node.dsn) as c, c.cursor() as cur:
        cur.execute("SELECT email_verified FROM user_profile WHERE id = 1")
        assert cur.fetchone()[0] is True
    r, _ = _send(node.client, "POST", "/api/p2p/email/verify-code", {"code": code, "grant": sent["grant"]})
    assert r.status_code == 403                       # a verified address spent it
    assert code not in node.caplog.text


def test_the_code_of_an_address_set_earlier_is_sent_on_the_password(node, mailbox):
    address = "earlier-b93@example.org"
    (node.dir / "node_info.json").write_text(json.dumps({"email": address, "email_verified": False}))
    r, _ = _send(node.client, "POST", "/api/p2p/email/send-code", {"password": "the wrong one"})
    assert r.status_code == 403 and mailbox.code(address) is None
    r, sent = _send(node.client, "POST", "/api/p2p/email/send-code", {"password": PASSWORD})
    assert r.status_code == 200 and sent["grant"] and mailbox.code(address)


# -- invite tokens -------------------------------------------------------------

@pytest.mark.parametrize("password", ["", "the wrong one"])
def test_a_token_with_rights_is_minted_only_on_the_password(node, password):
    r, _ = _send(node.client, "POST", "/api/p2p/tokens", {"label": "club", "rights": ["can_message"],
                                                          "password": password})
    assert r.status_code == 403 and _tokens(node.dsn) == []


def test_a_token_without_rights_is_a_friends_add_and_takes_the_token_alone(node):
    r, minted = _send(node.client, "POST", "/api/p2p/tokens", {"label": "plain", "rights": []})
    assert r.status_code == 200 and minted["share_string"].endswith(minted["id"])
    assert _tokens(node.dsn) == [("plain", None, [])]


def test_a_patch_asks_the_password_only_where_it_widens_a_link(node):
    r, minted = _send(node.client, "POST", "/api/p2p/tokens", {"label": "club", "rights": ["can_message"],
                                                               "max_uses": 2, "expires_at": "2030-01-01T00:00:00Z",
                                                               "password": PASSWORD})
    assert r.status_code == 200
    path = f"/api/p2p/tokens/{minted['id']}"
    for widening in ({"rights": ["can_message", "can_search"]}, {"max_uses": 10},
                     {"expires_at": ""}, {"expires_at": "2031-01-01T00:00:00Z"}):
        r, _ = _send(node.client, "PATCH", path, widening)
        assert r.status_code == 403, widening
    assert _tokens(node.dsn) == [("club", 2, ["can_message"])]
    r, _ = _send(node.client, "PATCH", path, {"label": "vinyl club", "max_uses": 1})
    assert r.status_code == 200
    r, _ = _send(node.client, "PATCH", path, {"max_uses": 5, "password": PASSWORD})
    assert r.status_code == 200 and _tokens(node.dsn) == [("vinyl club", 5, ["can_message"])]


def test_a_link_without_rights_is_not_widened_on_the_token_alone(node):
    """Its reach widened now would ride into the rights its owner adds
    later on his password: more uses and a later expiry ask it too."""
    r, minted = _send(node.client, "POST", "/api/p2p/tokens", {"label": "plain", "rights": [], "max_uses": 1,
                                                               "expires_at": "2030-01-01T00:00:00Z"})
    for widening in ({"max_uses": 50}, {"expires_at": ""}, {"rights": [], "expires_at": "2031-01-01T00:00:00Z"}):
        r, _ = _send(node.client, "PATCH", f"/api/p2p/tokens/{minted['id']}", widening)
        assert r.status_code == 403, widening
    assert _tokens(node.dsn) == [("plain", 1, [])]


def test_an_unvouched_patch_is_judged_again_under_the_rows_lock(node, monkeypatch):
    """A patch read the token after a right it now adds was already there
    (a stale read): judged again with the row locked, it asks the password
    and changes nothing."""
    r, minted = _send(node.client, "POST", "/api/p2p/tokens", {"label": "plain", "rights": [], "max_uses": 1})
    monkeypatch.setattr(p2p, "_db_query_one", lambda sql, args: {
        "max_uses": 1, "expires_at": None, "rights": ["can_message"], "extends": False})
    r, _ = _send(node.client, "PATCH", f"/api/p2p/tokens/{minted['id']}", {"rights": ["can_message"]})
    assert r.status_code == 403 and r.headers["x-sautium-auth-error"] == "step-up"
    assert _tokens(node.dsn) == [("plain", 1, [])]


def test_a_vouched_patch_lands_only_on_the_terms_the_password_was_asked_for(node, monkeypatch):
    """Another edit lands while the password is derived (its rights taken
    away and its expiry cleared): the vouched patch finds other terms under
    the lock, changes nothing and says so."""
    r, minted = _send(node.client, "POST", "/api/p2p/tokens", {"label": "club", "rights": ["can_message"],
                                                               "max_uses": 1, "expires_at": "2030-01-01T00:00:00Z",
                                                               "password": PASSWORD})
    real_step_up = p2p.step_up

    async def meanwhile(req):
        await real_step_up(req)
        with psycopg2.connect(node.dsn) as c, c.cursor() as cur:
            cur.execute("DELETE FROM invite_token_rights WHERE token_id = %s", (minted["id"],))
            cur.execute("UPDATE invite_tokens SET expires_at = NULL WHERE id = %s", (minted["id"],))
    monkeypatch.setattr(p2p, "step_up", meanwhile)
    r, _ = _send(node.client, "PATCH", f"/api/p2p/tokens/{minted['id']}",
                 {"rights": ["can_message"], "max_uses": 5, "password": PASSWORD})
    assert r.status_code == 409
    assert _tokens(node.dsn) == [("club", 1, [])]


def test_a_re_accept_signs_only_the_snapshot_and_a_token_the_node_knows_the_friend_by(node):
    """An established token friend presents another link's id, a grant
    naming rights of its own making, or nothing of ours: the re-accept's
    grant carries the rights snapshotted at the join — empty stays empty,
    though the owner has widened the link it joined since — and the token
    it joined through; a friend with no such token is known by a grant this
    node signed for that same guest, not revoked here; else none is signed."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import invite_tokens
    own = Ed25519PrivateKey.generate()
    own_pub = own.public_key().public_bytes_raw().hex()
    guest = "ab" * 32
    _, plain = _send(node.client, "POST", "/api/p2p/tokens", {"label": "plain", "rights": []})
    _, club = _send(node.client, "POST", "/api/p2p/tokens", {"label": "club", "rights": ["can_message", "can_search"],
                                                             "password": PASSWORD})
    with psycopg2.connect(node.dsn) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO friends (username, public_key_hex, invite_code, source, source_token_id)
                       VALUES ('guest', %s, 'guest#AAAA-BBBB-CCCC', 'token', %s) RETURNING id""",
                    (guest, plain["id"]))
        fid = cur.fetchone()[0]
        # the link it joined through widened since, on the owner's password
        cur.execute("INSERT INTO invite_token_rights (token_id, p2p_right) VALUES (%s, 'can_message')",
                    (plain["id"],))
        forged = {"token_id": club["id"], "rights": ["can_message", "can_search"], "guest_pubkey": guest}
        signed = invite_tokens.sign_grant(own, club["id"], ["can_message"], guest, own_pub)
        for grant in (forged, signed, None):
            assert invite_tokens.reaccept_terms(c, fid, grant, own_pub, guest) == (plain["id"], [])
        invite_tokens.snapshot_rights(c, fid, ["can_message"])
        assert invite_tokens.reaccept_terms(c, fid, forged, own_pub, guest) == (plain["id"], ["can_message"])

        cur.execute("UPDATE friends SET source_token_id = NULL WHERE id = %s", (fid,))
        assert invite_tokens.reaccept_terms(c, fid, forged, own_pub, guest) == (None, ["can_message"])
        assert invite_tokens.reaccept_terms(c, fid, signed, own_pub, guest) == (club["id"], ["can_message"])
        other = invite_tokens.sign_grant(own, club["id"], ["can_message"], "cd" * 32, own_pub)
        assert invite_tokens.reaccept_terms(c, fid, other, own_pub, guest)[0] is None
        cur.execute("UPDATE invite_tokens SET revoked_at = NOW() WHERE id = %s", (club["id"],))
        assert invite_tokens.reaccept_terms(c, fid, signed, own_pub, guest)[0] is None
        cur.execute("DELETE FROM friends WHERE id = %s", (fid,))
