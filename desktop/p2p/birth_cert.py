"""Node-side birth certificate handling: verify, store, export, import.

A birth certificate is the master authority's signed statement
{pubkey, born_at} — "the network first saw this identity then". The node
keeps its own certificate next to its identity files and can move it
between devices as a plain JSON file: identity is portable (the Argon2id
login+password derivation yields the same keypair everywhere), and the
certificate follows it either by re-requesting from the master (issuance
is idempotent — same certificate comes back) or by this export/import
path, which needs neither email nor connectivity.

Certificates are issued by the Cloudflare Worker (worker/verify.js) — the
network's only always-on component. TRUSTED_AUTHORITIES and the payload
format have THREE mirrors: this file, backend/birth_authority.py (launcher
build cannot import backend modules) and worker/verify.js. Update all three
together.
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# [0] is the active signing key (its private half is the Worker's
# BIRTH_SIGNING_KEY secret); later entries would be co-authorities.
TRUSTED_AUTHORITIES = [
    "a9f40f70a796926828d894d4384655963ae5bdce38d2c502ede75792552d33cd",
]

CERT_VERSION = 1


def canonical_payload(pubkey_hex: str, born_at_iso: str) -> bytes:
    return f"sautium-birth:v{CERT_VERSION}:{pubkey_hex}:{born_at_iso}".encode("utf-8")


def verify_certificate(cert: dict,
                       trusted: Optional[list] = None) -> bool:
    """Offline check that a certificate was signed by a trusted authority."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        issuer = cert.get("issuer")
        if cert.get("v") != CERT_VERSION or issuer not in (trusted or TRUSTED_AUTHORITIES):
            return False
        raw = bytes.fromhex(cert["pubkey"])
        if len(raw) != 32:
            return False
        Ed25519PublicKey.from_public_bytes(raw)
        authority = Ed25519PublicKey.from_public_bytes(bytes.fromhex(issuer))
        authority.verify(bytes.fromhex(cert["sig"]),
                         canonical_payload(cert["pubkey"], cert["born_at"]))
        return True
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return False


def _cert_path() -> Path:
    from desktop.node_identity import _identity_dir
    return _identity_dir() / "birth_certificate.json"


def _own_pubkey_hex() -> Optional[str]:
    from desktop.node_identity import get_account_info
    info = get_account_info()
    return (info or {}).get("public_key_hex")


def load_certificate() -> Optional[dict]:
    """This node's stored certificate, or None. Verified on every load so a
    tampered file degrades to 'no certificate' instead of a bad claim."""
    p = _cert_path()
    if not p.exists():
        return None
    try:
        cert = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return cert if verify_certificate(cert) else None


def save_certificate(cert: dict) -> bool:
    """Persist this node's certificate (e.g. fresh from the master).
    Rejects invalid signatures and certificates for a different identity."""
    if not verify_certificate(cert):
        logger.warning("birth cert rejected: bad signature/format")
        return False
    own = _own_pubkey_hex()
    if own and cert["pubkey"].lower() != own.lower():
        logger.warning("birth cert rejected: subject %s is not this node",
                       cert["pubkey"][:8])
        return False
    _cert_path().write_text(json.dumps(cert, indent=2), encoding="utf-8")
    logger.info("birth cert stored (born_at=%s)", cert["born_at"])
    return True


def export_certificate(dest_path: str) -> bool:
    """Copy this node's certificate to a user-chosen file for transfer."""
    cert = load_certificate()
    if cert is None:
        return False
    Path(dest_path).write_text(json.dumps(cert, indent=2), encoding="utf-8")
    return True


def import_certificate(src_path: str) -> bool:
    """Adopt a certificate from a file (moved from another device)."""
    try:
        cert = json.loads(Path(src_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("birth cert import: unreadable file %s", src_path)
        return False
    return save_certificate(cert)


def request_certificate(pubkey_hex: Optional[str] = None,
                        sign_fn=None) -> Optional[dict]:
    """Request (or idempotently re-fetch) a certificate from the Worker
    authority. The request is signed by the subject key itself — only the
    key's owner can trigger first issuance.

    Defaults to this node's saved identity; pass pubkey_hex + sign_fn for a
    derived-but-not-yet-saved identity (wizard flows). The verified result
    is persisted next to the identity either way — the derivation is
    deterministic, so the file stays valid after create_account()."""
    if pubkey_hex is None:
        from desktop.node_identity import get_account_info, sign_message
        info = get_account_info()
        if not info:
            return None
        pubkey_hex = info["public_key_hex"]
        sign_fn = sign_message

    pubkey = pubkey_hex.lower()
    signature = sign_fn(f"birth:{pubkey}".encode("utf-8")).hex()

    from desktop.p2p.email_verify import _post_worker
    cert = _post_worker("/birth-certificate", {
        "pubkey_hex": pubkey,
        "signature": signature,
    })
    if not cert or cert.get("pubkey") != pubkey or not verify_certificate(cert):
        logger.warning("birth cert request failed or returned invalid cert")
        return None

    _cert_path().write_text(json.dumps(cert, indent=2), encoding="utf-8")
    logger.info("birth cert obtained (born_at=%s)", cert["born_at"])
    return cert


def ensure_certificate() -> Optional[dict]:
    """This node's certificate: stored copy, or a silent fetch from the
    Worker when missing (issuance is idempotent — a re-fetch after moving
    devices returns the original birth date)."""
    return load_certificate() or request_certificate()
