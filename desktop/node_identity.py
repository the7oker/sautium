"""
Ed25519 node identity and account management for P2P networking.

Supports two modes:
1. Random identity: auto-generated Ed25519 keypair (legacy, per-installation)
2. Account identity: deterministic Ed25519 keypair derived from username+password
   via Argon2id KDF (portable across devices)

Identity files are stored in %APPDATA%/Sautium/node_identity/.

Requires: `cryptography`, `argon2-cffi` (for accounts), `PyNaCl` (for chat encryption).
"""

import hashlib
import json
import logging
import os
import stat
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives import serialization
    from cryptography.exceptions import InvalidSignature
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False
    logger.info("cryptography package not installed — node identity disabled")

try:
    from argon2.low_level import hash_secret_raw, Type
    HAS_ARGON2 = True
except ImportError:
    HAS_ARGON2 = False

# Argon2id parameters for key derivation
ARGON2_TIME_COST = 4
ARGON2_MEMORY_COST = 262144  # 256 MB
ARGON2_PARALLELISM = 2
ARGON2_HASH_LEN = 32


def _identity_dir() -> Path:
    """Return the directory for identity files."""
    from desktop.config_manager import get_config_dir
    d = get_config_dir() / "node_identity"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_keypair(private_key, info: dict) -> str:
    """Save Ed25519 keypair and info to disk. Returns node_id."""
    d = _identity_dir()
    public_key = private_key.public_key()

    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    priv_path = d / "node_ed25519.key"
    priv_path.write_bytes(priv_pem)
    try:
        os.chmod(priv_path, stat.S_IRUSR | stat.S_IWUSR)  # 600
    except OSError:
        pass  # Windows may not support full POSIX perms

    (d / "node_ed25519.pub").write_bytes(pub_pem)
    (d / "node_info.json").write_text(
        json.dumps(info, indent=2), encoding="utf-8"
    )

    return info["node_id"]


# ---------------------------------------------------------------------------
# Random identity (legacy)
# ---------------------------------------------------------------------------

def has_identity() -> bool:
    """Check whether a node identity already exists."""
    return (_identity_dir() / "node_info.json").exists()


def generate_identity() -> str:
    """Generate a new random Ed25519 keypair and write it to disk."""
    if not HAS_CRYPTO:
        raise RuntimeError("cryptography package required for identity generation")

    private_key = Ed25519PrivateKey.generate()
    pub_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    node_id = pub_raw.hex()

    info = {
        "node_id": node_id,
        "public_key_hex": node_id,
        "algorithm": "Ed25519",
    }
    _save_keypair(private_key, info)
    logger.info(f"Generated node identity: {node_id[:16]}...")
    return node_id


# ---------------------------------------------------------------------------
# Account identity (deterministic, portable)
# ---------------------------------------------------------------------------

def derive_seed(username: str, password: str) -> bytes:
    """Derive 32-byte Ed25519 seed from username+password using Argon2id."""
    if not HAS_ARGON2:
        raise RuntimeError("argon2-cffi package required for account creation")
    salt = f"{username}:sautium".encode("utf-8")
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
        hash_len=ARGON2_HASH_LEN,
        type=Type.ID,
    )


def make_invite_code(username: str, public_key_raw: bytes) -> str:
    """Generate invite code: username#XXXX-XXXX-XXXX (6 bytes of SHA-256)."""
    digest = hashlib.sha256(public_key_raw).digest()[:6]
    h = digest.hex().upper()
    return f"{username}#{h[:4]}-{h[4:8]}-{h[8:]}"


def parse_invite_code(invite_code: str) -> tuple[str, str]:
    """Parse invite code into (username, hash_part). Raises ValueError on bad format."""
    if "#" not in invite_code:
        raise ValueError(f"Invalid invite code format: {invite_code}")
    username, hash_part = invite_code.split("#", 1)
    clean = hash_part.replace("-", "")
    if len(clean) != 12:
        raise ValueError(f"Invalid invite code hash length: {hash_part}")
    return username, clean.upper()


def verify_invite_code(invite_code: str, public_key_hex: str) -> bool:
    """Verify that an invite code matches a public key."""
    try:
        username, hash_part = parse_invite_code(invite_code)
    except ValueError:
        return False
    pub_raw = bytes.fromhex(public_key_hex)
    digest = hashlib.sha256(pub_raw).digest()[:6]
    return digest.hex().upper() == hash_part


