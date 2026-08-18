"""Node-side identity (birth) certificate handling: verify, store, export,
import.

A certificate is the network authority's signed statement {pubkey,
issued_at, method, difficulty, params_version, [email_token, email_class]}
— "the network first saw this identity then, under this policy". The node
keeps its own certificate next to its identity files and can move it
between devices as a plain JSON file: identity is portable (the Argon2id
login+password derivation yields the same keypair everywhere), and the
certificate follows it either by re-requesting from the Worker (issuance
is idempotent — same certificate comes back; an email-verified identity
gets its method:email certificate back too) or by this export/import
path, which needs neither email nor connectivity.

Certificates are issued by the Cloudflare Worker (worker/verify.js) — the
network's only always-on component. TRUSTED_AUTHORITIES and the payload
format have THREE mirrors: this file, backend/birth_authority.py (launcher
build cannot import backend modules) and worker/verify.js. Update all three
together.
"""

import json
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# [0] is the active signing key (its private half is the Worker's
# BIRTH_SIGNING_KEY secret); later entries would be co-authorities.
TRUSTED_AUTHORITIES = [
    "a9f40f70a796926828d894d4384655963ae5bdce38d2c502ede75792552d33cd",
]

# v2 (2026-08-17): the birth certificate and the proof-of-work certificate are
# one type. `method: pow` is the anonymous path — its holder must present a
# separate proof mined over this certificate's signature (identity_pow.py);
# `method: email` carries no work requirement and adds the Worker-peppered
# email_token (equality = same mailbox, the similarity hard link and the
# succession key) plus a coarse email_class. `difficulty` is the expected
# number of ~2 GiB Argon2id attempts pinned at issuance; `issued_at` is the
# first-issuance moment (idempotent, the age anchor).
# v3 (2026-08-18) adds email_domain_token — the peppered DOMAIN of the
# mailbox: a similarity axis on its own (a rare domain shared by many
# identities) that the whole-address token cannot express; empty on email
# records migrated before the field existed until their next email touch.
CERT_VERSION = 3
CERT_METHODS = ("pow", "email")
EMAIL_CLASSES = ("major", "other", "disposable")

_ISSUED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_EMAIL_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_payload(cert: dict) -> bytes:
    """Fixed ten-field payload; the three email fields are empty for pow.
    Mirrors birthPayload() in worker/verify.js."""
    return (
        f"sautium-birth:v{CERT_VERSION}:{cert['pubkey']}:{cert['issued_at']}:"
        f"{cert['method']}:{int(cert['difficulty'])}:{int(cert['params_version'])}:"
        f"{cert.get('email_token') or ''}:{cert.get('email_class') or ''}:"
        f"{cert.get('email_domain_token') or ''}"
    ).encode("utf-8")


def _valid_shape(cert: dict, trusted: list) -> bool:
    if cert.get("v") != CERT_VERSION or cert.get("issuer") not in trusted:
        return False
    if cert.get("method") not in CERT_METHODS:
        return False
    if not isinstance(cert.get("issued_at"), str) or not _ISSUED_AT_RE.match(cert["issued_at"]):
        return False
    difficulty, params_version = cert.get("difficulty"), cert.get("params_version")
    if not (isinstance(difficulty, int) and not isinstance(difficulty, bool) and difficulty >= 1):
        return False
    if not (isinstance(params_version, int) and not isinstance(params_version, bool) and params_version >= 1):
        return False
    token, klass, domain = cert.get("email_token"), cert.get("email_class"), cert.get("email_domain_token")
    if cert["method"] == "email":
        return (isinstance(token, str) and bool(_EMAIL_TOKEN_RE.match(token))
                and klass in EMAIL_CLASSES
                and (not domain or (isinstance(domain, str) and bool(_EMAIL_TOKEN_RE.match(domain)))))
    return not token and not klass and not domain


