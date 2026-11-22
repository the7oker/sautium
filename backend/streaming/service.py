"""Process-wide streaming-preview service: the media proxy + provider registry,
shared by the player router.

It follows the owner's switch — the streaming library
(``discovery.phantom_layer``): up while it is on, down while it is off, and
the switch's change is the event (``follow_switch``, called at the app's
start and by the switch's route; since 2026-11-19, PI-NODE-2B). A node whose
library is off has nothing to stream, so it starts no provider, no yt-dlp
build and no refresh loop; the media proxy alone still starts for DLNA and a
remote HQPlayer (``ensure_proxy``). One decision, one switch.

On by default — YouTube ships with the distribution (the node installs
yt-dlp's stable build itself, streaming/ytdlp_release.py), so a fresh node
streams phantoms once its first build is in place.
``streaming_preview_enabled=false`` keeps the subsystem down whatever the
switch says: a deployment's own off (a test stand), never a person's second
switch."""
from __future__ import annotations

import asyncio
import logging
import shutil
import threading
import time
from typing import Optional

import api_cooldown
from .base import ProviderRegistry, StreamProvider, on_aside_change
from .proxy import MediaProxy
from .youtube import YouTubeProvider

logger = logging.getLogger(__name__)

_proxy: Optional[MediaProxy] = None
_registry: Optional[ProviderRegistry] = None
_enricher = None
_lyrics_enricher = None
# One change of state at a time: the app's start and the switch's route
_switch_lock = threading.Lock()
# The app's loop, kept from its start: the refresh loop is its task, and the
# switch's route runs on a worker thread
_loop: Optional[asyncio.AbstractEventLoop] = None
_refresh_task: Optional[asyncio.Task] = None
# The proxy's hooks and the status observer are attached once a process; what
# they reach (the registry, the enrichers) comes and goes with the switch
_wired = False


def wanted(settings) -> bool:
    """Whether the subsystem should be up: the owner's streaming library is
    on, and the deployment did not turn streaming off."""
    if not getattr(settings, "streaming_preview_enabled", False):
        return False
    from routers.settings import _read as read_setting
    return bool(read_setting("discovery.phantom_layer"))


def demo_runtime() -> bool:
    """The image carries deno, the sandbox yt-dlp runs YouTube's player code
    in (Dockerfile.mac's STREAMING=0 leaves it out): without it the demo
    channel cannot work, and the node streams excerpts alone."""
    return shutil.which("deno") is not None


def follow_switch(settings, loop: Optional[asyncio.AbstractEventLoop] = None) -> bool:
    """Bring the subsystem to what the owner's switch says; True while it is
    up. The app's start passes its loop; the switch's route calls it after
    the write."""
    global _loop
    with _switch_lock:
        if loop is not None:
            _loop = loop
        up = wanted(settings)
        if up and _registry is None:
            _start(settings)
        elif not up and _registry is not None:
            _stop()
        elif not up:
            logger.info("streaming is off: %s",
                        "the streaming library is switched off"
                        if getattr(settings, "streaming_preview_enabled", False)
                        else "streaming_preview_enabled=false")
        return up


