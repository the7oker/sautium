"""The yt-dlp this node runs: the latest release of yt-dlp's STABLE channel,
upstream's own self-contained build for this platform — its Python, its
dependencies and the yt-dlp-ejs challenge solver inside — checked before it
is unpacked, and run from the node's cache as the node's user. Nothing of it
enters the backend's own interpreter.

The check: the release's SHA2-256SUMS must carry a signature of yt-dlp's
release key, pinned here, and the archive the digest that file names. The
sums alone come from the same release as the archive; the signature is what
ties them to yt-dlp's release process. A release that fails either is never
unpacked, and the build in place stays.

How often: a pass at the backend's start, then once a day, and sooner when a
download fails the way an outdated build fails (service.ytdlp_refresh_loop).
A pass asks GitHub for the latest stable release and fetches it only when it
is not the one in place; a release older than the one in place is refused
(an older release is validly signed too), and a build replaces the one in
place only once it runs and names its own release.
"""

import base64
import hashlib
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_LATEST = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
_ASSET_URL = "https://github.com/yt-dlp/yt-dlp/releases/download/{tag}/{name}"
# A release tag is a date (2026.10.26, a hotfix 2026.10.26.1): it names a
# folder, so nothing else is taken.
_TAG = re.compile(r"\d{4}\.\d{2}\.\d{2}(?:\.\d+)?")

# yt-dlp's release key: its public-key packet alone (RFC 4880 §5.5.2 — v4,
# RSA 4096, made 2023-02-26), as published in yt-dlp's repository
# (public.key), and the fingerprint that packet must hash to.
SIGNER_FINGERPRINT = "AC0CBBE6848D6A873464AF4E57CF65933B5A7581"
SIGNER_PACKET = base64.b64decode(
    "BGP78C4BEAD0rF9zjGPAt0thlt5C1ebzccAVX7Nb1v+eqQjk+WEZdTETVCg3WAM5ngArlHdm/fZq"
    "zUgO+pAYrB60GKeg7ffUDf+S0XFKEZdeRLYeAaqqKhSibValDjvOBOztu3W607HLETQAqA7wTPuI"
    "t2WqmpL60NIcyr27LxqmgdN3mNvZ2iLO+bP0nKR/C+PgE9H4ytywDa12zMx6PmZCnVOOOu6XZEFm"
    "dUxxdQ9fFDqd9LcBKY2LDOcSYo1saY0YWiZWHtzVoZu1kOzjnS5Fjq/yBHJLImDH7pNxHm7s/Pna"
    "urpmQFtDFrukt+2lhDnpKUmGr/I/3IHqH/X+9nPoS4uiqQ5HpblB8BK+4WfpaiEg75LnvuOPfZIP"
    "KYyXa/0A7QojMwgOrD88ozT+VCkKkkJ+ijXZ7gHNjmcBaUdKK7fDIEOYI63Lyc6QWkGQTigFffSU"
    "XWHDCO9aXNhP3ejqFWgGMtCUsrbkcJkWuWY7q5ARy/05HbSM3K4DU9eqtnxmiV1WQ8nXuI9JgJQR"
    "vh5PTkny5LtxqzcmqvWO9TjHBbrs14BPEO9fcXxKL/CFBbzXDSvvAgArdqqlMoncQ/yicTlfL6qz"
    "J8EKFiqW14QMTdAn6SuuZTodXCTiInwoT7WjjuFPKKdvfH1GP4bnqdzTnzLxCSDIEtfyfPsIX+9G"
    "I7Jkk/zZjQARAQAB")

# What a yt-dlp child inherits: enough to find its JS runtime and ffmpeg, a
# proxy and its own caches, and what Windows needs to open a socket — none of
# the node's credentials.
_CHILD_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ",
              "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME", "DENO_DIR",
              "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
              "SSL_CERT_FILE", "SSL_CERT_DIR",
              "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE",
              "APPDATA", "LOCALAPPDATA", "PROGRAMDATA")


class Refused(Exception):
    """A release that is not what yt-dlp's release key and its sums say."""


def _tag_key(tag: str) -> tuple:
    return tuple(int(part) for part in tag.split("."))


