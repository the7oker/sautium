"""
Sautium - FastAPI Application
Main entry point for the API server.
"""

import faulthandler
faulthandler.enable()   # native crashes (PortAudio/ASIO AVs) leave a traceback in the log

import asyncio
import logging
import logging.config
import threading
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional

import psycopg2
try:
    import torch
except ImportError:
    torch = None
from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path as _Path

from auth_hmac import HMACAuthMiddleware, ensure_secret
from auth_hmac import secret_path as _secret_path
from config import settings, get_settings, ui_build, LOGGING_CONFIG
from dht_service import DHTService, HAS_LIBTORRENT

_API_SECRET_PATH = _secret_path()
_INDEX_HTML_PATH = _Path(__file__).parent / "static" / "index.html"
_API_SECRET_CACHE: Optional[bytes] = None
_INDEX_HTML_CACHE: Optional[str] = None


def _get_api_secret() -> bytes:
    """Module-level cache for the HMAC secret bytes.

    The secret is immutable for the process lifetime, so reading the
    file on every GET / pointlessly re-exposes the request to
    transient WSL2 drvfs EIO. Cache once, reuse forever.
    """
    global _API_SECRET_CACHE
    if _API_SECRET_CACHE is None:
        _API_SECRET_CACHE = ensure_secret(_API_SECRET_PATH)
    return _API_SECRET_CACHE


def _get_index_html() -> str:
    """Module-level cache for the Web UI shell.

    `index.html` lives on drvfs in WSL2 deployments and re-reading it
    on every GET / sporadically surfaces EIO (Errno 5). The file is
    static for the process lifetime — bake it into memory at startup
    and reuse the string forever.
    """
    global _INDEX_HTML_CACHE
    if _INDEX_HTML_CACHE is None:
        _INDEX_HTML_CACHE = _INDEX_HTML_PATH.read_text(encoding="utf-8")
    return _INDEX_HTML_CACHE

# Configure logging
logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger(__name__)

# Global DHT service reference (set during lifespan)
_dht_service: DHTService | None = None
_dht_reannounce_task: asyncio.Task | None = None
_p2p_server_task: asyncio.Task | None = None
_identity_task: asyncio.Task | None = None
_identity_stop: threading.Event | None = None
_mailbox_task: asyncio.Task | None = None
_load_meter = None


async def _relay_cap_loop() -> None:
    """Adaptive relay cap (Phase D, Валерій's design): a full relay stops
    advertising Sautium-cap:relay so newcomers don't knock in vain; if it
    then sees NO other relay in the DHT it grows the cap instead of letting
    the network starve. Binary signal, one-sided reaction on the re-announce
    cadence — no oscillation. The Docker surface has no observed external
    IP to subtract itself from the lookup, so "others visible" is the crude
    but safe `more than one announcer` (we are one of them while we
    announce; miscounts only delay a cap step by one cycle)."""
    from dht_service import REANNOUNCE_INTERVAL
    from routers.peer_chat import adapt_relay_cap, relay_has_room
    from routers.settings import _read
    while True:
        await asyncio.sleep(REANNOUNCE_INTERVAL)
        if _dht_service is None:
            continue
        try:
            if not _read("p2p.relay_enabled"):
                _dht_service.withdraw_capability("relay")
                continue
            peers = await _dht_service.lookup_capability("relay")
            others = len({(ip, port) for ip, port in peers}) > 1
            adapt_relay_cap(others)
            if relay_has_room():
                await _dht_service.announce_capability("relay")
            else:
                _dht_service.withdraw_capability("relay")
        except Exception as e:
            logger.debug(f"relay cap loop: {e}")


def _default_gateway() -> Optional[str]:
    """The container's default gateway from /proc/net/route — the address
    every host-local connection arrives from inside a bridge-networked
    container, the trusted front's included."""
    import socket
    import struct
    try:
        rows = _Path("/proc/net/route").read_text().splitlines()[1:]
    except OSError:
        return None
    for row in rows:
        fields = row.split()
        if len(fields) >= 3 and fields[1] == "00000000":
            return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    return None