def _start(settings) -> None:
    """Register the providers, attach to the media proxy, start the yt-dlp
    refresh loop."""
    global _proxy, _registry, _wired
    registry = ProviderRegistry()
    # The provider runs the yt-dlp build the refresh loop puts in place (its
    # first pass is at start); until one lands it answers "unavailable", and
    # the excerpt provider below stands in.
    demo = demo_runtime()
    if demo:
        registry.register(YouTubeProvider(ffmpeg_location=settings.ffmpeg_location))
    else:
        logger.info("this install was set up without the demo channel's runtime (deno): "
                    "tracks stream as 30 s excerpts")
    # The excerpt provider ships in core and needs no tool: 30 s clips from the
    # public catalog API — the fallback when the demo channel is missing or a
    # track's one demo listen is spent (streaming/demo.py), and why a node
    # without yt-dlp still previews, as excerpts.
    from .deezer_preview import DeezerPreviewProvider
    registry.register(DeezerPreviewProvider())

    # Bring-your-own providers (e.g. a lossless provider) drop into the
    # local providers directory — NOT bundled here (DRM providers stay out of tree). Core has no
    # knowledge of them; a missing/broken plugin is skipped, never fatal.
    from pathlib import Path
    from .loader import load_external_providers
    providers_dir = (Path(settings.streaming_providers_dir)
                     if getattr(settings, "streaming_providers_dir", None)
                     else Path(__file__).parent / "providers")
    n = load_external_providers(registry, providers_dir)
    if n:
        logger.info("loaded %d external stream provider(s) from %s", n, providers_dir)
    _persist_registry(registry)

    # ONE proxy per process: the playback output may already have started
    # a provider-less one (the HQPlayer attach mirrors a restored queue
    # before this runs), and binding a second instance would fail after
    # the global had been repointed at it — leaving the port served by an
    # object nothing references.
    _proxy = ensure_proxy()
    _start_enrichers(settings)
    if not _wired:
        _wire(_proxy)
        _wired = True
    # From here the subsystem is up (is_enabled): the resolve finds providers
    _registry = registry
    if demo and _loop is not None:
        # yt-dlp's stable build, put in place at start and kept current for
        # the life of the subsystem (see the loop's own note on why
        # start-only refreshes are not enough).
        _loop.call_soon_threadsafe(_start_refresh_loop)
    logger.info("streaming preview ready (proxy %s:%d, advertised %s)",
                settings.media_proxy_host, settings.media_proxy_port,
                settings.media_proxy_advertised_host)


def _start_refresh_loop() -> None:
    global _refresh_task
    if _registry is not None and _refresh_task is None:
        _refresh_task = asyncio.get_running_loop().create_task(ytdlp_refresh_loop())


def _stop_refresh_loop() -> None:
    global _refresh_task
    if _refresh_task is not None:
        _refresh_task.cancel()
        _refresh_task = None


def _stop() -> None:
    """The streaming library was switched off: nothing resolves to a provider
    from now on, the refresh loop ends, the enrichers take no new job. The
    media proxy stays (DLNA, a remote HQPlayer), and so does a buffer already
    fetched for a queued slot."""
    global _registry
    _registry = None
    if _loop is not None:
        _loop.call_soon_threadsafe(_stop_refresh_loop)
    stop_enrichers()
    logger.info("streaming stopped: the streaming library was switched off")


def _start_enrichers(settings) -> None:
    """Tee fetched previews through CLAP/feature analysis (gated on known
    duration) and lyrics. The audio half needs the model stack: a node whose
    models are off or absent analyses no stream (it would fail in its first
    import, once per streamed track)."""
    global _enricher, _lyrics_enricher
    if not getattr(settings, "streaming_preview_analyze", False):
        return
    import hardware_profile
    from .enrichment import PreviewEnricher, PreviewLyricsEnricher
    _enricher = PreviewEnricher() if hardware_profile.resolve().ml_available else None
    _lyrics_enricher = PreviewLyricsEnricher()
    logger.info("preview enrichment enabled (%s on preview)",
                "analyze + lyrics" if _enricher is not None else "lyrics")


def _on_track_ready(e) -> None:
    """The proxy's hook, CLAP-agnostic: audio features (GPU, gated on a known
    catalog length; never on an excerpt) + lyrics text and its embedding
    (network/text, metadata-derived, ungated — see the enricher docstrings
    for the gating rationale)."""
    from .base import attested_lengths
    enricher, lyrics = _enricher, _lyrics_enricher
    if enricher is not None:
        enricher.submit(
            e.query.track_id,
            e.audio.data,
            attested_lengths(e.query),
            e.audio.lossless,   # ACTUAL fetch quality (may be a degraded tier)
            e.provider.manifest.id,  # the row's provenance (stream_providers)
            excerpt=e.audio.excerpt,
        )
    if lyrics is not None:
        lyrics.submit(e.query)


