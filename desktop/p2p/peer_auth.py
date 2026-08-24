"""Peer request authentication — wire format v1 (P2P-SYNC-INTEGRITY.md
§ "Wire format v1"; vectors in tests/p2p/vectors/peer_wire_v1.json).

A peer request may carry an Ed25519 signature by the requesting node
(`X-Sautium-Peer-Pubkey/-Ts/-Sig`) over

    sautium-request:v1 ⏎ {server_pubkey} ⏎ {METHOD} ⏎ {request-target} ⏎ {ts} ⏎ {sha256_hex(body)}

— one recipient per signature (the server's pubkey is inside), the raw
request-target as sent, ±60 s. Unsigned requests are the anonymous lane;
signed ones are the stranger lane until the identity registry says
`verified` (and ripe): the identity lane. On first contact the requester
introduces itself with `X-Sautium-Peer-Cert` = base64url {cert, proof};
the server only records it (verification is lazy — identity_registry.py).

Shared by the launcher and the Docker backend; both peer surfaces verify
with the same function, both peer clients sign with the same function.
Depends on `cryptography` only.
"""

import base64
import hashlib
import json
import ssl
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Tuple

HDR_PUBKEY = "X-Sautium-Peer-Pubkey"
HDR_TS = "X-Sautium-Peer-Ts"
HDR_SIG = "X-Sautium-Peer-Sig"
HDR_CERT = "X-Sautium-Peer-Cert"
HDR_IDENTITY = "X-Sautium-Peer-Identity"      # response: unknown|unverified|verified|failed|banned
HDR_LANE = "X-Sautium-Peer-Lane"              # response: anonymous|stranger|identity

TS_WINDOW = 60
IDENTITY_BOUND_PREFIXES = ("/api/sync/", "/api/mb/")

LANE_ANONYMOUS = "anonymous"
LANE_STRANGER = "stranger"
LANE_IDENTITY = "identity"


def is_identity_bound(path: str) -> bool:
    return path.startswith(IDENTITY_BOUND_PREFIXES)


def canonical_message(server_pubkey: str, method: str, request_target: str,
                      ts: int, body: bytes) -> bytes:
    return ("sautium-request:v1\n" + server_pubkey.lower() + "\n" + method.upper() + "\n"
            + request_target + "\n" + str(int(ts)) + "\n"
            + hashlib.sha256(body or b"").hexdigest()).encode("utf-8")


def sign_headers(sign: Callable[[bytes], bytes], client_pubkey: str, server_pubkey: str,
                 method: str, request_target: str, body: bytes,
                 ts: Optional[int] = None) -> dict:
    ts = int(time.time()) if ts is None else int(ts)
    sig = sign(canonical_message(server_pubkey, method, request_target, ts, body))
    return {HDR_PUBKEY: client_pubkey.lower(), HDR_TS: str(ts), HDR_SIG: sig.hex()}


def encode_cert_bundle(cert: dict, proof: Optional[dict]) -> str:
    raw = json.dumps({"cert": cert, "proof": proof}, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cert_bundle(value: str) -> Optional[dict]:
    """{"cert": …, "proof": …|None} or None for anything malformed. The
    certificate itself is verified by the caller with its authority mirror."""
    try:
        padded = value + "=" * (-len(value) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("cert"), dict):
        return None
    proof = data.get("proof")
    if proof is not None and not isinstance(proof, dict):
        return None
    return {"cert": data["cert"], "proof": proof}


def verify_request(headers: Mapping[str, str], server_pubkey: str, method: str,
                   request_target: str, body: bytes,
                   now: Optional[float] = None) -> Tuple[Optional[str], Optional[str]]:
    """(pubkey, None) for a valid signature, (None, None) for an unsigned
    request, (None, error) for a signature that is present but wrong —
    the caller answers 403 in the last case."""
    pubkey = headers.get(HDR_PUBKEY)
    ts = headers.get(HDR_TS)
    sig = headers.get(HDR_SIG)
    if not (pubkey or ts or sig):
        return None, None
    if not (pubkey and ts and sig):
        return None, "identity signature incomplete"
    try:
        ts_int = int(ts)
        raw = bytes.fromhex(pubkey)
        sig_bytes = bytes.fromhex(sig)
    except ValueError:
        return None, "identity signature malformed"
    if len(raw) != 32 or len(sig_bytes) != 64:
        return None, "identity signature malformed"
    if abs((now if now is not None else time.time()) - ts_int) > TS_WINDOW:
        return None, "identity signature stale"
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(raw).verify(
            sig_bytes, canonical_message(server_pubkey, method, request_target, ts_int, body))
    except (InvalidSignature, ValueError):
        return None, "identity signature invalid"
    return pubkey.lower(), None


@dataclass(frozen=True)
class PeerIdentity:
    """What a peer CLIENT needs to sign and introduce itself: its pubkey,
    a signer over raw bytes, and a loader for the {cert, proof} bundle
    (called lazily — the certificate can arrive after the client exists)."""
    pubkey: str
    sign: Callable[[bytes], bytes]
    cert_bundle: Callable[[], Optional[dict]]


# ---------------------------------------------------------------------------
# TLS channel binding — the peer transport's server authentication.
#
# Peer TLS certs are self-signed, so chain PKI proves nothing; before this,
# clients ran CERT_NONE and learned a server's node_id from its /health body,
# which any impostor on the path could write. The binding closes that: the
# server's cert carries a private X.509 extension (TLS_BINDING_OID)
# holding the node's Ed25519 pubkey and its signature over the cert's OWN
# SubjectPublicKeyInfo. The TLS handshake proves possession of the TLS key,
# the extension proves the node key vouches for that TLS key — together the
# channel belongs to the node. Verification is per-handshake, client-side,
# via pinned_ssl_context(); servers embed the extension in
# desktop/node_identity.ensure_tls_cert (launcher) and backend/tls_gen.py
# (Docker — the same cert the master's Caddy front serves).
# ---------------------------------------------------------------------------

# Under the IANA private-enterprise arc with a project-derived number
# (first 4 bytes of the UUID v5 namespace, masked to 31 bits — PENs are
# assigned sequentially and sit below 10^5, so no collision in practice).
# NOT the prettier 2.25.{uuid} arc: Go's x509 (the master's Caddy front)
# rejects OID components over int32 as "malformed extension OID field"
# and then refuses to load the whole cert.
TLS_BINDING_OID = "1.3.6.1.4.1.767683595.1"
_TLS_BINDING_CONTEXT = b"sautium-tls-bind:v1:"
_TLS_BINDING_VERSION = b"\x01"


def tls_binding_value(sign: Callable[[bytes], bytes], pubkey_hex: str,
                      spki_der: bytes) -> bytes:
    """Extension payload: version ‖ node pubkey (32) ‖ Ed25519 sig (64) over
    the context string + sha256 of the TLS key's SubjectPublicKeyInfo."""
    sig = sign(_TLS_BINDING_CONTEXT + hashlib.sha256(spki_der).digest())
    return _TLS_BINDING_VERSION + bytes.fromhex(pubkey_hex) + sig


def tls_bound_pubkey(cert_der: bytes) -> Optional[str]:
    """The node pubkey (lowercase hex) a served certificate is bound to,
    verified against the certificate's actual SPKI — or None when the cert
    carries no binding or a wrong one."""
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat)
    try:
        cert = x509.load_der_x509_certificate(cert_der)
        ext = cert.extensions.get_extension_for_oid(
            x509.ObjectIdentifier(TLS_BINDING_OID))
        blob = ext.value.value
    except Exception:
        return None
    if len(blob) != 1 + 32 + 64 or blob[:1] != _TLS_BINDING_VERSION:
        return None
    node_pub, sig = blob[1:33], blob[33:]
    spki = cert.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    try:
        Ed25519PublicKey.from_public_bytes(node_pub).verify(
            sig, _TLS_BINDING_CONTEXT + hashlib.sha256(spki).digest())
    except (InvalidSignature, ValueError):
        return None
    return node_pub.hex()


def _check_channel(obj) -> None:
    """Post-handshake check shared by the socket (urllib) and BIO (asyncio/
    aiohttp) paths. `obj` is an SSLSocket or SSLObject: both expose
    .context and .getpeercert."""
    der = obj.getpeercert(binary_form=True)
    bound = tls_bound_pubkey(der) if der else None
    if bound is None:
        raise ssl.SSLCertVerificationError(
            "peer TLS cert carries no valid node-key binding")
    ctx = obj.context
    expected = getattr(ctx, "_sautium_pin", None)
    if expected is None:
        ctx._sautium_pin = bound     # lock the context on first contact
    elif bound != expected:
        raise ssl.SSLCertVerificationError(
            f"peer TLS channel belongs to {bound[:16]}…, "
            f"expected {expected[:16]}…")


class _PinnedSSLSocket(ssl.SSLSocket):
    def do_handshake(self, *args, **kwargs):
        super().do_handshake(*args, **kwargs)
        _check_channel(self)


class _PinnedSSLObject(ssl.SSLObject):
    def do_handshake(self):
        super().do_handshake()   # raises SSLWantRead/Write until complete
        _check_channel(self)


def pinned_ssl_context(expected_pubkey: Optional[str] = None) -> ssl.SSLContext:
    """Client context that REQUIRES a valid node-key binding in the server
    cert. With `expected_pubkey` the handshake fails unless the channel
    belongs to that node; without it, the first successful handshake locks
    the context to whatever verified key it saw (per-context trust-on-first-
    use) — read the result with pinned_pubkey(). One context per logical
    peer; never share a TOFU context between peers."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # chain PKI replaced by _check_channel
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.sslsocket_class = _PinnedSSLSocket
    ctx.sslobject_class = _PinnedSSLObject
    ctx._sautium_pin = expected_pubkey.lower() if expected_pubkey else None
    return ctx


def pinned_pubkey(ctx: ssl.SSLContext) -> Optional[str]:
    """The node pubkey the context is locked to — the constructor argument,
    or the key the first handshake verified. None until either happens."""
    return getattr(ctx, "_sautium_pin", None)