async def _serve_p2p(port: int) -> None:
    """Serve the peer surface (p2p_app) on its own port, HTTPS with the same
    self-signed cert as the Web UI. A second uvicorn in this process rather
    than a second container: it shares the DB pool and the DHT service, and
    the split that matters is which ROUTES face the port, not which process
    serves them.

    Behind a trusted front (P2P_TRUSTED_FRONT, scripts/master-front/) the
    front terminates the peer TLS on the host and forwards plain HTTP with
    X-Forwarded-For, so here it is plain HTTP and uvicorn's proxy-headers
    middleware rewrites scope["client"] from that header — but only on
    connections from the docker gateway, which is what every host-local
    connection looks like from inside. The real guarantee is the publish
    spec (P2P_SYNC_PUBLISH=127.0.0.1:<port>): nothing but the front can
    reach the upstream, so nothing else can set the header. The address
    feeds signals (contact log, registry, pricing, backstops, similarity),
    never auth — CLAUDE.md Security Posture rule #5.

    Nothing here may take the main server down with it. uvicorn answers a
    failed bind with sys.exit(), i.e. SystemExit — which is a BaseException
    and sails straight past `except Exception`. On a dev host where Docker
    already publishes this port, that killed the whole backend seconds after
    startup (WinError 10048, measured 2026-07-28). The peer surface is
    optional; the Web UI is not."""
    import os
    import socket

    import uvicorn
    from tls_gen import ensure_cert
    from p2p_app import app as peer_app

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("0.0.0.0", port))
    except OSError as e:
        logger.error(
            "P2P peer surface disabled — port %d is already in use (%s). "
            "Another Sautium node on this host? Set P2P_SYNC_PORT to a free "
            "port; the Web UI is unaffected.", port, e)
        return
    finally:
        probe.close()

    if settings.p2p_trusted_front:
        gateway = _default_gateway()
        if gateway is None:
            logger.warning("P2P peer surface on :%d behind a trusted front, but no default "
                           "gateway in /proc/net/route — X-Forwarded-For will be ignored", port)
        else:
            logger.info("P2P peer surface on :%d behind a trusted front — plain HTTP, "
                        "X-Forwarded-For trusted from %s", port, gateway)
        config = uvicorn.Config(
            peer_app, host="0.0.0.0", port=port, log_level="info",
            proxy_headers=True, forwarded_allow_ips=gateway,
        )
    else:
        from p2p_identity import tls_binding
        cert_path, key_path = ensure_cert(
            _Path(os.getenv("SAUTIUM_TLS_DIR", "/app/data/tls")),
            [s.strip() for s in os.getenv("SAUTIUM_HOST_IPS", "").split(",") if s.strip()],
            binding=tls_binding(settings),
        )
        config = uvicorn.Config(
            peer_app, host="0.0.0.0", port=port, log_level="info",
            ssl_keyfile=str(key_path), ssl_certfile=str(cert_path),
        )
    try:
        await uvicorn.Server(config).serve()
    except asyncio.CancelledError:
        raise
    except SystemExit as e:
        logger.error("P2P peer surface on :%d failed to start (exit %s) — "
                     "continuing without it", port, e.code)
    except Exception as e:
        logger.error(f"P2P sync server on :{port} stopped: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup and shutdown events."""
    # Startup
    logger.info(f"Starting {settings.app_name} v{settings.app_version}")

    # Materialize the API secret on disk so launcher / sync_server can
    # read the same value, and warm the module-level cache. Reading at
    # startup surfaces filesystem errors here rather than on first
    # 401, and populating the cache means GET / never re-reads drvfs.
    _get_api_secret()
    _get_index_html()

    # Validate configuration
    missing_settings = settings.validate_required_settings()
    if missing_settings:
        logger.warning(
            f"Missing required settings: {', '.join(missing_settings)}"
        )

    # Check GPU availability
    if torch and torch.cuda.is_available():
        gpu_name = torch.cuda.get_device_name(0)
        gpu_memory = torch.cuda.get_device_properties(0).total_memory / 1e9
        logger.info(f"GPU available: {gpu_name} ({gpu_memory:.1f} GB)")
    elif torch:
        logger.warning("No GPU detected. Audio embedding will be slow.")
    else:
        logger.warning("PyTorch not installed. Audio embedding features unavailable.")

    # Test database connection
    try:
        test_db_connection()
        logger.info("Database connection successful")
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
    else:
        # Bring the schema and the data to this code's version before
        # anything writes: pending desktop/migrations/NNN_*.sql deltas and
        # the Python data migrations (identity rule). One place for every
        # node — Docker or launcher-run — see backend/db_migrate.py.
        import db_migrate
        db_migrate.apply_pending()

    # Resolve the hardware profile (full/standard/lite — HARDWARE-TIERS.md).
    # Drives the pre-warm set below plus pool sizes, phantom minting and
    # stream-enrichment mode at their call sites.
    import hardware_profile
    _profile = hardware_profile.resolve()
    if _profile.torch_cpu_threads and torch:
        # Without a cap torch claims every core for intra-op parallelism and
        # starves the decode pool + event loop on small CPU-only machines.
        torch.set_num_threads(_profile.torch_cpu_threads)
        logger.info(f"torch CPU threads capped at {_profile.torch_cpu_threads}")

    # Overlay Last.fm credentials from user_settings (Pydantic Settings
    # only reads .env at startup; the OAuth callback persists to DB).
    _load_lastfm_from_db()

    # Warm the external-API rate-limit cooldown cache from its table so a
    # restart mid-cooldown doesn't resume hammering a still-banning source.
    try:
        import api_cooldown
        api_cooldown.load_from_db()
    except Exception as e:
        logger.warning(f"Failed to load API cooldowns at startup: {e}")

    # Same idea for AI provider API keys — Web UI writes them to DB,
    # but providers/__init__.py reads `settings.anthropic_api_key`
    # etc. Without this overlay AnthropicProvider would not register
    # until the user re-saved the key after a backend restart.
    try:
        from routers.settings import load_ai_credentials_from_db
        load_ai_credentials_from_db()
    except Exception as e:
        logger.warning(f"Failed to overlay AI credentials at startup: {e}")

    # HQPlayer host/port editable from the Web UI; same env→DB overlay
    # so the change survives a restart and applies on next request.
    try:
        from routers.settings import load_hqplayer_from_db
        load_hqplayer_from_db()
    except Exception as e:
        logger.warning(f"Failed to overlay HQPlayer settings at startup: {e}")

    # Activate the persisted playback output (HQPlayer legacy default —
    # HARDWARE-TIERS §2.6: no configured output, no status loop).
    from routers.player import stop_status_poller
    from playback.manager import manager as playback_manager
    playback_manager.init_from_settings()

    # Start streaming-preview service (media proxy + provider registry).
    # No-op unless streaming_preview_enabled — costs nothing when off.
    try:
        from streaming import service as streaming_service
        if streaming_service.init(settings):
            # yt-dlp lives on the nightly channel; this keeps it there for the
            # life of the process (see the loop's own note on why start-only
            # refreshes are not enough).
            asyncio.create_task(streaming_service.ytdlp_refresh_loop())
    except Exception as e:
        logger.warning(f"Streaming preview init failed: {e}")

    # Start chat SSE listener
    from routers.p2p import start_chat_listener, stop_chat_listener
    start_chat_listener()

    # Start sync SSE listener (bridges launcher NOTIFY → library SSE)
    from routers.settings import start_sync_listener, stop_sync_listener
    start_sync_listener()

    # Start gear research worker (drains queued gear_models, wakes on NOTIFY)
    from gear_research_worker import start_gear_research_worker, stop_gear_research_worker
    start_gear_research_worker()

    # Bridge worker state NOTIFYs to the research-state SSE for UI chips
    from routers.gear_models import start_gear_state_listener, stop_gear_state_listener
    start_gear_state_listener()
    from routers.discovery import (start_mb_sources_listener,
                                   stop_mb_sources_listener)
    start_mb_sources_listener()

    # Derive P2P identity (single resolution point, cached in p2p_identity —
    # the same cache later serves peer-chat, receipts and /health).
    _p2p_identity = None
    if settings.p2p_enabled:
        try:
            from p2p_identity import resolve_identity
            _p2p_identity = await asyncio.to_thread(resolve_identity, settings)
        except Exception as e:
            logger.warning(f"P2P identity derivation failed: {e}")

    # Identity registry schema (idempotent) — the peer surface's identity gate
    # writes p2p_identities / p2p_node_bans; a node updated in place gains
    # the table without a manual migration.
    try:
        from desktop.p2p import identity_registry
        from db_pool import get_conn as _get_conn

        def _registry_schema():
            from desktop.p2p import contact_log, gate_pool
            with _get_conn() as conn:
                identity_registry.ensure_schema(conn)
                contact_log.ensure_schema(conn)
                gate_pool.ensure_schema(conn)
        await asyncio.to_thread(_registry_schema)
    except Exception as e:
        logger.warning(f"identity registry schema init failed: {e}")

    # Load meter: this process tree's CPU against the profile ceiling +
    # playback as a priority signal — headroom/dormancy for everything
    # discretionary (miner hold, DHT pacing, later the gate). Published to
    # user_settings['p2p.load'] on band changes for the Web UI.
    global _load_meter
    try:
        from desktop.p2p import load_meter as _lm
        import hardware_profile as _hp

        def _playing() -> bool:
            from playback.manager import manager as _pm
            return _pm.latest_status.get("state") in ("playing", "loading")

        _load_meter = _lm.install(_lm.LoadMeter(_hp.resolve().name, playback_probe=_playing))

        def _publish_load(snap: dict) -> None:
            try:
                from routers.settings import _write
                from db_pool import db_execute as _dbx
                _write("p2p.load", snap)
                _dbx("NOTIFY sautium_identity")
            except Exception as e:
                logger.debug(f"load publish failed: {e}")
        _load_meter.subscribe(_publish_load)
        _load_meter.start()
        # The gate price unit w (one 64 MiB task) — measured on this machine
        # once, off the hot path, before the peer surface prices anything.
        try:
            from p2p_app import _price as _gate_price, _gate
            await asyncio.to_thread(_gate_price().calibrate_w)
            logger.info(f"gate price unit w = {_gate_price().w_ms:.1f} ms")
            await asyncio.to_thread(_gate)   # eager: the idle gold seeder rides the load meter from now on
        except Exception as e:
            logger.warning(f"gate price calibration skipped: {e}")
    except Exception as e:
        logger.warning(f"load meter unavailable: {e}")

    # Identity certificate + proof: cache/fetch the certificate, mine the
    # proof for a pow identity in the background (routers/p2p.identity_proof_task).
    # ONE miner per deployment: in desktop mode (node_info.json present — the
    # launcher process owns the identity, its own worker mines and publishes
    # p2p.identity) this backend must not start a second one. Two miners over
    # the same identity dir each mined their own proof and raced for the file
    # (Mac stand, 2026-08-19: the registry held one nonce, the disk another).
    global _identity_task, _identity_stop, _mailbox_task
    _identity_stop = threading.Event()
    if _p2p_identity:
        from pathlib import Path as _Path
        launcher_owned = bool(settings.p2p_identity_dir) and (
            _Path(settings.p2p_identity_dir) / "node_info.json").exists()
        if launcher_owned:
            logger.info("identity proof: launcher owns the miner — backend task skipped")
        else:
            from routers.p2p import identity_proof_task
            _identity_task = asyncio.create_task(identity_proof_task(_identity_stop))

    # The master's Worker mailbox (Ф16): messages parked while this node was
    # offline are drained on the wake socket — only the shipped master has one.
    try:
        from master_node import MASTER_PUBKEY_HEX
        if _p2p_identity and _p2p_identity.get("public_key_hex") == MASTER_PUBKEY_HEX:
            from desktop.p2p import mailbox_client
            from p2p_identity import load_signing_key
            from routers.peer_chat import mailbox_import
            _mailbox = mailbox_client.MasterMailbox(
                MASTER_PUBKEY_HEX, load_signing_key(settings).sign, mailbox_import,
                peer_port=settings.p2p_announce_port or settings.p2p_sync_port)
            _mailbox_task = asyncio.create_task(_mailbox.run(lambda: not _identity_stop.is_set()))
            logger.info("master mailbox: wake socket task started")
            # The support desk (routers/support, routers/peer_diag): parked
            # warrants go down a node's wake stream the moment it subscribes;
            # old reports and bundles are swept once per start.
            from routers import peer_chat, peer_diag, support
            peer_chat.set_subscribe_hook(peer_diag.on_wake_subscribed)
            await asyncio.to_thread(support.sweep_retention)
            logger.info("support desk: warrant dispatch armed")
    except Exception as e:
        logger.warning(f"master mailbox init failed: {e}")

    # The peer surface: sync protocol only, on its own port (p2p_app.py).
    # Everything a peer is told about this node points here — never at the
    # Web UI port, whose page carries the API secret.
    global _p2p_server_task
    announce_port = settings.p2p_announce_port or settings.p2p_sync_port
    if settings.p2p_sync_port:
        _p2p_server_task = asyncio.create_task(
            _serve_p2p(settings.p2p_sync_port))

    # Start DHT service for P2P peer discovery
    global _dht_service, _dht_reannounce_task
    if settings.p2p_enabled and HAS_LIBTORRENT and settings.p2p_sync_port:
        try:
            _dht_service = DHTService(
                listen_port=settings.p2p_dht_port,
                http_port=announce_port,
            )
            await _dht_service.start()

            if _load_meter is not None:
                _dht_service.set_pace_provider(_load_meter.announce_pace)

            # The discovery key — how peers find this node at all.
            await _dht_service.announce_node()

            # Announce user identity in DHT
            if _p2p_identity:
                await _dht_service.announce_user(
                    _p2p_identity["invite_code"]
                )

            # Advertise the MB dump so dump-less nodes can find this one as a
            # slice source without a LAN beacon or a hand-written peer entry.
            try:
                from routers.sync import mb_dump_version
                if mb_dump_version():
                    await _dht_service.announce_capability("mbdump")
            except Exception as e:
                logger.warning(f"MB dump capability announce failed: {e}")

            # Relay role (Phase D). A Docker peer surface is reachable by
            # deployment definition (its port was forwarded by hand), so the
            # only gate is the setting. The announce is what CGNAT clients
            # discover relays by; the wake/forward/ack contract is already
            # served on the peer port either way.
            try:
                from routers.peer_chat import set_client_announce_cbs
                set_client_announce_cbs(
                    lambda code: asyncio.get_running_loop().create_task(
                        _dht_service.announce_user_for(code)),
                    _dht_service.withdraw_user_for)
                from routers.settings import _read
                if _read("p2p.relay_enabled"):
                    await _dht_service.announce_capability("relay")
                    asyncio.create_task(_relay_cap_loop())
            except Exception as e:
                logger.warning(f"relay role init failed: {e}")

            # Rare-artist tail — registration only; the drip loop in
            # dht_service announces it at its own spacing.
            artist_uuids = await asyncio.to_thread(_get_announce_tail_uuids)
            if artist_uuids:
                asyncio.create_task(_dht_service.announce_artists(artist_uuids))
                logger.info(
                    f"P2P online: node announced, {len(artist_uuids)} rare "
                    f"artists queued (peer port {announce_port})"
                )
            else:
                logger.info("P2P online: node announced (no rare-artist tail)")

            # Periodic re-announce
            _dht_reannounce_task = asyncio.create_task(
                _dht_service.periodic_reannounce()
            )
        except Exception as e:
            logger.error(f"DHT startup failed: {e}")
            _dht_service = None
    elif settings.p2p_enabled and not settings.p2p_sync_port:
        # P2P_SYNC_PORT=0 is how the launcher says "my own sync server owns
        # the peer surface and the DHT" — announcing a port we do not serve
        # would publish a dead address. Not a fault, and it must be tested
        # BEFORE the libtorrent check: a launcher-mode backend has no
        # libtorrent by design, and the old order blamed that first, sending
        # readers hunting a phantom install bug (support bundle, 2026-08-26).
        logger.info("Backend peer surface off (P2P_SYNC_PORT=0) — DHT owned "
                    "by the launcher's sync server")
    elif settings.p2p_enabled:
        logger.warning("P2P enabled but libtorrent not installed — DHT disabled")

    # Pre-warm search models so Discovery's first query doesn't hit a
    # ~30-60s cold-load. Fired as background tasks; uvicorn reports
    # "startup complete" immediately and model_cache.get_model is
    # single-flight so a request landing mid-warmup queues on the same
    # load, not a duplicate one. Each model is independent — Discovery
    # endpoints check `model_cache.is_loaded` per block and serve
    # `status: "loading"` for any block whose model is still warming.
    async def _prewarm(label: str, key: str, factory):
        try:
            import model_cache
            await asyncio.to_thread(model_cache.get_model, key, factory)
            logger.info(f"{label} pre-warm complete")
        except Exception as e:
            logger.warning(f"{label} pre-warm failed: {e}")

    _prewarm_labels = {"clap": "CLAP", "enrichment": "BGE-M3",
                       "lyrics": "Lyrics-BGE", "translate": "MADLAD"}

    # Sequential, search-critical first: loads serialize anyway on
    # model_cache._factory_lock (HF loading is not thread-safe across
    # concurrent from_pretrained calls), and chaining pins the order so
    # MADLAD's one-time 12GB download can never delay CLAP/BGE.
    #
    # The set comes from the hardware profile: full warms everything,
    # standard drops MADLAD (it lazy-loads on the first Sound-scope
    # Discovery query via kick_load), lite warms nothing — every model
    # cold-loads on first use and Discovery serves its non-ML blocks
    # meanwhile. An empty set also covers the torch-less install, whose
    # model modules do not import at all.
    async def _prewarm_all():
        if not _profile.prewarm_keys:
            return
        # Eagerly import model modules in the main thread before pre-warm
        # tasks spawn worker threads. The transformers package uses
        # _LazyModule whose __getattr__ is not thread-safe — concurrent
        # first-time imports from sibling pre-warm threads (CLAP +
        # sentence_transformers + lyrics) race and one of them surfaces as
        # `cannot import name 'ClapProcessor'`. Loading the modules here
        # serialises their `from transformers import …` statements through
        # the main-thread import lock.
        import embeddings, enrichment_embeddings, lyrics_embeddings, translation  # noqa: F401
        from routers.discovery import (_clap_loader, _enrichment_loader,
                                       _lyrics_loader, _translate_loader)
        factories = {"clap": _clap_loader, "enrichment": _enrichment_loader,
                     "lyrics": _lyrics_loader, "translate": _translate_loader}
        for key in _profile.prewarm_keys:
            label, factory = _prewarm_labels[key], factories[key]
            await _prewarm(label, key, factory)
        # Model loads stage tensors through the caching allocator (HF
        # from_pretrained staging, dtype conversions); the freed blocks
        # otherwise sit reserved for the process lifetime and read as
        # startup VRAM growth in nvidia-smi. Weights stay resident. The
        # log line reports the BACKEND'S OWN footprint — the whole-GPU
        # nvidia-smi number also contains host apps (HQPlayer's CUDA DSP)
        # and WSL can't attribute per-process, so this is the only ground
        # truth for "what does Sautium itself hold".
        from device import empty_cache
        empty_cache()
        if torch is not None and torch.cuda.is_available():
            logger.info(
                "Pre-warm chain complete — backend VRAM: %.2f GB "
                "allocated, %.2f GB reserved",
                torch.cuda.memory_allocated() / 1e9,
                torch.cuda.memory_reserved() / 1e9,
            )
        else:
            logger.info("Pre-warm chain complete")

    asyncio.create_task(_prewarm_all())

    # Background (network-only) enrichment — gated by the
    # `enrichment.background_enabled` user_settings flag. The toggle in
    # More → Sync & P2P also calls start()/stop() so the flag and the
    # thread state stay in lockstep.
    try:
        import background_enrichment
        from routers.settings import _read as _read_setting
        if bool(_read_setting("enrichment.background_enabled")):
            background_enrichment.start()
    except Exception as e:
        logger.warning(f"Background enrichment autostart failed: {e}")

    # MusicBrainz dump auto-update (opt-in toggle in More → Library).
    try:
        from routers.settings import maybe_auto_update
        maybe_auto_update()
    except Exception as e:
        logger.warning(f"MusicBrainz auto-update check failed: {e}")

    yield

    # Shutdown
    if _load_meter is not None:
        _load_meter.stop()
    if _identity_stop:
        _identity_stop.set()
    if _identity_task:
        _identity_task.cancel()
        try:
            await _identity_task
        except asyncio.CancelledError:
            pass
    if _mailbox_task:
        _mailbox_task.cancel()
        try:
            await _mailbox_task
        except asyncio.CancelledError:
            pass
    if _p2p_server_task:
        _p2p_server_task.cancel()
        try:
            await _p2p_server_task
        except asyncio.CancelledError:
            pass
    if _dht_reannounce_task:
        _dht_reannounce_task.cancel()
        try:
            await _dht_reannounce_task
        except asyncio.CancelledError:
            pass
    if _dht_service:
        await _dht_service.stop()
        _dht_service = None

    stop_status_poller()
    stop_chat_listener()
    stop_sync_listener()
    stop_gear_research_worker()
    stop_gear_state_listener()
    stop_mb_sources_listener()

    try:
        import background_enrichment
        background_enrichment.stop()
    except Exception:
        pass

    # Cleanup resources
    import model_cache
    model_cache.shutdown()

    from db_pool import close_pool
    close_pool()

    logger.info("Shutting down application")


# Initialize FastAPI app
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="AI-powered music library management and recommendation system",
    lifespan=lifespan,
)