def _wire(proxy: MediaProxy) -> None:
    """The proxy's hooks and the demo policy's observer, attached once. The
    queue's own (what landed on which slot, what failed, the lane's next
    want) are the lane's, attached with the proxy (ensure_proxy)."""
    from playback.manager import manager as playback_manager
    from . import demo

    # The output hears of a landing with the slot already true (the lane's
    # hook runs first): a start it parked on the stream goes, an entry it
    # holds in another shape is handed again
    proxy.track_ready_hooks.append(lambda e: playback_manager.stream_landed(e.token, e.reshaped))
    # A fetch that ended with no audio: a start the output parked on it goes,
    # and meets the error the attempt then says
    proxy.track_failed_hooks.append(lambda e: playback_manager.stream_failed(e.token))
    # The demo policy: the fetch-time gate and the listen observer.
    proxy.link_admissible = demo.link_admissible
    playback_manager.subscribe_status(demo.status_observer)
    proxy.track_ready_hooks.append(_on_track_ready)
    # A link's refusal with a reason is remembered beside the resolve's
    # answers, so the next resolve does not offer that provider for the track
    from routers.player import note_refusal
    proxy.link_refused = note_refusal


def stop_enrichers() -> None:
    """The node stops, or the streaming library was switched off: a stream's
    analysis and lyrics take no new job, and the queued ones go with the
    process rather than hold its exit (their workers are joined there)."""
    global _enricher, _lyrics_enricher
    enrichers, _enricher, _lyrics_enricher = (_enricher, _lyrics_enricher), None, None
    for enricher in enrichers:
        if enricher is not None:
            enricher.stop()


# yt-dlp is perishable: YouTube changes its side every few weeks, and a node
# runs for weeks (restart: unless-stopped), so refreshing only at start is not
# enough. One loop here covers every runtime, as every runtime runs this
# backend: a pass at start, then daily, and sooner when a download fails the
# way an outdated build fails — never two passes within six hours while a
# build works or a release's install has failed, a few minutes apart while
# nothing is in place yet (a node that started offline); a request inside
# that gap is answered as it ends, and one made during a pass is kept. A pass
# installs yt-dlp's latest STABLE release, checked against the release key's
# signature (streaming/ytdlp_release.py). A platform yt-dlp builds nothing
# for runs no pass at all.
_REFRESH_INTERVAL_S = 24 * 3600
_REFRESH_MIN_GAP_S = 6 * 3600      # a burst of failures is still one pass
_FIRST_BUILD_GAP_S = 5 * 60        # no build yet: a node that started offline gets one soon
_refresh_wake: Optional[asyncio.Event] = None
_refresh_loop: Optional[asyncio.AbstractEventLoop] = None


def request_ytdlp_refresh() -> None:
    """Ask for an out-of-band refresh: a fetch failed the way a stale build
    fails (403 / signature), which is YouTube telling us our yt-dlp no longer
    speaks its protocol. Never blocks the caller — the failing track is already
    lost, and the point is that the NEXT one isn't. Called from the proxy's
    fetch thread, hence call_soon_threadsafe."""
    loop, wake = _refresh_loop, _refresh_wake
    if loop is None or wake is None:
        return
    loop.call_soon_threadsafe(wake.set)


