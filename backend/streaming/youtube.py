"""YouTube provider — ships in core (yt-dlp on non-DRM content is a ToS matter,
not §1201 anti-circumvention; it survived the 2020 RIAA takedown). DRM
providers (Deezer/Spotify) are NOT bundled — they live as external BYO modules.

HQPlayer doesn't decode AAC/m4a (tested), so we transcode YouTube's m4a/opus
source to FLAC via yt-dlp's built-in ffmpeg pass (``-x --audio-format flac``) —
lossless of the lossy source, and HQPlayer's native format. yt-dlp and ffmpeg
are external tools; their paths are configurable (user-installed)."""
from __future__ import annotations

import glob
import logging
import os
import subprocess
import tempfile

from .base import (FetchedAudio, ProviderError, ProviderManifest, StreamProvider,
                   TrackQuery)

logger = logging.getLogger(__name__)


class YouTubeProvider(StreamProvider):
    manifest = ProviderManifest(
        id="youtube", name="YouTube", kind="direct_url", lossless=False,
    )

    def __init__(self, ytdlp_path: str = "yt-dlp",
                 ffmpeg_location: str | None = None, timeout: float = 120.0):
        # ffmpeg_location: directory containing ffmpeg, or None to use PATH.
        self._ytdlp = ytdlp_path
        self._ffmpeg_location = ffmpeg_location
        self._timeout = timeout

    def fetch(self, query: TrackQuery) -> FetchedAudio:
        search = f"{query.artist} {query.title}".strip()
        # One yt-dlp invocation: search -> download bestaudio -> ffmpeg->FLAC.
        # Temp dir is a transient processing buffer, deleted immediately after
        # we read the bytes — no persisted rip (the in-memory / no-cache rule).
        with tempfile.TemporaryDirectory(prefix="sautium-yt-") as tmp:
            cmd = [
                self._ytdlp, f"ytsearch1:{search}",
                "-x", "--audio-format", "flac",
                "--no-playlist", "--no-warnings", "--quiet",
                "-o", os.path.join(tmp, "t.%(ext)s"),
            ]
            if self._ffmpeg_location:
                cmd += ["--ffmpeg-location", self._ffmpeg_location]
            try:
                subprocess.run(cmd, check=True, timeout=self._timeout,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except subprocess.TimeoutExpired as e:
                raise ProviderError(f"youtube timeout: {search!r}") from e
            except subprocess.CalledProcessError as e:
                tail = e.stderr.decode("utf-8", "replace")[-300:] if e.stderr else ""
                raise ProviderError(f"youtube fetch failed for {search!r}: {tail}") from e

            flacs = glob.glob(os.path.join(tmp, "*.flac"))
            if not flacs:
                raise ProviderError(f"youtube: no result for {search!r}")
            with open(flacs[0], "rb") as f:
                data = f.read()

        logger.info("youtube fetch ok: %r (%d KiB FLAC)", search, len(data) // 1024)
        return FetchedAudio(data=data, mime="audio/flac")
