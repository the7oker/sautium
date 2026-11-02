"""What a launcher's first run downloads, and the one way it downloads it:
each archive at a versioned URL, checked against the SHA-256 this release
pins before anything is extracted from it or run. A file that does not
match is deleted and the step fails; nothing in it is used.

A version moves with its digest, in one commit. Where the publisher prints
its own sums they were checked against it at the pin — python.org's MD5,
PyPI, nodejs.org's SHASUMS256, GitHub's asset digests, gyan.dev's .sha256,
deno's .sha256sum — and the rest (EDB's PostgreSQL, fpcalc, flac) were taken
from the file as served then: from that day on a changed file is refused.
"""

import hashlib
import logging
import os
import urllib.request
from pathlib import Path
from typing import Callable, NamedTuple, Optional

logger = logging.getLogger(__name__)


class Artifact(NamedTuple):
    url: str
    sha256: str


_DENO = "https://github.com/denoland/deno/releases/download/v2.9.8/"

ARTIFACTS = {
    # The backend's interpreter on Windows (python_env), and the pip it installs into it
    "python": Artifact(
        "https://www.python.org/ftp/python/3.12.9/python-3.12.9-embed-amd64.zip",
        "615861fb801e8b04c847598db4e1e46e4b046295017caa37cb5486dde72b5865"),
    "pip": Artifact(
        "https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330"
        "e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl",
        "fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"),
    # Windows' portable PostgreSQL and its pgvector (db_init)
    "postgresql": Artifact(
        "https://get.enterprisedb.com/postgresql/postgresql-18.4-1-windows-x64-binaries.zip",
        "7effe34c0bf89027b3f171447d351cbc460f4566c8d0f643daec67f140787858"),
    "pgvector": Artifact(
        "https://github.com/andreiramani/pgvector_pgsql_windows/releases/download/0.8.3_18.4/"
        "vector.v0.8.3-pg18.zip",
        "861e2b4cfe2b041065c22047a875adcc1c9257f41b53bc84ce5f813a9154e6df"),
    # The media tools (db_init._MEDIA_TOOLS)
    "ffmpeg": Artifact(
        "https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/"
        "ffmpeg-9.0.2-essentials_build.zip",
        "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"),
    "fpcalc": Artifact(
        "https://github.com/acoustid/chromaprint/releases/download/v1.5.1/"
        "chromaprint-fpcalc-1.5.1-windows-x86_64.zip",
        "36b478e16aa69f757f376645db0d436073a42c0097b6bb2677109e7835b59bbc"),
    "flac": Artifact(
        "https://ftp.osuosl.org/pub/xiph/releases/flac/flac-1.4.3-win.zip",
        "c4558cf79fc1365d1822f7945a20bdbf55f99642cee95a823d44f3a61fb748c6"),
    "deno-windows": Artifact(
        _DENO + "deno-x86_64-pc-windows-msvc.zip",
        "cba84b26e0df5ae7f74fc66b86a95b866872a86292ebd84fa1e959927edd93bf"),
    "deno-macos-aarch64": Artifact(
        _DENO + "deno-aarch64-apple-darwin.zip",
        "9f0bf660393da2eaff044cdc7444cb08c73f235849b61d224d825a194ce7c98e"),
    "deno-macos-x86_64": Artifact(
        _DENO + "deno-x86_64-apple-darwin.zip",
        "4b2f5edd2639b7470f034238b321d7002d8dfb4726d8558f52c17afac370eb2c"),
    # Node for the AI agents' npm installs on Windows (db_init.install_node)
    "node": Artifact(
        "https://nodejs.org/dist/v22.23.2/node-v22.23.2-win-x64.zip",
        "1177b4137ba5adaa56354ae40f1080c7450e8ae09cecb47da459d1c52ac99f97"),
}

# Bytes between two progress reports, and the silence after which a stalled
# connection fails instead of holding the step for ever.
_CHUNK = 1 << 20
_TIMEOUT_S = 60


class Refused(RuntimeError):
    """A download that is not the file this release pins."""


def fetch(name: str, dest: Path,
          progress: Optional[Callable[[int, int], None]] = None) -> Path:
    """Artifact `name` at `dest`, checked: its bytes hashed as they arrive,
    the file renamed into place only when the digest is the pinned one.
    `progress(done, total)` hears the bytes so far (total 0 when the server
    did not say). Raises Refused on a mismatch, with nothing left at `dest`
    or beside it; a transport error raises as it is."""
    artifact = ARTIFACTS[name]
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(artifact.url, timeout=_TIMEOUT_S) as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            for chunk in iter(lambda: resp.read(_CHUNK), b""):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    got = digest.hexdigest()
    if got != artifact.sha256:
        part.unlink()
        raise Refused(f"{name}: the download from {artifact.url} is not the file this release "
                      f"pins (sha256 {got}, expected {artifact.sha256})")
    os.replace(part, dest)
    logger.info("%s downloaded and verified (%s)", name, artifact.url)
    return dest