# HMAC signature check on every privileged request. Whitelist for
# /health, /, /static/*, /api/sync/*, /sync/*, /api/p2p/chat/wake
# is hardcoded inside the middleware. See backend/auth_hmac.py.
app.add_middleware(HMACAuthMiddleware, secret_path=_API_SECRET_PATH)

# Enable gzip compression for sync API responses
app.add_middleware(GZipMiddleware, minimum_size=1000)


def test_db_connection() -> bool:
    """Test PostgreSQL connection."""
    conn = psycopg2.connect(settings.database_url)
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT version();")
            version = cursor.fetchone()
        logger.debug(f"PostgreSQL version: {version[0]}")
        return True
    finally:
        conn.close()


def _get_announce_tail_uuids() -> list[str]:
    """The rare-artist tail to announce by exact key (dht_service docstring).
    Query and reasoning live in sync_queries.get_announce_tail_uuids — the
    two surfaces must announce by the same rule or a carrier's holdings are
    findable from one and invisible from the other."""
    from routers.settings import _read
    from routers.sync import carry_queries
    limit = _read("sync.announce_limit")
    limit = int(limit) if limit else 0
    if limit <= 0:
        return []
    if carry_queries is None:
        logger.error("desktop/ is not mounted — announcing the node key only")
        return []
    conn = psycopg2.connect(settings.database_url)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(carry_queries.ANNOUNCE_TAIL_SQL, (limit,))
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


