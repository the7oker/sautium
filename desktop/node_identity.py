"""
Ed25519 node identity management for P2P networking.

Generates and stores a persistent Ed25519 keypair used for node identification
and message signing. Identity files are stored in %APPDATA%/MusicAIDJ/node_identity/.

Requires the `cryptography` package. All functions degrade gracefully if unavailable.
"""

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


def _identity_dir() -> Path:
    """Return the directory for identity files."""
    from desktop.config_manager import get_config_dir
    d = get_config_dir() / "node_identity"
    d.mkdir(parents=True, exist_ok=True)
    return d


def has_identity() -> bool:
    """Check whether a node identity already exists."""
    return (_identity_dir() / "node_info.json").exists()


def generate_identity() -> str:
    """
    Generate a new Ed25519 keypair and write it to disk.

    Returns the node_id (hex-encoded public key).
    Raises RuntimeError if cryptography is not installed.
    """
    if not HAS_CRYPTO:
        raise RuntimeError("cryptography package required for identity generation")

    d = _identity_dir()
    priv_path = d / "node_ed25519.key"
    pub_path = d / "node_ed25519.pub"
    info_path = d / "node_info.json"

    # Generate keypair
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    # Serialize
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pub_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    node_id = pub_raw.hex()

    # Write private key with restricted permissions
    priv_path.write_bytes(priv_pem)
    try:
        os.chmod(priv_path, stat.S_IRUSR | stat.S_IWUSR)  # 600
    except OSError:
        pass  # Windows may not support full POSIX perms

    pub_path.write_bytes(pub_pem)

    info = {
        "node_id": node_id,
        "public_key_hex": node_id,
        "algorithm": "Ed25519",
    }
    info_path.write_text(json.dumps(info, indent=2), encoding="utf-8")

    logger.info(f"Generated node identity: {node_id[:16]}...")
    return node_id


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