def verify_certificate(cert: dict,
                       trusted: Optional[list] = None) -> bool:
    """Offline check that a certificate was signed by a trusted authority."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        if not _valid_shape(cert, trusted or TRUSTED_AUTHORITIES):
            return False
        raw = bytes.fromhex(cert["pubkey"])
        if len(raw) != 32:
            return False
        Ed25519PublicKey.from_public_bytes(raw)
        authority = Ed25519PublicKey.from_public_bytes(bytes.fromhex(cert["issuer"]))
        authority.verify(bytes.fromhex(cert["sig"]), canonical_payload(cert))
        return True
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return False


def _cert_path() -> Path:
    from desktop.node_identity import _identity_dir
    return _identity_dir() / "birth_certificate.json"


def proof_path() -> Path:
    from desktop.node_identity import _identity_dir
    from desktop.p2p.identity_proof import PROOF_FILENAME
    return _identity_dir() / PROOF_FILENAME


def load_proof() -> Optional[dict]:
    """This node's identity proof, only if it binds to the stored
    certificate (structural check — the worker re-verifies the work)."""
    from desktop.p2p import identity_proof
    cert = load_certificate()
    if cert is None:
        return None
    proof = identity_proof.load_proof(proof_path())
    return proof if identity_proof.proof_binds(proof, cert) else None


def _own_pubkey_hex() -> Optional[str]:
    from desktop.node_identity import get_account_info
    info = get_account_info()
    return (info or {}).get("public_key_hex")


def load_certificate() -> Optional[dict]:
    """This node's stored certificate, or None. Verified on every load so a
    tampered file degrades to 'no certificate' instead of a bad claim; a
    certificate for a different key (another account once used on this
    machine) counts as absent too — presenting it would be a protocol
    violation in the peer's eyes, not a harmless mismatch."""
    p = _cert_path()
    if not p.exists():
        return None
    try:
        cert = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not verify_certificate(cert):
        return None
    own = _own_pubkey_hex()
    if own and cert["pubkey"].lower() != own.lower():
        logger.warning("stored identity cert belongs to %s, not this node — ignoring",
                       cert["pubkey"][:8])
        return None
    return cert


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
    logger.info("identity cert stored (issued_at=%s method=%s)",
                cert["issued_at"], cert["method"])
    return True


def export_certificate(dest_path: str) -> bool:
    """Write this node's certificate + proof bundle to a user-chosen file.
    The proof is the minutes of mining that made a pow certificate worth
    something — it moves with the certificate, so the other device never
    mines twice."""
    cert = load_certificate()
    if cert is None:
        return False
    bundle = {"certificate": cert, "proof": load_proof()}
    Path(dest_path).write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    return True


def import_certificate(src_path: str) -> bool:
    """Adopt a certificate (+ proof) from a file moved from another device.
    Accepts the export bundle or a bare certificate as the Worker returns
    it; a proof that does not bind to the certificate is dropped."""
    from desktop.p2p import identity_proof
    try:
        data = json.loads(Path(src_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("birth cert import: unreadable file %s", src_path)
        return False
    if not isinstance(data, dict):
        return False
    cert = data.get("certificate", data)
    if not save_certificate(cert):
        return False
    proof = data.get("proof")
    if identity_proof.proof_binds(proof, cert):
        identity_proof.save_proof(proof_path(), proof)
    return True


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
    logger.info("identity cert obtained (issued_at=%s method=%s)",
                cert["issued_at"], cert["method"])
    return cert


def ensure_certificate() -> Optional[dict]:
    """This node's certificate: stored copy, or a silent fetch from the
    Worker when missing (issuance is idempotent — a re-fetch after moving
    devices returns the original birth date). A stored `method: pow`
    certificate is re-fetched too: the identity is portable (same
    login+password on another device), so the email upgrade may have
    happened elsewhere — one cheap request keeps this device from mining
    and presenting a superseded certificate. Unreachable Worker → the
    stored copy stands."""
    cert = load_certificate()
    if cert is not None and cert["method"] != "pow":
        return cert
    return request_certificate() or cert
