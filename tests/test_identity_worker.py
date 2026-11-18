"""The identity worker of a node (B-110): who runs it, when it starts, and
what it wakes — against a real PostgreSQL built from the migrations, a fake
Worker on loopback (a throwaway authority issues the certificates) and
Argon2 at the suite's test parameters.

- a Docker-shaped backend with no account makes one through the route: its
  worker starts, the proof is mined and `p2p.identity` reaches `ready`, with
  no restart;
- the same node started again with node_info.json in its folder runs its
  worker; a launcher-mode backend runs none;
- a handshake refused for what our identity lacks (its certificate, its
  proof) is asked again only when that identity changes, and goes out at the
  worker's `ready`, never by the resolver's 15 s fallback; an issuer says an
  absent certificate is required, not invalid;
- an identity change stops the old worker (held mid-attempt, the new key
  waits for it) and starts one for the new key;
- an email verified — by verify-code, or found verified for the key when the
  address is given again — restarts the worker on the `email` certificate;
- a 429 at the issuance is a wait, ended by a certificate a route stores; a
  stop does not wait out the Worker's requests;
- two workers over one identity folder make one proof — two in a process,
  one in another process holding the folder's lock;
- a launcher's worker restarts on the stored certificate, and a restart
  racing its stop starts nothing.
Skipped without a cluster."""

import asyncio
import json
import os
import select
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("fastapi")

import db_pool  # noqa: E402
import device_auth  # noqa: E402
import p2p_chat  # noqa: E402
import p2p_identity  # noqa: E402
from config import settings  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: E402
from desktop.p2p import birth_cert, identity_pow, identity_proof  # noqa: E402
from routers import p2p as p2p_router  # noqa: E402
from tests import slice_certs  # noqa: E402
from tests.p2p.test_identity_proof import TEST_PARAMS  # noqa: E402

EASY = 4                    # a test difficulty: a few 8 MiB attempts


def _issue(pubkey: str, method: str, difficulty: int) -> dict:
    cert = slice_certs.certificate(pubkey, method)
    cert["difficulty"] = difficulty
    cert["sig"] = slice_certs.AUTHORITY.sign(birth_cert.canonical_payload(cert)).hex()
    return cert