@app.get("/")
async def root() -> HTMLResponse:
    """Serve the Web UI shell. Carries NO key.

    This page used to inline the HMAC secret, on the reasoning that
    cross-origin scripts cannot read a response body. True — but it made the
    key readable by every device that could load the page, permanent,
    unrevocable and shared, and DNS rebinding defeats the same-origin
    argument by making the attacker's page *be* the origin. The browser now
    signs with a device token it obtains through /api/auth (see
    backend/device_auth.py) and keeps in localStorage, which a rebinding
    origin cannot reach.
    """
    html = _get_index_html()
    inject = f'<script>window.__SAUTIUM_BUILD={ui_build()};</script>'
    if "</head>" in html:
        html = html.replace("</head>", f"  {inject}\n</head>", 1)
    else:
        html = inject + html
    return HTMLResponse(
        content=html, headers={"Cache-Control": "no-store"}
    )


@app.get("/health")
async def health_check() -> Dict[str, Any]:
    """Detailed health check including database, GPU, and P2P status.

    Deliberately NOT `type: "sautium-peer"` — that marker is how probes
    recognise a peer surface, and this port serves the Web UI, whose page
    carries the API secret. Claiming it here is how the Web UI port ended
    up in peer candidate lists after the peer-protocol moved to its own
    port (p2p_app.py). The peer surface is the only thing that may claim
    it."""
    health_status = {
        "status": "healthy",
        "type": "sautium-webui",
        "checks": {}
    }

    # Database check
    try:
        test_db_connection()
        health_status["checks"]["database"] = "ok"
    except Exception as e:
        health_status["checks"]["database"] = f"error: {str(e)}"
        health_status["status"] = "degraded"

    # GPU check
    if torch and torch.cuda.is_available():
        health_status["checks"]["gpu"] = {
            "available": True,
            "name": torch.cuda.get_device_name(0),
            "memory_gb": round(
                torch.cuda.get_device_properties(0).total_memory / 1e9, 1
            )
        }
    else:
        health_status["checks"]["gpu"] = {
            "available": False,
            "torch_installed": torch is not None,
        }

    # Music library check
    health_status["checks"]["music_library"] = {
        "path": settings.music_library_path,
        "exists": settings.music_library_exists
    }

    # P2P / DHT status
    if _dht_service:
        health_status["checks"]["dht"] = {"owner": "backend", **_dht_service.get_dht_stats()}
    else:
        # Launcher mode (P2P_SYNC_PORT=0): the DHT lives in the launcher's
        # own process — this backend never runs one, so reporting its own
        # libtorrent here described a DHT nobody uses (support bundle,
        # 2026-08-26). The launcher's sync server /health is the real one.
        launcher_owned = bool(settings.p2p_enabled and not settings.p2p_sync_port)
        health_status["checks"]["dht"] = {
            "owner": "launcher" if launcher_owned else "backend",
            "available": None if launcher_owned else HAS_LIBTORRENT,
            "running": None if launcher_owned else False,
            "enabled": settings.p2p_enabled,
        }

    # MB dump capability — dump-less peers pick slice sources by this field
    # (same contract as the launcher sync server's /health). node_id = the
    # backend's Ed25519 pubkey, matching the launcher convention.
    from routers.sync import SYNC_CAPABILITIES, mb_dump_version, node_pubkey_hex
    health_status["mb_dump"] = mb_dump_version()
    health_status["node_id"] = node_pubkey_hex()
    # Sync-protocol capabilities — peers pick pull categories by this list
    # (e.g. `segments` bundles vs the legacy mean-vector pull).
    health_status["capabilities"] = SYNC_CAPABILITIES

    return health_status


