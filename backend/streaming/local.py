"""In-memory transcode of owned files HQPlayer can't decode to FLAC, served
through the same media proxy as phantom previews.

HQPlayer doesn't handle the MP4/m4a container (tested — both AAC and ALAC), so an
owned .m4a never plays as a file:// URI. Rather than rewrite the library on disk,
we read the file locally and ffmpeg it to a seekable FLAC in memory, then hand
HQPlayer the proxy's http URL (the proven phantom-stream path). ALAC → FLAC is
lossless; AAC → FLAC just rewraps the lossy source. The library files are
untouched; the transcode is re-done on each play (cheap — local disk, no network).
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile

from .base import FetchedAudio, ProviderError, ProviderManifest, StreamProvider, TrackQuery

logger = logging.getLogger(__name__)

# Owned formats HQPlayer can't decode → transcode on play. APE / WV / DSF / MP3
# play natively, so they stay file://.
TRANSCODE_FORMATS = {"M4A"}


def _source_is_lossless(path: str) -> bool:
    """True if the m4a holds ALAC (lossless), False for AAC — sets the served
    audio's provenance + the quality badge. The scanner's format-only is_lossless
    guess is wrong for m4a, so probe the real codec here."""
    try:
        import mutagen
        info = mutagen.File(path).info
        return (getattr(info, "codec", "") or "").lower().startswith("alac")
    except Exception:
        return False


class LocalTranscodeProvider(StreamProvider):
    """A media-proxy source backed by a local file: ``download(path)`` reads the
    file and transcodes it to FLAC. Not a phantom provider (no resolve / catalog)
    — the chain carries the container path as the source id directly."""

    manifest = ProviderManifest(
        id="local", name="Local transcode", kind="transcode", lossless=True,
    )

    def __init__(self, timeout: float = 180.0):
        self._timeout = timeout

    def fetch(self, query: TrackQuery) -> FetchedAudio:   # never used (path is the source id)
        raise ProviderError("local transcode needs a container path as source id")

    def _download(self, container_path: str) -> FetchedAudio:
        if not container_path or not os.path.isfile(container_path):
            raise ProviderError(f"local file not found: {container_path}")
        lossless = _source_is_lossless(container_path)
        # ffmpeg to a temp file (seekable → proper FLAC STREAMINFO with total
        # samples, which a stdout pipe can't give), then read into memory.
        fd, out = tempfile.mkstemp(prefix="sautium-xcode-", suffix=".flac")
        os.close(fd)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", container_path, "-c:a", "flac", out],
                check=True, timeout=self._timeout,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            with open(out, "rb") as f:
                data = f.read()
        except subprocess.CalledProcessError as e:
            tail = e.stderr.decode("utf-8", "replace")[-300:] if e.stderr else ""
            raise ProviderError(f"transcode failed for {container_path}: {tail}") from e
        except subprocess.TimeoutExpired as e:
            raise ProviderError(f"transcode timeout: {container_path}") from e
        finally:
            try:
                os.unlink(out)
            except OSError:
                pass
        logger.info("local transcode ok: %s (%d KiB FLAC, lossless=%s)",
                    os.path.basename(container_path), len(data) // 1024, lossless)
        return FetchedAudio(data=data, mime="audio/flac", lossless=lossless)
