"""
P2P identity for Docker backend.

Derives deterministic Ed25519 keypair from username+password using the same
Argon2id algorithm as the desktop launcher. This makes the Docker backend
a regular P2P peer with a stable identity and invite code.

Identity is derived in-memory at startup (no files saved).
"""

import hashlib
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Same parameters as desktop/node_identity.py
ARGON2_TIME_COST = 4
ARGON2_MEMORY_COST = 262144  # 256 MB
ARGON2_PARALLELISM = 2
ARGON2_HASH_LEN = 32

# Mirrors desktop/node_identity.USERNAME_RE and worker/verify.js — the
# username is embedded in invite codes and Worker KV keys, so it must stay
# URL/CLI-safe at every identity-creation boundary.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")


def make_invite_code(username: str, public_key_raw: bytes) -> str:
    """Mirror of desktop/node_identity.make_invite_code — keep in step."""
    digest = hashlib.sha256(public_key_raw).digest()[:6]
    h = digest.hex().upper()
    return f"{username}#{h[:4]}-{h[4:8]}-{h[8:]}"


def parse_invite_code(invite_code: str) -> tuple:
    """Mirror of desktop/node_identity.parse_invite_code — keep in step."""
    if "#" not in invite_code:
        raise ValueError(f"Invalid invite code format: {invite_code}")
    username, hash_part = invite_code.split("#", 1)
    clean = hash_part.replace("-", "")
    if len(clean) != 12:
        raise ValueError(f"Invalid invite code hash length: {hash_part}")
    return username, clean.upper()


def verify_invite_code(invite_code: str, public_key_hex: str) -> bool:
    """Mirror of desktop/node_identity.verify_invite_code — keep in step."""
    try:
        _username, hash_part = parse_invite_code(invite_code)
    except ValueError:
        return False
    pub_raw = bytes.fromhex(public_key_hex)
    digest = hashlib.sha256(pub_raw).digest()[:6]
    return digest.hex().upper() == hash_part


def parse_share_string(share: str) -> tuple:
    """Mirror of desktop/node_identity.parse_share_string — keep in step.

    `username#XXXX-XXXX-XXXX[#token-uuid]` -> (invite_code, token_id|None);
    downstream consumers always receive the canonical 2-segment code."""
    import uuid as uuid_mod
    parts = share.strip().split("#")
    if len(parts) not in (2, 3):
        raise ValueError(f"Invalid share string: {share}")
    invite_code = f"{parts[0]}#{parts[1]}"
    parse_invite_code(invite_code)
    if len(parts) == 2:
        return invite_code, None
    return invite_code, str(uuid_mod.UUID(parts[2].strip()))


def derive_identity(username: str, password: str, email: str = "") -> dict:
    """
    Derive P2P identity from username + password.

    Returns: {node_id, public_key_hex, username, invite_code}
    """
    from argon2.low_level import hash_secret_raw, Type
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    if not USERNAME_RE.match(username):
        raise ValueError(
            "P2P username must be 3-32 characters: letters, digits, '-' or '_'")

    # Derive seed (same algorithm as desktop)
    salt = f"{username}:sautium".encode("utf-8")
    seed = hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
        hash_len=ARGON2_HASH_LEN,
        type=Type.ID,
    )

    # Generate Ed25519 keypair from seed
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    pub_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    node_id = pub_raw.hex()

    invite_code = make_invite_code(username, pub_raw)

    logger.info(f"P2P identity: {username} ({invite_code})")
    return {
        "node_id": node_id,
        "public_key_hex": node_id,
        "username": username,
        "invite_code": invite_code,
        "email": email,
    }


def derive_private_key(username: str, password: str):
    """The Ed25519 private key for this account — same Argon2id derivation as
    derive_identity, exposed for signing (enrichment records, requests)."""
    from argon2.low_level import hash_secret_raw, Type
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    salt = f"{username}:sautium".encode("utf-8")
    seed = hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
        hash_len=ARGON2_HASH_LEN,
        type=Type.ID,
    )
    return Ed25519PrivateKey.from_private_bytes(seed)


# Cached on success only: identity sources are fixed for the process
# lifetime, but a web-first-run account can appear mid-process — a cached
# None would then hide it until restart. Argon2id costs ~1-2 s per
# derivation, so uncached per-call use is not an option.
_signing_key_cache = None
_identity_cache = None
_identity_cache_mtime = None


def load_signing_key(settings):
    """The node's Ed25519 signing key — desktop PEM or docker-derived — or
    None when no identity is configured."""
    from pathlib import Path

    from cryptography.hazmat.primitives import serialization

    global _signing_key_cache
    if _signing_key_cache is not None:
        return _signing_key_cache

    if settings.p2p_identity_dir:
        key_path = Path(settings.p2p_identity_dir) / "node_ed25519.key"
        if key_path.exists():
            _signing_key_cache = serialization.load_pem_private_key(
                key_path.read_bytes(), password=None)
            return _signing_key_cache
    if settings.p2p_username and settings.p2p_password:
        _signing_key_cache = derive_private_key(
            settings.p2p_username, settings.p2p_password)
    return _signing_key_cache