@app.post("/dht/reannounce")
async def dht_reannounce() -> Dict[str, Any]:
    """Re-announce the node key and re-query the rare-artist tail. Paced —
    the tail sweep runs in the background, the response returns the queued
    count immediately."""
    if not _dht_service:
        return {"success": False, "message": "DHT not running"}

    await _dht_service.announce_node()
    artist_uuids = await asyncio.to_thread(_get_announce_tail_uuids)
    new_count = len(set(artist_uuids) - _dht_service._announced)
    asyncio.create_task(_dht_service.announce_artists(artist_uuids))
    return {
        "success": True,
        "total_announced": _dht_service.announced_count,
        "queued": new_count,
    }


@app.get("/config")
async def get_config() -> Dict[str, Any]:
    """Get current configuration (excluding sensitive data)."""
    return {
        "app_name": settings.app_name,
        "app_version": settings.app_version,
        "music_library_path": settings.music_library_path,
        "music_library_exists": settings.music_library_exists,
        "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.embedding_dimension,
        "audio_sample_duration": settings.audio_sample_duration,
        "embedding_batch_size": settings.embedding_batch_size,
        "default_search_limit": settings.default_search_limit,
        "min_similarity_threshold": settings.min_similarity_threshold,
        "hqplayer_enabled": settings.hqplayer_enabled,
        "hqplayer_host": settings.hqplayer_host,
        "hqplayer_port": settings.hqplayer_port,
        "lastfm_username": settings.lastfm_username or "",
        "lastfm_authorized": bool(settings.lastfm_session_key),
        "postgres_port": settings.postgres_port,
        "p2p_enabled": settings.p2p_enabled,
    }


@app.get("/stats")
async def get_stats() -> Dict[str, Any]:
    """Get library statistics including enrichment coverage."""
    from sqlalchemy import text
    from database import get_db_context
    from sql_queries import ARTIST_ENGAGED

    defaults = {
        "total_artists": 0, "total_albums": 0, "total_tracks": 0,
        "total_media_files": 0, "tracks_with_embeddings": 0,
        "tracks_with_lyrics": 0, "total_duration_seconds": 0,
        "total_file_size_bytes": 0, "unique_genres": 0,
        # Enrichment coverage
        "tracks_with_features": 0,
        "library_artists": 0, "artists_with_lastfm": 0,
        "library_albums": 0,
    }
    try:
        with get_db_context() as db:
            result = db.execute(text("SELECT * FROM library_stats")).fetchone()

            if result:
                row = dict(result._mapping)
                dur = row.get("total_duration_seconds")
                row["total_duration_seconds"] = float(dur) if dur else 0
            else:
                row = {}

            # Enrichment coverage stats
            enrichment_sql = f"""
            SELECT
                -- Owned tracks only, like every other coverage figure — see
                -- library_stats. A raw table count includes phantoms and
                -- peer-imported analysis and reads as >100% coverage.
                (SELECT COUNT(*) FROM tracks t
                  WHERE EXISTS (SELECT 1 FROM media_files mf WHERE mf.track_id = t.id)
                    AND EXISTS (SELECT 1 FROM audio_features af WHERE af.track_id = t.id)
                ) as tracks_with_features,
                -- Engaged artists only, for the same reason the track figures
                -- are owned-only: a raw track_artists count is dominated by
                -- phantom tracklist credits, which the Last.fm pipeline will
                -- never call. Numerator and denominator must name the SAME
                -- population or the ratio is meaningless.
                (SELECT COUNT(*) FROM artists a
                  WHERE {ARTIST_ENGAGED}
                ) as library_artists,
                (SELECT COUNT(*) FROM artists a
                  WHERE {ARTIST_ENGAGED}
                    AND EXISTS (SELECT 1 FROM artist_bios ab
                                 WHERE ab.artist_id = a.id AND ab.source = 'lastfm')
                ) as artists_with_lastfm,
                (SELECT COUNT(DISTINCT av.album_id) FROM album_variants av
                 JOIN media_files mf ON mf.album_variant_id = av.id
                ) as library_albums
            """
            enr = db.execute(text(enrichment_sql)).fetchone()
            if enr:
                row.update(dict(enr._mapping))

            # Not a DB figure, but this payload feeds the launcher's stats
            # panel and the Library screen, and that panel owns the
            # "Analyse library" button: on a node with no ML runtime the run
            # is a no-op (analysis arrives via P2P import), so the button
            # must not exist there.
            import hardware_profile
            row["analysis_available"] = hardware_profile.resolve().ml_available

            return {**defaults, **row}
    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve library stats")


# -- Scan background task -------------------------------------------------------
import threading

_scan_state: Dict[str, Any] = {
    "running": False,
    "cancel_requested": False,
    "progress": "",
    "stats": None,        # live stats dict (processed/added/skipped/errors)
    "result": None,       # final result when done
}
_scan_lock = threading.Lock()