def _asset() -> tuple:
    """(the release's archive for this platform, the executable in it)."""
    machine = platform.machine().lower()
    if sys.platform == "win32" and machine in ("amd64", "x86_64"):
        return "yt-dlp_win.zip", "yt-dlp.exe"
    if sys.platform == "darwin":
        return "yt-dlp_macos.zip", "yt-dlp_macos"
    if sys.platform == "linux" and machine == "x86_64":
        return "yt-dlp_linux.zip", "yt-dlp_linux"
    if sys.platform == "linux" and machine in ("aarch64", "arm64"):
        return "yt-dlp_linux_aarch64.zip", "yt-dlp_linux_aarch64"
    raise Refused(f"yt-dlp publishes no build this node runs ({sys.platform} {machine})")


def supported() -> bool:
    try:
        _asset()
    except Refused:
        return False
    return True


def root() -> Path:
    """Beside the node's model caches: a folder of the node's user."""
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "sautium" / "yt-dlp"


def installed_tag() -> Optional[str]:
    try:
        tag = (root() / "current").read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None
    return tag if _TAG.fullmatch(tag) else None


def executable() -> Optional[Path]:
    """The build in place; None before the first one lands, or on a platform
    yt-dlp publishes no build for."""
    tag = installed_tag()
    if tag is None:
        return None
    try:
        exe = root() / tag / _asset()[1]
    except Refused:
        return None
    return exe if exe.is_file() else None


def child_env() -> dict:
    return {k: v for k, v in os.environ.items() if k in _CHILD_ENV}


# -- the signature (RFC 4880, as much of it as one v4 RSA signature needs) ----

def _packets(data: bytes):
    """(tag, body) of each OpenPGP packet in `data`."""

    def take(i: int, width: int) -> int:
        if i + width > len(data):
            raise Refused("a truncated packet header")
        return int.from_bytes(data[i:i + width], "big")

    i = 0
    while i < len(data):
        first = data[i]
        i += 1
        if not first & 0x80:
            raise Refused("not an OpenPGP packet")
        if first & 0x40:                            # new format
            tag, octet = first & 0x3F, take(i, 1)
            i += 1
            if octet < 192:
                length = octet
            elif octet < 224:
                length = ((octet - 192) << 8) + take(i, 1) + 192
                i += 1
            elif octet == 255:
                length = take(i, 4)
                i += 4
            else:
                raise Refused("a partial-length packet")
        else:                                       # old format
            tag, kind = (first >> 2) & 0x0F, first & 3
            if kind == 3:
                raise Refused("an indeterminate-length packet")
            width = (1, 2, 4)[kind]
            length = take(i, width)
            i += width
        if i + length > len(data):
            raise Refused("a truncated packet")
        yield tag, data[i:i + length]
        i += length


def _mpi(body: bytes, i: int) -> tuple:
    bits = int.from_bytes(body[i:i + 2], "big")
    end = i + 2 + (bits + 7) // 8
    if end > len(body):
        raise Refused("a truncated number")
    return int.from_bytes(body[i + 2:end], "big"), end


def _release_key():
    from cryptography.hazmat.primitives.asymmetric import rsa
    body = SIGNER_PACKET
    fingerprint = hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).hexdigest().upper()
    if fingerprint != SIGNER_FINGERPRINT or body[0] != 4 or body[5] not in (1, 2, 3):
        raise Refused("the pinned release key is not the key its fingerprint names")
    n, i = _mpi(body, 6)
    e, _ = _mpi(body, i)
    return rsa.RSAPublicNumbers(e, n).public_key()


