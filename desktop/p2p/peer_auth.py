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
