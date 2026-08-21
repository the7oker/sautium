"""Process-wide streaming-preview service: the media proxy + provider registry,
created once at app startup (``main.lifespan``) and shared by the player router.

On by default — YouTube ships with the distribution (yt-dlp is a backend
dependency on every runtime), so a fresh node streams phantoms out of the
box. ``streaming_preview_enabled=false`` is an explicit opt-out for nodes
that never preview."""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import subprocess
import sys
import time
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
    # yt-dlp is a dependency of the backend interpreter itself (requirements.txt,
    # kept on the nightly channel at every start), and the provider runs it as
    # `-m yt_dlp`. If the import is missing, skip the provider with a loud
    # warning — registering it anyway would just fail every resolve one track
    # at a time.
    if importlib.util.find_spec("yt_dlp"):
        _registry.register(YouTubeProvider(ffmpeg_location=settings.ffmpeg_location))
    else:
        logger.warning("yt-dlp is not installed in %s — YouTube streaming is "
                       "unavailable until it is and the backend restarted",
                       sys.executable)

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


# yt-dlp is perishable: YouTube changes its side every few weeks and upstream's
# stable channel lags behind the breakage, so every runtime keeps it on the
# NIGHTLY channel. Refreshing only at start is not enough — this node runs for
# weeks (restart: unless-stopped) and a nightly lands most days, which is
# exactly how a working node wakes up unable to stream. One loop here covers
# all three runtimes because all three run this backend; the launcher's own
# "check for updates" is start-only too, and docker-compose.mac.yml bypasses
# entrypoint.py entirely.
_REFRESH_INTERVAL_S = 24 * 3600
_REFRESH_MIN_GAP_S = 6 * 3600      # a burst of failures is still one pip run
_refresh_wake: Optional[asyncio.Event] = None
_refresh_loop: Optional[asyncio.AbstractEventLoop] = None


def _pip_refresh() -> None:
    """Move this interpreter's yt-dlp to the latest nightly. Blocking (pip) —
    called in a worker thread. Offline or rate-limited → whatever is installed
    stays; never raises."""
    cmd = [sys.executable, "-m", "pip", "install", "-q", "-U", "--pre",
           "--no-cache-dir", "--root-user-action=ignore",
           "--retries", "2", "--timeout", "15", "yt-dlp[default]"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp refresh timed out; keeping the installed build")
        return
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()
        logger.warning("yt-dlp refresh skipped: %s",
                       tail[-1] if tail else f"rc={r.returncode}")
    ver = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                         capture_output=True, text=True).stdout.strip()
    logger.info("yt-dlp %s", ver or "not installed")


def request_ytdlp_refresh() -> None:
    """Ask for an out-of-band refresh: a fetch failed the way a stale build
    fails (403 / signature), which is YouTube telling us our yt-dlp no longer
    speaks its protocol. Never blocks the caller — the failing track is already
    lost, and the point is that the NEXT one isn't. Called from the proxy's
    fetch thread, hence call_soon_threadsafe."""
    if _refresh_loop is None or _refresh_wake is None:
        return
    _refresh_loop.call_soon_threadsafe(_refresh_wake.set)


async def ytdlp_refresh_loop() -> None:
    """Daily refresh, plus whatever request_ytdlp_refresh() asks for, no more
    often than _REFRESH_MIN_GAP_S apart."""
    global _refresh_wake, _refresh_loop
    _refresh_wake = asyncio.Event()
    _refresh_loop = asyncio.get_running_loop()
    last = float("-inf")
    while True:
        if time.monotonic() - last >= _REFRESH_MIN_GAP_S:
            last = time.monotonic()
            await asyncio.to_thread(_pip_refresh)
        _refresh_wake.clear()
        try:
            await asyncio.wait_for(_refresh_wake.wait(), timeout=_REFRESH_INTERVAL_S)
        except asyncio.TimeoutError:
            pass


def ensure_proxy() -> Optional[MediaProxy]:
    """Start the media proxy WITHOUT the provider registry when it isn't
    running yet — DLNA output needs the plain-http file server even on nodes
    where streaming preview is disabled. Phantom streaming stays gated on
    streaming_preview_enabled."""
    global _proxy
    if _proxy is None:
        from config import settings
        proxy = MediaProxy(
            port=settings.media_proxy_port,
            advertised_host=settings.media_proxy_advertised_host,
            bind_host=settings.media_proxy_host,
        )
        try:
            proxy.start()
        except OSError as e:
            raise RuntimeError(
                f"media port {settings.media_proxy_port} is already in use by "
                "another process on this host — likely another Sautium node "
                "(Docker). Only one node per machine can serve DLNA/HQPlayer "
                "media") from e
        _proxy = proxy
        logger.info("media proxy started for local file serving (no providers)")
    return _proxy


def is_enabled() -> bool:
    """Streaming PREVIEW (providers) is up — distinct from the proxy alone,
    which ensure_proxy() may start provider-less for DLNA file serving."""
    return _proxy is not None and _registry is not None


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
