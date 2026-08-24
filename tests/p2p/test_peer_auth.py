"""Wire format v1 — the implementation must reproduce the fixed-seed vectors
byte for byte (tests/p2p/vectors/peer_wire_v1.json)."""

import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop.p2p import peer_auth as pa
from desktop.p2p import birth_cert, identity_proof

VECTORS = json.loads((Path(__file__).parent / "vectors" / "peer_wire_v1.json").read_text())
CLIENT = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(VECTORS["client_seed"]))
SERVER_PUB = VECTORS["server_pubkey"]


def test_canonical_message_and_signature_match_vectors():
    for v in VECTORS["requests"]:
        body = v["body_utf8"].encode("utf-8")
        msg = pa.canonical_message(SERVER_PUB, v["method"], v["request_target"], v["ts"], body)
        assert msg.decode() == v["canonical_message"]
        headers = pa.sign_headers(CLIENT.sign, VECTORS["client_pubkey"], SERVER_PUB,
                                  v["method"], v["request_target"], body, ts=v["ts"])
        assert headers == v["headers"]


def test_verify_accepts_vectors_and_rejects_everything_else():
    for v in VECTORS["requests"]:
        body = v["body_utf8"].encode("utf-8")
        h = dict(v["headers"])
        ok = lambda hh, **kw: pa.verify_request(hh, SERVER_PUB, v["method"], v["request_target"], body, now=v["ts"] + kw.get("skew", 0))
        assert ok(h) == (VECTORS["client_pubkey"], None)
        assert ok(h, skew=pa.TS_WINDOW) == (VECTORS["client_pubkey"], None)
        assert ok(h, skew=pa.TS_WINDOW + 1) == (None, "identity signature stale")
        assert ok(h, skew=-(pa.TS_WINDOW + 1)) == (None, "identity signature stale")
        # a different recipient, method, target or body invalidates the signature
        assert pa.verify_request(h, "ab" * 32, v["method"], v["request_target"], body, now=v["ts"])[1] == "identity signature invalid"
        assert pa.verify_request(h, SERVER_PUB, "PUT", v["request_target"], body, now=v["ts"])[1] == "identity signature invalid"
        assert pa.verify_request(h, SERVER_PUB, v["method"], v["request_target"] + "&x=1", body, now=v["ts"])[1] == "identity signature invalid"
        assert pa.verify_request(h, SERVER_PUB, v["method"], v["request_target"], body + b" ", now=v["ts"])[1] == "identity signature invalid"
        # incomplete / malformed
        assert pa.verify_request({}, SERVER_PUB, v["method"], v["request_target"], body) == (None, None)
        assert pa.verify_request({pa.HDR_PUBKEY: h[pa.HDR_PUBKEY]}, SERVER_PUB, v["method"], v["request_target"], body)[1] == "identity signature incomplete"
        assert pa.verify_request(dict(h, **{pa.HDR_SIG: "zz"}), SERVER_PUB, v["method"], v["request_target"], body, now=v["ts"])[1] == "identity signature malformed"
        assert pa.verify_request(dict(h, **{pa.HDR_PUBKEY: "ab" * 32}), SERVER_PUB, v["method"], v["request_target"], body, now=v["ts"])[1] == "identity signature invalid"


def test_cert_bundle_round_trip_matches_vector():
    bundle = json.loads(VECTORS["cert_bundle"]["json"])
    encoded = pa.encode_cert_bundle(bundle["cert"], bundle["proof"])
    assert encoded == VECTORS["cert_bundle"]["header"][pa.HDR_CERT]
    decoded = pa.decode_cert_bundle(encoded)
    assert decoded == bundle
    assert birth_cert.verify_certificate(decoded["cert"], trusted=[VECTORS["authority_pubkey"]])
    assert identity_proof.proof_binds(decoded["proof"], decoded["cert"])
    assert pa.decode_cert_bundle("not base64!") is None
    assert pa.decode_cert_bundle(pa.encode_cert_bundle({"v": 2}, "junk")) is None   # proof must be an object
    assert pa.decode_cert_bundle(pa.encode_cert_bundle({"v": 2}, None)) == {"cert": {"v": 2}, "proof": None}


def test_identity_bound_paths():
    assert pa.is_identity_bound("/api/sync/inventory")
    assert pa.is_identity_bound("/api/mb/search?q=x")
    assert not pa.is_identity_bound("/health")
    assert not pa.is_identity_bound("/api/chat/handshake")
    assert not pa.is_identity_bound("/api/relay/voucher")


# ---------------------------------------------------------------------------
# TLS channel binding + pinning
# ---------------------------------------------------------------------------

import datetime
import socket
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

NODE = Ed25519PrivateKey.from_private_bytes(b"\x07" * 32)
NODE_PUB = NODE.public_key().public_bytes_raw().hex()
OTHER = Ed25519PrivateKey.from_private_bytes(b"\x08" * 32)
OTHER_PUB = OTHER.public_key().public_bytes_raw().hex()


def _make_cert(bind_key=NODE, bound=True, tamper_sig=False):
    """Self-signed ECDSA cert, optionally carrying the node-key binding."""
    tls_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-peer")])
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(tls_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
    )
    if bound:
        spki = tls_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        blob = pa.tls_binding_value(
            bind_key.sign, bind_key.public_key().public_bytes_raw().hex(), spki)
        if tamper_sig:
            blob = blob[:-1] + bytes([blob[-1] ^ 1])
        builder = builder.add_extension(
            x509.UnrecognizedExtension(
                x509.ObjectIdentifier(pa.TLS_BINDING_OID), blob),
            critical=False)
    cert = builder.sign(tls_key, hashes.SHA256())
    key_pem = tls_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    return cert.public_bytes(serialization.Encoding.PEM), key_pem


