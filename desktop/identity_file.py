"""The identity file: this node's identity, brought to another machine (SEC-S5 §16–§17).

One file holds what makes a node itself: the account's private key, its
node_info.json, the account's nickname and sign-in hash, the birth
certificate and the mined proof, and the keys it retired (the rotation
archive, previous/), so old chats and old backups still open on the new
machine. Its import places all of it, and a move with it derives nothing: an
anonymous identity, which no nickname and password make again, comes back
only so. The account's id is not carried — a machine mints its own, and with
it its own device tokens.

The key leaves and enters only by the machine's own hands: the launcher's
window (Settings & Tools › Backup & Restore) and the console of a Linux
Docker node (`./sautium identity export|import`) — never the Web UI, whose
device tokens must not take the node's key away for good.

Whoever holds the file is this node — its friends, its chat, its name. So it
may be locked with a passphrase, offered first: a key Argon2id derives at the
account key's own cost (node_identity.ARGON2_*) from the passphrase (NFKC) and
a random salt, the contents under XChaCha20-Poly1305 as a backup's are. The
cost is written in the header, so a file opens after a later release changes
the constants. The passphrase meets the same estimate as an account's
password (desktop/password_strength): the public key the network already
shows tells a guess that opens the file from one that does not, so the file
is as strong as its passphrase at that cost.

Format v1:

    MAGIC  ">HI" (version, header length)  header (canonical JSON)  body

    header  {"created_at", "identity": {"public_key_hex", "username",
            "invite_code"}, "passphrase": null | {"alg": "argon2id", "t", "m",
            "p", "salt": 32 hex, "check": 32 hex}}
    body    canonical JSON {"key", "info", "account", "documents", "previous"}
            — plain, or nonce (24 bytes) + XChaCha20-Poly1305 of it, the AAD
            the SHA-256 of everything before the body. `check` tells a wrong
            passphrase from a changed file.

A file is untrusted input: every name, size and field is checked before
anything is written, and the key inside must be the public key the file
names. The import places the key exclusively, as node_identity.place_key
does — never over a key: a folder that holds another identity, or an account
whose key is still to be made, is refused with its words.

THE CALLER'S DUTY. This module reads no database. Placing an identity on a
node whose database names another one as its own (its signing key of
record, its seals) makes the records that one signed another author's: the
caller asks the owner first, with `node_identity.another_identity_at(dsn,
summary(data)["public_key_hex"])` — before the derivation, the public key
is in the header — and places only on his yes (the account setup's
AnotherIdentity question, backend/device_auth). After a placement the
runtime starts again on the new key: the launcher restarts its backend and
P2P, a Docker node's backend is restarted by the console.

The command, `python -m desktop.identity_file`, is a Docker node's: it runs
in a container of the backend's image, on the node's own files, and the host
reaches them only through its stdin and stdout (scripts/setup.py,
`./sautium identity`). On a Windows host (Docker Desktop), by hand — the
file written in the container and copied out, so no shell's redirection
touches its bytes:

    docker exec -it -u agent sautium-backend python -m desktop.identity_file export --out /tmp/identity.sautium-identity
    docker cp sautium-backend:/tmp/identity.sautium-identity .
    docker exec -u agent sautium-backend rm /tmp/identity.sautium-identity

(`export --no-passphrase` for the plain file). On a node with no identity,
the import by hand: `docker cp` the file to /tmp in the container, `docker
exec -it -u agent sautium-backend python -m desktop.identity_file import --in
/tmp/<file>`, then `docker restart sautium-backend`.

The master's identity is made by this command too — a random key, in a
container that sees no network, written into the node's own identity folder
(SEC-S5 §18), then exported for its offline copies. From the checkout, with
the node stopped (PowerShell on the Windows host; in a Linux shell the same,
with `--user "$(id -u):$(id -g)"` added):

    docker run --rm --network none --entrypoint python -v "${PWD}/backend:/app:ro" -v "${PWD}/desktop:/app/desktop:ro" -v "${PWD}/data/node_identity:/id" $(docker inspect --format '{{.Image}}' sautium-backend) -m desktop.identity_file new --nickname Sautium --dir /id

It prints the invite code and the public key, for the pins.
"""

import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from desktop import node_identity
from desktop.node_backup import ARCHIVE_DIR
from desktop.node_identity import CERT_FILENAME, PREVIOUS_DIRNAME, ROTATION_FILENAME
from desktop.p2p.identity_proof import PROOF_FILENAME

logger = logging.getLogger(__name__)

