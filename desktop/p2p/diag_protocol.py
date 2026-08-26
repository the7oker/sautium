"""Support diagnostics — the wire protocol (P2P_NETWORK.md § "Support
diagnostics"). Shared by the launcher and the Docker backend.

A WARRANT is the master's signed request for a diagnostic bundle. It is
bound to ONE target node, expires, and names only SCOPES from the fixed
enum below — never a path, a table or a query: each scope is a fixed
collector with its own allowlist (desktop/diag_bundle.py). A node acts on
a warrant only when it arrives on the node's own wake stream to the master
AND verify_warrant() passes; the first receipt is decided by the
diag_warrants primary key (diag_events.receive_warrant), so a re-delivery
resumes an unfinished upload and a finished one is a no-op. The answer
goes to the pinned master, never to an address the warrant could name —
a captured warrant buys an attacker nothing, and a warrant for node A is
inert on node B.

REPORTS are a node's proactive event notes (EVENT_KINDS): states, counters
and error strings, never content. Fields in LOCAL_ONLY_FIELDS (log tails)
stay in the node's diag_events and travel only inside a bundle the master
asked for.

Both payloads are NaCl Box between the Curve25519 keys derived from the
Ed25519 node keys — the chat construction — so only the master reads
them. Depends on `cryptography` + PyNaCl.
"""

import json
import re
import time
import uuid
from datetime import datetime
from typing import Callable, Iterable, Optional, Tuple

from desktop.p2p.master_node import MASTER_PUBKEY_HEX

PROTOCOL_VERSION = 1
FRAME_TYPE = "diag_warrant"          # SSE frame type on the master's wake stream

SCOPES = ("system", "settings", "p2p", "jobs", "events", "logs", "chat")

EVENT_KINDS = frozenset({
    "node.started",
    "service.start_failed",
    "p2p.start_failed",
    "backend.crashed",
    "backend.restarted",
    "backend.gave_up",
    "agent.signin_opened",
    "agent.signin_timeout",
    "agent.state_changed",
    "chat.error",
    "sync.failed",
    "update.failed",
})

BUNDLE_MAX_BYTES = 32 * 1024 * 1024   # boxed tar.gz the master accepts
REPORT_MAX_BYTES = 256 * 1024         # boxed report the master accepts
REPORT_MAX_EVENTS = 50                # events per report
EVENT_DETAIL_MAX_CHARS = 4096         # one event's detail as JSON (node truncates, master refuses)
LOG_TAIL_BYTES = 2 * 1024 * 1024      # per log file inside a bundle
EVENTS_MAX_ROWS = 500                 # diag_events rows inside a bundle
CHAT_DEFAULT_DAYS = 14                # chat scope window when `since` is absent
WARRANT_TTL_DEFAULT_S = 7 * 86400     # parked warrants wait this long for the node
WARRANT_ISSUE_SKEW_S = 60             # issued_at may lead the node's clock by this much
BUNDLE_ACCEPT_GRACE_S = 3600          # the master still takes a bundle this long past expiry
REPORT_MAX_AGE_DAYS = 7               # older unreported events are never shipped
REPORT_MAX_PER_HOUR = 30              # node-side send cap and master-side per-node backstop
LOCAL_ONLY_FIELDS = frozenset({"log_tail"})


# ---------------------------------------------------------------------------
# Warrant: canonical payload + sign/verify (grant / voucher pattern)
# ---------------------------------------------------------------------------

def warrant_payload(warrant_id: str, issuer: str, target: str, issued_at: int,
                    expires_at: int, scopes: Iterable[str],
                    since: Optional[int]) -> bytes:
    return (f"sautium-diag-warrant:v{PROTOCOL_VERSION}:{warrant_id}:{issuer.lower()}:"
            f"{target.lower()}:{int(issued_at)}:{int(expires_at)}:"
            f"{','.join(sorted(scopes))}:{int(since) if since else ''}"
            ).encode("utf-8")


def sign_warrant(sign: Callable[[bytes], bytes], *, target: str, scopes: Iterable[str],
                 since: Optional[int] = None, issuer: str = MASTER_PUBKEY_HEX,
                 ttl_s: int = WARRANT_TTL_DEFAULT_S, now: Optional[float] = None,
                 warrant_id: Optional[str] = None) -> dict:
    issued_at = int(time.time() if now is None else now)
    warrant_id = warrant_id or str(uuid.uuid4())
    scopes = sorted(set(scopes))
    if not scopes or not set(scopes) <= set(SCOPES):
        raise ValueError(f"unknown scopes: {sorted(set(scopes) - set(SCOPES))}")
    expires_at = issued_at + int(ttl_s)
    since = int(since) if since else None
    sig = sign(warrant_payload(warrant_id, issuer, target, issued_at, expires_at,
                               scopes, since))
    return {
        "v": PROTOCOL_VERSION,
        "id": warrant_id,
        "issuer": issuer.lower(),
        "target": target.lower(),
        "issued_at": issued_at,
        "expires_at": expires_at,
        "scopes": scopes,
        "since": since,
        "sig": sig.hex(),
    }