async def ytdlp_refresh_loop() -> None:
    """Daily refresh, plus whatever request_ytdlp_refresh() asks for, no more
    often than _REFRESH_MIN_GAP_S apart (_FIRST_BUILD_GAP_S while no build is
    in place)."""
    from . import ytdlp_release
    global _refresh_wake, _refresh_loop
    if not ytdlp_release.supported():
        logger.warning("yt-dlp publishes no build for this platform: the demo channel stays off")
        return
    _refresh_wake = asyncio.Event()
    _refresh_loop = asyncio.get_running_loop()
    last = float("-inf")
    held = False            # a build works, or a release's install failed
    try:
        while True:
            early = last + (_REFRESH_MIN_GAP_S if held else _FIRST_BUILD_GAP_S) - time.monotonic()
            if early > 0:
                # The rate limit, not a wait for state: the request is
                # answered once the gap since the last pass is over
                await asyncio.sleep(early)
            _refresh_wake.clear()
            last = time.monotonic()
            try:
                held = (await asyncio.to_thread(ytdlp_release.refresh) is not None
                        or ytdlp_release.failed_tag() is not None)
            except Exception:
                logger.exception("yt-dlp refresh pass failed; the next one runs on schedule")
            try:
                await asyncio.wait_for(_refresh_wake.wait(), timeout=_REFRESH_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
    finally:
        _refresh_wake = _refresh_loop = None


def ensure_proxy() -> Optional[MediaProxy]:
    """Start the media proxy WITHOUT the provider registry when it isn't
    running yet — the DLNA output and a remote HQPlayer need the plain-http
    file server even on nodes where streaming is off. Phantom streaming
    follows the owner's switch; follow_switch() attaches the providers to
    this same instance."""
    global _proxy
    if _proxy is None:
        from config import settings
        import media_urls
        proxy = MediaProxy(
            port=settings.media_proxy_port,
            advertised_host=settings.media_proxy_advertised_host,
            file_token_key=media_urls.secret(),
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
        # The fetch lane's queue side: what the queue wants next, in its order
        from playback.manager import manager as playback_manager
        playback_manager.lane.attach(proxy)
        logger.info("media proxy started (providers attach when streaming "
                    "preview initializes)")
    return _proxy


def is_enabled() -> bool:
    """Streaming PREVIEW (providers) is up — distinct from the proxy alone,
    which ensure_proxy() may start provider-less for DLNA file serving."""
    return _proxy is not None and _registry is not None


def get_proxy() -> Optional[MediaProxy]:
    return _proxy


# Provider health as the notices channel sees it: a provider that answered
# every query of a resolve pass with "unavailable" (quota, outage, a gate)
# is silent from that pass until one where it answers. Written by the
# resolve waterfall (routers/player.py), read by routers/settings.
provider_health: dict = {}          # id -> {"silent_since": iso, "reason": str}


def _persist_registry(registry: ProviderRegistry) -> None:
    """Upsert every registered manifest into stream_providers — the persisted
    snapshot analysis_sources.provider_id references, so a row outlives its
    plugin with the provider's name and tier intact. Capabilities only: the
    actual quality of one fetch lives on the analysis_sources row."""
    from db_pool import db_execute
    manifests = [p.manifest for p in registry.enabled()]
    if not manifests:
        return
    params = []
    for m in manifests:
        params += [m.id, m.name, m.lossless, m.excerpt, m.demo_limited, m.version]
    db_execute(
        "INSERT INTO stream_providers (id, name, lossless, excerpt, demo_limited, version) "
        "VALUES " + ", ".join(["(%s, %s, %s, %s, %s, %s)"] * len(manifests)) + " "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, "
        "lossless = EXCLUDED.lossless, excerpt = EXCLUDED.excerpt, "
        "demo_limited = EXCLUDED.demo_limited, version = EXCLUDED.version, "
        "last_seen_at = now()",
        params)


def provider_name(key: str) -> str:
    """Display name behind a provider id or a health key (a shared upstream
    reports under its cooldown_source): the manifest name of the provider it
    names, else the key itself."""
    registry = _registry        # read once: the switch's stop clears it from another thread
    for p in (registry.enabled() if registry is not None else []):
        if p.manifest.id == key or health_key(p) == key:
            return p.manifest.name
    return key


def health_key(provider) -> str:
    """What a provider's silence is reported under: its upstream API when it
    shares one (both providers on one catalog are one voice — one notice,
    and either answering clears it), else its own id."""
    return provider.manifest.cooldown_source or provider.manifest.id


def note_provider_health(silent: dict, answered: set) -> bool:
    """`silent`: health key -> reason for providers that answered nothing
    this pass; `answered`: keys that gave at least one definite answer.
    Returns True on a transition either way — the caller wakes the channel."""
    from datetime import datetime, timezone
    changed = False
    for pid, reason in silent.items():
        if pid not in provider_health:
            provider_health[pid] = {
                "silent_since": datetime.now(timezone.utc).isoformat(),
                "reason": reason[:200]}
            changed = True
    for pid in answered:
        if provider_health.pop(pid, None) is not None:
            changed = True
    return changed


def _aside_changed(provider: StreamProvider) -> None:
    """A provider stepped aside, or came back (its own word,
    StreamProvider.aside_changed): the notices channel derives the set anew,
    and an open album page re-reads what can stream and from where. The
    ranking and the resolved chains need no wake — both read the provider's
    state when they run, and a chain is cached under the order it was
    resolved in."""
    aside = provider.stepped_aside()
    if aside is None:
        logger.info("stream provider %s is offered again", provider.manifest.id)
    else:
        # The cause's code only: its words are the module's, said in the
        # module's own log line and on the notice
        logger.warning("stream provider %s stepped aside (%s)", provider.manifest.id, aside.code)
    from db_pool import db_execute
    from .events import preview_events
    preview_events.ping()
    db_execute("NOTIFY sautium_notices")


on_aside_change(_aside_changed)


def providers_aside() -> list:
    """(provider, its SteppedAside) for every registered provider that has
    stepped aside — read from the providers as they stand, never kept here.
    Its notice is the provider's own voice, under its own id: a shared
    upstream's health key (`health_key`) is cleared by any provider on that
    upstream answering, and an account's state is not the catalog's."""
    registry = _registry
    if registry is None:
        return []
    return [(p, aside) for p in sorted(registry.enabled(), key=lambda p: p.manifest.id)
            if (aside := p.stepped_aside()) is not None]


def providers_preferred() -> list:
    """The providers on offer, best first: full-length before excerpts, and
    lossless before lossy within that. The per-track resolve waterfall tries
    each in order, so a track absent from the lossless provider still streams
    from YouTube instead of showing up as unavailable, and one the demo
    channel cannot serve still plays as a 30 s excerpt.

    A provider that has stepped aside by its own word (base.SteppedAside) is
    registered and not on offer: the resolve never asks it, and a chain built
    while it was offered is another cache key.

    A provider may share its API host with photo enrichment (and with another
    provider — `manifest.cooldown_source`), so a 429 there surfaces as an
    armed cooldown for that source. We react by ROUTING, never blocking:
    while a source is cooling, demote every provider on it to the end so
    playback starts immediately on what is left; if it's chronically banned
    (>=3 strikes), drop them this round entirely. Consumer-side policy over
    api_cooldown — enrichment pauses on cooling_down(), streaming reorders on
    status()."""
    registry = _registry
    if registry is None:
        return []
    provs = sorted((p for p in registry.enabled() if p.stepped_aside() is None),
                   key=lambda p: (p.manifest.excerpt, not p.manifest.lossless, p.manifest.id))
    # cooling_down() is the cheap cache gate; only read the richer status()
    # (a DB hit) on the rare occasions a source is actually cooling.
    cooling = {p.manifest.cooldown_source for p in provs
               if p.manifest.cooldown_source
               and api_cooldown.cooling_down(p.manifest.cooldown_source)}
    if cooling:
        banned = set()
        for source in cooling:
            st = api_cooldown.status(source)
            if st and st.strikes >= 3:
                banned.add(source)
        provs = ([p for p in provs if p.manifest.cooldown_source not in cooling]
                 + [p for p in provs if p.manifest.cooldown_source in cooling - banned])
    return provs


def get_provider(provider_id: Optional[str] = None) -> Optional[StreamProvider]:
    """A specific provider by id, or — with no id — the preferred one: a lossless
    source ranks above a lossy one (YouTube)."""
    registry = _registry
    if registry is None:
        return None
    if provider_id:
        return registry.get(provider_id)
    provs = providers_preferred()
    return provs[0] if provs else None


def preview_meta(token: str) -> Optional[dict]:
    """Provider metadata for a preview token, or None (no proxy / unknown)."""
    return _proxy.preview_meta(token) if _proxy else None
