"""The core excerpt provider: 30 s preview clips from the public API (see
deezer_catalog.py) — the fallback when the demo channel is unavailable (no
yt-dlp, no listing, a failed download) or a track's one demo listen is spent.

Legitimate by construction: the clip is what the catalog publishes for exactly
this purpose, fetched from its CDN with no credentials and no decryption. What
it is not: a recording. ``FetchedAudio.excerpt`` says so, and everything
downstream reads it — no analysis, no listen, a [30s] badge, the clip's own
length in the queue.
"""
from __future__ import annotations

import io
import logging
import urllib.error
import urllib.request

from mutagen import MutagenError
from mutagen.mp3 import MP3

from . import deezer_catalog
from .base import FetchedAudio, ProviderError, ProviderManifest
from .deezer_catalog import DeezerCatalogProvider, catalog

logger = logging.getLogger(__name__)


class DeezerPreviewProvider(DeezerCatalogProvider):
    manifest = ProviderManifest(
        id="deezer_preview", name="Deezer", kind="direct_url", lossless=False,
        excerpt=True, cooldown_source=deezer_catalog.COOLDOWN_SOURCE,
    )

    def __init__(self, timeout: float = 20.0):
        self._timeout = timeout

    def _download(self, track_id: str) -> FetchedAudio:
        # The clip lives on the CDN, not the API: no pacer, no quota.
        url = catalog.preview_url(track_id)
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as r:
                data = r.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderError(f"deezer_preview: clip fetch failed for {track_id}: {e}") from e
        try:
            seconds = float(MP3(io.BytesIO(data)).info.length)
        except MutagenError as e:
            raise ProviderError(f"deezer_preview: clip for {track_id} is not MPEG audio") from e
        logger.info("deezer_preview fetch ok: %s (%d KiB, %.0fs excerpt)",
                    track_id, len(data) // 1024, seconds)
        return FetchedAudio(data=data, mime="audio/mpeg", lossless=False,
                            excerpt=True, seconds=seconds)
