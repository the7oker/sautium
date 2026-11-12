"""
The release signature check: SSH signatures (SSHSIG, PROTOCOL.sshsig) over
git's annotated tag objects, verified here in Python rather than by `git
verify-tag` — the Windows installer's MinGit carries no `ssh-keygen`.

Two key types and nothing else: `ssh-ed25519`, and `sk-ssh-ed25519@openssh.com`
(a FIDO2 key; PROTOCOL.u2f). An `sk-` signature counts only with BOTH the
user-presence and the user-verification flags set: a touch AND the PIN. No RSA,
no ECDSA, no certificates.

The keys come from an allowed-signers file in git's format, read strictly: a
line is `principals [namespaces="..."] keytype base64 [comment]`; any other
option, or anything that does not parse, refuses that line (logged) and keeps
the rest. Which file is the caller's to choose — the updater reads the
INSTALLED tree's (desktop/updater.py), a first install the installer's payload.

Stdlib + `cryptography` only: the bootstraps run this from the payload before
anything else of the tree is installed.
"""

import base64
import binascii
import hashlib
import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = logging.getLogger(__name__)

ED25519 = "ssh-ed25519"
SK_ED25519 = "sk-ssh-ed25519@openssh.com"
KEY_TYPES = (ED25519, SK_ED25519)

# The namespace git signs tags and commits under (`ssh-keygen -Y sign -n git`).
GIT_NAMESPACE = "git"

# PROTOCOL.u2f / sk-api.h: the authenticator's flags in an sk- signature.
SK_USER_PRESENCE = 0x01
SK_USER_VERIFICATION = 0x04

_MAGIC = b"SSHSIG"
_ARMOR_BEGIN = b"-----BEGIN SSH SIGNATURE-----"
_ARMOR_END = b"-----END SSH SIGNATURE-----"
_HASHES = {"sha256": hashlib.sha256, "sha512": hashlib.sha512}