MAGIC = b"SAUTIUM-IDENTITY\0"
FORMAT_VERSION = 1
FILE_SUFFIX = ".sautium-identity"
# Bounds a file is read within: the largest real one is a few rotations'
# keys and documents, tens of KiB
FILE_MAX = 4 * 1024 * 1024
HEADER_MAX = 4096
MEMBER_MAX = 64 * 1024
ARCHIVE_MAX = 64
SALT_LEN = 16
NONCE_LEN = 24
KEY_LEN = 32
CHECK_LEN = 16
# A header's cost is taken only inside these: a file locked by a later
# release at a higher cost still opens, a doctored one cannot ask for hours
# or for more memory than a small board has
TIME_MAX = 100_000
MEMORY_MIN, MEMORY_MAX = 8 * 1024, 1024 * 1024        # KiB
PARALLELISM_MAX = 16
CHECK_CONTEXT = b"sautium-identity:v1:check"

KEY_FILENAME = "node_ed25519.key"
INFO_FILENAME = "node_info.json"
DOCUMENTS = (CERT_FILENAME, PROOF_FILENAME)
ARCHIVE_NAMES = (KEY_FILENAME, "node_ed25519.pub", INFO_FILENAME, CERT_FILENAME,
                 PROOF_FILENAME, ROTATION_FILENAME)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_PHC = re.compile(r"\$argon2id\$v=19\$m=\d{1,8},t=\d{1,6},p=\d{1,3}\$[A-Za-z0-9+/]{16,64}\$[A-Za-z0-9+/]{16,128}")
EMAIL_MAX = 320

# The words beside the export (level C drafts, for the words review)
PASSPHRASE_WORDS = (
    "This passphrase locks the identity file. It is asked when the file is imported on another "
    "machine. Without it nobody can open the file — not you, and not Sautium's Support. Write it "
    "down and keep it apart from the file.")
PLAIN_WORDS = (
    "Whoever holds this file is this node: its friends, its chat, its name. Keep it apart from "
    "your backups — a folder that holds both makes the backup's encryption worth nothing. Never "
    "send it to anyone, Sautium's Support included.")
ESTIMATE_NOTE = (
    "Anyone who has this file can test guesses at home, unseen and without a limit. The time "
    "above is how long one GPU would take to find the passphrase.")


class IdentityFileError(Exception):
    """The file cannot be read, exported or placed; the message is for the person."""


class NotAnIdentityFile(IdentityFileError):
    pass


class Damaged(IdentityFileError):
    """Malformed, truncated, changed, or holding a key that is not its own."""


class NewerFormat(IdentityFileError):
    pass


class WrongPassphrase(IdentityFileError):
    pass


class WeakPassphrase(IdentityFileError):
    """A passphrase under the estimate's edge (password_strength.verdict:
    refused under an hour of one GPU, kept under a year only on the
    person's word). `verdict` is the estimate's."""

    def __init__(self, verdict: dict):
        super().__init__(verdict["door"])
        self.verdict = verdict


class NothingToExport(IdentityFileError):
    pass


class IdentityHere(IdentityFileError):
    """The identity folder holds an identity, or an account, already."""


@dataclass(frozen=True, repr=False)
class Contents:
    """An identity file's contents, checked: what a placement writes."""
    pubkey: str
    username: Optional[str]
    invite_code: Optional[str]
    key_pem: bytes
    info: dict
    account: Optional[dict]                     # {"username", "signin", "anonymous", "email"}
    documents: Dict[str, bytes]
    previous: Dict[str, Dict[str, bytes]]

    def __repr__(self) -> str:
        return f"<identity {self.invite_code or self.pubkey[:16]}>"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")


def _load_key(pem: bytes):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as e:
        raise Damaged(f"a key in the file is not one: {e}") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise Damaged("a key in the file is not an Ed25519 key")
    return key


# ---------------------------------------------------------------------------
# The contents, checked — one rule for what an export writes and an import takes
# ---------------------------------------------------------------------------

def _text(value, what: str) -> bytes:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MEMBER_MAX:
        raise Damaged(f"{what} is missing or too large")
    return value.encode("utf-8")


def _json_doc(data: bytes, what: str) -> dict:
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Damaged(f"{what} is not JSON") from None
    if not isinstance(doc, dict):
        raise Damaged(f"{what} is not a JSON object")
    return doc


def _identity_fields(pubkey, username, invite_code) -> None:
    if not isinstance(pubkey, str) or not _HEX64.fullmatch(pubkey):
        raise Damaged("the file names no public key")
    if username is None:
        if invite_code is not None:
            raise Damaged("the file's invite code names no nickname")
        return
    try:
        node_identity.validate_username(username)
    except (ValueError, TypeError):
        raise Damaged("the file's nickname is not one") from None
    if invite_code != node_identity.make_invite_code(username, bytes.fromhex(pubkey)):
        raise Damaged("the file's invite code is not its nickname's and key's")


def _account(value, username: Optional[str]) -> Optional[dict]:
    if value is None:
        if username is not None:
            raise Damaged("the file names a nickname and holds no account")
        return None
    if not isinstance(value, dict) or set(value) != {"username", "signin", "anonymous", "email"}:
        raise Damaged("the file's account is malformed")
    signin, anonymous, email = value["signin"], value["anonymous"], value["email"]
    if value["username"] != username or not isinstance(anonymous, bool):
        raise Damaged("the file's account is not its identity's")
    if anonymous != (signin is None):
        raise Damaged("the file's account has a sign-in hash only when it has a password")
    if signin is not None and not (isinstance(signin, str) and _PHC.fullmatch(signin)):
        raise Damaged("the file's sign-in hash is not one")
    if not isinstance(email, str) or len(email) > EMAIL_MAX:
        raise Damaged("the file's email is not one")
    return dict(value)


def _info(value, pubkey: str, username: Optional[str], invite_code: Optional[str]) -> dict:
    if not isinstance(value, dict) or len(_canonical(value)) > MEMBER_MAX:
        raise Damaged("the file's node_info is malformed")
    if (str(value.get("public_key_hex", "")).lower() != pubkey
            or str(value.get("node_id", "")).lower() != pubkey
            or value.get("username") != username or value.get("invite_code") != invite_code):
        raise Damaged("the file's node_info is not its identity's")
    previous = value.get("previous", [])
    if not isinstance(previous, list) or len(previous) > ARCHIVE_MAX:
        raise Damaged("the file's retired identities are malformed")
    for entry in previous:
        # `dir` is a path component every reader of the archive joins
        # (node_identity.load_previous_seeds)
        if not (isinstance(entry, dict) and isinstance(entry.get("public_key_hex"), str)
                and _HEX64.fullmatch(entry["public_key_hex"])
                and isinstance(entry.get("dir"), str) and ARCHIVE_DIR.fullmatch(entry["dir"])):
            raise Damaged("a retired identity in the file is malformed")
    return dict(value)


def _documents(value, pubkey: str) -> Dict[str, bytes]:
    if not isinstance(value, dict) or not set(value) <= set(DOCUMENTS):
        raise Damaged("the file's documents are malformed")
    out = {}
    for name, text in value.items():
        data = _text(text, name)
        if str(_json_doc(data, name).get("pubkey", "")).lower() != pubkey:
            raise Damaged(f"the file's {name} is another key's")
        out[name] = data
    return out


def _previous(value) -> Dict[str, Dict[str, bytes]]:
    if not isinstance(value, dict) or len(value) > ARCHIVE_MAX:
        raise Damaged("the file's archive is malformed")
    out = {}
    for folder, files in value.items():
        if not (isinstance(folder, str) and ARCHIVE_DIR.fullmatch(folder)):
            raise Damaged("a folder of the file's archive is not one")
        if not isinstance(files, dict) or not set(files) <= set(ARCHIVE_NAMES) or KEY_FILENAME not in files:
            raise Damaged(f"the archive's {folder} is malformed")
        kept = {name: _text(text, f"{folder}/{name}") for name, text in files.items()}
        retired = node_identity.public_hex(_load_key(kept[KEY_FILENAME]))
        if not folder.endswith("-" + retired[:12]):
            raise Damaged(f"the archive's {folder} holds another key")
        if INFO_FILENAME in kept and str(_json_doc(kept[INFO_FILENAME], f"{folder}/{INFO_FILENAME}")
                                         .get("public_key_hex", "")).lower() != retired:
            raise Damaged(f"the archive's {folder} holds another key's info")
        out[folder] = kept
    return out


def _contents(identity: dict, body: dict) -> Contents:
    if not isinstance(body, dict) or set(body) != {"key", "info", "account", "documents", "previous"}:
        raise Damaged("the file's contents are malformed")
    pubkey, username, invite_code = (identity["public_key_hex"], identity["username"],
                                     identity["invite_code"])
    key_pem = _text(body["key"], "the key")
    if node_identity.public_hex(_load_key(key_pem)) != pubkey:
        raise Damaged("the key in the file is not the public key it names")
    return Contents(pubkey=pubkey, username=username, invite_code=invite_code, key_pem=key_pem,
                    info=_info(body["info"], pubkey, username, invite_code),
                    account=_account(body["account"], username),
                    documents=_documents(body["documents"], pubkey),
                    previous=_previous(body["previous"]))