class _Worker:
    """The Worker's identity routes on loopback: issuance (first a read, then
    a signed request), the email registration's re-signed certificate."""

    def __init__(self):
        self.issued: dict = {}
        self.difficulty: dict = {}           # pubkey -> difficulty, else EASY
        self.calls: list = []                # (method, path, pubkey)
        self.brake = 0                       # issuances refused for volume (429) before one is made
        self.read_brake = 0                  # certificate reads refused so
        self.retry_after = "60"
        self.stall = threading.Event()       # set: reads hang until `unstall`
        self.unstall = threading.Event()
        self.verified_email: set = set()     # pubkeys whose address the Worker holds verified
        worker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, status, body=None):
                raw = json.dumps(body or {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _refuse(self):
                self.send_response(429)
                self.send_header("Retry-After", worker.retry_after)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                url = urlparse(self.path)
                query = parse_qs(url.query)
                pubkey = (query.get("pubkey") or query.get("public_key_hex") or [""])[0]
                worker.calls.append(("GET", url.path, pubkey))
                if worker.stall.is_set():
                    worker.unstall.wait(30)
                if url.path == "/birth-certificate" and worker.read_brake:
                    worker.read_brake -= 1
                    return self._refuse()
                if url.path == "/birth-certificate":
                    cert = worker.issued.get(pubkey)
                    return self._answer(200, cert) if cert else self._answer(404)
                if url.path == "/check-email":
                    if pubkey not in worker.verified_email:
                        return self._answer(200, {"verified": False})
                    cert = worker.issued[pubkey] = _issue(pubkey, "email", 32)
                    return self._answer(200, {"verified": True, "birth_cert": cert})
                self._answer(404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                pubkey = body.get("pubkey_hex") or body.get("public_key_hex", "")
                worker.calls.append(("POST", self.path, pubkey))
                if self.path == "/birth-certificate":
                    if worker.brake:
                        worker.brake -= 1
                        return self._refuse()
                    Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey)).verify(
                        bytes.fromhex(body["signature"]), f"birth:{pubkey}".encode())
                    cert = worker.issued.setdefault(pubkey, _issue(
                        pubkey, "pow", worker.difficulty.get(pubkey, EASY)))
                    return self._answer(200, cert)
                if self.path == "/register-email":
                    cert = _issue(body["public_key_hex"], "email", 32)
                    worker.issued[body["public_key_hex"]] = cert
                    return self._answer(200, {"status": "registered", "birth_cert": cert})
                self._answer(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def issuances(self) -> int:
        return sum(1 for call in self.calls if call[:2] == ("POST", "/birth-certificate"))

    def asked_for(self, pubkey: str) -> bool:
        return any(call[2] == pubkey for call in self.calls)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def node(scratch_dsn, tmp_path, monkeypatch):
    """A Docker-shaped backend (P2P on, its own peer port) with no account:
    its identity folder empty, its database the module's, the Worker fake,
    the KDF and the proof at the tests' cost, the chat client left out (the
    handshake tests build their own)."""
    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port, postgres_user=url.username,
                           postgres_password=url.password, postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    db_pool.close_pool()
    folder = tmp_path / "node_identity"
    folder.mkdir()
    for key, value in dict(p2p_identity_dir=str(folder), p2p_enabled=True, p2p_sync_port=8801,
                           p2p_listen_port=0, p2p_username="", p2p_password="", p2p_email="").items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(p2p_identity, "ARGON2_TIME_COST", 1)
    monkeypatch.setattr(p2p_identity, "ARGON2_MEMORY_COST", 8 * 1024)
    monkeypatch.setitem(identity_pow.POW_PARAMS, 1, TEST_PARAMS)
    monkeypatch.setitem(identity_proof.ensure_identity_proof.__kwdefaults__, "mem_guard_kib", 0)
    monkeypatch.setitem(identity_proof.ensure_identity_proof.__kwdefaults__, "pause_seconds", 0.05)
    slice_certs.trust(monkeypatch)
    worker = _Worker()
    monkeypatch.setattr(p2p_router, "VERIFY_WORKER_URL", worker.url)
    monkeypatch.setattr(p2p_router, "_identity_worker", None)
    monkeypatch.setattr(p2p_router, "_identity_worker_closed", False)
    monkeypatch.setattr(p2p_router, "_identity_worker_lock", asyncio.Lock())
    monkeypatch.setattr(p2p_router, "_cached_private_key", None)
    monkeypatch.setattr(device_auth, "_create_lock", asyncio.Lock())
    monkeypatch.setattr(device_auth, "_setup_code", None)
    monkeypatch.setattr(device_auth, "_identity_missing", False)
    monkeypatch.setattr(device_auth, "_epoch_cache", None)
    monkeypatch.setattr(device_auth, "_grants", {})

    async def nothing():
        return None
    monkeypatch.setattr(p2p_chat, "start_client", nothing)
    monkeypatch.setattr(p2p_chat, "restart_client", nothing)
    monkeypatch.setattr(p2p_chat, "_client", None)
    monkeypatch.setattr(p2p_chat, "_service", None)
    p2p_identity.forget_cached()
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("TRUNCATE friends CASCADE")
        cur.execute("DELETE FROM user_settings")
    yield type("Node", (), {"folder": folder, "worker": worker, "conn": conn})
    if p2p_router._identity_worker is not None:
        p2p_router._identity_worker.stop.set()           # a test that failed leaves no miner on
    worker.unstall.set()
    conn.close()
    worker.close()
    p2p_identity.forget_cached()
    db_pool.close_pool()


def _state(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM user_settings WHERE key = 'p2p.identity'")
        row = cur.fetchone()
    return row[0] if row else {}


def _await_identity(dsn: str, done, seconds: float = 30) -> dict:
    """Wait on NOTIFY sautium_identity until `done(state)`."""
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("LISTEN sautium_identity")
        deadline = time.monotonic() + seconds
        while True:
            state = _state(conn)
            if done(state):
                return state
            left = deadline - time.monotonic()
            assert left > 0, f"identity state never got there: {state}"
            select.select([conn], [], [], left)
            conn.poll()
            conn.notifies.clear()
    finally:
        conn.close()


def _ready(state: dict) -> bool:
    return state.get("status") == "ready"


def _account(node, username="owner", password="a password") -> dict:
    """An account on disk, as the Web UI's setup leaves it."""
    return device_auth._write_identity(node.folder, username, password)


def _proof_binds_its_certificate(node) -> dict:
    cert = p2p_identity.load_certificate(settings)
    proof = identity_proof.load_proof(node.folder / identity_proof.PROOF_FILENAME)
    assert cert is not None and identity_proof.verify_proof(proof, cert, TEST_PARAMS)
    return cert


# ---------------------------------------------------------------------------
# Who runs it, and when it starts
# ---------------------------------------------------------------------------

def test_an_account_made_in_the_web_ui_earns_its_proof_with_no_restart(node, scratch_dsn, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import auth
    from tests.test_auth_doors import _seal
    monkeypatch.setattr(auth, "_secret", lambda: b"node secret")
    device_auth.prepare_setup()
    code = device_auth._setup_code
    app = FastAPI()
    app.include_router(auth.router)
    with TestClient(app) as web:
        r = web.post("/api/auth/create-account", json=_seal(web.get("/api/auth/handshake").json(), {
            "username": "owner", "password": "a password", "setup_code": code}))
        assert r.status_code == 200, r.text
        assert (node.folder / "node_info.json").exists()
        state = _await_identity(scratch_dsn, _ready)
    assert state["detail"] == "proof" and state["difficulty"] == EASY
    cert = _proof_binds_its_certificate(node)
    assert cert["method"] == "pow" and node.worker.issuances() == 1


def test_a_node_started_again_with_its_account_on_disk_runs_its_worker(node, scratch_dsn):
    """node_info.json is how every account lies on disk — launcher's or
    Web UI's — so it says nothing about who runs the worker."""
    _account(node)
    assert (node.folder / "node_info.json").exists()

    async def start():
        await p2p_router.start_identity_worker()
        await p2p_router.start_identity_worker()           # a second start is the same worker
        await p2p_router._identity_worker.task
    asyncio.run(start())
    assert _ready(_state(node.conn))
    _proof_binds_its_certificate(node)
    assert node.worker.issuances() == 1


def test_a_launcher_mode_backend_runs_no_worker(node, monkeypatch, caplog):
    monkeypatch.setattr(settings, "p2p_sync_port", 0)
    monkeypatch.setattr(settings, "p2p_listen_port", 19000)
    _account(node)
    with caplog.at_level("INFO", logger="routers.p2p"):
        asyncio.run(p2p_router.start_identity_worker())
    assert p2p_router._identity_worker is None and node.worker.calls == []
    assert _state(node.conn) == {}
    assert "the launcher's process runs it" in caplog.text


def test_an_identity_change_stops_the_old_worker_and_starts_one_for_the_new_key(node, scratch_dsn,
                                                                               monkeypatch):
    """The old worker's thread is held mid-mining across the rotation: the new
    key's worker asks the Worker nothing and publishes nothing until the old
    thread has returned."""
    import notary
    monkeypatch.setattr(notary, "drain_witness", lambda: None)
    old = _account(node, "first", "first password")["public_key_hex"]
    node.worker.difficulty[old] = 1 << 40               # the old key's proof is never done
    published = []
    rotating, release = threading.Event(), threading.Event()
    real = p2p_router._publish_identity_state

    def publish(state):
        if rotating.is_set() and state["difficulty"] != EASY:
            release.wait(30)                            # the old thread, held in its attempt
        published.append(dict(state))
        real(state)
    monkeypatch.setattr(p2p_router, "_publish_identity_state", publish)

    workers = []

    async def scene():
        try:
            await p2p_router.start_identity_worker()
            first = p2p_router._identity_worker
            workers.append(first)
            await asyncio.to_thread(_await_identity, scratch_dsn, lambda s: s.get("attempts", 0) > 2)
            rotating.set()
            info = await device_auth.rotate_identity("second", "second password", "first password")
            await asyncio.sleep(1)
            held = (node.worker.asked_for(info["public_key_hex"].lower()),
                    any(s["difficulty"] == EASY for s in published), first.mining.done())
            release.set()
            await asyncio.to_thread(_await_identity, scratch_dsn,
                                    lambda s: _ready(s) and s["difficulty"] == EASY)
            return first, info, held
        finally:
            # A failure here leaves no miner on: asyncio.run waits for its thread
            release.set()
            for worker in workers + [p2p_router._identity_worker]:
                if worker is not None:
                    worker.stop.set()
    first, info, held = asyncio.run(scene())
    assert held == (False, False, False)                # nothing of the new key while the old ran
    assert first.task.done() and first.stop.is_set() and first.mining.done()
    assert info["public_key_hex"] != old
    cert = _proof_binds_its_certificate(node)
    assert cert["pubkey"] == info["public_key_hex"].lower()
    olds = [i for i, s in enumerate(published) if s["difficulty"] != EASY]
    news = [i for i, s in enumerate(published) if s["difficulty"] == EASY]
    assert published[olds[-1]]["status"] == "stopped" and olds[-1] < news[0]


@pytest.mark.parametrize("retry_after", [60, 7200])
def test_a_worker_braked_by_the_worker_goes_on_when_a_route_stores_the_certificate(node, scratch_dsn,
                                                                                  caplog, retry_after):
    """A 429 at the issuance is the Worker's word to wait — as long as it
    says, the backoff at least — not the worker's end; a certificate a route
    stores meanwhile (Friends' pickup, an invite) ends the wait."""
    _account(node)
    node.worker.brake = 1
    node.worker.retry_after = str(retry_after)
    caplog.set_level("WARNING", logger="routers.p2p")

    async def scene():
        await p2p_router.start_identity_worker()
        worker = p2p_router._identity_worker
        deadline = time.monotonic() + 10
        while node.worker.issuances() < 1:                   # braked
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        assert not worker.task.done()                         # waiting, not ended
        assert (await p2p_router._get_own_birth_cert())["method"] == "pow"   # a route asks
        await asyncio.wait_for(worker.task, 30)
    asyncio.run(scene())
    assert f"retry in {max(retry_after, 300)}s" in caplog.text
    assert _ready(_state(node.conn))
    _proof_binds_its_certificate(node)


def test_a_brake_on_a_check_of_the_certificate_held_leaves_it_standing(node):
    """The refresh of a pow certificate held only looks for a newer one: the
    Worker's 429 there is no reason to wait, the mining goes on."""
    _account(node)
    assert asyncio.run(p2p_router._get_own_birth_cert())["method"] == "pow"
    node.worker.read_brake = 1

    async def scene():
        await p2p_router.start_identity_worker()
        await asyncio.wait_for(p2p_router._identity_worker.task, 30)
    asyncio.run(scene())
    assert node.worker.read_brake == 0                       # it was asked, and braked
    assert _ready(_state(node.conn))
    _proof_binds_its_certificate(node)


def test_a_stop_does_not_wait_out_the_workers_requests(node):
    """Shutdown, or a restart, while the Worker is slow to answer: its
    requests are cancelled, they publish nothing."""
    _account(node)
    node.worker.stall.set()

    async def scene():
        await p2p_router.start_identity_worker()
        deadline = time.monotonic() + 10
        while not node.worker.calls:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        began = time.monotonic()
        await p2p_router.close_identity_worker()
        return time.monotonic() - began
    assert asyncio.run(scene()) < 2
    assert p2p_router._identity_worker is None and _state(node.conn) == {}


@pytest.mark.parametrize("how", ["verify-code", "the address given again"])
def test_an_email_verified_ends_the_mining_and_says_ready(node, scratch_dsn, monkeypatch, how):
    """A certificate that changes under the worker is an event: the worker
    mining for the pow one stops, and the new one says `ready · email` — the
    publish whose `ready` wakes the chat client. Either by the code mailed
    here (verify-code), or by an address the Worker holds verified for this
    key already (PUT /account/email's check-email: a reinstall, a second
    device)."""
    identity = _account(node)
    pubkey = identity["public_key_hex"].lower()
    node.worker.difficulty[pubkey] = 1 << 40
    if how == "verify-code":
        info = json.loads((node.folder / "node_info.json").read_text())
        info["email"] = "owner@example.org"
        (node.folder / "node_info.json").write_text(json.dumps(info))
    else:
        node.worker.verified_email.add(pubkey)
    woken = []
    monkeypatch.setattr(p2p_chat, "_client", type("Client", (), {"resolve_soon": lambda self: woken.append(1)})())

    async def scene():
        try:
            await p2p_router.start_identity_worker()
            mining = p2p_router._identity_worker
            await asyncio.to_thread(_await_identity, scratch_dsn, lambda s: s.get("status") == "mining")
            # The routes as their box opens them (routers/auth.boxed): the
            # payload, and a reply that seals nothing here
            if how == "verify-code":
                grant = device_auth.grant("email", 60)
                answer = await p2p_router.email_verify_code(opened=(
                    p2p_router.VerifyCodeRequest(code="ABC123", grant=grant), lambda body: body))
                assert answer["verified"]
            else:
                answer = await p2p_router.set_account_email(opened=(
                    p2p_router.SetEmailRequest(email="owner@example.org", password="a password"),
                    lambda body: body))
                assert answer["email_verified"]
            await asyncio.to_thread(_await_identity, scratch_dsn,
                                    lambda s: _ready(s) and s["detail"] == "email")
            return mining
        finally:
            if p2p_router._identity_worker is not None:
                p2p_router._identity_worker.stop.set()
    mining = asyncio.run(scene())
    assert mining.task.done() and mining.mining.done()
    assert p2p_identity.load_certificate(settings)["method"] == "email"
    assert woken


# ---------------------------------------------------------------------------
# One miner per identity folder
# ---------------------------------------------------------------------------

def test_two_workers_over_one_identity_folder_make_one_proof(tmp_path):
    """The first holds the folder through its whole run: the second, started
    while it mines, stands down at once, having published nothing."""
    cert = _issue(os.urandom(32).hex(), "pow", EASY)
    path = tmp_path / identity_proof.PROOF_FILENAME
    first_states, second_states = [], []
    mining, second_done = threading.Event(), threading.Event()

    def first_state(state):
        first_states.append(state)
        mining.set()
        second_done.wait(30)                               # held in its run until the second ends

    def run(on_state, out):
        out.append(identity_proof.ensure_identity_proof(
            cert, path, stop=threading.Event(), on_state=on_state, params=TEST_PARAMS,
            mem_available_kib=lambda: 10 ** 9, pause_seconds=0.01))
    first, second = [], []
    t1 = threading.Thread(target=run, args=(first_state, first))
    t1.start()
    assert mining.wait(30)
    run(second_states.append, second)
    second_done.set()
    t1.join(60)
    assert second == [None] and second_states == []
    assert first[0] is not None and identity_proof.load_proof(path) == first[0]


def test_a_worker_in_another_process_holds_the_folder(tmp_path):
    """The Mac stand's race of 2026-08-19: two processes over one folder."""
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time\n"
         "from desktop.p2p import identity_proof\n"
         "with identity_proof.worker_lock(sys.argv[1]) as held:\n"
         "    print(held, flush=True)\n"
         "    sys.stdin.readline()\n", str(tmp_path)],
        cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "True"
        states = []
        proof = identity_proof.ensure_identity_proof(
            _issue(os.urandom(32).hex(), "pow", EASY), tmp_path / identity_proof.PROOF_FILENAME,
            stop=threading.Event(), on_state=states.append, params=TEST_PARAMS,
            mem_available_kib=lambda: 10 ** 9)
        assert proof is None and states == []
        assert not (tmp_path / identity_proof.PROOF_FILENAME).exists()
    finally:
        holder.kill()                                      # no clean exit: the OS drops the lock
        holder.wait(10)
    # The holder gone, the folder is free again: no lock outlives its process
    with identity_proof.worker_lock(tmp_path) as held:
        assert held


# ---------------------------------------------------------------------------
# The parked handshake
# ---------------------------------------------------------------------------

class _Issuer:
    """A cert-gated token issuer over TLS bound to its key: it refuses a
    handshake without a certificate (in `missing_word`), a pow one without
    its proof, and accepts one that carries both."""

    def __init__(self, tls_dir: Path, missing_word: str):
        import ssl

        import uvicorn
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse

        import tls_gen
        from desktop import node_identity
        self.key = Ed25519PrivateKey.generate()
        self.pub = self.key.public_key().public_bytes_raw().hex()
        self.invite = node_identity.make_invite_code("issuer", bytes.fromhex(self.pub))
        self.seen: list = []
        app = FastAPI()

        @app.post("/api/chat/handshake")
        async def handshake(request: Request):
            body = await request.json()
            self.seen.append((time.monotonic(), body))
            if not body.get("birth_cert"):
                return JSONResponse({"error": missing_word}, status_code=403)
            if not body.get("identity_proof"):
                return JSONResponse({"error": "identity proof required"}, status_code=403)
            return {"accepted": True, "public_key_hex": self.pub, "username": "issuer",
                    "invite_code": self.invite}

        cert, key = tls_gen.ensure_cert(tls_dir, binding=(self.pub, self.key.sign))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                                                    ssl_certfile=str(cert), ssl_keyfile=str(key)))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    def close(self):
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture
def parked(node, scratch_dsn, tmp_path, monkeypatch):
    """`(issuer, client, identity)`: a node with an account and a contact
    waiting for its token handshake at a cert-gated issuer; the client the
    backend would run, its addresses and its history pull stubbed. The
    issuer's word for an absent certificate is `missing_word` (param)."""
    import uuid

    from desktop.p2p import chat_client as cc
    from desktop.p2p.chat_service import ChatService

    def make(missing_word="identity certificate required"):
        issuer = _Issuer(tmp_path / "tls", missing_word)
        made.append(issuer)
        identity = _account(node)
        with node.conn.cursor() as cur:
            cur.execute("INSERT INTO friends (username, public_key_hex, invite_code, source, join_token_id) "
                        "VALUES ('issuer', %s, %s, 'token', %s)",
                        (f"pending:{issuer.invite}", issuer.invite, str(uuid.uuid4())))
        key = p2p_identity.load_signing_key(settings)
        client = cc.ChatClient(ChatService(scratch_dsn, key.private_bytes_raw(), identity["public_key_hex"]),
                               identity=p2p_chat.client_identity, announce_port=lambda: 1)

        async def here(friend, refresh=False, untargeted=True):
            return [("127.0.0.1", issuer.port)]

        async def nothing(*a, **k):
            return None
        client.find_friend_peers = here
        client._sync_chat_history = nothing
        client._note_peer_alive = nothing
        monkeypatch.setattr(p2p_chat, "_client", client)
        return issuer, client, identity
    made: list = []
    yield make
    for issuer in made:
        issuer.close()


def _resolved(node, issuer) -> bool:
    with node.conn.cursor() as cur:
        cur.execute("SELECT public_key_hex FROM friends WHERE invite_code = %s", (issuer.invite,))
        return cur.fetchone()[0] == issuer.pub


def test_a_handshake_refused_for_what_our_identity_lacks_is_asked_again_only_when_it_changes(node, parked):
    """The lab Pi's ten refusals: a pass every 15 s, each without a
    certificate, each refused the same."""
    issuer, client, identity = parked("identity certificate invalid")

    async def passes():
        client._loop = asyncio.get_running_loop()
        await client._do_resolve_pending()
        await client._do_resolve_pending()
        assert len(issuer.seen) == 1                          # parked: no certificate
        await p2p_router._get_own_birth_cert()                # the certificate lands (pow)
        await client._do_resolve_pending()
        await client._do_resolve_pending()
        assert len(issuer.seen) == 2                          # asked once more, parked: no proof
        await p2p_router.start_identity_worker()
        await p2p_router._identity_worker.task                          # the proof lands
        await client._do_resolve_pending()
    asyncio.run(passes())
    words = [(bool(b.get("birth_cert")), bool(b.get("identity_proof"))) for _, b in issuer.seen]
    assert words == [(False, False), (True, False), (True, True)]
    assert _resolved(node, issuer)


def test_an_absent_certificate_is_said_to_be_required_not_invalid():
    from desktop.p2p.identity_registry import IdentityGate
    gate = IdentityGate(lambda: pytest.fail("no database for an absent certificate"), lambda c: False)
    for absent in ({}, None):
        admission = asyncio.run(gate.admit("ab" * 32, absent, None, "192.0.2.1"))
        assert (admission.status, admission.detail, admission.http_status) == (
            "invalid", "identity certificate required", 403)
    admission = asyncio.run(gate.admit("ab" * 32, {"pubkey": "ab" * 32}, None, "192.0.2.1"))
    assert admission.detail == "identity certificate invalid"


@pytest.mark.parametrize("cert_first, missing_word", [
    (False, "identity certificate invalid"),        # the lab Pi's: an issuer of before 2026-11-18
    (False, "identity certificate required"),
    (True, None),                                   # the certificate held, the proof not yet
])
def test_a_parked_handshake_goes_out_at_the_workers_ready(node, parked, cert_first, missing_word):
    issuer, client, identity = parked(missing_word or "identity certificate required")
    if cert_first:
        assert asyncio.run(p2p_router._get_own_birth_cert())["method"] == "pow"

    async def scene():
        client.running = True
        client._loop = asyncio.get_running_loop()
        client._resolve_nudge = asyncio.Event()
        resolver = asyncio.create_task(client._resolve_loop())
        deadline = time.monotonic() + 15
        while not issuer.seen:                                # the first pass (after 5 s)
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        await asyncio.sleep(1)
        parked_at = len(issuer.seen)
        await p2p_router.start_identity_worker()
        await p2p_router._identity_worker.task                          # the proof mined: `ready`
        ready_at = time.monotonic()
        while len(issuer.seen) == parked_at:
            assert time.monotonic() < ready_at + 5, "the parked handshake did not go out"
            await asyncio.sleep(0.05)
        client.running = False
        resolver.cancel()
        return parked_at
    assert asyncio.run(scene()) == 1                          # not asked again meanwhile
    first_at, first = issuer.seen[0]
    sent_at, sent = issuer.seen[-1]
    assert bool(first.get("birth_cert")) == cert_first and "identity_proof" not in first
    assert sent["birth_cert"]["pubkey"] == identity["public_key_hex"].lower() and sent["identity_proof"]
    assert sent_at - first_at < 15                            # the wake, not the 15 s fallback
    assert _resolved(node, issuer)


def test_a_certificate_a_launcher_mode_backend_stores_tells_the_launcher(node, scratch_dsn, monkeypatch):
    """A launcher whose start found no certificate (the Worker unreachable)
    started no miner: the certificate its backend stores later is its event."""
    monkeypatch.setattr(settings, "p2p_sync_port", 0)
    _account(node)
    listen = psycopg2.connect(scratch_dsn)
    listen.autocommit = True
    try:
        with listen.cursor() as cur:
            cur.execute("LISTEN sautium_identity_cert")
        assert asyncio.run(p2p_router._get_own_birth_cert())["method"] == "pow"
        select.select([listen], [], [], 10)
        listen.poll()
        assert [n.channel for n in listen.notifies] == ["sautium_identity_cert"]
        listen.notifies.clear()
        asyncio.run(p2p_router._get_own_birth_cert())        # held already: no event
        select.select([listen], [], [], 0.5)
        listen.poll()
        assert listen.notifies == []
    finally:
        listen.close()


def test_a_launchers_worker_starts_again_on_a_certificate_the_backend_saved(monkeypatch):
    """NOTIFY sautium_identity_cert (verify-code, served by the launcher-mode
    backend): the launcher's worker for the old certificate ends, and one
    starts on the certificate stored now."""
    from desktop.p2p import p2p_manager
    manager = object.__new__(p2p_manager.P2PManager)
    manager._proof_lock = threading.Lock()
    manager._proof_stopping = False
    manager._proof_stop = threading.Event()
    manager._proof_thread = threading.Thread(target=manager._proof_stop.wait)
    manager._proof_thread.start()
    old = manager._proof_thread
    stored = _issue(os.urandom(32).hex(), "email", 32)
    monkeypatch.setattr(birth_cert, "load_certificate", lambda: stored)
    started = []
    manager._start_identity_proof_worker = started.append
    manager._restart_identity_proof_worker()
    assert not old.is_alive() and started == [stored]


def test_a_launchers_restart_racing_its_stop_starts_nothing(monkeypatch):
    from desktop.p2p import p2p_manager
    manager = object.__new__(p2p_manager.P2PManager)
    manager._proof_lock = threading.Lock()
    manager._proof_stopping = True                       # stop() has begun
    manager._proof_stop, manager._proof_thread = None, None
    monkeypatch.setattr(birth_cert, "load_certificate", lambda: pytest.fail("nothing to start"))
    manager._start_identity_proof_worker = lambda cert: pytest.fail("nothing to start")
    manager._restart_identity_proof_worker()
