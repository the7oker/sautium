"""
Ed25519 node identity and account management for P2P networking.

Supports two modes:
1. Random identity: auto-generated Ed25519 keypair (legacy, per-installation)
2. Account identity: deterministic Ed25519 keypair derived from username+password
   via Argon2id KDF (portable across devices)

Identity files are stored in %APPDATA%/Sautium/node_identity/.

Requires: `cryptography`, `argon2-cffi` (for accounts), `PyNaCl` (for chat encryption).
"""

import base64
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


def _save_keypair(private_key, info: dict, identity_dir: Optional[Path] = None) -> str:
    """Save Ed25519 keypair and info to disk. Returns node_id."""
    d = identity_dir or _identity_dir()
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
            "Nickname: 3-32 Latin letters, digits, '-' or '_'")


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
    anonymous: bool = False,
) -> dict:
    """
    Create or recover account from username + password.

    Uses Argon2id to derive deterministic Ed25519 keypair.
    Same username + password on any device = same keys = same identity.

    `anonymous` records that the password was minted rather than chosen, so
    nobody can ever type it. It is what tells the login gate to offer the
    pairing PIN instead of a password prompt nothing could satisfy.

    Returns: {node_id, public_key_hex, algorithm, username, invite_code, email, email_verified, anonymous}
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
        "anonymous": anonymous,
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
# Key rotation — a new name and/or password
# ---------------------------------------------------------------------------
# The name and the password are both KDF inputs, so changing either IS a new
# key. The old identity is not discarded: its key still decrypts what friends
# who have not heard yet send to it, still signs the notice that names the
# successor, and is what a succession claim would ever be argued from.
# Everything a key owned lives in one archive directory beside the live files.

PREVIOUS_DIRNAME = "previous"
ROTATION_FILENAME = "rotation.json"
_PUBKEY_RE = re.compile(r"[0-9a-f]{64}")
_ARCHIVED_FILES = ("node_ed25519.key", "node_ed25519.pub", "node_info.json",
                   "birth_certificate.json", "identity_proof.json")


def _canonical_notice(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def parse_rotation_notice(message: bytes, old_signature: bytes,
                          new_signature: bytes) -> Optional[dict]:
    """The notice's fields, or None unless BOTH keys vouch for it: the old
    one (continuity — only its holder may name a successor) and the new one
    (possession — a stolen old key cannot point the friendship at a key its
    thief does not hold). Receivers take every field from the signed bytes,
    never from the request that carried them."""
    try:
        payload = json.loads(message.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "key_rotation":
        return None
    old_pub, new_pub = payload.get("old_public_key"), payload.get("new_public_key")
    if not (isinstance(old_pub, str) and _PUBKEY_RE.fullmatch(old_pub)
            and isinstance(new_pub, str) and _PUBKEY_RE.fullmatch(new_pub)
            and old_pub != new_pub
            and isinstance(payload.get("new_invite_code"), str)
            and len(old_signature) == 64 and len(new_signature) == 64):
        return None
    if _canonical_notice(payload) != message:
        return None
    if not verify_signature(message, old_signature, old_pub):
        return None
    if not verify_signature(message, new_signature, new_pub):
        return None
    return payload


def rotate_identity(identity_dir: Path, username: str, password: str,
                    anonymous: bool) -> dict:
    """Replace the identity in `identity_dir` with the one (username,
    password) derives, archiving the old one under previous/.

    Returns {"info": new node_info, "rotation": the signed notice}. The
    notice is written into the archive too — delivery to friends happens
    later, from the P2P layer, and may need retrying across restarts.
    Nothing is written before the new key and both signatures exist in
    memory, so a crash mid-way leaves the old identity whole."""
    if not HAS_CRYPTO:
        raise RuntimeError("cryptography package required")
    validate_username(username)

    old_info = json.loads((identity_dir / "node_info.json").read_text(encoding="utf-8"))
    old_private = serialization.load_pem_private_key(
        (identity_dir / "node_ed25519.key").read_bytes(), password=None)
    old_pub = old_info["public_key_hex"].lower()

    new_private = Ed25519PrivateKey.from_private_bytes(derive_seed(username, password))
    new_pub_raw = new_private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    new_pub = new_pub_raw.hex()
    if new_pub == old_pub:
        raise ValueError("That name and password are this identity already")
    new_invite = make_invite_code(username, new_pub_raw)
    rotated_at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)

    message = _canonical_notice({
        "type": "key_rotation",
        "v": 1,
        "old_public_key": old_pub,
        "new_public_key": new_pub,
        "new_invite_code": new_invite,
        "rotated_at": rotated_at.isoformat().replace("+00:00", "Z"),
    })
    rotation = {
        "old_public_key": old_pub,
        "new_public_key": new_pub,
        "new_invite_code": new_invite,
        "rotated_at": rotated_at.isoformat().replace("+00:00", "Z"),
        "message": base64.b64encode(message).decode("ascii"),
        "old_signature": old_private.sign(message).hex(),
        "new_signature": new_private.sign(message).hex(),
    }

    archive = identity_dir / PREVIOUS_DIRNAME / (
        rotated_at.strftime("%Y%m%dT%H%M%SZ") + "-" + old_pub[:12])
    archive.mkdir(parents=True, exist_ok=False)
    for name in _ARCHIVED_FILES:
        path = identity_dir / name
        if path.exists():
            path.rename(archive / name)
    (archive / ROTATION_FILENAME).write_text(json.dumps(rotation, indent=2),
                                             encoding="utf-8")

    new_info = {
        "node_id": new_pub,
        "public_key_hex": new_pub,
        "algorithm": "Ed25519",
        "username": username,
        "invite_code": new_invite,
        # The Worker maps invite code → mailbox; a new code is unmapped until
        # the owner verifies again — from the new key, which is what makes the
        # notary name the old one as predecessor.
        "email": old_info.get("email", ""),
        "email_verified": False,
        "anonymous": anonymous,
        "previous": old_info.get("previous", []) + [{
            "public_key_hex": old_pub,
            "username": old_info.get("username", ""),
            "invite_code": old_info.get("invite_code", ""),
            "retired_at": rotation["rotated_at"],
            "dir": archive.name,
        }],
    }
    _save_keypair(new_private, new_info, identity_dir)
    logger.info("Identity rotated: %s… → %s… (%s)", old_pub[:16], new_pub[:16], username)
    return {"info": new_info, "rotation": rotation}


def previous_identities(identity_dir: Optional[Path] = None) -> list:
    """The retired identities recorded in node_info.json, oldest first."""
    info_path = (identity_dir or _identity_dir()) / "node_info.json"
    if not info_path.exists():
        return []
    try:
        return list(json.loads(info_path.read_text(encoding="utf-8")).get("previous", []))
    except (OSError, ValueError):
        return []


def load_previous_seeds(identity_dir: Optional[Path] = None) -> list:
    """Raw Ed25519 seeds of every archived key, newest first — what a
    message encrypted to a retired key is still opened with."""
    d = identity_dir or _identity_dir()
    seeds = []
    for entry in reversed(previous_identities(d)):
        key_path = d / PREVIOUS_DIRNAME / entry["dir"] / "node_ed25519.key"
        if not key_path.exists():
            continue
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        seeds.append(key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption()))
    return seeds


def rotation_record(old_public_key_hex: str,
                    identity_dir: Optional[Path] = None) -> Optional[dict]:
    """The signed notice that retired `old_public_key_hex`, if this node
    holds it."""
    d = identity_dir or _identity_dir()
    for entry in previous_identities(d):
        if entry["public_key_hex"] != old_public_key_hex.lower():
            continue
        path = d / PREVIOUS_DIRNAME / entry["dir"] / ROTATION_FILENAME
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return None


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
    Generate the peer-surface TLS certificate if missing or unbound.

    Uses ECDSA P-256 for broad TLS compatibility. When the node has an
    identity, the cert carries the channel binding (peer_auth.py: the node
    pubkey plus its Ed25519 signature over the TLS key's SPKI) — that is
    what lets peers pin the channel to this node instead of running
    CERT_NONE blind. An existing cert whose binding is absent or belongs
    to a previous identity is regenerated.

    Returns: (cert_path, key_path)
    """
    if not HAS_CRYPTO:
        raise RuntimeError("cryptography package required")

    from desktop.p2p import peer_auth

    d = _identity_dir()
    cert_path = d / "tls_cert.pem"
    key_path = d / "tls_key.pem"
    node_id = get_node_id()

    if cert_path.exists() and key_path.exists():
        if node_id is None:
            return cert_path, key_path
        try:
            from cryptography import x509
            der = x509.load_pem_x509_certificate(
                cert_path.read_bytes()).public_bytes(
                serialization.Encoding.DER)
        except Exception:
            der = b""
        if peer_auth.tls_bound_pubkey(der) == node_id.lower():
            return cert_path, key_path
        logger.info("Peer TLS cert has no binding for the current identity "
                    "— regenerating")

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes

    tls_key = ec.generate_private_key(ec.SECP256R1())

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, node_id or "sautium-node"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Sautium"),
    ])

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(tls_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(
            datetime.datetime.utcnow() + datetime.timedelta(days=3650)
        )
    )
    if node_id is not None:
        spki = tls_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        builder = builder.add_extension(
            x509.UnrecognizedExtension(
                x509.ObjectIdentifier(peer_auth.TLS_BINDING_OID),
                peer_auth.tls_binding_value(sign_message, node_id, spki)),
            critical=False)
    cert = builder.sign(tls_key, hashes.SHA256())

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

    logger.info(f"Generated TLS certificate (CN={(node_id or 'sautium-node')[:16]}..., "
                f"bound={node_id is not None})")
    return cert_path, key_path


def get_server_ssl_context() -> ssl.SSLContext:
    """Create SSL context for the P2P sync server (HTTPS)."""
    cert_path, key_path = ensure_tls_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx
