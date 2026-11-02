"""The launcher's witness record end to end: a real SyncServer middleware and
handshake handler served over TLS bound to the node's key, a real
BackendAPIClient dialing it, one PostgreSQL built from the migrations.

- a signed request that introduces its sender records `introduced`, after
  the admission gate's verdict, and a raising record still answers;
- a client's /health read records the server's certificate as `probe` —
  never one for another key, never the node's own, never from a server
  without the field;
- an accepted token or grant handshake records `handshake`; a refused one
  and the unsigned classic path record nothing.
Skipped without a cluster."""

import asyncio
import datetime as dt
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.pool  # noqa: E402
from aiohttp import web  # noqa: E402

from desktop import node_identity  # noqa: E402
from desktop.api_client import BackendAPIClient  # noqa: E402
from desktop.p2p import birth_cert, peer_auth, witness  # noqa: E402
from desktop.p2p.chat_service import ChatService  # noqa: E402
from desktop.p2p.sync_server import SyncServer  # noqa: E402
from tests.p2p.test_witness import ISSUER, certificate  # noqa: E402


class Node:
    def __init__(self, name: str):
        self.key = Ed25519PrivateKey.generate()
        raw = self.key.public_key().public_bytes_raw()
        self.pub = raw.hex()
        self.seed = self.key.private_bytes_raw()
        self.name = name
        self.invite = node_identity.make_invite_code(name, raw)
        self.cert = certificate(self.pub)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(birth_cert, "TRUSTED_AUTHORITIES", [ISSUER])
    monkeypatch.setattr(witness, "TRUSTED_AUTHORITIES", [ISSUER])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))      # the node's identity folder: empty


@pytest.fixture(scope="module")
def pool(scratch_dsn):
    p = psycopg2.pool.ThreadedConnectionPool(1, 4, scratch_dsn)

    @contextmanager
    def connect():
        conn = p.getconn()
        try:
            conn.autocommit = True
            yield conn
        finally:
            p.putconn(conn)
    yield connect
    p.closeall()


def _rows(pool, subject):
    with pool() as conn, conn.cursor() as cur:
        cur.execute("SELECT channel::text FROM p2p_witness WHERE subject = %s ORDER BY id", (subject,))
        return [r[0] for r in cur.fetchall()]


def _tls_context(node: Node) -> ssl.SSLContext:
    """A server context whose self-signed certificate carries `node`'s key
    binding — what a peer surface serves."""
    tls_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "witness-test")])
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    spki = tls_key.public_key().public_bytes(serialization.Encoding.DER,
                                             serialization.PublicFormat.SubjectPublicKeyInfo)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(tls_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.UnrecognizedExtension(
                x509.ObjectIdentifier(peer_auth.TLS_BINDING_OID),
                peer_auth.tls_binding_value(node.key.sign, node.pub, spki)), critical=False)
            .sign(tls_key, hashes.SHA256()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (Path(d) / "k.pem").write_bytes(tls_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        ctx.load_cert_chain(str(Path(d) / "c.pem"), str(Path(d) / "k.pem"))
    return ctx


class Served:
    """An aiohttp app on its own loop thread, over TLS on a free port."""

    def __init__(self, app: web.Application, ssl_ctx: ssl.SSLContext):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

        async def up():
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_ctx)
            await site.start()
            return runner, site._server.sockets[0].getsockname()[1]
        self.runner, port = asyncio.run_coroutine_threadsafe(up(), self.loop).result(10)
        self.url = f"https://127.0.0.1:{port}"

    def close(self):
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


async def _ok(request):
    return web.json_response({"ok": True})


@pytest.fixture
def server(scratch_dsn, monkeypatch):
    """Node S's sync server: the real middleware, /health and handshake."""
    s = Node("server")
    monkeypatch.setattr(birth_cert, "served_certificate", lambda: s.cert)
    monkeypatch.setattr(node_identity, "get_private_key_raw", lambda: s.seed)
    srv = SyncServer(scratch_dsn, port=0, node_id=s.pub,
                     account_info={"public_key_hex": s.pub, "username": s.name, "invite_code": s.invite})
    srv.set_chat_service(ChatService(scratch_dsn, s.seed, s.pub))
    app = web.Application(middlewares=[srv._inbound_middleware])
    app.router.add_get("/health", srv.handle_health)
    app.router.add_get("/api/sync/witness-test", _ok)            # any identity-bound route
    app.router.add_post("/api/chat/handshake", srv.handle_chat_handshake)
    served = Served(app, _tls_context(s))
    yield s, srv, served
    served.close()


def _client(url: str, node: Node, recorder=None, proof=None) -> BackendAPIClient:
    sink = None if recorder is None else (lambda cert, pubkey: recorder.note(cert, "probe", pubkey))
    return BackendAPIClient(url, peer=peer_auth.PeerIdentity(
        pubkey=node.pub, sign=node.key.sign, cert_bundle=lambda: {"cert": node.cert, "proof": proof},
        witness=sink))


def test_an_introduction_and_a_probe_are_each_recorded_where_they_land(server, pool):
    s, srv, served = server
    c = Node("client")
    client_record = witness.Recorder(pool, own_keys=lambda: frozenset({c.pub}))
    api = _client(served.url, c, client_record)
    assert api.get_health()["birth_cert"] == s.cert
    assert _rows(pool, s.pub) == ["probe"]                   # the client witnessed the server
    assert api._get_json("/api/sync/witness-test") == {"ok": True}
    assert api.last_peer_identity == "unverified"
    assert _rows(pool, c.pub) == ["introduced"]              # the server witnessed the client
    # The proof that comes later — the client mined since — adds no row.
    again = _client(served.url, c, client_record, proof={"v": 1, "nonce": "00" * 16})
    assert again._get_json("/api/sync/witness-test") == {"ok": True}
    assert _rows(pool, c.pub) == ["introduced"] and _rows(pool, s.pub) == ["probe"]


def test_a_request_the_gate_refuses_is_no_sighting(server, pool, monkeypatch):
    s, srv, served = server

    class Enforcing:
        def would_be(self, *_a):
            return 5

        def mode(self):
            return "enforce"
    c = Node("unpaid")
    with monkeypatch.context() as m:
        m.setattr(srv, "_price", lambda: Enforcing())
        api = _client(served.url, c)
        assert api._get_json("/api/sync/witness-test", _paid=True) is None
        assert api.last_http_error == 402
        assert _rows(pool, c.pub) == []
    assert _client(served.url, c)._get_json("/api/sync/witness-test") == {"ok": True}   # admitted
    assert _rows(pool, c.pub) == ["introduced"]


def test_a_record_that_fails_never_costs_the_request(server, pool, monkeypatch, caplog):
    s, srv, served = server

    def broken(*_a, **_k):
        raise RuntimeError("the database is gone")
    monkeypatch.setattr(witness.Recorder, "record", broken)
    c = Node("client")
    api = _client(served.url, c)
    assert api._get_json("/api/sync/witness-test") == {"ok": True}
    assert api.last_peer_identity == "unverified"            # the registry still answered
    assert any("could not be recorded: the database is gone" in r.getMessage() for r in caplog.records)
    assert _rows(pool, c.pub) == []


def test_a_probe_records_only_the_channels_own_valid_certificate(pool, monkeypatch):
    s, c, other = Node("server"), Node("client"), Node("other")
    body = {"node_id": s.pub, "status": "ok"}

    async def health(request):
        return web.json_response(body)
    app = web.Application()
    app.router.add_get("/health", health)
    served = Served(app, _tls_context(s))
    try:
        record = witness.Recorder(pool, own_keys=lambda: frozenset({c.pub}))
        assert _client(served.url, c, record).get_health() == body          # no field: as before
        body["birth_cert"] = other.cert                                     # another key's
        assert _client(served.url, c, record).get_health()["node_id"] == s.pub
        body["birth_cert"] = dict(s.cert, difficulty=31)                    # not the authority's
        assert _client(served.url, c, record).get_health()["node_id"] == s.pub
        body["birth_cert"] = "not a certificate"
        assert _client(served.url, c, record).get_health()["node_id"] == s.pub
        assert _rows(pool, s.pub) == [] and _rows(pool, other.pub) == []
        body["birth_cert"] = s.cert                                         # an older client ignores it
        assert _client(served.url, c).get_health()["birth_cert"] == s.cert
        assert _rows(pool, s.pub) == []
        assert _client(served.url, c, record).get_health()["birth_cert"] == s.cert   # the channel's own
        assert _rows(pool, s.pub) == ["probe"] and _rows(pool, other.pub) == []
    finally:
        served.close()


def test_a_node_that_dials_itself_records_nothing(pool):
    me = Node("me")

    async def health(request):
        return web.json_response({"node_id": me.pub, "birth_cert": me.cert})
    app = web.Application()
    app.router.add_get("/health", health)
    served = Served(app, _tls_context(me))
    try:
        record = witness.Recorder(pool, own_keys=lambda: frozenset({me.pub}))
        assert _client(served.url, me, record).get_health()["node_id"] == me.pub
        assert _rows(pool, me.pub) == []
        peer = Node("peer")                                  # anyone else dialing it witnesses it
        other_record = witness.Recorder(pool, own_keys=lambda: frozenset({peer.pub}))
        assert _client(served.url, peer, other_record).get_health()["node_id"] == me.pub
        assert _rows(pool, me.pub) == ["probe"]
    finally:
        served.close()


# ---------------------------------------------------------------------------
# Handshakes
# ---------------------------------------------------------------------------

def _post(url: str, body: dict) -> tuple:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    req = urllib.request.Request(url + "/api/chat/handshake", method="POST",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=10, context=ctx)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _token(pool) -> str:
    tid = str(uuid.uuid4())
    with pool() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO invite_tokens (id, label, max_uses) VALUES (%s, 'test', 5)", (tid,))
        cur.execute("INSERT INTO invite_token_rights (token_id, p2p_right) VALUES (%s, 'can_message')", (tid,))
    return tid


def _handshake(guest: Node, host: Node, *, token=None, grant=None, cert=True) -> dict:
    body = {"public_key_hex": guest.pub, "username": guest.name, "invite_code": guest.invite}
    if token or grant:
        ts = int(time.time())
        label = "token_handshake" if token else "grant_handshake"
        tid = token or grant["token_id"]
        body.update(ts=ts, signature=guest.key.sign(f"{label}:{ts}:{tid}:{host.invite}".encode()).hex())
        body.update({"token_id": token} if token else {"grant": grant})
    else:
        from desktop.p2p import chat_wire
        body = chat_wire.sign_handshake(guest.key.sign, body, host.invite)
    if cert:
        body["birth_cert"] = guest.cert
    return body


def _sigs(pool, subject):
    with pool() as conn, conn.cursor() as cur:
        cur.execute("SELECT cert_sig FROM p2p_witness WHERE subject = %s AND channel = 'handshake' "
                    "ORDER BY id", (subject,))
        return [r[0] for r in cur.fetchall()]


def test_an_accepted_token_handshake_is_a_sighting(server, pool):
    s, srv, served = server
    guest = Node("guest")
    tid = _token(pool)
    status, reply = _post(served.url, _handshake(guest, s, token=tid))
    assert (status, reply["accepted"]) == (200, True)
    assert _sigs(pool, guest.pub) == [guest.cert["sig"]]
    # The re-accept of a friend, any token id, no use burned: the same
    # certificate however spelled adds nothing…
    spaced = " ".join(guest.cert["sig"][i:i + 2] for i in range(0, 128, 2))
    for sig in (guest.cert["sig"].upper(), spaced):
        body = _handshake(guest, s, token=str(uuid.uuid4()))
        body["birth_cert"] = dict(guest.cert, sig=sig)
        assert _post(served.url, body)[0] == 200
    assert _sigs(pool, guest.pub) == [guest.cert["sig"]]
    # …while the friend's new certificate (its email upgrade) is a sighting,
    # and so is a grant recovery's.
    guest.cert = certificate(guest.pub, method="email", email_token="ab" * 32, email_class="major")
    assert _post(served.url, _handshake(guest, s, token=tid))[0] == 200
    with pool() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM friends WHERE public_key_hex = %s", (guest.pub,))
    guest.cert = certificate(guest.pub, method="email", email_token="cd" * 32, email_class="other")
    assert _post(served.url, _handshake(guest, s, grant=reply["grant"]))[0] == 200
    assert len(_sigs(pool, guest.pub)) == 3


def test_the_managers_probe_sink_records_through_the_sync_server(server, pool, monkeypatch, tmp_path):
    from desktop.p2p.p2p_manager import P2PManager
    s, srv, served = server
    c = Node("client")
    d = node_identity._identity_dir()
    (d / "node_info.json").write_text(json.dumps({"node_id": c.pub, "public_key_hex": c.pub,
                                                  "username": c.name, "invite_code": c.invite}))
    mgr = P2PManager("unused", {})
    assert mgr._peer_identity().witness is not None
    mgr._sync_server = srv
    other = Node("other")
    mgr._peer_identity().witness(other.cert, other.pub)
    assert _rows(pool, other.pub) == ["probe"]
    mgr._sync_server = None
    mgr._peer_identity().witness(c.cert, c.pub)              # no sync server: nothing to record into
    assert _rows(pool, c.pub) == []


def test_the_served_certificate_follows_its_file(monkeypatch):
    me = Node("me")
    d = node_identity._identity_dir()
    (d / "node_info.json").write_text(json.dumps({"node_id": me.pub, "public_key_hex": me.pub,
                                                  "username": me.name}))
    monkeypatch.setattr(birth_cert, "_served", (None, None))
    path = d / "birth_certificate.json"
    assert birth_cert.served_certificate() is None
    path.write_text(json.dumps(me.cert))
    assert birth_cert.served_certificate() == me.cert
    upgraded = certificate(me.pub, method="email", email_token="ab" * 32, email_class="major")
    path.write_text(json.dumps(upgraded))
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 10 ** 9))
    assert birth_cert.served_certificate() == upgraded
    path.write_text(json.dumps(certificate(Node("other").pub)))          # another key's: not this node's
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 2 * 10 ** 9))
    assert birth_cert.served_certificate() is None
    path.unlink()
    assert birth_cert.served_certificate() is None


def test_a_refused_or_unsigned_handshake_is_no_sighting(server, pool):
    s, srv, served = server
    stranger, friend = Node("stranger"), Node("friend")
    assert _post(served.url, _handshake(stranger, s, token=str(uuid.uuid4())))[0] == 403   # no such token
    bad = _handshake(stranger, s, token=_token(pool))
    bad["signature"] = "00" * 64
    assert _post(served.url, bad)[0] == 403
    assert _post(served.url, _handshake(stranger, s))[0] == 403                            # not a friend
    srv._chat_service.add_friend(public_key_hex=friend.pub, invite_code=friend.invite, username=friend.name)
    status, reply = _post(served.url, _handshake(friend, s))                              # classic, accepted
    assert (status, reply["accepted"]) == (200, True)
    assert _rows(pool, stranger.pub) == [] and _rows(pool, friend.pub) == []
    assert _post(served.url, _handshake(stranger, s, token=_token(pool)))[0] == 200        # the same guest, signed
    assert _rows(pool, stranger.pub) == ["handshake"]