# A release's name: `v<major>.<minor>.<patch>`, no leading zeros, nothing
# after, six digits a part at most — a name every filesystem stores (a pushed
# `v1.0.<300 digits>` could not be, and failed the fetch it was in).
_VERSION = re.compile(r"v(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
_HEX_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class VerifyError(Exception):
    """Why a signature, a tag or a signer line is refused — the text goes to
    the log and the update dialog's details."""


@dataclass(frozen=True)
class Signer:
    principals: Tuple[str, ...]
    namespaces: Optional[Tuple[str, ...]]   # None: the line names none, so every one
    key_type: str
    key_blob: bytes                          # the public key in SSH wire form

    def allows(self, namespace: str) -> bool:
        return self.namespaces is None or namespace in self.namespaces


@dataclass(frozen=True)
class SignedTag:
    name: str
    version: Tuple[int, int, int]
    commit: str
    signer: Signer


def parse_version(name: str) -> Optional[Tuple[int, int, int]]:
    match = _VERSION.fullmatch(name)
    return tuple(int(part) for part in match.groups()) if match else None


# ── SSH wire format ───────────────────────────────────────────────────────

class _Reader:
    def __init__(self, data: bytes, what: str):
        self.data = data
        self.at = 0
        self.what = what

    def take(self, n: int) -> bytes:
        if self.at + n > len(self.data):
            raise VerifyError(f"{self.what} is truncated")
        out = self.data[self.at:self.at + n]
        self.at += n
        return out

    def uint32(self) -> int:
        return int.from_bytes(self.take(4), "big")

    def byte(self) -> int:
        return self.take(1)[0]

    def string(self) -> bytes:
        return self.take(self.uint32())

    def text(self) -> str:
        try:
            return self.string().decode("utf-8")
        except UnicodeDecodeError:
            raise VerifyError(f"{self.what} has a field that is not UTF-8") from None

    def end(self) -> None:
        if self.at != len(self.data):
            raise VerifyError(f"{self.what} has {len(self.data) - self.at} trailing bytes")


def _string(value: bytes) -> bytes:
    return len(value).to_bytes(4, "big") + value


@dataclass(frozen=True)
class _PublicKey:
    key_type: str
    point: bytes                    # the 32-byte Ed25519 public key
    application: Optional[str]      # an sk- key's FIDO application, e.g. "ssh:"


def _parse_public_key(blob: bytes) -> _PublicKey:
    reader = _Reader(blob, "the public key")
    key_type = reader.text()
    if key_type not in KEY_TYPES:
        raise VerifyError(f"key type {key_type!r} is not accepted")
    point = reader.string()
    if len(point) != 32:
        raise VerifyError("the Ed25519 public key is not 32 bytes")
    application = reader.text() if key_type == SK_ED25519 else None
    reader.end()
    return _PublicKey(key_type, point, application)


# ── allowed signers ───────────────────────────────────────────────────────

_NAMESPACES_OPTION = re.compile(r'namespaces="([^"]*)"')
_PRINCIPALS = re.compile(r"[^\s\",*?!]+(,[^\s\",*?!]+)*")


def _parse_signer_line(line: str) -> Signer:
    fields = line.split()
    if len(fields) < 3:
        raise VerifyError("a line needs principals, a key type and a key")
    principals, rest = fields[0], fields[1:]
    if not _PRINCIPALS.fullmatch(principals):
        raise VerifyError(f"principals {principals!r} are not a plain comma-separated list")
    namespaces = None
    if rest[0] not in KEY_TYPES:
        option = _NAMESPACES_OPTION.fullmatch(rest[0])
        if not option:
            raise VerifyError(f"option {rest[0]!r} is not accepted (only namespaces=\"...\")")
        namespaces = tuple(name for name in option.group(1).split(",") if name)
        if not namespaces:
            raise VerifyError("namespaces=\"\" names no namespace")
        rest = rest[1:]
    if len(rest) < 2 or rest[0] not in KEY_TYPES:
        raise VerifyError(f"key type {rest[0] if rest else '(none)'!r} is not accepted")
    key_type, encoded = rest[0], rest[1]
    try:
        blob = base64.b64decode(encoded, validate=True)
    except binascii.Error:
        raise VerifyError("the key is not base64") from None
    if _parse_public_key(blob).key_type != key_type:
        raise VerifyError("the key's own type differs from the line's")
    return Signer(tuple(principals.split(",")), namespaces, key_type, blob)


def parse_allowed_signers(text: str, source: str = "allowed signers") -> List[Signer]:
    """Every line that parses; a line that does not is refused alone and said
    in the log — one broken line must not take the others' keys away."""
    signers = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            signers.append(_parse_signer_line(line))
        except VerifyError as exc:
            logger.warning(f"{source} line {number} refused: {exc}")
    return signers


# ── SSHSIG ────────────────────────────────────────────────────────────────

def _dearmor(armored: bytes) -> bytes:
    lines = armored.strip().splitlines()
    if len(lines) < 3 or lines[0].strip() != _ARMOR_BEGIN or lines[-1].strip() != _ARMOR_END:
        raise VerifyError("the signature is not an SSH SIGNATURE block")
    try:
        return base64.b64decode(b"".join(line.strip() for line in lines[1:-1]), validate=True)
    except binascii.Error:
        raise VerifyError("the signature block is not base64") from None


def verify(message: bytes, armored: bytes, signers: List[Signer], namespace: str) -> Signer:
    """The signer of `armored` over `message` under `namespace`, or
    VerifyError. The key in the signature must be one of `signers` allowed for
    that namespace — the signature names its key, the list decides."""
    reader = _Reader(_dearmor(armored), "the signature")
    if reader.take(6) != _MAGIC:
        raise VerifyError("the signature does not start with SSHSIG")
    if reader.uint32() != 1:
        raise VerifyError("the signature's version is not 1")
    key_blob = reader.string()
    signed_namespace = reader.text()
    reserved = reader.string()
    hash_name = reader.text()
    signature = reader.string()
    reader.end()

    if signed_namespace != namespace:
        raise VerifyError(f"signed under namespace {signed_namespace!r}, not {namespace!r}")
    if hash_name not in _HASHES:
        raise VerifyError(f"hash {hash_name!r} is not accepted")
    key = _parse_public_key(key_blob)
    signer = next((s for s in signers if s.key_blob == key_blob and s.allows(namespace)), None)
    if signer is None:
        raise VerifyError(f"the {key.key_type} key that signed is not listed for {namespace!r}")

    signed_data = (_MAGIC + _string(namespace.encode()) + _string(reserved)
                   + _string(hash_name.encode()) + _string(_HASHES[hash_name](message).digest()))

    sig = _Reader(signature, "the signature's inner blob")
    if sig.text() != key.key_type:
        raise VerifyError("the signature's type differs from its key's")
    raw = sig.string()
    if len(raw) != 64:
        raise VerifyError("the Ed25519 signature is not 64 bytes")
    if key.key_type == SK_ED25519:
        flags = sig.byte()
        counter = sig.take(4)
        sig.end()
        if not flags & SK_USER_PRESENCE:
            raise VerifyError("the security key signed without a touch (user presence)")
        if not flags & SK_USER_VERIFICATION:
            raise VerifyError("the security key signed without its PIN (user verification)")
        # PROTOCOL.u2f: the authenticator signs the application's hash, its
        # flags and counter, then the hash of what was asked to be signed.
        to_verify = (hashlib.sha256(key.application.encode()).digest() + bytes([flags])
                     + counter + hashlib.sha256(signed_data).digest())
    else:
        sig.end()
        to_verify = signed_data
    try:
        Ed25519PublicKey.from_public_bytes(key.point).verify(raw, to_verify)
    except InvalidSignature:
        raise VerifyError("the signature does not match") from None
    return signer


# ── git tag objects ───────────────────────────────────────────────────────

def split_tag(raw: bytes) -> Tuple[bytes, bytes]:
    """(payload, armored signature) of a tag object as `git cat-file tag`
    gives it: git appends the signature to the message, and what it signed is
    everything before the block's first line."""
    at = raw.rfind(b"\n" + _ARMOR_BEGIN + b"\n")
    if at == -1:
        raise VerifyError("the tag carries no SSH signature")
    payload, armored = raw[:at + 1], raw[at + 1:]
    if not armored.endswith(_ARMOR_END + b"\n"):
        raise VerifyError("the tag's signature block is not at its end")
    if b"-----BEGIN " in payload:
        raise VerifyError("the tag carries more than one signature block")
    return payload, armored


def tag_headers(payload: bytes) -> dict:
    head, sep, _message = payload.partition(b"\n\n")
    if not sep:
        raise VerifyError("the tag object has no message")
    headers = {}
    for line in head.split(b"\n"):
        key, _, value = line.partition(b" ")
        name = key.decode("ascii", "replace")
        if name not in ("object", "type", "tag", "tagger"):
            raise VerifyError(f"the tag object has a header {name!r}")
        if name in headers:
            raise VerifyError(f"the tag object names {name!r} twice")
        headers[name] = value.decode("utf-8", "replace")
    for name in ("object", "type", "tag"):
        if name not in headers:
            raise VerifyError(f"the tag object has no {name!r}")
    return headers


def verify_tag(raw: bytes, ref_name: str, signers: List[Signer]) -> SignedTag:
    """A release tag: signed under `git` by a listed key, naming a commit, and
    carrying in its own `tag` header the name it was fetched under — a signed
    v0.2.0 pushed as v9.9.9 is refused, and the version is the signed name."""
    version = parse_version(ref_name)
    if version is None:
        raise VerifyError(f"{ref_name!r} is not a release name (v<major>.<minor>.<patch>)")
    payload, armored = split_tag(raw)
    headers = tag_headers(payload)
    if headers["tag"] != ref_name:
        raise VerifyError(f"the signed tag is named {headers['tag']!r}, fetched as {ref_name!r}")
    if headers["type"] != "commit":
        raise VerifyError(f"the tag points at a {headers['type']!r}, not a commit")
    if not _HEX_ID.fullmatch(headers["object"]):
        raise VerifyError("the tag's object is not an object id")
    signer = verify(payload, armored, signers, GIT_NAMESPACE)
    return SignedTag(ref_name, version, headers["object"], signer)
