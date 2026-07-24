"""On-the-fly Opus transcoding for the bandwidth-saving output tiers.

Remote listening (browser / DLNA over a tunnel) can trade lossless bytes for
Opus — HQPlayer and the local engine always stay lossless (they never pass a
quality param, so they never reach here). The quality choice is a GLOBAL
setting; it is snapshotted into each track's media URL (`?q=opus_192`) at
dispatch time so a mid-session change applies from the NEXT track while the
current one plays out — and so every Range request for one track references
the same fixed format (a format that flipped mid-download would produce
unstitchable chunks).

Transcode is to a cached Ogg/Opus FILE on disk, not a live stream: byte-range
seek then just works (a real file with a known length), at the cost of a few
seconds' encode on the first request for a track — which the browser's blob
prefetch warms ahead for everything but the very first track. The cache is
LRU-bounded; Opus tracks are small (~3-6 MB), so a modest budget holds a long
queue.
"""

import hashlib
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Quality tiers. `lossless` (or any unknown/absent value) means "don't
# transcode" — the raw file/buffer is served. libopus (not the native `opus`
# encoder) is the high-quality one; VBR is Opus's sweet spot.
OPUS_BITRATES = {"opus_192": 192, "opus_96": 96}
MIME = "audio/ogg"

_CACHE_DIR = Path(__file__).parent.parent / "data" / "transcode_cache"
_BUDGET_BYTES = 512 * 1024 * 1024        # ~512 MB of cached Opus
_index_lock = threading.Lock()
_key_locks: dict[str, threading.Lock] = {}


def wants_opus(quality: Optional[str]) -> bool:
    return quality in OPUS_BITRATES


def _key_lock(key: str) -> threading.Lock:
    with _index_lock:
        lk = _key_locks.get(key)
        if lk is None:
            lk = _key_locks[key] = threading.Lock()
        return lk


def _evict_if_needed() -> None:
    """LRU by access time — drop the least-recently-served Opus files until the
    cache is back under budget. Runs under _index_lock."""
    files = []
    total = 0
    for p in _CACHE_DIR.glob("*.ogg"):
        try:
            st = p.stat()
        except OSError:
            continue
        files.append((st.st_atime, st.st_size, p))
        total += st.st_size
    if total <= _BUDGET_BYTES:
        return
    for _atime, size, p in sorted(files):        # oldest access first
        try:
            p.unlink()
        except OSError:
            continue
        total -= size
        if total <= _BUDGET_BYTES:
            break


def _run_ffmpeg(args_in: list, bitrate: int, out_path: Path,
                stdin_bytes: Optional[bytes]) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           *args_in,
           "-vn", "-c:a", "libopus", "-b:a", f"{bitrate}k", "-vbr", "on",
           "-application", "audio", "-f", "ogg", str(out_path)]
    tmp = out_path.with_suffix(".ogg.part")
    cmd[-1] = str(tmp)
    try:
        subprocess.run(cmd, input=stdin_bytes, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        os.replace(tmp, out_path)               # atomic — no half file is ever served
    except subprocess.CalledProcessError as e:
        tmp.unlink(missing_ok=True)
        err = (e.stderr or b"").decode(errors="replace")[-300:]
        raise RuntimeError(f"opus transcode failed: {err}") from e
    finally:
        tmp.unlink(missing_ok=True)


def _seek_args(ss: float, tail: list) -> list:
    """`-ss N` BEFORE -i (input seeking) so ffmpeg starts decoding at the
    offset — the server-side seek for renderers that can't time-seek VBR
    Opus themselves. ss=0 is the whole track (the common case)."""
    return (["-ss", str(int(ss))] + tail) if ss and ss > 0 else tail


def opus_path_for_file(source_path: str, quality: str, ss: float = 0.0) -> Optional[str]:
    """Cached Opus of an owned file, optionally starting at `ss` seconds
    (server-side seek). Keyed by path + mtime + bitrate + offset. Returns
    None if the quality tier isn't Opus."""
    bitrate = OPUS_BITRATES.get(quality)
    if bitrate is None:
        return None
    try:
        mtime = int(os.path.getmtime(source_path))
    except OSError:
        return None
    key = hashlib.sha1(f"{source_path}|{mtime}|{bitrate}|{int(ss)}".encode()).hexdigest()
    return _produce(key, bitrate, args_in=_seek_args(ss, ["-i", source_path]),
                    stdin_bytes=None)


def opus_path_for_bytes(token: str, source_bytes: bytes, quality: str,
                        ss: float = 0.0) -> Optional[str]:
    """Cached Opus of an in-memory buffer (phantom stream), optionally from
    `ss` seconds. Keyed by the proxy token + bitrate + offset — a token maps
    to one fetched buffer for its life."""
    bitrate = OPUS_BITRATES.get(quality)
    if bitrate is None:
        return None
    key = hashlib.sha1(f"{token}|{bitrate}|{int(ss)}".encode()).hexdigest()
    return _produce(key, bitrate, args_in=_seek_args(ss, ["-i", "pipe:0"]),
                    stdin_bytes=source_bytes)


def _produce(key: str, bitrate: int, *, args_in: list,
             stdin_bytes: Optional[bytes]) -> str:
    out = _CACHE_DIR / f"{key}.ogg"
    if out.exists():
        os.utime(out, None)                     # bump access time for LRU
        return str(out)
    with _key_lock(key):                        # one encode per key; others wait
        if out.exists():
            os.utime(out, None)
            return str(out)
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        _run_ffmpeg(args_in, bitrate, out, stdin_bytes)
        logger.info("opus %dk transcode in %.1fs (%s)", bitrate,
                    time.monotonic() - started, out.name)
    with _index_lock:
        _evict_if_needed()
    return str(out)