def verify_warrant(warrant: dict, expected_target: str, now: Optional[float] = None,
                   issuer: str = MASTER_PUBKEY_HEX) -> Tuple[bool, Optional[str]]:
    """(True, None) for a warrant this node must act on; (False, reason)
    otherwise. `issuer` is the pinned master in production — a parameter
    only so tests can sign with a throwaway key."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    now = time.time() if now is None else now
    if not isinstance(warrant, dict) or warrant.get("v") != PROTOCOL_VERSION:
        return False, "unsupported version"
    try:
        warrant_id = str(uuid.UUID(str(warrant.get("id"))))
        issued_at = int(warrant["issued_at"])
        expires_at = int(warrant["expires_at"])
        scopes = list(warrant["scopes"])
        since = warrant.get("since")
        since = int(since) if since else None
        sig = bytes.fromhex(warrant["sig"])
    except (KeyError, TypeError, ValueError):
        return False, "malformed"
    if str(warrant.get("issuer", "")).lower() != issuer.lower():
        return False, "not from the master"
    if str(warrant.get("target", "")).lower() != expected_target.lower():
        return False, "addressed to another node"
    if not scopes or not set(scopes) <= set(SCOPES):
        return False, "unknown scope"
    if issued_at > now + WARRANT_ISSUE_SKEW_S:
        return False, "issued in the future"
    if now >= expires_at:
        return False, "expired"
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(issuer)).verify(
            sig, warrant_payload(warrant_id, issuer, expected_target, issued_at,
                                 expires_at, scopes, since))
    except (InvalidSignature, ValueError):
        return False, "bad signature"
    return True, None


def warrant_frame(warrant: dict) -> dict:
    return {"type": FRAME_TYPE, "warrant": warrant}


# ---------------------------------------------------------------------------
# Payload encryption: NaCl Box over the node keys (the chat construction)
# ---------------------------------------------------------------------------

def _curve_private(seed: bytes):
    from nacl.signing import SigningKey
    return SigningKey(seed).to_curve25519_private_key()


def _curve_public(pubkey_hex: str):
    from nacl.signing import VerifyKey
    return VerifyKey(bytes.fromhex(pubkey_hex)).to_curve25519_public_key()


def encrypt_to(seed: bytes, recipient_pubkey_hex: str, data: bytes) -> bytes:
    """nonce ‖ ciphertext, readable by `recipient` from `seed`'s owner."""
    from nacl.public import Box
    return bytes(Box(_curve_private(seed), _curve_public(recipient_pubkey_hex)).encrypt(data))


def decrypt_from(seed: bytes, sender_pubkey_hex: str, blob: bytes) -> bytes:
    """Inverse of encrypt_to; raises nacl.exceptions.CryptoError when the
    blob was not boxed between exactly these two keys."""
    from nacl.public import Box
    return Box(_curve_private(seed), _curve_public(sender_pubkey_hex)).decrypt(bytes(blob))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def strip_local_only(detail: dict) -> dict:
    return {k: v for k, v in (detail or {}).items() if k not in LOCAL_ONLY_FIELDS}


def _bounded_detail(detail: dict) -> dict:
    detail = strip_local_only(detail)
    if len(json.dumps(detail, default=str)) <= EVENT_DETAIL_MAX_CHARS:
        return detail
    return {"truncated": True, "keys": sorted(detail)}


def encode_report(events: Iterable[dict]) -> bytes:
    """The plaintext a node boxes for /api/diag/report: content-free,
    size-bounded events only — applied here, so no caller can forget."""
    out = [{"kind": e["kind"], "ts": e["ts"], "detail": _bounded_detail(e.get("detail") or {})}
           for e in events]
    return json.dumps({"v": PROTOCOL_VERSION, "events": out}, ensure_ascii=False,
                      default=str).encode("utf-8")


def decode_report(raw: bytes) -> list:
    """Validate a decrypted report on the master; ValueError on anything
    that is not a well-formed list of known event kinds."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"report is not JSON: {e}")
    if not isinstance(data, dict) or data.get("v") != PROTOCOL_VERSION:
        raise ValueError("unsupported report version")
    events = data.get("events")
    if not isinstance(events, list) or not events or len(events) > REPORT_MAX_EVENTS:
        raise ValueError("report event count out of range")
    out = []
    for e in events:
        if not isinstance(e, dict) or e.get("kind") not in EVENT_KINDS:
            raise ValueError("unknown event kind")
        ts = e.get("ts")
        detail = e.get("detail")
        if not isinstance(ts, str) or not isinstance(detail, dict):
            raise ValueError("malformed event")
        try:
            datetime.fromisoformat(ts)
        except ValueError:
            raise ValueError("malformed event timestamp")
        if len(json.dumps(detail, default=str)) > EVENT_DETAIL_MAX_CHARS:
            raise ValueError("event detail too large")
        out.append({"kind": e["kind"], "ts": ts, "detail": strip_local_only(detail)})
    return out


# ---------------------------------------------------------------------------
# Log scrubbing — logs are collected as text and MAY have caught a secret
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "[redacted]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [redacted]"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)(\"?\s*[:=]\s*\"?)[^\s\"',;&]+"),
     r"\1\2[redacted]"),
    (re.compile(r"(?i)\b(postgres(?:ql)?://[^:/\s@]+:)[^@\s]+@"), r"\1[redacted]@"),
    (re.compile(r"\b[0-9a-f]{128}\b"), "[redacted]"),          # signatures, key material
)


def scrub_secrets(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text