def resolve_identity(settings) -> Optional[dict]:
    """The node's account identity dict — node_info.json (desktop mode)
    first, env-derived (Docker mode) second, None without either. The
    single resolution point mirrored by main.py's lifespan and
    routers/p2p._get_identity."""
    import json
    from pathlib import Path

    global _identity_cache, _identity_cache_mtime

    if settings.p2p_identity_dir:
        # Desktop mode: node_info.json is written by the launcher AND by
        # this backend (email changes) while we run — the cache is keyed
        # on the file's mtime, so an edit is seen on the next call and a
        # stale copy can never claim "no email configured" after one was
        # just saved (the bug this replaced: two independent caches, one
        # invalidated, the other not).
        info_path = Path(settings.p2p_identity_dir) / "node_info.json"
        if info_path.exists():
            try:
                mtime = info_path.stat().st_mtime_ns
                if _identity_cache is not None and _identity_cache_mtime == mtime:
                    return _identity_cache
                data = json.loads(info_path.read_text(encoding="utf-8"))
                if data.get("username"):
                    _identity_cache = {
                        "node_id": data["node_id"],
                        "public_key_hex": data["public_key_hex"],
                        "username": data["username"],
                        "invite_code": data["invite_code"],
                        "email": data.get("email", ""),
                    }
                    _identity_cache_mtime = mtime
                    return _identity_cache
            except Exception as e:
                logger.warning(f"Failed to read node_info.json: {e}")
    if _identity_cache is not None:
        return _identity_cache
    if settings.p2p_username and settings.p2p_password:
        _identity_cache = derive_identity(
            settings.p2p_username, settings.p2p_password, settings.p2p_email)
    return _identity_cache


# ---------------------------------------------------------------------------
# Identity documents on disk: certificate + proof
# ---------------------------------------------------------------------------
# The Docker node derives its KEY in memory, but the certificate (a Worker
# fact) and the proof (minutes of mining) are worth keeping: p2p_identity_dir
# is a bind mount (./data/node_identity), so both survive container
# recreation and the node never re-mines. Same file names as the launcher —
# the export/import bundle moves between the two unchanged.

CERT_FILENAME = "birth_certificate.json"


def identity_dir(settings):
    from pathlib import Path
    if not settings.p2p_identity_dir:
        return None
    d = Path(settings.p2p_identity_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_certificate(settings) -> Optional[dict]:
    """The cached certificate for THIS identity, verified on every load so a
    tampered or foreign file degrades to 'no certificate'."""
    import json
    from birth_authority import verify_certificate

    d = identity_dir(settings)
    identity = resolve_identity(settings)
    if d is None or not identity:
        return None
    try:
        cert = json.loads((d / CERT_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if verify_certificate(cert) and cert["pubkey"] == identity["public_key_hex"].lower():
        return cert
    return None


def save_certificate(settings, cert: dict) -> bool:
    import json
    from birth_authority import verify_certificate

    d = identity_dir(settings)
    identity = resolve_identity(settings)
    if d is None or not identity or not verify_certificate(cert):
        return False
    if cert["pubkey"] != identity["public_key_hex"].lower():
        return False
    (d / CERT_FILENAME).write_text(json.dumps(cert, indent=2), encoding="utf-8")
    logger.info("identity cert stored (issued_at=%s method=%s)",
                cert["issued_at"], cert["method"])
    return True


def proof_path(settings):
    from desktop.p2p.identity_proof import PROOF_FILENAME
    d = identity_dir(settings)
    return None if d is None else d / PROOF_FILENAME


def tls_binding(settings):
    """(node_pubkey_hex, sign_fn) for tls_gen.ensure_cert's peer channel
    binding — None without identity (the cert is then generated unbound and
    regenerated bound on the first start after an account exists)."""
    ident = resolve_identity(settings)
    key = load_signing_key(settings)
    if not ident or key is None:
        return None
    return ident["public_key_hex"].lower(), key.sign


def peer_identity(settings):
    """This node as a peer CLIENT (wire format v1, desktop/p2p/peer_auth.py):
    signer, pubkey and a lazy {cert, proof} loader. None without identity."""
    from desktop.p2p import identity_proof, peer_auth

    ident = resolve_identity(settings)
    key = load_signing_key(settings)
    if not ident or key is None:
        return None

    def bundle():
        cert = load_certificate(settings)
        if not cert:
            return None
        path = proof_path(settings)
        proof = identity_proof.load_proof(path) if path is not None else None
        return {"cert": cert,
                "proof": proof if identity_proof.proof_binds(proof, cert) else None}

    return peer_auth.PeerIdentity(pubkey=ident["public_key_hex"].lower(),
                                  sign=key.sign, cert_bundle=bundle)
