"""
Ed25519 node identity and account management for P2P networking.

Supports two modes:
1. Random identity: auto-generated Ed25519 keypair (legacy, per-installation)
2. Account identity: deterministic Ed25519 keypair derived from username+password
   via Argon2id KDF (portable across devices)

Identity files are stored in %APPDATA%/Sautium/node_identity/.

Requires: `cryptography`, `argon2-cffi` (for accounts), `PyNaCl` (for chat encryption).
"""

import datetime
import hashlib
import json
import logging
import os
import re
import ssl
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

    # Remove old TLS cert so it gets regenerated with the new node_id in CN
    tls_cert = d / "tls_cert.pem"
    tls_key = d / "tls_key.pem"
    if tls_cert.exists():
        tls_cert.unlink()
    if tls_key.exists():
        tls_key.unlink()

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

# The username is embedded verbatim in the invite code (username#XXXX-…),
# in Worker KV storage keys and in email templates. Restricting it to a
# URL/CLI-safe alphabet at this boundary keeps every downstream format
# unambiguous ('#' must appear exactly once in an invite code; ':' is the
# KV key substitute for '#'). Mirrored by USERNAME_RE in worker/verify.js.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")


def validate_username(username: str) -> None:
    """Raise ValueError unless the username fits the network-wide format."""
    if not USERNAME_RE.match(username):
        raise ValueError(
            "Username must be 3-32 characters: letters, digits, '-' or '_'")


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


def derive_account_identity(username: str, password: str):
    """Derive (private_key, public_key_hex, invite_code) WITHOUT saving.

    For wizard flows that must sign Worker requests (email verification,
    birth-certificate issuance) before the account is persisted — the
    derivation is deterministic, so the eventual create_account() yields
    the same identity."""
    validate_username(username)
    seed = derive_seed(username, password)
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    pub_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_key, pub_raw.hex(), make_invite_code(username, pub_raw)


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


def parse_share_string(share: str) -> tuple[str, Optional[str]]:
    """Parse `username#XXXX-XXXX-XXXX[#token-uuid]` into
    (invite_code, token_id).

    The 3-segment form carries an invite-token id (auto-confirm invites).
    Every downstream consumer — parse_invite_code, verify_invite_code,
    Worker calls, DHT keys, LAN beacons — receives the canonical 2-segment
    invite code; the token travels separately. Mirrored in
    backend/p2p_identity.py. Raises ValueError on any malformed segment.
    """
    import uuid as uuid_mod
    parts = share.strip().split("#")
    if len(parts) not in (2, 3):
        raise ValueError(f"Invalid share string: {share}")
    invite_code = f"{parts[0]}#{parts[1]}"
    parse_invite_code(invite_code)
    if len(parts) == 2:
        return invite_code, None
    return invite_code, str(uuid_mod.UUID(parts[2].strip()))


def create_account(
    username: str,
    password: str,
    email: str = "",
    email_verified: bool = False,
) -> dict:
    """
    Create or recover account from username + password.

    Uses Argon2id to derive deterministic Ed25519 keypair.
    Same username + password on any device = same keys = same identity.

    Returns: {node_id, public_key_hex, algorithm, username, invite_code, email, email_verified}
    """
    if not HAS_CRYPTO:
        raise RuntimeError("cryptography package required")

    validate_username(username)
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
        "email": email,
        "email_verified": email_verified,
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

    Safety: derives new keys and signs rotation message BEFORE
    overwriting disk.  Old keys are preserved until the signed
    message is ready, so a crash at any point before _save_keypair
    leaves the old identity intact.

    Returns: {new_info, rotation_message, old_signature}
    """
    if not HAS_CRYPTO:
        raise RuntimeError("cryptography package required")

    # 1. Load old key (still on disk, untouched)
    old_private = _load_private_key()
    old_pub_raw = old_private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    old_public_key_hex = old_pub_raw.hex()

    # 2. Derive new keypair in memory (DO NOT write to disk yet)
    new_seed = derive_seed(username, new_password)
    new_private = Ed25519PrivateKey.from_private_bytes(new_seed)
    new_pub_raw = new_private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    new_public_key_hex = new_pub_raw.hex()
    new_invite_code = make_invite_code(username, new_pub_raw)

    # 3. Build and sign rotation message with OLD key
    rotation_msg = json.dumps({
        "type": "key_rotation",
        "old_public_key": old_public_key_hex,
        "new_public_key": new_public_key_hex,
        "new_invite_code": new_invite_code,
    }, separators=(",", ":")).encode("utf-8")

    old_signature = old_private.sign(rotation_msg)

    # 4. Only NOW persist new keys to disk (point of no return)
    new_info = {
        "node_id": new_public_key_hex,
        "public_key_hex": new_public_key_hex,
        "algorithm": "Ed25519",
        "username": username,
        "invite_code": new_invite_code,
    }
    _save_keypair(new_private, new_info)

    logger.info(f"Key rotation: {old_public_key_hex[:16]}... → {new_public_key_hex[:16]}...")
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


# ---------------------------------------------------------------------------
# TLS certificate (self-signed, for P2P HTTPS transport)
# ---------------------------------------------------------------------------

def ensure_tls_cert() -> tuple[Path, Path]:
    """
    Generate self-signed TLS certificate if not exists.

    Uses ECDSA P-256 for broad TLS compatibility.
    Embeds the Ed25519 node_id in the certificate CN for identity verification.

    Returns: (cert_path, key_path)
    """
    if not HAS_CRYPTO:
        raise RuntimeError("cryptography package required")

    d = _identity_dir()
    cert_path = d / "tls_cert.pem"
    key_path = d / "tls_key.pem"

    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes

    tls_key = ec.generate_private_key(ec.SECP256R1())
    node_id = get_node_id() or "sautium-node"

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, node_id),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Sautium"),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(tls_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(
            datetime.datetime.utcnow() + datetime.timedelta(days=3650)
        )
        .sign(tls_key, hashes.SHA256())
    )

    key_pem = tls_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)

    key_path.write_bytes(key_pem)
    try:
        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    cert_path.write_bytes(cert_pem)

    logger.info(f"Generated TLS certificate (CN={node_id[:16]}...)")
    return cert_path, key_path


def get_server_ssl_context() -> ssl.SSLContext:
    """Create SSL context for the P2P sync server (HTTPS)."""
    cert_path, key_path = ensure_tls_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def get_client_ssl_context() -> ssl.SSLContext:
    """
    Create SSL context for connecting to P2P peers.

    Accepts self-signed certificates (verification done at application level
    via Ed25519 public key matching).
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx
