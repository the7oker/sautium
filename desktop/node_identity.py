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
import secrets
import ssl
import stat
from pathlib import Path
from typing import Optional

import psycopg2

try:
    import fcntl
except ImportError:                  # Windows: os.fsync is its sync
    fcntl = None

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


def _move_aside(d: Path, name: str) -> Optional[Path]:
    """The identity folder's file `name`, moved whole to a folder of its own,
    replaced-<when>-<random>/ (two moves in one tick of a coarse clock — a
    bundle's certificate, then its proof — get two). Returns the folder it
    went to, or None when there was none."""
    import tempfile
    path = d / name
    if not path.exists():
        return None
    aside = Path(tempfile.mkdtemp(dir=d, prefix="replaced-" + datetime.datetime.now(
        datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-"))
    try:
        path.rename(aside / name)
    except FileNotFoundError:
        aside.rmdir()                # moved meanwhile by another writer: nothing of ours to keep
        return None
    except OSError:
        aside.rmdir()
        raise
    return aside


def _move_info_aside(d: Path) -> Optional[Path]:
    """The info of an identity whose key is gone, moved aside — beside a new
    key it would name the identity that is gone."""
    return _move_aside(d, "node_info.json")


CERT_FILENAME = "birth_certificate.json"


def _document_key(path: Path) -> Optional[str]:
    """The key (lower-case hex) an identity document on disk names, read by
    its shape alone — this is about keeping a file, not trusting it: a
    certificate of an older version or a retired issuer is still someone's.
    None for no file, or one that names no key."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    key = doc.get("pubkey") if isinstance(doc, dict) else None
    return key.lower() if isinstance(key, str) and _PUBKEY_RE.fullmatch(key.lower()) else None


def write_identity_document(d: Path, name: str, data: bytes, pubkey: str) -> None:
    """The one writer of the identity folder `d`'s certificate
    (birth_certificate.json) and proof (identity_proof.json), on either
    runtime: `data`, the document of the key `pubkey`. A document there of
    another key is never written over — it is moved whole to replaced-<when>/
    with an error naming both keys, and `data` written as if no file had been
    there (B-113: a start under another key got its own certificate stored
    over the master's). This key's document, or a file that names no key, is
    written over in place."""
    held = _document_key(d / name)
    aside = _move_aside(d, name) if held is not None and held != pubkey.lower() else None
    if aside is not None:
        logger.error("The identity folder's %s was of the key %s…, not of this one (%s…): "
                     "moved whole to %s", name, held[:16], pubkey.lower()[:16], aside.name)
    (d / name).write_bytes(data)


def _sync(fd: int) -> None:
    """A file's bytes onto the disk: F_FULLFSYNC where the platform has it —
    macOS's fsync stops at the drive's cache."""
    if hasattr(fcntl, "F_FULLFSYNC"):
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    else:
        os.fsync(fd)


def _write_exclusive(path: Path, pem: bytes) -> None:
    """`pem` at `path`, created exclusively (FileExistsError) and synced; a
    write that fails takes its file away — no partial key stays."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
            f.flush()
            _sync(f.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _link_exclusive(tmp: Path, key: Path, pem: bytes) -> None:
    """`tmp`'s key at `key`, which must not exist (FileExistsError): a hard
    link appears whole or not at all. A filesystem without hard links (a FAT
    drive, some bind mounts of Docker Desktop) gets the file created
    exclusively instead."""
    try:
        os.link(tmp, key)
    except FileExistsError:
        raise
    except OSError:
        _write_exclusive(key, pem)


def place_key(d: Path, pem: bytes, *, keep_info: bool = False) -> None:
    """A new private key (PEM) into the identity folder `d`, never over one:
    FileExistsError when a key is there or lands meanwhile. It is written
    under a temporary name and linked into place, so no reader meets half a
    key, and the info of an identity whose key is gone is moved aside first
    (unless `keep_info`: the info names this same key), so no crash leaves
    a new key beside it; moved back when the key is not placed. A temporary
    key an earlier placement left (a crash, a name Windows would not remove)
    is swept first; once the key is placed, a temporary name that cannot be
    removed is a warning, never a failure."""
    for stale in d.glob(".node_ed25519.key.*"):
        stale.unlink()
    key = d / "node_ed25519.key"
    if key.exists():
        raise FileExistsError(str(key))
    tmp = d / f".node_ed25519.key.{secrets.token_hex(6)}"
    aside = None
    try:
        _write_exclusive(tmp, pem)
        aside = None if keep_info else _move_info_aside(d)
        try:
            _link_exclusive(tmp, key, pem)
        except Exception:
            if aside is not None and not (d / "node_info.json").exists():
                (aside / "node_info.json").rename(d / "node_info.json")
                aside.rmdir()
            raise
        if os.name == "posix":
            # The new name onto the disk with the bytes: a power cut after
            # this keeps the key that was placed
            fd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("The temporary key %s could not be removed (%s): the next placement "
                           "sweeps it", tmp.name, e)
    if aside is not None:
        logger.warning("An identity's info without its key moved to %s", aside.name)


def _save_keypair(private_key, info: dict, identity_dir: Optional[Path] = None,
                  exclusive: bool = False) -> str:
    """Save Ed25519 keypair and info to disk. Returns node_id. `exclusive`:
    the key is placed, never over one (place_key: FileExistsError), the info
    of an identity whose key is gone moved aside."""
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
    if exclusive:
        place_key(d, priv_pem)
    else:
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
    """Whether the identity folder holds this node's identity: its key, which
    is what every reader signs with (p2p_identity.load_signing_key) and what
    the web interface's account setup refuses to write over. node_info.json
    alone is no identity — a key left without it still is one."""
    return (_identity_dir() / "node_ed25519.key").exists()


# Where a node's own seals sit: arms over tables whose sealed rows carry
# author_pubkey and signature as `x`. First its own analysis — segments,
# features, instruments under a source that is not imported, or under none —
# then its own canon rows (the album layer and the recording bindings, none
# imported) and its witness statements. Each seal names the key that made it,
# which was this node's: with no identity in sight they say the node had one.
_ANALYSIS_ARMS = (
    "embedding_segments x JOIN embeddings e ON e.id = x.embedding_id"
    " LEFT JOIN analysis_sources s ON s.id = e.analysis_source_id"
    " WHERE x.signature IS NOT NULL AND s.imported IS NOT TRUE",
    "audio_features x LEFT JOIN analysis_sources s ON s.id = x.analysis_source_id"
    " WHERE x.signature IS NOT NULL AND s.imported IS NOT TRUE",
    "track_instruments x LEFT JOIN analysis_sources s ON s.id = x.analysis_source_id"
    " WHERE x.signature IS NOT NULL AND s.imported IS NOT TRUE",
)
_RECORD_ARMS = _ANALYSIS_ARMS + (
    "albums x WHERE x.signature IS NOT NULL AND NOT x.imported",
    "album_tracks x WHERE x.signature IS NOT NULL AND NOT x.imported",
    "track_mbids x WHERE x.signature IS NOT NULL AND NOT x.imported",
    "p2p_witness x WHERE x.signature IS NOT NULL",
)
# The node's signing key of record: the key this node signs with, recorded
# by whoever puts it in place or finds it — the backend at each start with a
# key (device_auth.record_identity_state), the web interface's account setup
# and a rotation (device_auth), the launcher's new identity
# (generate_identity) and its check that finds one (ensure_identity), each
# after the key is in place. What the web interface's account setup takes
# back over a lost key.
SIGNING_KEY_SETTING = "identity.signing_key"
SIGNING_KEY_SQL = ("(SELECT value #>> '{}' FROM user_settings"
                   " WHERE key = 'identity.signing_key')")
# The account key the last chat client's run held (ChatClient
# _reintroduce_after_identity_change writes it, the launcher's and — since
# 2026-11-17 — a Docker node's):
# for a node that lost its folder before its first start on this release,
# which recorded no signing key.
BOUND_IDENTITY_SQL = ("(SELECT value #>> '{}' FROM user_settings"
                      " WHERE key = 'p2p.bound_identity')")


def _any(arms, by_key: bool = False) -> str:
    named = " AND lower(x.author_pubkey) = %(key)s" if by_key else ""
    return " OR ".join(f"EXISTS (SELECT 1 FROM {arm}{named})" for arm in arms)


# The analysis records' cutover (backend/db_migrate) decides nothing without
# the node's keys over these.
SEALED_FIRST_HAND_SQL = f"SELECT ({_any(_ANALYSIS_ARMS)}) AS found"
# What says this node had an identity: its signing key of record, the key
# its last P2P run held, or any seal of its own. No start makes a new
# identity over it (ensure_identity), and the web interface's account setup
# makes another only on the owner's word (backend/device_auth.create_account).
OWN_SEALS_SQL = (f"SELECT ({SIGNING_KEY_SQL} IS NOT NULL OR {BOUND_IDENTITY_SQL} IS NOT NULL"
                 f" OR {_any(_RECORD_ARMS)}) AS found")
# Whether the key `%(key)s` (lower-case hex) made any of the seals.
OWN_SEALS_BY_SQL = f"SELECT ({_any(_RECORD_ARMS, by_key=True)}) AS found"
# `%(key)s` (a JSON string) as the node's signing key of record; the row's
# updated_at moves only when the key does.
RECORD_SIGNING_KEY_SQL = """
    INSERT INTO user_settings (key, value) VALUES ('identity.signing_key', %(key)s::jsonb)
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
     WHERE user_settings.value IS DISTINCT FROM EXCLUDED.value"""


def record_signing_key(dsn: str, pubkey: str) -> None:
    """`pubkey` as the node's signing key of record (SIGNING_KEY_SETTING)."""
    conn = psycopg2.connect(dsn, connect_timeout=10)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(RECORD_SIGNING_KEY_SQL, {"key": json.dumps(pubkey.lower())})
    finally:
        conn.close()


def pubkey_on_disk(d: Path) -> str:
    """The public key (lower-case hex) of the key in the identity folder `d`."""
    key = serialization.load_pem_private_key((d / "node_ed25519.key").read_bytes(), password=None)
    return key.public_key().public_bytes(encoding=serialization.Encoding.Raw,
                                         format=serialization.PublicFormat.Raw).hex()


def _rebuild_info(d: Path) -> None:
    """A key left without its node_info.json: the info a random identity has
    (node_id, public_key_hex, algorithm), from the key — P2P would run on an
    empty node id, its TLS unbound. Never over an info that landed meanwhile."""
    node_id = pubkey_on_disk(d)
    try:
        with open(d / "node_info.json", "x", encoding="utf-8") as f:
            json.dump({"node_id": node_id, "public_key_hex": node_id, "algorithm": "Ed25519"},
                      f, indent=2)
    except FileExistsError:
        return
    logger.warning("The identity folder held a key without its node_info.json: the info is made "
                   "again from the key (%s…), with no account", node_id[:16])


def ensure_identity(dsn: str) -> str:
    """This node's identity as a start finds it, before anything P2P runs:
    "present" — the identity folder holds its key, which is put on record
    as the node's signing key (a placement whose recording a crash cut
    short heals here; a key left without its info gets the info again from
    the key); "minted" — it holds none and nothing in the database says the
    node had one (OWN_SEALS_SQL), so a new one is generated and recorded;
    "missing" — it holds none and the database does say so: nothing is
    generated, and only the owner's choice makes a new one
    (generate_identity), which makes the records the lost one signed another
    author's. A database that cannot be read or written raises
    (psycopg2.Error) and nothing is generated. A key that lands meanwhile (a
    restore, the web interface's account setup) is the identity: generating
    never writes over one."""
    if has_identity():
        d = _identity_dir()
        if not (d / "node_info.json").exists():
            _rebuild_info(d)
        record_signing_key(dsn, pubkey_on_disk(d))
        return "present"
    conn = psycopg2.connect(dsn, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute(OWN_SEALS_SQL)
            had_one = cur.fetchone()[0]
    finally:
        conn.close()
    if had_one:
        return "missing"
    try:
        generate_identity(dsn)
    except FileExistsError:
        if not has_identity():
            raise
        return "present"
    return "minted"


def generate_identity(dsn: Optional[str] = None) -> str:
    """Generate a new random Ed25519 keypair and write it to disk — never
    over a key (place_key: FileExistsError says one is there). With `dsn`,
    the key placed is recorded there as the node's signing key of record —
    after it is placed: a placement that fails leaves the record as it was;
    a crash before the record leaves the key, which the next check records.
    The info of an identity whose key is gone is moved aside
    (replaced-<when>/), not written over."""
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
    try:
        _save_keypair(private_key, info, exclusive=True)
    except FileExistsError:
        if dsn is not None and has_identity():
            # The identity that came back is the one on record
            record_signing_key(dsn, pubkey_on_disk(_identity_dir()))
        raise
    if dsn is not None:
        record_signing_key(dsn, node_id)
    logger.info(f"Generated node identity: {node_id[:16]}...")
    return node_id


class IdentitySettling:
    """The launcher's way to the identity P2P starts on, without Tk. One
    operation at a time — a check (ensure_identity) or the owner's choice of
    a new identity (generate_identity) — and only its completion acts: a
    request while one runs is refused (False), so no second check reads the
    folder a mint is writing, and no two completions start P2P.

    `run(fn)` runs fn off the caller's thread; `ui(fn)` hands fn back to it
    (the launcher's ui_call); `closing()` says the caller quits, and a
    completion then calls nothing — no notice, no P2P start. The state is
    the caller's thread's alone. Its callbacks, there: `ready()` — an
    identity is on disk, P2P may start on it; `missing()` — none is, while
    the database says the node had one; `unsettled(error)` — the check
    decided nothing (the database could not be read, or the check failed:
    re-raised for the thread's hook after the callback is handed back);
    `failed(error)` — the owner's new identity could not be made."""

    def __init__(self, dsn, run, ui, closing, *, ready, missing, unsettled, failed):
        self._dsn, self._run, self._ui, self._closing = dsn, run, ui, closing
        self._ready, self._missing = ready, missing
        self._unsettled, self._failed = unsettled, failed
        self.busy = False

    def check(self) -> bool:
        """Settle the identity; False when an operation already runs."""
        return self._begin(self._checking)

    def make_new(self) -> bool:
        """The owner's choice: a new identity; False when an operation runs."""
        return self._begin(self._making)

    def _begin(self, work) -> bool:
        if self.busy:
            return False
        self.busy = True
        self._run(work)
        return True

    def _end(self, then, *args) -> None:
        def finish():
            self.busy = False
            if not self._closing():
                then(*args)
        self._ui(finish)

    def _checking(self) -> None:
        try:
            state = ensure_identity(self._dsn())
        except psycopg2.Error as e:
            logger.error("The node identity could not be settled, the database neither read nor "
                         "written: %s — none made, P2P off until the next start", e)
            self._end(self._unsettled, e)
            return
        except Exception as e:
            self._end(self._unsettled, e)
            raise
        if state == "missing":
            logger.warning("No node identity, and the database says this node had one (its "
                           "signing key of record, its own seals, or the key its last P2P run "
                           "held) — none made, P2P off until the owner decides")
            self._end(self._missing)
            return
        if state == "minted":
            logger.info("Node identity generated")
        self._end(self._ready)

    def _making(self) -> None:
        try:
            generate_identity(self._dsn())
        except FileExistsError as e:
            if not has_identity():
                logger.error("A new node identity could not be made: %s", e, exc_info=True)
                self._end(self._failed, e)
                return
            logger.info("An identity came back before the owner's choice of a new one was made: "
                        "it stays")
        except Exception as e:
            logger.error("A new node identity could not be made: %s", e, exc_info=True)
            self._end(self._failed, e)
            return
        else:
            logger.warning("Node identity generated on the owner's choice, over records the "
                           "identity that is gone signed")
        self._end(self._ready)


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