def test_tls_binding_round_trip_and_rejections():
    cert_pem, _ = _make_cert()
    der = x509.load_pem_x509_certificate(cert_pem).public_bytes(
        serialization.Encoding.DER)
    assert pa.tls_bound_pubkey(der) == NODE_PUB

    unbound_pem, _ = _make_cert(bound=False)
    der_unbound = x509.load_pem_x509_certificate(unbound_pem).public_bytes(
        serialization.Encoding.DER)
    assert pa.tls_bound_pubkey(der_unbound) is None

    tampered_pem, _ = _make_cert(tamper_sig=True)
    der_tampered = x509.load_pem_x509_certificate(tampered_pem).public_bytes(
        serialization.Encoding.DER)
    assert pa.tls_bound_pubkey(der_tampered) is None

    assert pa.tls_bound_pubkey(b"not a cert") is None


def _serve_tls(cert_pem: bytes, key_pem: bytes, tmp_path):
    """One-shot TLS server on an ephemeral loopback port."""
    cert_file, key_file = tmp_path / "c.pem", tmp_path / "k.pem"
    cert_file.write_bytes(cert_pem)
    key_file.write_bytes(key_pem)
    srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    srv_ctx.load_cert_chain(str(cert_file), str(key_file))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]

    def _serve():
        try:
            conn, _ = sock.accept()
            conn.settimeout(5)
            try:
                tls = srv_ctx.wrap_socket(conn, server_side=True)
                tls.recv(1)
                tls.close()
            except (ssl.SSLError, OSError):
                conn.close()
        finally:
            sock.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    return port, t


def _connect(port: int, ctx: ssl.SSLContext):
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    tls = ctx.wrap_socket(raw)   # handshake runs the pin check
    tls.send(b"x")
    tls.close()


def test_pinned_handshake_accepts_the_bound_node(tmp_path):
    port, t = _serve_tls(*_make_cert(), tmp_path)
    ctx = pa.pinned_ssl_context(NODE_PUB)
    _connect(port, ctx)
    assert pa.pinned_pubkey(ctx) == NODE_PUB
    t.join(5)


def test_pinned_handshake_rejects_a_foreign_node(tmp_path):
    port, t = _serve_tls(*_make_cert(), tmp_path)   # bound to NODE
    with pytest.raises(ssl.SSLCertVerificationError):
        _connect(port, pa.pinned_ssl_context(OTHER_PUB))
    t.join(5)


def test_pinned_handshake_rejects_an_unbound_cert(tmp_path):
    port, t = _serve_tls(*_make_cert(bound=False), tmp_path)
    with pytest.raises(ssl.SSLCertVerificationError):
        _connect(port, pa.pinned_ssl_context(NODE_PUB))
    t.join(5)


def test_tofu_context_locks_on_first_contact(tmp_path):
    port, t = _serve_tls(*_make_cert(), tmp_path)
    ctx = pa.pinned_ssl_context()          # no expectation
    assert pa.pinned_pubkey(ctx) is None
    _connect(port, ctx)
    assert pa.pinned_pubkey(ctx) == NODE_PUB
    t.join(5)
    # locked now: a different node on the same context must fail
    port2, t2 = _serve_tls(*_make_cert(bind_key=OTHER), tmp_path)
    with pytest.raises(ssl.SSLCertVerificationError):
        _connect(port2, ctx)
    t2.join(5)


def test_asyncio_path_uses_the_same_check(tmp_path):
    """aiohttp rides asyncio's wrap_bio → SSLObject subclass — verify that
    path enforces the pin too."""
    import asyncio

    async def _dial(port, ctx):
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", port, ssl=ctx)
        writer.write(b"x")
        await writer.drain()
        writer.close()

    port, t = _serve_tls(*_make_cert(), tmp_path)
    with pytest.raises(ssl.SSLCertVerificationError):
        asyncio.run(_dial(port, pa.pinned_ssl_context(OTHER_PUB)))
    t.join(5)

    port2, t2 = _serve_tls(*_make_cert(), tmp_path)
    ctx = pa.pinned_ssl_context(NODE_PUB)
    asyncio.run(_dial(port2, ctx))
    assert pa.pinned_pubkey(ctx) == NODE_PUB
    t2.join(5)


def test_ensure_cert_regenerates_until_bound(tmp_path):
    """backend/tls_gen.ensure_cert: an unbound cert is regenerated once a
    binding is requested, then stays stable."""
    import sys
    backend = str(Path(__file__).resolve().parents[2] / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from tls_gen import ensure_cert

    cert_path, _ = ensure_cert(tmp_path)                 # identity-less boot
    unbound = cert_path.read_bytes()
    der = x509.load_pem_x509_certificate(unbound).public_bytes(
        serialization.Encoding.DER)
    assert pa.tls_bound_pubkey(der) is None

    ensure_cert(tmp_path, binding=(NODE_PUB, NODE.sign))  # account now exists
    bound = cert_path.read_bytes()
    assert bound != unbound
    der = x509.load_pem_x509_certificate(bound).public_bytes(
        serialization.Encoding.DER)
    assert pa.tls_bound_pubkey(der) == NODE_PUB

    ensure_cert(tmp_path, binding=(NODE_PUB, NODE.sign))  # idempotent
    assert cert_path.read_bytes() == bound

    ensure_cert(tmp_path)                                 # no binding asked —
    assert cert_path.read_bytes() == bound                # no churn either