def _scan_worker(limit: Optional[int], skip_existing: bool, subpath: Optional[str], prune: bool = False):
    """Background worker for library scanning."""
    state = _scan_state
    from datetime import datetime, timezone
    _scan_started = datetime.now(timezone.utc)   # AI-canon `since`: only this scan's new files
    try:
        from scanner import LibraryScanner
        from routers.settings import notify_library_subscribers

        def progress_cb(msg: str, stats: dict):
            state["progress"] = msg
            state["stats"] = dict(stats)
            # Push a wake event to every connected Library SSE client.
            # Throttled to scanner's existing checkpoint cadence (≈ one
            # callback per 100-256 files / phase transition), not the
            # per-row state["progress"] write.
            notify_library_subscribers()

        def cancel_check() -> bool:
            return state["cancel_requested"]

        scanner = LibraryScanner()
        result = scanner.scan_and_import(
            limit=limit, skip_existing=skip_existing, subpath=subpath,
            progress_cb=progress_cb, cancel_check=cancel_check,
        )

        if state["cancel_requested"]:
            state["progress"] = "Scan cancelled"
        else:
            # Run artist normalization Pass 1 (safe patterns only)
            state["progress"] = "Normalizing artists..."
            try:
                from canon.migrations import normalize_artists as do_normalize
                from database import get_db_context
                with get_db_context() as db:
                    norm_stats = do_normalize(db, pass1=True)
                    if norm_stats.get('pass1', {}).get('split', 0) > 0:
                        logger.info(f"Post-scan normalization: {norm_stats}")
            except Exception as e:
                logger.error(f"Post-scan normalization failed: {e}")

            # MB canonicalization (local dump): resolve RG/MBID + collapse editions for
            # artists with new content since their last canon. No-op without the dump.
            # Runs HERE — after scan, before sync (peers get canonical content) and before
            # enrich (dedup + correction cut wasted fetches). Renames are local + reversible
            # (album_variants.raw_title), so it auto-applies without a review gate.
            from routers.settings import mb_load_active
            if not state["cancel_requested"] and mb_load_active():
                # A dump op is downloading/loading concurrently — running canon now
                # would read a stale/half-loaded dump and watermark these artists out
                # of the dump's own fresh post-load canon. Defer; that pass covers them.
                logger.info("Post-scan canon deferred — MB dump operation in progress")
            elif not state["cancel_requested"]:
                state["progress"] = "Canonicalizing (MusicBrainz)..."
                notify_library_subscribers()
                try:
                    from canon.content import canonicalize_pending
                    from canon import algo_canon
                    canon_stats = {}
                    with algo_canon() as _ok:   # priority over AI; holds the dump lock
                        if _ok:
                            canon_stats = canonicalize_pending()
                            # Catch the NULL-rg owned-album residue the main matcher
                            # rejects on its bidirectional size gate — first by edition-
                            # stripped name, then the content-only studio uniques the name
                            # pass can't reach (cross-script / reworded titles). Free,
                            # algorithmic, event-driven.
                            from canon.content import distill_album_residue, distill_album_coverage
                            bound = distill_album_residue().get("bound", 0)
                            bound += distill_album_coverage().get("bound", 0)
                            if bound:
                                canon_stats["album_residue_bound"] = bound
                    if canon_stats.get("artists") or canon_stats.get("album_residue_bound"):
                        result["mb_canon"] = canon_stats
                        logger.info(f"Post-scan MB canon: {canon_stats}")
                    # AI judgment tier on what the deterministic canon left — scoped
                    # to THIS scan's new files (since=_scan_started), async + gated.
                    from routers.settings import start_aicanon_job
                    if start_aicanon_job(since=_scan_started):
                        logger.info("Post-scan: AI canonization started (new files)")
                except Exception as e:
                    logger.error(f"Post-scan MB canonicalization failed: {e}")

            # Sign whatever analysis this scan produced (idempotent — exits
            # without a Worker call when nothing is unsigned).
            if not state["cancel_requested"]:
                try:
                    import sign_audio
                    sign_audio.run()
                except Exception as e:
                    logger.warning(f"Post-scan signing failed: {e}")

            if prune and not state["cancel_requested"]:
                state["progress"] = "Pruning missing files..."
                try:
                    from scanner import prune_missing_files
                    prune_stats = prune_missing_files(
                        progress_cb=lambda msg: state.update(progress=msg),
                        subpath=subpath,
                        cancel_check=lambda: state["cancel_requested"],
                        disk_paths=scanner.last_disk_paths,
                    )
                    result["prune"] = prune_stats
                    logger.info(f"Prune results: {prune_stats}")
                except Exception as e:
                    logger.error(f"Prune failed: {e}")
                    result["prune_error"] = str(e)

            state["progress"] = "Scan complete"
            # Persist the completion timestamp so the Library screen's
            # "Last scan" row reflects reality across backend restarts.
            try:
                from datetime import datetime, timezone
                import json as _json
                from db_pool import db_execute
                db_execute(
                    """
                    INSERT INTO user_settings (key, value) VALUES (%s, %s::jsonb)
                    ON CONFLICT (key) DO UPDATE
                        SET value = EXCLUDED.value,
                            updated_at = CURRENT_TIMESTAMP
                    """,
                    ("library.last_scan_at",
                     _json.dumps(datetime.now(timezone.utc).isoformat())),
                )
            except Exception as e:
                logger.warning(f"Failed to persist last_scan_at: {e}")
        state["result"] = result

    except Exception as e:
        logger.error(f"Scan worker failed: {e}", exc_info=True)
        state["progress"] = f"Scan failed: {str(e)[:200]}"
        state["result"] = {"error": str(e)}
    finally:
        state["running"] = False
        try:
            from routers.settings import notify_library_subscribers
            notify_library_subscribers()
        except Exception:
            pass


@app.post("/scan/start")
async def scan_start(
    limit: Optional[int] = None,
    skip_existing: bool = True,
    subpath: Optional[str] = None,
    prune: bool = False,
) -> Dict[str, Any]:
    """Start library scan as a background task. Poll /scan/status for progress."""
    with _scan_lock:
        if _scan_state["running"]:
            raise HTTPException(status_code=409, detail="Scan already running")
        _scan_state.update(
            running=True, cancel_requested=False,
            progress="Starting scan...", stats=None, result=None,
        )

    t = threading.Thread(
        target=_scan_worker,
        args=(limit, skip_existing, subpath, prune),
        daemon=True,
    )
    t.start()
    return {"success": True, "message": "Scan started"}


@app.post("/scan/cancel")
async def scan_cancel() -> Dict[str, Any]:
    """Request cancellation of a running scan."""
    if not _scan_state["running"]:
        return {"success": False, "message": "No scan running"}
    _scan_state["cancel_requested"] = True
    return {"success": True, "message": "Cancellation requested"}


@app.get("/scan/status")
async def scan_status() -> Dict[str, Any]:
    """Get current scan progress."""
    return {
        "running": _scan_state["running"],
        "progress": _scan_state["progress"],
        "stats": _scan_state["stats"],
        "result": _scan_state["result"],
    }


# Legacy sync scan endpoint (for CLI / backward compat)
@app.post("/scan")
async def scan_library_endpoint(
    limit: Optional[int] = None,
    skip_existing: bool = True,
    subpath: Optional[str] = None
) -> Dict[str, Any]:
    """Synchronous scan (for CLI/scripts). Use /scan/start for UI."""
    from scanner import scan_library as do_scan

    try:
        logger.info(f"Starting library scan (limit={limit}, skip_existing={skip_existing}, subpath={subpath})")
        stats = do_scan(limit=limit, skip_existing=skip_existing, subpath=subpath)
        return {
            "success": True,
            "statistics": stats
        }
    except Exception as e:
        logger.error(f"Scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/normalize-artists")