def verify_signature(document: bytes, signature: bytes) -> None:
    """Raises Refused unless `signature` is a v4 signature of `document`, as
    a binary document, by yt-dlp's release key."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    packets = list(_packets(signature))
    if len(packets) != 1 or packets[0][0] != 2:
        raise Refused("not one signature packet")
    body = packets[0][1]
    digests = {8: hashes.SHA256, 10: hashes.SHA512}
    if len(body) < 6 or body[0] != 4 or body[1] != 0 or body[2] != 1 or body[3] not in digests:
        raise Refused("not a v4 RSA signature of a binary document")
    hashed = body[:6 + int.from_bytes(body[4:6], "big")]
    at = len(hashed)
    at += 2 + int.from_bytes(body[at:at + 2], "big") + 2      # unhashed subpackets, hash prefix
    value, _ = _mpi(body, at)
    key = _release_key()
    trailer = b"\x04\xff" + len(hashed).to_bytes(4, "big")
    try:
        key.verify(value.to_bytes((key.key_size + 7) // 8, "big"), document + hashed + trailer,
                   padding.PKCS1v15(), digests[body[3]]())
    except InvalidSignature:
        raise Refused("the sums carry no valid signature of yt-dlp's release key") from None


def _digest(sums: bytes, name: str) -> str:
    for line in sums.decode("ascii", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == name and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            return parts[0]
    raise Refused(f"the signed sums name no {name}")


# -- a pass ----------------------------------------------------------------------

def _client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True,
                        headers={"User-Agent": "Sautium"})


def _unpack(archive: Path, into: Path) -> None:
    """Every member under `into`, with the mode the archive recorded."""
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            name = member.filename
            if name.startswith(("/", "\\")) or ":" in name or ".." in re.split(r"[/\\]", name):
                raise Refused(f"the archive names a path outside its folder: {name!r}")
            path = Path(zf.extract(member, into))
            mode = (member.external_attr >> 16) & 0o777
            if mode and not member.is_dir():
                os.chmod(path, mode)


def _install(client: httpx.Client, tag: str) -> None:
    name, exe = _asset()
    sums = client.get(_ASSET_URL.format(tag=tag, name="SHA2-256SUMS"))
    sums.raise_for_status()
    signature = client.get(_ASSET_URL.format(tag=tag, name="SHA2-256SUMS.sig"))
    signature.raise_for_status()
    verify_signature(sums.content, signature.content)
    expected = _digest(sums.content, name)

    folder = root()
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / f"{tag}.zip.part"
    digest = hashlib.sha256()
    try:
        with client.stream("GET", _ASSET_URL.format(tag=tag, name=name)) as r:
            r.raise_for_status()
            with open(archive, "wb") as out:
                for chunk in r.iter_bytes(1 << 20):
                    out.write(chunk)
                    digest.update(chunk)
        if digest.hexdigest() != expected:
            raise Refused(f"{name} of {tag} is not the archive its signed sums name")
        unpacked = folder / f"{tag}.part"
        shutil.rmtree(unpacked, ignore_errors=True)
        _unpack(archive, unpacked)
        if not (unpacked / exe).is_file():
            raise Refused(f"{name} of {tag} holds no {exe}")
    finally:
        archive.unlink(missing_ok=True)
    said = _version(unpacked / exe)
    if said != tag:
        shutil.rmtree(unpacked, ignore_errors=True)
        raise Refused(f"the build of {tag} says it is {said!r}")
    target = folder / tag
    shutil.rmtree(target, ignore_errors=True)
    os.replace(unpacked, target)
    previous = installed_tag()
    current = folder / "current.part"
    current.write_text(tag, encoding="ascii")
    os.replace(current, folder / "current")
    for old in folder.iterdir():
        if old.is_dir() and old.name not in (tag, previous):
            # A build still running on Windows cannot go yet: the next pass takes it
            shutil.rmtree(old, ignore_errors=True)


def _version(exe: Path) -> Optional[str]:
    """What the build says it is; None when it does not start."""
    try:
        out = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=120, env=child_env(),
                             check=True)
    except (OSError, subprocess.SubprocessError) as e:
        logger.error("the yt-dlp build at %s does not start: %s", exe, e)
        return None
    return out.stdout.strip()


def refresh() -> Optional[str]:
    """One pass: the latest stable release in place, fetched and checked when
    it is not there yet. The tag in place after the pass, None when there is
    none. A release refused, or a network that failed, leaves the build in
    place and says why in the log."""
    try:
        with _client() as client:
            latest = client.get(_LATEST, headers={"Accept": "application/vnd.github+json"})
            latest.raise_for_status()
            body = latest.json()
            tag = body.get("tag_name") if isinstance(body, dict) else None
            if not isinstance(tag, str) or not _TAG.fullmatch(tag):
                raise Refused(f"the latest release is named {tag!r}")
            current = installed_tag()
            if current is not None and _tag_key(tag) < _tag_key(current):
                raise Refused(f"the latest release, {tag}, is older than {current} in place")
            if tag != current or executable() is None:
                _install(client, tag)
    except Refused as e:
        logger.error("yt-dlp release refused, the build in place stays: %s", e)
    except (httpx.HTTPError, OSError, ValueError, zipfile.BadZipFile) as e:
        logger.warning("yt-dlp refresh skipped (%s); the build in place stays", e)
    exe = executable()
    if exe is None:
        logger.warning("yt-dlp is not in place")
        return None
    said = _version(exe)
    if said is None:
        return None
    logger.info("yt-dlp %s (stable, %s)", said, exe)
    return installed_tag()
