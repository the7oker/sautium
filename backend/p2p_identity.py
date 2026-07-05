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

    # Generate invite code (same as desktop)
    digest = hashlib.sha256(pub_raw).digest()[:6]
    h = digest.hex().upper()
    invite_code = f"{username}#{h[:4]}-{h[4:8]}-{h[8:]}"

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


def load_signing_key(settings):
    """The node's Ed25519 signing key — desktop PEM or docker-derived — or
    None when no identity is configured."""
    from pathlib import Path

    from cryptography.hazmat.primitives import serialization

    if settings.p2p_identity_dir:
        key_path = Path(settings.p2p_identity_dir) / "node_ed25519.key"
        if key_path.exists():
            return serialization.load_pem_private_key(
                key_path.read_bytes(), password=None)
    if settings.p2p_username and settings.p2p_password:
        return derive_private_key(settings.p2p_username, settings.p2p_password)
    return None