async def normalize_artists_endpoint(
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run artist normalization — deterministic, offline (splits feat./vs. only)."""
    from canon.migrations import normalize_artists as do_normalize
    from database import get_db_context

    try:
        with get_db_context() as db:
            stats = do_normalize(db, pass1=True, dry_run=dry_run)
        return {"success": True, "statistics": stats}
    except Exception as e:
        logger.error(f"Normalization failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


_canon_trigger_lock = threading.Lock()
_canon_trigger_running = False


def _canon_trigger_worker():
    global _canon_trigger_running
    try:
        # Re-evaluate the MB data source: the caller (P2P slice import) may have
        # just inserted the FIRST mb_artist rows into a dump-less node.
        import mb_backend as mb
        mb.refresh()
        if not mb.LOCAL_DUMP:
            logger.info("Canonicalize trigger: no local MB data — skipped")
            return
        # Phantom disambiguation joins through the curated genre vocabulary,
        # which slices never carry (it comes from the MB web API, not the dump).
        from db_pool import db_query
        if not db_query("SELECT 1 FROM mb_genre LIMIT 1"):
            import mb_dump_load
            try:
                mb_dump_load.load_genre_list()
            except Exception as e:
                logger.warning(f"Genre vocabulary fetch failed (phantom "
                               f"disambiguation degraded): {e}")
        from canon import algo_canon, canonize_phantom_similars
        from canon.content import (canonicalize_pending, distill_album_residue,
                                   distill_album_coverage)
        canon = {}
        with algo_canon() as _ok:
            if _ok:
                canon = canonicalize_pending()
                canon["album_residue_bound"] = (
                    distill_album_residue().get("bound", 0)
                    + distill_album_coverage().get("bound", 0))
                # Slice-fed phantoms (similar-artist stubs, streaming mints)
                # are consumed here — their only other chance is the slow
                # background distill loop.
                canon["phantom"] = canonize_phantom_similars(dry_run=False)
        logger.info(f"Triggered canon: {canon}")
    except Exception:
        logger.exception("canonicalize trigger worker crashed")
    finally:
        with _canon_trigger_lock:
            _canon_trigger_running = False


@app.post("/canonicalize")
async def canonicalize_endpoint() -> Dict[str, Any]:
    """Kick deterministic canonicalization in the background — the event hook
    for P2P MB slice imports (the launcher calls this after landing a slice).
    Single-flight; returns immediately."""
    global _canon_trigger_running
    with _canon_trigger_lock:
        if _canon_trigger_running:
            return {"started": False, "running": True}
        _canon_trigger_running = True
    threading.Thread(target=_canon_trigger_worker, daemon=True).start()
    return {"started": True}


@app.post("/embeddings/generate")
async def generate_embeddings_endpoint(
    limit: Optional[int] = None,
    batch_size: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Generate audio embeddings for tracks without embeddings.

    Args:
        limit: Maximum number of tracks to process.
        batch_size: Override default batch size.

    Returns:
        Statistics about the generation run.
    """
    from embeddings import generate_embeddings as do_generate

    try:
        logger.info(f"Starting embedding generation (limit={limit}, batch_size={batch_size})")
        stats = do_generate(limit=limit, batch_size=batch_size)
        return {
            "success": True,
            "statistics": stats,
        }
    except Exception as e:
        logger.error(f"Embedding generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# -- Enrichment background task -----------------------------------------------

_enrich_state: Dict[str, Any] = {
    "running": False,
    "cancel_requested": False,
    "step": "",           # current step name
    "progress": "",       # human-readable progress
    "result": None,       # final result when done
}
_enrich_lock = threading.Lock()


def _notify_library_subs_safe():
    try:
        from routers.settings import notify_library_subscribers
        notify_library_subscribers()
    except Exception:
        pass


def _enrich_worker(limit: Optional[int], local_analysis: bool):
    """Background worker for the analysis run: run_parallel_enrichment with
    the network pipelines off (Last.fm and lyrics are background_enrichment's)."""
    state = _enrich_state

    def _progress_cb(msg):
        state["progress"] = msg
        _notify_library_subs_safe()

    def _cancel_flag():
        return state["cancel_requested"]

    try:
        state["step"] = "running"
        _notify_library_subs_safe()
        # Imported inside the try: anything raised before `finally` leaves
        # running=True with no subscriber wake, and the UI sits on "Starting
        # enrichment..." until the backend restarts — /enrich/start answers
        # 409 for the rest of the process lifetime.
        from track_enrichment import run_parallel_enrichment
        result = run_parallel_enrichment(
            limit=limit,
            skip_embeddings=not local_analysis,
            skip_audio_analysis=not local_analysis,
            skip_lastfm=True,
            skip_lyrics=True,
            cancel_flag=_cancel_flag,
            progress_cb=_progress_cb,
        )

        # Build summary for status endpoint
        gpu_s = result.get("gpu", {})
        emb_s = gpu_s.get("embeddings", {})
        af_s = gpu_s.get("audio_features", {})
        text_s = result.get("text_embeddings", {})
        lyrics_emb_s = result.get("lyrics_embeddings", {})
        bio_emb_s = result.get("enrichment_embeddings", {}).get("artist_bios", {})
        parts = [
            f"Emb: {emb_s.get('success', 0)}",
            f"Features: {af_s.get('success', 0)}",
            f"Text: {text_s.get('success', 0)}",
            f"Lyrics emb: {lyrics_emb_s.get('success', 0)}",
            f"Bio emb: {bio_emb_s.get('success', 0)}",
        ]
        if result.get("note"):
            parts.append(result["note"])
        state["progress"] = " | ".join(parts)
        state["result"] = {"success": True, "statistics": result}
        # Signing moved into run_parallel_enrichment (right after the audio
        # pipeline commits) so it survives a manual cancel of the slow lyrics
        # tail and is not gated on the whole run finishing.

    except Exception as e:
        logger.error(f"Enrichment failed: {e}", exc_info=True)
        state["result"] = {"success": False, "detail": str(e)}
        state["progress"] = f"Error: {str(e)[:100]}"
    finally:
        # Models stay resident by design (clap_model/instrument_tagger/
        # text_embedder singletons), but the per-batch tensors enrichment
        # allocated linger in PyTorch's caching allocator — nvidia-smi /
        # Activity Monitor keep reporting them as held memory long after
        # the run. Return the free blocks at the session boundary via
        # device.empty_cache — the old direct torch.cuda call was a NO-OP
        # on MPS, so Mac nodes kept the whole enrichment high-water pool
        # in unified memory forever. Weights are untouched.
        if torch is not None:
            from device import empty_cache as _empty_cache
            _empty_cache()
        state["running"] = False
        state["step"] = "done"
        _notify_library_subs_safe()


def _fetch_lyrics_sync(limit=None, cancel_flag=None, progress_cb=None):
    """Fetch lyrics synchronously. Delegates to track_enrichment._fetch_lyrics_batch."""
    from track_enrichment import _fetch_lyrics_batch
    return _fetch_lyrics_batch(limit=limit, cancel_flag=cancel_flag, progress_cb=progress_cb)


@app.post("/enrich/start")
async def enrich_start(limit: Optional[int] = None) -> Dict[str, Any]:
    """Start the analysis run as a background task; poll /enrich/status.

    Audio embeddings + features, sealed, then the text encoders (titles,
    lyrics, bios, genres). Network enrichment — Last.fm, lyrics — is not
    part of this run on purpose: background_enrichment.py fetches it on
    its own cadence, on every profile. Lite skips the audio phase (its
    analysis arrives via P2P import) and still runs the text encoders; a
    node with no ML runtime at all is refused — both UIs hide the button
    there (`/stats` → analysis_available).
    """
    import hardware_profile
    profile = hardware_profile.resolve()
    if not profile.ml_available:
        raise HTTPException(
            status_code=400,
            detail="No ML runtime on this node — analysis arrives via P2P import",
        )
    local_analysis = profile.local_analysis

    with _enrich_lock:
        if _enrich_state["running"]:
            raise HTTPException(status_code=409, detail="Enrichment already running")
        _enrich_state.update(
            running=True, cancel_requested=False,
            step="starting", progress="Starting...", result=None,
        )

    t = threading.Thread(
        target=_enrich_worker, args=(limit, local_analysis), daemon=True,
    )
    t.start()
    return {"success": True, "message": "Enrichment started"}


@app.post("/enrich/cancel")
async def enrich_cancel() -> Dict[str, Any]:
    """Request cancellation of a running enrichment task."""
    if not _enrich_state["running"]:
        return {"success": False, "message": "No enrichment running"}
    _enrich_state["cancel_requested"] = True
    return {"success": True, "message": "Cancellation requested"}


@app.get("/enrich/status")
async def enrich_status() -> Dict[str, Any]:
    """Get current enrichment progress."""
    return {
        "running": _enrich_state["running"],
        "step": _enrich_state["step"],
        "progress": _enrich_state["progress"],
        "result": _enrich_state["result"],
    }


# Keep simple sync endpoints for backward compat / direct calls
@app.post("/enrich")
async def enrich_tracks_endpoint(
    limit: Optional[int] = None,
    skip_embeddings: bool = False,
    skip_lastfm: bool = False,
    skip_audio_analysis: bool = False,
) -> Dict[str, Any]:
    """Run enrichment synchronously (for CLI/Docker use). Prefer /enrich/start for UI."""
    import asyncio
    from track_enrichment import TrackEnrichmentPipeline

    def _run():
        _skip_lastfm = skip_lastfm or not settings.lastfm_api_key
        pipeline = TrackEnrichmentPipeline(
            skip_embeddings=skip_embeddings,
            skip_lastfm=_skip_lastfm,
            skip_audio_analysis=skip_audio_analysis,
        )
        return pipeline.enrich_tracks(limit=limit)

    try:
        stats = await asyncio.to_thread(_run)
        return {"success": True, "statistics": stats}
    except Exception as e:
        logger.error(f"Enrichment failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/fetch-lyrics")
async def fetch_lyrics_endpoint(
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Fetch lyrics synchronously."""
    import asyncio
    try:
        stats = await asyncio.to_thread(_fetch_lyrics_sync, limit)
        return {"success": True, "statistics": stats}
    except Exception as e:
        logger.error(f"Lyrics fetch failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/lyrics/embeddings/generate")
async def generate_lyrics_embeddings_endpoint(
    limit: Optional[int] = None,
    batch_size: Optional[int] = None,
) -> Dict[str, Any]:
    """Generate embeddings from track lyrics for semantic lyrics search."""
    import asyncio
    from lyrics_embeddings import generate_lyrics_embeddings

    try:
        stats = await asyncio.to_thread(
            generate_lyrics_embeddings, limit=limit, batch_size=batch_size
        )
        return {"success": True, "statistics": stats}
    except Exception as e:
        logger.error(f"Lyrics embedding generation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))



# -- Last.fm Auth -------------------------------------------------------------

# Temporary storage for auth URL (one per server instance)
_lastfm_auth_state: Dict[str, Any] = {}

# DB persistence — Pydantic Settings reads .env at startup, so the OAuth
# callback writes credentials to user_settings instead and the lifespan
# hook copies them onto `settings` runtime. This survives backend restarts
# without depending on a writable .env file (launcher mode keeps .env
# read-only after first generation).
_LASTFM_SESSION_KEY_KEY = "lastfm.session_key"
_LASTFM_USERNAME_KEY = "lastfm.username"


def _persist_lastfm_credentials(session_key: str, username: Optional[str]) -> None:
    """Write OAuth result to user_settings and update `settings` runtime."""
    import json as _json
    from db_pool import db_execute as _db_execute
    _db_execute(
        """
        INSERT INTO user_settings (key, value) VALUES (%s, %s::jsonb)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                                        updated_at = CURRENT_TIMESTAMP
        """,
        (_LASTFM_SESSION_KEY_KEY, _json.dumps(session_key)),
    )
    if username:
        _db_execute(
            """
            INSERT INTO user_settings (key, value) VALUES (%s, %s::jsonb)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                                            updated_at = CURRENT_TIMESTAMP
            """,
            (_LASTFM_USERNAME_KEY, _json.dumps(username)),
        )
    settings.lastfm_session_key = session_key
    if username:
        settings.lastfm_username = username


def _load_lastfm_from_db() -> None:
    """Overlay user_settings Last.fm creds onto `settings`. DB wins over env."""
    try:
        from db_pool import db_query_one as _db_query_one
        row = _db_query_one(
            "SELECT value FROM user_settings WHERE key = %(k)s",
            {"k": _LASTFM_SESSION_KEY_KEY},
        )
        if row and row.get("value"):
            settings.lastfm_session_key = row["value"]
        row = _db_query_one(
            "SELECT value FROM user_settings WHERE key = %(k)s",
            {"k": _LASTFM_USERNAME_KEY},
        )
        if row and row.get("value"):
            settings.lastfm_username = row["value"]
    except Exception as e:
        logger.warning(f"Failed to load Last.fm credentials from DB: {e}")


@app.post("/lastfm/auth/start")
async def lastfm_auth_start() -> Dict[str, str]:
    """Start Last.fm OAuth flow. Returns auth URL to open in browser."""
    import pylast

    network = pylast.LastFMNetwork(
        api_key=settings.lastfm_api_key,
        api_secret=settings.lastfm_api_secret,
    )
    skg = pylast.SessionKeyGenerator(network)
    url = skg.get_web_auth_url()
    _lastfm_auth_state["skg"] = skg
    _lastfm_auth_state["url"] = url
    return {"auth_url": url}


@app.post("/lastfm/auth/complete")
async def lastfm_auth_complete() -> Dict[str, Any]:
    """Complete Last.fm OAuth flow. Call after user authorized in browser.

    Persists session_key and username to user_settings so subsequent
    /config calls return `lastfm_authorized: true` without restart.

    auth.getSession returns both key and name in a single XML response —
    pylast exposes that as get_web_auth_session_key_username(), so we get
    the username without a second API round-trip."""
    skg = _lastfm_auth_state.get("skg")
    url = _lastfm_auth_state.get("url")
    if not skg or not url:
        raise HTTPException(status_code=400, detail="Auth flow not started. Call /lastfm/auth/start first.")
    try:
        session_key, username = skg.get_web_auth_session_key_username(url)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Authorization failed. Make sure you allowed access in the browser. ({e})",
        )
    _lastfm_auth_state.clear()

    _persist_lastfm_credentials(session_key, username)
    return {
        "success": True,
        "session_key": session_key,
        "username": username or "",
        "lastfm_authorized": True,
    }


# -- Routers & Static Files ---------------------------------------------------

from routers.player import events_router
from routers.player import router as player_router
from routers.chat import router as chat_router
from routers.media import router as media_router

app.include_router(player_router)
app.include_router(events_router)
app.include_router(chat_router)
app.include_router(media_router)

from routers.eq import router as eq_router
app.include_router(eq_router)

from routers.sync import router as sync_router, mb_router as mb_sync_router
app.include_router(sync_router)
app.include_router(mb_sync_router)

from routers.auth import router as auth_router
app.include_router(auth_router)

from routers.p2p import router as p2p_router
app.include_router(p2p_router)

from routers.covers import router as covers_router
app.include_router(covers_router)

from routers.home import router as home_router
app.include_router(home_router)

from routers.artists import router as artists_router
app.include_router(artists_router)

from routers.albums import router as albums_router
app.include_router(albums_router)

from routers.release_groups import router as release_groups_router
app.include_router(release_groups_router)

from routers.discovery import router as discovery_router
app.include_router(discovery_router)

from routers.genres import router as genres_router
app.include_router(genres_router)

from routers.hqplayer import router as hqplayer_router
app.include_router(hqplayer_router)

from routers.profile import router as profile_router
app.include_router(profile_router)

from routers.gear_models import router as gear_models_router
app.include_router(gear_models_router)

from routers.settings import router as settings_router
app.include_router(settings_router)

from routers.support import router as support_router
app.include_router(support_router)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


if __name__ == "__main__":
    import uvicorn

    from p2p_identity import tls_binding
    from tls_gen import ensure_cert

    cert_path, key_path = ensure_cert(_Path(__file__).parent / "data" / "tls",
                                      binding=tls_binding(settings))
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        log_config=LOGGING_CONFIG,
        ssl_keyfile=str(key_path),
        ssl_certfile=str(cert_path),
    )
