"""Birth certificates — the identity-age anchor of the P2P trust design.

A birth certificate is the network authority's signed statement
{pubkey, born_at}: "the network first saw this identity at this moment". It
anchors identity age for cold-start trust weights (see
docs/design/P2P-SYNC-INTEGRITY.md) — a certificate grants initial weight,
never immunity.

ISSUANCE LIVES ON THE CLOUDFLARE WORKER (worker/verify.js): the master node
is a laptop with laptop uptime and a home IP, so it cannot be the network's
issuance endpoint. The Worker holds the signing key as a secret
(BIRTH_SIGNING_KEY) and the registry in KV (`born:{pubkey}`); issuance is
idempotent — the first request anchors born_at, every later request returns
the same certificate, so recreating an account on another device (Argon2id
login+password → same keypair) never changes the birth date. The offline
backup of the signing key is data/authority/master_signing.key on the
master host (gitignored).

This module keeps the OFFLINE VERIFICATION side: any node (this backend
included) checks certificates against the committed authority keys without
network access.

TRUSTED_AUTHORITIES and the payload format have THREE mirrors — this file,
desktop/p2p/birth_cert.py (launcher build cannot import backend modules) and
worker/verify.js. Update all three together; authority key rotation also
means redeploying the Worker.
"""

from typing import Optional

# The network's certificate authorities. [0] is the active signing key (its
# private half is the Worker's BIRTH_SIGNING_KEY secret); later entries
# would be co-authorities (accepted for verification, not signing).
TRUSTED_AUTHORITIES = [
    "a9f40f70a796926828d894d4384655963ae5bdce38d2c502ede75792552d33cd",
]

CERT_VERSION = 1


def canonical_payload(pubkey_hex: str, born_at_iso: str) -> bytes:
    return f"sautium-birth:v{CERT_VERSION}:{pubkey_hex}:{born_at_iso}".encode("utf-8")


def _validate_pubkey(pubkey_hex: str) -> bytes:
    """Reject anything that is not a valid 32-byte Ed25519 public key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    raw = bytes.fromhex(pubkey_hex)
    if len(raw) != 32:
        raise ValueError("pubkey must be 32 bytes")
    Ed25519PublicKey.from_public_bytes(raw)   # raises on invalid point
    return raw


def verify_certificate(cert: dict,
                       trusted: Optional[list] = None) -> bool:
    """Offline check that a certificate was signed by a trusted authority."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        issuer = cert.get("issuer")
        if cert.get("v") != CERT_VERSION or issuer not in (trusted or TRUSTED_AUTHORITIES):
            return False
        _validate_pubkey(cert["pubkey"])
        authority = Ed25519PublicKey.from_public_bytes(bytes.fromhex(issuer))
        authority.verify(bytes.fromhex(cert["sig"]),
                         canonical_payload(cert["pubkey"], cert["born_at"]))
        return True
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return False