def _body(contents: Contents) -> dict:
    return {"key": contents.key_pem.decode("ascii"), "info": contents.info,
            "account": contents.account,
            "documents": {name: data.decode("utf-8") for name, data in contents.documents.items()},
            "previous": {folder: {name: data.decode("utf-8") for name, data in files.items()}
                         for folder, files in contents.previous.items()}}


# ---------------------------------------------------------------------------
# The passphrase
# ---------------------------------------------------------------------------

def _derive(passphrase: str, lock: dict) -> Tuple[bytes, bytes]:
    """(the contents' key, the check) for `passphrase` under the header's lock."""
    from argon2.low_level import Type, hash_secret_raw
    key = hash_secret_raw(secret=node_identity.account_password(passphrase).encode("utf-8"),
                          salt=bytes.fromhex(lock["salt"]), time_cost=lock["t"],
                          memory_cost=lock["m"], parallelism=lock["p"], hash_len=KEY_LEN,
                          type=Type.ID)
    return key, hmac.new(key, CHECK_CONTEXT, hashlib.sha256).digest()[:CHECK_LEN]


def _aead():
    from nacl import bindings
    return (bindings.crypto_aead_xchacha20poly1305_ietf_encrypt,
            bindings.crypto_aead_xchacha20poly1305_ietf_decrypt)


