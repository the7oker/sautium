"""Throwaway release keys for the tests: SSHSIG signatures made here in
Python, so an `sk-` key's flags and counter can be anything a test needs (a
security key alone decides them on real hardware), and signed tag objects
written straight into a repository. No key here is ever anyone's."""

import base64
import hashlib
import subprocess
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from desktop import release_verify as rv


def _string(value: bytes) -> bytes:
    return len(value).to_bytes(4, "big") + value


@dataclass
class Key:
    sk: bool = False
    application: str = "ssh:sautium-release"
    private: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)

    @property
    def key_type(self) -> str:
        return rv.SK_ED25519 if self.sk else rv.ED25519

    @property
    def point(self) -> bytes:
        return self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    @property
    def blob(self) -> bytes:
        blob = _string(self.key_type.encode()) + _string(self.point)
        return blob + _string(self.application.encode()) if self.sk else blob

    def line(self, principal: str = "release", namespaces: str = 'namespaces="git"') -> str:
        options = f" {namespaces}" if namespaces else ""
        return f"{principal}{options} {self.key_type} {base64.b64encode(self.blob).decode()} throwaway"

    def sign(self, message: bytes, namespace: str = "git", *, flags: int = 0x05,
             counter: int = 7, hash_name: str = "sha512", signed_namespace: str = None) -> bytes:
        """An armored SSH SIGNATURE. `signed_namespace` puts another
        namespace into the signed data than the blob declares."""
        digest = getattr(hashlib, hash_name)(message).digest()
        signed = (b"SSHSIG" + _string((signed_namespace or namespace).encode()) + _string(b"")
                  + _string(hash_name.encode()) + _string(digest))
        if self.sk:
            tail = bytes([flags]) + counter.to_bytes(4, "big")
            raw = self.private.sign(hashlib.sha256(self.application.encode()).digest() + tail
                                    + hashlib.sha256(signed).digest())
            inner = _string(self.key_type.encode()) + _string(raw) + tail
        else:
            inner = _string(self.key_type.encode()) + _string(self.private.sign(signed))
        blob = (b"SSHSIG" + (1).to_bytes(4, "big") + _string(self.blob) + _string(namespace.encode())
                + _string(b"") + _string(hash_name.encode()) + _string(inner))
        text = base64.b64encode(blob).decode()
        body = "\n".join(text[i:i + 70] for i in range(0, len(text), 70))
        return f"-----BEGIN SSH SIGNATURE-----\n{body}\n-----END SSH SIGNATURE-----\n".encode()


def signers(*lines: str):
    return rv.parse_allowed_signers("\n".join(lines) + "\n")


def tag_payload(commit: str, name: str, message: str = None) -> bytes:
    return (f"object {commit}\ntype commit\ntag {name}\n"
            f"tagger Throwaway <t@example.invalid> 1794470000 +0000\n\n"
            f"{message or 'Sautium ' + name[1:]}\n").encode()


def signed_tag_object(key: Key, commit: str, name: str, **sign) -> bytes:
    payload = tag_payload(commit, name)
    return payload + key.sign(payload, **sign)


def git(cwd, *args, stdin: bytes = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          input=stdin).stdout.decode().strip()


def push_signed_tag(repo, key: Key, commit: str, name: str, *, header_name: str = None,
                    **sign) -> str:
    """Write a signed tag object into `repo` and point refs/tags/<name> at it.
    `header_name` signs another name than the ref it is pushed under."""
    raw = signed_tag_object(key, commit, header_name or name, **sign)
    oid = git(repo, "hash-object", "-t", "tag", "-w", "--stdin", stdin=raw)
    git(repo, "update-ref", f"refs/tags/{name}", oid)
    return oid
