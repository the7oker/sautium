"""Process-wide streaming-preview service: the media proxy + provider registry,
created once at app startup (``main.lifespan``) and shared by the player router.

Disabled by default — ``init`` is a no-op unless ``streaming_preview_enabled``,
so a backend that never previews phantoms pays nothing (no proxy thread, no
yt-dlp/ffmpeg requirement)."""
from __future__ import annotations

import logging
from typing import Optional

import api_cooldown
from .base import ProviderRegistry, StreamProvider
from .proxy import MediaProxy
from .youtube import YouTubeProvider

logger = logging.getLogger(__name__)

_proxy: Optional[MediaProxy] = None
_registry: Optional[ProviderRegistry] = None
_enricher = None
_lyrics_enricher = None


def init(settings) -> bool:
    """Start the media proxy and register the core (YouTube) provider. Returns
    True if the subsystem came up. Called from the app lifespan."""
    global _proxy, _registry
    if not getattr(settings, "streaming_preview_enabled", False):
        logger.info("streaming preview disabled (streaming_preview_enabled=false)")
        return False

    _registry = ProviderRegistry()
    _registry.register(YouTubeProvider(
        ytdlp_path=settings.ytdlp_path,
        ffmpeg_location=settings.ffmpeg_location,
    ))

    # Bring-your-own providers (e.g. a lossless Deezer bridge) drop into the
    # local providers directory — NOT bundled here (§1201). Core has no
    # knowledge of them; a missing/broken plugin is skipped, never fatal.
    from pathlib import Path
    from .loader import load_external_providers
    providers_dir = (Path(settings.streaming_providers_dir)
                     if getattr(settings, "streaming_providers_dir", None)
                     else Path(__file__).parent / "providers")
    n = load_external_providers(_registry, providers_dir)
    if n:
        logger.info("loaded %d external stream provider(s) from %s", n, providers_dir)

    _proxy = MediaProxy(
        port=settings.media_proxy_port,
        advertised_host=settings.media_proxy_advertised_host,
        bind_host=settings.media_proxy_host,
    )
    _proxy.start()

    # Tee fetched previews through CLAP/feature analysis (gated on known
    # duration). The proxy stays CLAP-agnostic — it just fires the hook.
    global _enricher, _lyrics_enricher
    if getattr(settings, "streaming_preview_analyze", False):
        from .enrichment import PreviewEnricher, PreviewLyricsEnricher
        _enricher = PreviewEnricher()
        _lyrics_enricher = PreviewLyricsEnricher()

        def _on_track_ready(e):
            # Audio features (GPU, gated on a known MB duration) + lyrics text and
            # its embedding (network/text, metadata-derived, ungated — see the
            # enricher docstrings for the gating rationale).
            _enricher.submit(
                e.query.track_id,
                e.audio.data if e.audio else None,
                e.query.duration,
                e.audio.lossless if e.audio else False,   # ACTUAL fetch quality (may be a degraded tier)
                e.provider.manifest.id if e.provider else None,  # provenance origin
            )
            _lyrics_enricher.submit(e.query)

        _proxy.on_track_ready = _on_track_ready
        logger.info("preview enrichment enabled (analyze + lyrics on preview)")

    logger.info("streaming preview ready (proxy %s:%d, advertised %s)",
                settings.media_proxy_host, settings.media_proxy_port,
                settings.media_proxy_advertised_host)
    return True


def is_enabled() -> bool:
    return _proxy is not None


def get_proxy() -> Optional[MediaProxy]:
    return _proxy


def providers_preferred() -> list:
    """All enabled providers, lossless-first (Deezer FLAC before YouTube lossy).
    The per-track resolve waterfall tries each in order, so a track absent from
    Deezer still streams from YouTube instead of showing up as unavailable.

    Deezer stream shares api.deezer.com with photo enrichment, so a 429 there
    (from a photo backfill or our own resolve) surfaces as an armed 'deezer'
    cooldown. We react by ROUTING, never blocking: while Deezer is cooling,
    demote it below the lossy fallback so playback starts immediately on
    YouTube; if it's chronically banned (>=3 strikes), drop it this round
    entirely. Consumer-side policy over api_cooldown — enrichment pauses on
    cooling_down(), streaming reorders on status()."""
    if _registry is None:
        return []
    provs = sorted(_registry.enabled(),
                   key=lambda p: (not p.manifest.lossless, p.manifest.id))
    # cooling_down() is the cheap cache gate; only read the richer status()
    # (a DB hit) on the rare occasions Deezer is actually cooling.
    if api_cooldown.cooling_down('deezer'):
        st = api_cooldown.status('deezer')
        deezer = [p for p in provs if p.manifest.id == 'deezer']
        others = [p for p in provs if p.manifest.id != 'deezer']
        provs = others if (st and st.strikes >= 3) else others + deezer
    return provs


def get_provider(provider_id: Optional[str] = None) -> Optional[StreamProvider]:
    """A specific provider by id, or — with no id — the preferred one: a lossless
    source (e.g. Deezer FLAC) ranks above a lossy one (YouTube)."""
    if _registry is None:
        return None
    if provider_id:
        return _registry.get(provider_id)
    provs = providers_preferred()
    return provs[0] if provs else None


def preview_meta(uri: str) -> Optional[dict]:
    """Provider metadata for a preview URI, or None (no proxy / not a preview)."""
    return _proxy.preview_meta(uri) if _proxy else None
