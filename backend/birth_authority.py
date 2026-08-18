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
#
# Grace (2026-08-18): a bump used to cut every peer on the previous release
# off at once (their certificates failed the shape check) and to leave every
# pow identity proof-less until it re-mined. Verification now accepts the
# CURRENT and the PREVIOUS format, each over its own payload (fields are
# only ever appended); issuance is always current, clients upgrade eagerly
# but STAGE the new pow certificate until its proof is mined
# (identity_proof.stage_certificate) — an identity is never left without a
# valid (certificate, proof) pair. When bumping: CERT_VERSION += 1, add the
# new field to canonical_payload/_valid_shape, drop the oldest accepted
# version. MIRRORS worker/verify.js ACCEPTED_CERT_VERSIONS.
CERT_VERSION = 4
ACCEPTED_CERT_VERSIONS = (4, 3)
CERT_METHODS = ("pow", "email")
EMAIL_CLASSES = ("major", "other", "disposable")

_ISSUED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_EMAIL_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_payload(cert: dict) -> bytes:
    """The payload of the certificate's OWN version (fields are appended per
    version: v3 added email_domain_token, v4 predecessor); the email fields
    and the predecessor are empty for pow. Mirrors birthPayload() in
    worker/verify.js."""
    v = int(cert.get("v") or CERT_VERSION)
    fields = [cert['pubkey'], cert['issued_at'], cert['method'], str(int(cert['difficulty'])),
              str(int(cert['params_version'])), cert.get('email_token') or '', cert.get('email_class') or '']
    if v >= 3:
        fields.append(cert.get('email_domain_token') or '')
    if v >= 4:
        fields.append(cert.get('predecessor') or '')
    return (f"sautium-birth:v{v}:" + ":".join(fields)).encode("utf-8")


def is_current(cert: dict) -> bool:
    """A verified certificate of an accepted-but-superseded version: still
    good on the wire, but the holder should refetch (and re-mine, staged)."""
    return cert.get("v") == CERT_VERSION


def _valid_shape(cert: dict, trusted: list) -> bool:
    v = cert.get("v")
    if v not in ACCEPTED_CERT_VERSIONS or isinstance(v, bool) or cert.get("issuer") not in trusted:
        return False
    if v < 4 and cert.get("predecessor"):
        return False                                   # a field the version does not carry
    if v < 3 and cert.get("email_domain_token"):
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