def estimate_inputs(identity_dir: Path) -> tuple:
    """The account's own words, which an attacker tries first against the
    passphrase (password_strength.verdict's `user_inputs`): its nickname and
    its email."""
    account = node_identity.read_account(identity_dir) or {}
    try:
        info = json.loads((identity_dir / INFO_FILENAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        info = {}
    return (account.get("username") or info.get("username") or "",
            account.get("email") or info.get("email") or "")


def check_passphrase(passphrase: str, user_inputs: tuple, keep: bool) -> dict:
    """The edge an account's password is held to (backend/device_auth.
    check_new_password): WeakPassphrase under an hour of one GPU, and under a
    year without the person's word (`keep`). Returns the verdict."""
    from desktop import password_strength
    verdict = password_strength.verdict(passphrase, user_inputs)
    if verdict["zone"] == "refused" or (verdict["zone"] == "word" and not keep):
        raise WeakPassphrase(verdict)
    return verdict


def opening_seconds(data: bytes) -> Optional[float]:
    """About how long opening the file takes on this machine (its lock's
    one derivation), None for a file with no passphrase."""
    from desktop.identity_birth import expected_seconds
    lock = _split(data)[0]["passphrase"]
    return None if lock is None else expected_seconds((lock["t"], lock["m"], lock["p"]))


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _read_member(path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) > MEMBER_MAX:
        raise NothingToExport(f"{path.name} in the identity folder is larger than any of its kind")
    return data


def _gather(d: Path) -> Contents:
    """The identity folder `d`'s identity: its key, node_info.json and
    account.json, the documents of that key, the archive."""
    account = node_identity.read_account(d)
    if not (d / KEY_FILENAME).exists():
        raise NothingToExport("This node has no identity to export yet: its account's key is still "
                              "to be made." if account is not None else
                              "This node has no identity to export.")
    key_pem = _read_member(d / KEY_FILENAME)
    pubkey = node_identity.public_hex(_load_key(key_pem))
    try:
        info = json.loads((d / INFO_FILENAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        info = {"node_id": pubkey, "public_key_hex": pubkey, "algorithm": "Ed25519"}
    if account is None and info.get("username"):
        raise NothingToExport("This node's identity was made by an older Sautium: it cannot be "
                              "exported. Make this node's account anew.")
    if account is not None and account.get("pubkey") != pubkey:
        raise NothingToExport("This node's identity is changing: export it when the change is done.")
    documents = {}
    for name in DOCUMENTS:
        path = d / name
        if path.is_file() and node_identity._document_key(path) == pubkey:
            documents[name] = _read_member(path)
    previous = {}
    archive = d / PREVIOUS_DIRNAME
    if archive.is_dir():
        for folder in sorted(archive.iterdir()):
            if folder.is_symlink() or not folder.is_dir() or not ARCHIVE_DIR.fullmatch(folder.name):
                continue
            files = {name: _read_member(folder / name) for name in ARCHIVE_NAMES
                     if (folder / name).is_file() and not (folder / name).is_symlink()}
            if KEY_FILENAME in files:
                previous[folder.name] = files
    username = info.get("username")
    contents = Contents(
        pubkey=pubkey, username=username, invite_code=info.get("invite_code"), key_pem=key_pem,
        info=info, documents=documents, previous=previous,
        account=None if account is None else {
            "username": account["username"], "signin": account.get("signin"),
            "anonymous": bool(account.get("anonymous")), "email": account.get("email") or ""})
    # What the export writes is what an import takes: the same checks, here
    _identity_fields(contents.pubkey, contents.username, contents.invite_code)
    try:
        return _contents(_header_identity(contents), json.loads(_canonical(_body(contents))))
    except (Damaged, UnicodeDecodeError) as e:
        raise NothingToExport(f"This node's identity folder cannot be exported as it is: {e}") from None


def _header_identity(contents: Contents) -> dict:
    return {"public_key_hex": contents.pubkey, "username": contents.username,
            "invite_code": contents.invite_code}


def export(identity_dir: Path, passphrase: Optional[str], *, keep: bool = False) -> bytes:
    """The identity file of the identity folder `identity_dir`. With a
    passphrase: held to the account password's edge (WeakPassphrase; `keep`
    the person's word for one under a year) and then one derivation at the
    key's cost — minutes, never on a UI thread. None: the plain file, whose
    words (PLAIN_WORDS) the caller says. NothingToExport while the folder
    holds no identity."""
    contents = _gather(identity_dir)
    if passphrase is not None:
        check_passphrase(passphrase, (contents.username or "", (contents.account or {}).get("email", "")),
                         keep)
    header = {"created_at": _now(), "identity": _header_identity(contents), "passphrase": None}
    body = _canonical(_body(contents))
    if passphrase is not None:
        lock = {"alg": "argon2id", "t": node_identity.ARGON2_TIME_COST,
                "m": node_identity.ARGON2_MEMORY_COST, "p": node_identity.ARGON2_PARALLELISM,
                "salt": secrets.token_hex(SALT_LEN)}
        key, check = _derive(passphrase, lock)
        header["passphrase"] = {**lock, "check": check.hex()}
    header_bytes = _canonical(header)
    lead = MAGIC + struct.pack(">HI", FORMAT_VERSION, len(header_bytes)) + header_bytes
    if passphrase is not None:
        nonce = secrets.token_bytes(NONCE_LEN)
        encrypt, _ = _aead()
        body = nonce + encrypt(body, hashlib.sha256(lead).digest(), nonce, key)
    data = lead + body
    if len(data) > FILE_MAX:
        raise NothingToExport("This identity is larger than an identity file may be.")
    logger.info("identity file made for %s (%s)", contents.invite_code or contents.pubkey[:16],
                "locked" if passphrase is not None else "no passphrase")
    return data


def write_file(path: Path, data: bytes) -> None:
    """`data` at `path`, a new file readable by its owner alone where the OS
    has modes (FileExistsError: never over a file)."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _split(data: bytes) -> Tuple[dict, bytes, bytes]:
    """(header, the bytes before the body, body), the header checked."""
    if len(data) > FILE_MAX:
        raise Damaged("This file is larger than any identity file.")
    if not data.startswith(MAGIC):
        raise NotAnIdentityFile("This is not a Sautium identity file.")
    start = len(MAGIC) + 6
    if len(data) < start:
        raise Damaged("This identity file is cut short.")
    version, length = struct.unpack(">HI", data[len(MAGIC):start])
    if version != FORMAT_VERSION:
        raise NewerFormat(f"This identity file is of format v{version}; this Sautium reads "
                          f"v{FORMAT_VERSION}. Update Sautium first." if version > FORMAT_VERSION
                          else f"This identity file's format v{version} is not one.")
    if length > HEADER_MAX or start + length > len(data):
        raise Damaged("This identity file is cut short or malformed.")
    try:
        header = json.loads(data[start:start + length].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Damaged("This identity file's header is not JSON.") from None
    if not isinstance(header, dict) or set(header) != {"created_at", "identity", "passphrase"}:
        raise Damaged("This identity file's header is malformed.")
    identity = header["identity"]
    if not isinstance(identity, dict) or set(identity) != {"public_key_hex", "username", "invite_code"}:
        raise Damaged("This identity file names no identity.")
    _identity_fields(identity["public_key_hex"], identity["username"], identity["invite_code"])
    if not isinstance(header["created_at"], str) or len(header["created_at"]) > 32:
        raise Damaged("This identity file's header is malformed.")
    lock = header["passphrase"]
    if lock is not None:
        ints = ("t", "m", "p")
        if not (isinstance(lock, dict) and set(lock) == {"alg", "salt", "check", *ints}
                and lock["alg"] == "argon2id"
                and all(isinstance(lock[k], int) and not isinstance(lock[k], bool) for k in ints)
                and 1 <= lock["t"] <= TIME_MAX and MEMORY_MIN <= lock["m"] <= MEMORY_MAX
                and 1 <= lock["p"] <= PARALLELISM_MAX
                and isinstance(lock["salt"], str) and re.fullmatch(r"[0-9a-f]{32}", lock["salt"])
                and isinstance(lock["check"], str) and re.fullmatch(r"[0-9a-f]{32}", lock["check"])):
            raise Damaged("This identity file's lock is malformed.")
    return header, data[:start + length], data[start + length:]


def summary(data: bytes) -> dict:
    """What the file says of itself, with no passphrase: its identity
    (`public_key_hex`, `username`, `invite_code` — public facts: the invite
    code carries them), whether it is locked, when it was made. Raises the
    file's errors."""
    header = _split(data)[0]
    return {**header["identity"], "locked": header["passphrase"] is not None,
            "created_at": header["created_at"]}


def open_file(data: bytes, passphrase: Optional[str]) -> Contents:
    """The file's contents, checked. A locked file takes its passphrase and
    one derivation at the cost its header names (minutes, never on a UI
    thread): WrongPassphrase when it is not the file's, Damaged when the file
    was changed."""
    header, before, body = _split(data)
    lock = header["passphrase"]
    if lock is not None:
        if not passphrase:
            raise WrongPassphrase("This identity file is locked: its passphrase opens it.")
        key, check = _derive(passphrase, lock)
        if not hmac.compare_digest(check, bytes.fromhex(lock["check"])):
            raise WrongPassphrase("That passphrase does not open this identity file.")
        from nacl.exceptions import CryptoError
        if len(body) < NONCE_LEN + 16:
            raise Damaged("This identity file is cut short.")
        _, decrypt = _aead()
        try:
            body = decrypt(body[NONCE_LEN:], hashlib.sha256(before).digest(), body[:NONCE_LEN], key)
        except CryptoError:
            raise Damaged("This identity file was changed after it was made.") from None
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Damaged("This identity file's contents are not readable.") from None
    return _contents(header["identity"], parsed)


def here(identity_dir: Path, pubkey: str) -> str:
    """What the identity folder holds against the identity `pubkey`:
    "empty" — no identity; "same" — this identity's own key, and a placement
    writes only what the folder lacks of it. Any other identity, or an
    account whose key is still to be made, raises IdentityHere with its
    words. Asked before the derivation, and again by place()."""
    d = identity_dir
    try:
        info = json.loads((d / INFO_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    if (d / KEY_FILENAME).exists():
        held = node_identity.pubkey_on_disk(d)
        if held == pubkey:
            return "same"
        named = info.get("invite_code") if str(info.get("public_key_hex", "")).lower() == held else None
        raise IdentityHere(
            f"This node has an identity already: {named or f'the key {held[:16]}…'}. An identity "
            "file is brought to a node that has none — a new installation, before its account is "
            "made. Nothing was changed.")
    account = node_identity.read_account(d)
    if account is not None:
        raise IdentityHere(
            f"This node has an account ({account.get('username')}) whose key is still to be made: "
            "an identity file is brought to a node that has none. Nothing was changed.")
    return "empty"


def _write_new(path: Path, data: bytes) -> None:
    """`data` as a new file readable by the node's user alone; a file there
    already stays as it is (the same retired key's, by the folder's name)."""
    try:
        node_identity._write_exclusive(path, data)
    except FileExistsError:
        logger.debug("%s is kept as it is", path)


def place(identity_dir: Path, contents: Contents) -> dict:
    """The file's identity into the identity folder `identity_dir`: the
    retired keys and their files, the documents, the key — exclusively
    (node_identity.place_key: never over a key) — its info, and a new account
    file with an id of its own (the file's nickname, sign-in hash, anonymity
    and email). A folder that holds this same identity gets only what it
    lacks, so a placement a stop cut short completes when run again.
    IdentityHere for a folder that holds another (here()). The caller's
    duty — asking the owner when the database names another identity, and
    starting the runtime again on the key — is the module's doc's."""
    from cryptography.hazmat.primitives import serialization
    d = identity_dir
    d.mkdir(parents=True, exist_ok=True)
    state = here(d, contents.pubkey)
    pubkey = contents.pubkey
    for folder, files in contents.previous.items():
        (d / PREVIOUS_DIRNAME / folder).mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            _write_new(d / PREVIOUS_DIRNAME / folder / name, data)
    for name, data in contents.documents.items():
        if node_identity._document_key(d / name) != pubkey:
            node_identity.write_identity_document(d, name, data, pubkey)
    key = _load_key(contents.key_pem)
    if state == "empty":
        try:
            node_identity.place_key(d, contents.key_pem)
        except FileExistsError:
            raise IdentityHere("A key landed in the identity folder meanwhile: it stays, and the "
                               "file's was not placed.") from None
        for name in ("tls_cert.pem", "tls_key.pem"):        # made again, bound to this key
            (d / name).unlink(missing_ok=True)
    (d / "node_ed25519.pub").write_bytes(key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo))
    if state == "empty" or not (d / INFO_FILENAME).exists():
        (d / INFO_FILENAME).write_text(json.dumps(contents.info, indent=2), encoding="utf-8")
    if contents.account is not None and node_identity.read_account(d) is None:
        node_identity.write_account(d, {
            "v": 1, "account_id": secrets.token_hex(16), **contents.account, "pubkey": pubkey,
            "source": "file", "created_at": _now()})
    logger.info("identity file %s: %s", "placed" if state == "empty" else "completed what the folder lacked of",
                contents.invite_code or pubkey[:16])
    return {"public_key_hex": pubkey, "username": contents.username,
            "invite_code": contents.invite_code, "placed": state == "empty"}


def import_(identity_dir: Path, data: bytes, passphrase: Optional[str]) -> dict:
    """open_file, then place: the whole import, for a caller that asked the
    owner first (the module's doc)."""
    contents = open_file(data, passphrase)
    return place(identity_dir, contents)


# ---------------------------------------------------------------------------
# The command, in a Docker node's container
# ---------------------------------------------------------------------------
# The console (scripts/setup.py) speaks to it through stdin and stdout alone:
#   phrase   → a phrase drawn (password_strength.phrase_offer) and the words, as JSON
#   check    ← the passphrase's line; → its verdict and the lock's estimate
#   export   ← the passphrase's line (none with --no-passphrase); → the file
#   peek     ← the file; → its summary, the folder's refusal, the database's word
#   import   ← the passphrase's line, the file's length's line, the file;
#            → {"opened": …} once open; ← "place"; → {"placed": …}
#   new      a random identity (no password makes it) in --dir: the master's
#            (SEC-S5 §18) — its public key and invite code, for the pins
# Its words go to stderr; exit 2 is a wrong passphrase, 3 a weak one.

def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _line(stream) -> str:
    raw = stream.readline(4096)
    return raw.decode("utf-8").rstrip("\r\n") if isinstance(raw, bytes) else raw.rstrip("\r\n")


def _reply(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _ask_twice() -> str:
    import getpass
    from desktop import password_strength
    offer = password_strength.phrase_offer()
    _say(f"A phrase drawn for you: {offer['phrase']}  ({offer['count']})")
    _say(PASSPHRASE_WORDS)
    first = getpass.getpass("Passphrase — the phrase above, or your own: ")
    if getpass.getpass("The passphrase again: ") != first:
        raise IdentityFileError("The two passphrases are not the same: nothing was written.")
    return first


def _node() -> Tuple[Path, str]:
    """This node's identity folder and database, as its backend reads them:
    the command is a Docker node's, run in its backend's container."""
    from config import settings
    return Path(settings.p2p_identity_dir), settings.database_url


ANOTHER_IDENTITY = ("This node's database names another identity as its own: placed here, this one "
                    "makes the records that one signed here another author's.")


def new_identity(identity_dir: Path, nickname: str) -> dict:
    """A random identity in the empty identity folder `identity_dir`, its
    account anonymous (node_identity.anonymous_account: the pairing code its
    only door, its way to another machine this file): the master's road,
    one logic with every anonymous node's (SEC-S5 §18). The nickname is
    taken as given, the reserved one included — the console is the
    machine's own hands, and the master's name is the reserved one; the
    pins it prints make the key Support's. IdentityHere over any key or
    account. Returns the info."""
    if (identity_dir / KEY_FILENAME).exists() or node_identity.read_account(identity_dir) is not None:
        raise IdentityHere(f"{identity_dir} holds an identity or an account already: nothing was made.")
    identity_dir.mkdir(parents=True, exist_ok=True)
    info = node_identity.anonymous_account(identity_dir, nickname, source="console")
    logger.warning("A random identity was made by the console: %s", info["invite_code"])
    return info


def main(argv=None) -> int:
    import argparse
    import getpass

    import psycopg2
    parser = argparse.ArgumentParser(prog="python -m desktop.identity_file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("phrase")
    sub.add_parser("check")
    p_export = sub.add_parser("export")
    p_export.add_argument("--no-passphrase", action="store_true")
    p_export.add_argument("--keep", action="store_true", help="keep a passphrase under a year")
    p_export.add_argument("--out", help="a new file in the container, not stdout")
    sub.add_parser("peek")
    p_import = sub.add_parser("import")
    p_import.add_argument("--another-identity", action="store_true",
                          help="the owner's yes: the database names another identity")
    p_import.add_argument("--in", dest="source", help="a file in the container, asked about here")
    p_new = sub.add_parser("new")
    p_new.add_argument("--nickname", required=True)
    p_new.add_argument("--dir", required=True, help="the identity folder, as mounted here")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(message)s")

    from desktop import password_strength
    from desktop.identity_birth import expected_seconds
    if args.command == "new":
        try:
            info = new_identity(Path(args.dir), args.nickname)
        except (IdentityFileError, ValueError) as e:
            _say(str(e))
            return 1
        print(f"invite_code    = \"{info['invite_code']}\"")
        print(f"public_key_hex = \"{info['public_key_hex']}\"")
        return 0
    d, dsn = _node()
    stdin = sys.stdin.buffer
    try:
        if args.command == "phrase":
            _reply({**password_strength.phrase_offer(), "words": PASSPHRASE_WORDS, "plain": PLAIN_WORDS})
            return 0
        if args.command == "check":
            verdict = password_strength.verdict(_line(stdin), estimate_inputs(d))
            seconds = expected_seconds()
            _reply({**verdict, "expected_s": round(seconds),
                    "expected_words": password_strength.words(seconds)})
            return 0
        if args.command == "export":
            interactive = args.out is not None and sys.stdin.isatty()
            passphrase = None if args.no_passphrase else _ask_twice() if interactive else _line(stdin)
            if args.out is None and sys.stdout.isatty():
                raise IdentityFileError("The file is bytes: send it to a file (> FILE, or --out FILE).")
            if passphrase is not None:
                check_passphrase(passphrase, estimate_inputs(d), args.keep)
                if interactive:
                    _say(f"Locking the file: {password_strength.words(expected_seconds())}.")
            data = export(d, passphrase, keep=args.keep)
            if args.out is not None:
                write_file(Path(args.out), data)
                _say(f"Written to {args.out}. " + (PASSPHRASE_WORDS if passphrase else PLAIN_WORDS))
            else:
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
            return 0
        if args.command == "peek":
            data = stdin.read(FILE_MAX + 1)
            facts = summary(data)
            try:
                here(d, facts["public_key_hex"])
                refusal = None
            except IdentityHere as e:
                refusal = str(e)
            seconds = opening_seconds(data)
            _reply({**facts, "refusal": refusal, "plain": PLAIN_WORDS, "another_words": ANOTHER_IDENTITY,
                    "expected_s": None if seconds is None else round(seconds),
                    "expected_words": None if seconds is None else password_strength.words(seconds),
                    "another_identity": refusal is None and node_identity.another_identity_at(
                        dsn, facts["public_key_hex"])})
            return 0
        # import
        interactive = args.source is not None
        if interactive:
            with open(args.source, "rb") as f:
                data = f.read(FILE_MAX + 1)
            facts = summary(data)
            here(d, facts["public_key_hex"])
            _say(f"An identity file of {facts['invite_code'] or facts['public_key_hex'][:16]}, made "
                 f"{facts['created_at'][:10]}.")
        else:
            passphrase = _line(stdin) or None
            try:
                length = int(_line(stdin))
            except ValueError:
                raise Damaged("The file's length was not given.") from None
            if not 0 < length <= FILE_MAX:
                raise Damaged("This file is larger than any identity file.")
            data = stdin.read(length)
            facts = summary(data)
        if node_identity.another_identity_at(dsn, facts["public_key_hex"]) and not args.another_identity:
            if not (interactive and input(ANOTHER_IDENTITY + " Type yes to bring it here all the "
                                          "same: ").strip().lower() == "yes"):
                raise IdentityHere(ANOTHER_IDENTITY + " Nothing was changed.")
        if interactive:
            seconds = opening_seconds(data)
            passphrase = None
            if seconds is not None:
                passphrase = getpass.getpass("Its passphrase: ")
                _say(f"Opening the file: {password_strength.words(seconds)}.")
        contents = open_file(data, passphrase)
        if not interactive:
            _reply({"opened": summary(data)})
            if _line(stdin) != "place":
                return 0
        placed = place(d, contents)
        if interactive:
            _say(f"This node is {placed['invite_code'] or placed['public_key_hex'][:16]} now: restart "
                 "its backend (docker restart sautium-backend).")
        else:
            _reply({"placed": placed})
        return 0
    except WrongPassphrase as e:
        _say(str(e))
        return 2
    except WeakPassphrase as e:
        _say(str(e))
        return 3
    except IdentityFileError as e:
        _say(str(e))
        return 1
    except OSError as e:
        _say(f"A file could not be read or written: {e}")
        return 1
    except psycopg2.Error as e:
        _say(f"The node's database could not be read: {str(e).strip()}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
