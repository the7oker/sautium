"""Identity (birth) certificates — the identity-age anchor of the P2P trust
design and, since v2, the challenge of the identity proof-of-work.

A certificate is the network authority's signed statement {pubkey,
issued_at, method, difficulty, params_version, [email_token, email_class]}:
"the network first saw this identity at this moment, under this policy". It
anchors identity age for cold-start trust weights (see
docs/design/P2P-SYNC-INTEGRITY.md) — a certificate grants initial weight,
never immunity; a method:pow certificate is worth nothing until its holder
also presents the proof mined over its signature (desktop/p2p/identity_pow.py).

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

import re
from typing import Optional

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
# v4 (2026-08-18) adds predecessor — the pubkey that held this mailbox
# before this one registered it (a password change makes a new key; the
# notary names the link, nodes carry witnessed age AND bans across it).
# Email records only, empty otherwise.
CERT_VERSION = 4
CERT_METHODS = ("pow", "email")
EMAIL_CLASSES = ("major", "other", "disposable")

_ISSUED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_EMAIL_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_payload(cert: dict) -> bytes:
    """Fixed eleven-field payload; the three email fields and the predecessor
    are empty for pow. Mirrors birthPayload() in worker/verify.js."""
    return (
        f"sautium-birth:v{CERT_VERSION}:{cert['pubkey']}:{cert['issued_at']}:"
        f"{cert['method']}:{int(cert['difficulty'])}:{int(cert['params_version'])}:"
        f"{cert.get('email_token') or ''}:{cert.get('email_class') or ''}:"
        f"{cert.get('email_domain_token') or ''}:{cert.get('predecessor') or ''}"
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
    predecessor = cert.get("predecessor")
    if cert["method"] == "email":
        return (isinstance(token, str) and bool(_EMAIL_TOKEN_RE.match(token))
                and klass in EMAIL_CLASSES
                and (not domain or (isinstance(domain, str) and bool(_EMAIL_TOKEN_RE.match(domain))))
                and (not predecessor or (isinstance(predecessor, str) and bool(_EMAIL_TOKEN_RE.match(predecessor))
                                         and predecessor != cert.get("pubkey"))))
    return not token and not klass and not domain and not predecessor


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
