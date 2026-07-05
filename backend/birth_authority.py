"""Birth certificates — the identity-age anchor of the P2P trust design.

A birth certificate is the master node's signed statement {pubkey, born_at}:
"the network first saw this identity at this moment". It anchors identity
age for cold-start trust weights (see docs/design/P2P-SYNC-INTEGRITY.md) —
a certificate grants initial weight, never immunity.

Key properties:
- born_at is the FIRST issuance moment, witnessed by the master — not a
  self-reported creation date (which would carry no weight). Issuance is
  idempotent: re-requesting for the same pubkey returns the original
  certificate, so recreating an account on another device (the Argon2id
  login+password derivation yields the same keypair, hence the same pubkey)
  never changes the birth date.
- Certificates are public facts: anyone may request/relay/verify one. The
  export/import path (node side) moves them between devices without email
  or connectivity to the master.
- MASTER_PUBLIC_KEY_HEX is committed to the repo so every node verifies
  certificates offline against the same authority.
- The signing key is a GENERATED Ed25519 key in a gitignored file on the
  master host (settings.master_signing_key_path) — deliberately NOT derived
  from the node's P2P password: a password-derived authority key would be
  offline-brute-forceable at password entropy, and the P2P account must
  stay rotatable without invalidating the authority. Key rotation is
  survivable regardless: the registry stores born_at itself, so a new key
  can re-sign every certificate with the original dates.

issue_certificate() runs ONLY on the master node — any node without the
signing key file (or with a mismatching one) refuses.
"""

import logging
from datetime import timezone
from typing import Optional

logger = logging.getLogger(__name__)

# The network's certificate authority: the public half of the master node's
# generated signing key (data/authority/master_signing.key on the master).
MASTER_PUBLIC_KEY_HEX = "a9f40f70a796926828d894d4384655963ae5bdce38d2c502ede75792552d33cd"

CERT_VERSION = 1


def canonical_payload(pubkey_hex: str, born_at_iso: str) -> bytes:
    return f"sautium-birth:v{CERT_VERSION}:{pubkey_hex}:{born_at_iso}".encode("utf-8")


def _born_at_iso(dt) -> str:
    """Deterministic ISO form the signature covers: UTC, seconds precision,
    trailing Z."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _validate_pubkey(pubkey_hex: str) -> bytes:
    """Reject anything that is not a valid 32-byte Ed25519 public key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    raw = bytes.fromhex(pubkey_hex)
    if len(raw) != 32:
        raise ValueError("pubkey must be 32 bytes")
    Ed25519PublicKey.from_public_bytes(raw)   # raises on invalid point
    return raw


def verify_certificate(cert: dict,
                       master_pubkey_hex: str = MASTER_PUBLIC_KEY_HEX) -> bool:
    """Offline check that a certificate was signed by the master authority."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        if cert.get("v") != CERT_VERSION or cert.get("issuer") != master_pubkey_hex:
            return False
        _validate_pubkey(cert["pubkey"])
        master = Ed25519PublicKey.from_public_bytes(bytes.fromhex(master_pubkey_hex))
        master.verify(bytes.fromhex(cert["sig"]),
                      canonical_payload(cert["pubkey"], cert["born_at"]))
        return True
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return False


def _master_private_key():
    """The master's signing key, loaded from the gitignored key file.
    Returns None when this node is not the master (no file, or a file whose
    public half does not match the committed authority key)."""
    import os

    from cryptography.hazmat.primitives import serialization

    from config import settings

    path = settings.master_signing_key_path
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw).hex()
    if pub != MASTER_PUBLIC_KEY_HEX:
        logger.warning("signing key file present but does not match the "
                       "committed authority pubkey — refusing to issue")
        return None
    return key


def issue_certificate(conn, subject_pubkey_hex: str) -> Optional[dict]:
    """Master-only, idempotent issuance.

    First request for a pubkey signs {pubkey, now} and stores it; every
    later request returns the stored certificate unchanged — the birth
    date is immutable by construction. Returns None when this node is
    not the master authority.
    """
    subject_pubkey_hex = subject_pubkey_hex.lower()
    _validate_pubkey(subject_pubkey_hex)

    key = _master_private_key()
    if key is None:
        return None

    with conn.cursor() as cur:
        cur.execute("SELECT born_at, signature FROM birth_certificates "
                    "WHERE pubkey = %s", (subject_pubkey_hex,))
        row = cur.fetchone()
        if row:
            born_iso, sig = _born_at_iso(row[0]), row[1]
        else:
            from datetime import datetime
            born_iso = _born_at_iso(datetime.now(timezone.utc))
            sig = key.sign(canonical_payload(subject_pubkey_hex, born_iso)).hex()
            cur.execute(
                """INSERT INTO birth_certificates (pubkey, born_at, signature)
                   VALUES (%s, %s::timestamptz, %s)
                   ON CONFLICT (pubkey) DO NOTHING""",
                (subject_pubkey_hex, born_iso, sig))
            # A concurrent first request may have won the insert — re-read
            # so both callers return the SAME certificate.
            cur.execute("SELECT born_at, signature FROM birth_certificates "
                        "WHERE pubkey = %s", (subject_pubkey_hex,))
            row = cur.fetchone()
            born_iso, sig = _born_at_iso(row[0]), row[1]
    conn.commit()

    return {
        "v": CERT_VERSION,
        "pubkey": subject_pubkey_hex,
        "born_at": born_iso,
        "issuer": MASTER_PUBLIC_KEY_HEX,
        "sig": sig,
    }