def create_account(username: str, password: str) -> dict:
    """
    Create or recover account from username + password.

    Uses Argon2id to derive deterministic Ed25519 keypair.
    Same username + password on any device = same keys = same identity.

    Returns: {node_id, public_key_hex, algorithm, username, invite_code}
    """
    if not HAS_CRYPTO:
        raise RuntimeError("cryptography package required")

    seed = derive_seed(username, password)
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    pub_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    node_id = pub_raw.hex()
    invite_code = make_invite_code(username, pub_raw)

    info = {
        "node_id": node_id,
        "public_key_hex": node_id,
        "algorithm": "Ed25519",
        "username": username,
        "invite_code": invite_code,
    }
    _save_keypair(private_key, info)
    logger.info(f"Account created: {username} (invite: {invite_code})")
    return info


def has_account() -> bool:
    """Check if an account (not just random node identity) exists."""
    info_path = _identity_dir() / "node_info.json"
    if not info_path.exists():
        return False
    try:
        data = json.loads(info_path.read_text(encoding="utf-8"))
        return "username" in data
    except Exception:
        return False


def get_account_info() -> Optional[dict]:
    """Get account info or None if no account exists."""
    info_path = _identity_dir() / "node_info.json"
    if not info_path.exists():
        return None
    try:
        data = json.loads(info_path.read_text(encoding="utf-8"))
        if "username" not in data:
            return None
        return data
    except Exception:
        return None


def get_invite_code() -> Optional[str]:
    """Get the current user's invite code."""
    info = get_account_info()
    return info.get("invite_code") if info else None


# ---------------------------------------------------------------------------
# Key rotation (password change)
# ---------------------------------------------------------------------------

def rotate_keys(username: str, new_password: str) -> dict:
    """
    Change password → new keypair. Returns key rotation message
    that should be sent to all friends (signed by old key).

    Returns: {new_info, rotation_message, old_signature}
    """
    if not HAS_CRYPTO:
        raise RuntimeError("cryptography package required")

    # Load old key for signing the rotation message
    old_private = _load_private_key()
    old_pub_raw = old_private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    old_public_key_hex = old_pub_raw.hex()

    # Create new account (overwrites old keys)
    new_info = create_account(username, new_password)

    # Sign rotation message with OLD key: "I'm moving to this new key"
    rotation_msg = json.dumps({
        "type": "key_rotation",
        "old_public_key": old_public_key_hex,
        "new_public_key": new_info["public_key_hex"],
        "new_invite_code": new_info["invite_code"],
    }, separators=(",", ":")).encode("utf-8")

    old_signature = old_private.sign(rotation_msg)

    logger.info(f"Key rotation: {old_public_key_hex[:16]}... → {new_info['public_key_hex'][:16]}...")
    return {
        "new_info": new_info,
        "rotation_message": rotation_msg,
        "old_signature": old_signature,
    }


# ---------------------------------------------------------------------------
# Common functions (work with both identity types)
# ---------------------------------------------------------------------------

def get_node_id() -> Optional[str]:
    """Read the node_id from node_info.json, or None if not present."""
    info_path = _identity_dir() / "node_info.json"
    if not info_path.exists():
        return None
    try:
        data = json.loads(info_path.read_text(encoding="utf-8"))
        return data.get("node_id")
    except Exception as e:
        logger.warning(f"Failed to read node_info.json: {e}")
        return None


def _load_private_key() -> "Ed25519PrivateKey":
    """Load the private key from disk."""
    if not HAS_CRYPTO:
        raise RuntimeError("cryptography package required")
    priv_pem = (_identity_dir() / "node_ed25519.key").read_bytes()
    return serialization.load_pem_private_key(priv_pem, password=None)


def get_private_key_raw() -> bytes:
    """Get 32-byte raw Ed25519 private key (seed) for NaCl operations."""
    key = _load_private_key()
    return key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def sign_message(message: bytes) -> bytes:
    """Sign a message with the node's private key. Returns raw signature bytes."""
    key = _load_private_key()
    return key.sign(message)


def verify_signature(message: bytes, signature: bytes, pubkey_hex: str) -> bool:
    """Verify a signature against a public key (hex-encoded raw bytes)."""
    if not HAS_CRYPTO:
        raise RuntimeError("cryptography package required")
    pub_raw = bytes.fromhex(pubkey_hex)
    public_key = Ed25519PublicKey.from_public_bytes(pub_raw)
    try:
        public_key.verify(signature, message)
        return True
    except InvalidSignature:
        return False
