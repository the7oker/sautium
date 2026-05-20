"""
Sautium - FastAPI Application
Main entry point for the API server.
"""

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
from config import settings, get_settings, LOGGING_CONFIG
from dht_service import DHTService, HAS_LIBTORRENT

_API_SECRET_PATH = _Path(__file__).parent / "data" / ".api_secret"
_INDEX_HTML_PATH = _Path(__file__).parent / "static" / "index.html"
_API_SECRET_CACHE: Optional[bytes] = None


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

# Configure logging
logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger(__name__)

# Global DHT service reference (set during lifespan)
_dht_service: DHTService | None = None
_dht_reannounce_task: asyncio.Task | None = None
_model_cleanup_task: asyncio.Task | None = None


async def _model_cleanup_loop():
    """Periodically unload idle ML models to free GPU memory."""
    import model_cache
    while True:
        await asyncio.sleep(60)
        model_cache.cleanup_idle()


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

    # Start SSE status poller
    from routers.player import start_status_poller, stop_status_poller
    start_status_poller()

    # Start chat SSE listener
    from routers.p2p import start_chat_listener, stop_chat_listener
    start_chat_listener()

    # Derive P2P identity
    _p2p_identity = None
    if settings.p2p_enabled:
        # Try reading pre-derived identity from node_info.json (desktop mode)
        if settings.p2p_identity_dir:
            import json as _json
            from pathlib import Path as _Path
            _info_path = _Path(settings.p2p_identity_dir) / "node_info.json"
            if _info_path.exists():
                try:
                    _data = _json.loads(_info_path.read_text(encoding="utf-8"))
                    if _data.get("username"):
                        _p2p_identity = {
                            "node_id": _data["node_id"],
                            "public_key_hex": _data["public_key_hex"],
                            "username": _data["username"],
                            "invite_code": _data["invite_code"],
                            "email": _data.get("email", ""),
                        }
                except Exception as e:
                    logger.warning(f"Failed to read node_info.json: {e}")
        # Fallback: derive from username+password (Docker mode)
        if not _p2p_identity and settings.p2p_username and settings.p2p_password:
            try:
                from p2p_identity import derive_identity
                _p2p_identity = await asyncio.to_thread(
                    derive_identity,
                    settings.p2p_username,
                    settings.p2p_password,
                    settings.p2p_email,
                )
            except Exception as e:
                logger.warning(f"P2P identity derivation failed: {e}")

    # Start DHT service for P2P peer discovery
    global _dht_service, _dht_reannounce_task
    if settings.p2p_enabled and HAS_LIBTORRENT:
        try:
            _dht_service = DHTService(
                listen_port=settings.p2p_dht_port,
                http_port=settings.p2p_announce_port,
            )
            await _dht_service.start()

            # Announce user identity in DHT
            if _p2p_identity:
                await _dht_service.announce_user(
                    _p2p_identity["invite_code"]
                )

            # Query and announce enriched artists
            artist_uuids = await asyncio.to_thread(_get_enriched_artist_uuids)
            if artist_uuids:
                await _dht_service.announce_artists(artist_uuids)
                logger.info(
                    f"P2P online: {len(artist_uuids)} artists announced "
                    f"(HTTP port {settings.p2p_announce_port})"
                )
            else:
                logger.info("P2P online: no enriched artists to announce")

            # Periodic re-announce
            _dht_reannounce_task = asyncio.create_task(
                _dht_service.periodic_reannounce()
            )
        except Exception as e:
            logger.error(f"DHT startup failed: {e}")
            _dht_service = None
    elif settings.p2p_enabled:
        logger.warning("P2P enabled but libtorrent not installed — DHT disabled")

    # Start model cache cleanup task
    global _model_cleanup_task
    _model_cleanup_task = asyncio.create_task(_model_cleanup_loop())

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

    def _clap_factory():
        from embeddings import AudioEmbeddingGenerator
        gen = AudioEmbeddingGenerator()
        gen.load_model()
        return gen

    def _enrichment_factory():
        from enrichment_embeddings import _BaseEnrichmentGenerator
        gen = _BaseEnrichmentGenerator()
        gen.load_model()
        return gen

    def _lyrics_factory():
        from lyrics_embeddings import LyricsEmbeddingGenerator
        gen = LyricsEmbeddingGenerator()
        gen.load_model()
        return gen

    asyncio.create_task(_prewarm("CLAP",       "clap",       _clap_factory))
    asyncio.create_task(_prewarm("BGE-M3",     "enrichment", _enrichment_factory))
    asyncio.create_task(_prewarm("Lyrics-BGE", "lyrics",     _lyrics_factory))

    yield

    # Shutdown
    if _model_cleanup_task:
        _model_cleanup_task.cancel()
        try:
            await _model_cleanup_task
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


def _get_enriched_artist_uuids() -> list[str]:
    """Query enriched artist UUIDs (has embedding or audio_features)."""
    conn = psycopg2.connect(settings.database_url)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT ta.artist_id::text
            FROM track_artists ta
            WHERE EXISTS (SELECT 1 FROM embeddings e WHERE e.track_id = ta.track_id)
               OR EXISTS (SELECT 1 FROM audio_features af WHERE af.track_id = ta.track_id)
        """)
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


@app.get("/")
async def root() -> HTMLResponse:
    """Serve the Web UI shell with the HMAC secret inlined.

    The frontend reads ``window.__SAUTIUM_SECRET`` to sign every
    request. Inlining it into HTML at the same origin keeps the
    secret unreachable from cross-origin scripts (browsers refuse to
    expose response bodies cross-origin without explicit CORS), and
    avoids a separate auth-less endpoint.
    """
    secret = _get_api_secret().decode("ascii")
    html = _INDEX_HTML_PATH.read_text(encoding="utf-8")
    inject = f'<script>window.__SAUTIUM_SECRET="{secret}";</script>'
    if "</head>" in html:
        html = html.replace("</head>", f"  {inject}\n</head>", 1)
    else:
        html = inject + html
    return HTMLResponse(
        content=html, headers={"Cache-Control": "no-store"}
    )


@app.get("/health")
async def health_check() -> Dict[str, Any]:
    """Detailed health check including database, GPU, and P2P status."""
    health_status = {
        "status": "healthy",
        "type": "sautium-peer",
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
        health_status["checks"]["dht"] = _dht_service.get_dht_stats()
    else:
        health_status["checks"]["dht"] = {
            "available": HAS_LIBTORRENT,
            "running": False,
            "enabled": settings.p2p_enabled,
        }

    return health_status


@app.post("/dht/reannounce")
async def dht_reannounce() -> Dict[str, Any]:
    """Re-query enriched artists and announce new ones in DHT."""
    if not _dht_service:
        return {"success": False, "message": "DHT not running"}

    artist_uuids = await asyncio.to_thread(_get_enriched_artist_uuids)
    new_count = len(set(artist_uuids) - _dht_service._announced)
    await _dht_service.announce_artists(artist_uuids)
    return {
        "success": True,
        "total_announced": _dht_service.announced_count,
        "new": new_count,
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

    defaults = {
        "total_artists": 0, "total_albums": 0, "total_tracks": 0,
        "total_media_files": 0, "tracks_with_embeddings": 0,
        "tracks_with_lyrics": 0, "total_duration_seconds": 0,
        "total_file_size_bytes": 0, "unique_genres": 0,
        # Enrichment coverage
        "tracks_with_features": 0,
        "library_artists": 0, "artists_with_lastfm": 0,
        "library_albums": 0, "albums_with_lastfm": 0,
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
            enrichment_sql = """
            SELECT
                (SELECT COUNT(*) FROM audio_features) as tracks_with_features,
                (SELECT COUNT(DISTINCT ta.artist_id) FROM track_artists ta) as library_artists,
                (SELECT COUNT(DISTINCT a.id) FROM artists a
                 JOIN track_artists ta ON ta.artist_id = a.id
                 WHERE EXISTS (SELECT 1 FROM artist_bios ab WHERE ab.artist_id = a.id AND ab.source = 'lastfm')
                ) as artists_with_lastfm,
                (SELECT COUNT(DISTINCT av.album_id) FROM album_variants av
                 JOIN media_files mf ON mf.album_variant_id = av.id
                ) as library_albums,
                (SELECT COUNT(DISTINCT al.id) FROM albums al
                 JOIN album_variants av ON av.album_id = al.id
                 JOIN media_files mf ON mf.album_variant_id = av.id
                 WHERE EXISTS (SELECT 1 FROM album_info ai WHERE ai.album_id = al.id AND ai.source = 'lastfm')
                ) as albums_with_lastfm
            """
            enr = db.execute(text(enrichment_sql)).fetchone()
            if enr:
                row.update(dict(enr._mapping))

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
                from normalize_artists import normalize_artists as do_normalize
                from database import get_db_context
                with get_db_context() as db:
                    norm_stats = do_normalize(db, pass1=True, pass2=False)
                    if norm_stats.get('pass1', {}).get('split', 0) > 0:
                        logger.info(f"Post-scan normalization: {norm_stats}")
            except Exception as e:
                logger.error(f"Post-scan normalization failed: {e}")

            if prune and not state["cancel_requested"]:
                state["progress"] = "Pruning missing files..."
                try:
                    from scanner import prune_missing_files
                    prune_stats = prune_missing_files(
                        progress_cb=lambda msg: state.update(progress=msg),
                        subpath=subpath,
                        cancel_check=lambda: state["cancel_requested"],
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
    pass2: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Run artist normalization.

    Pass 1 (always): Split safe patterns (feat./vs.) — no external API.
    Pass 2 (optional): Verify suspicious patterns (&, comma) via Last.fm.
    """
    from normalize_artists import normalize_artists as do_normalize
    from database import get_db_context

    try:
        with get_db_context() as db:
            stats = do_normalize(db, pass1=True, pass2=pass2, dry_run=dry_run)
        return {"success": True, "statistics": stats}
    except Exception as e:
        logger.error(f"Normalization failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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


def _enrich_worker(limit: Optional[int], skip_embeddings: bool,
                   skip_lastfm: bool, skip_audio_analysis: bool):
    """Background worker that delegates to run_parallel_enrichment."""
    from track_enrichment import run_parallel_enrichment

    state = _enrich_state

    def _progress_cb(msg):
        state["progress"] = msg
        _notify_library_subs_safe()

    def _cancel_flag():
        return state["cancel_requested"]

    try:
        state["step"] = "running"
        _notify_library_subs_safe()
        result = run_parallel_enrichment(
            limit=limit,
            skip_embeddings=skip_embeddings,
            skip_lastfm=skip_lastfm,
            skip_audio_analysis=skip_audio_analysis,
            cancel_flag=_cancel_flag,
            progress_cb=_progress_cb,
        )

        # Build summary for status endpoint
        gpu_s = result.get("gpu", {})
        emb_s = gpu_s.get("embeddings", {})
        af_s = gpu_s.get("audio_features", {})
        lastfm_s = result.get("lastfm", {})
        lyrics_s = result.get("lyrics", {})
        parts = [
            f"Emb: {emb_s.get('success', 0)}",
            f"Features: {af_s.get('success', 0)}",
            f"Last.fm: {lastfm_s.get('artists_success', 0)} artists, {lastfm_s.get('albums_success', 0)} albums",
            f"Lyrics: {lyrics_s.get('found', 0)}/{lyrics_s.get('processed', 0)}",
        ]
        state["progress"] = " | ".join(parts)
        state["result"] = {"success": True, "statistics": result}

    except Exception as e:
        logger.error(f"Enrichment failed: {e}", exc_info=True)
        state["result"] = {"success": False, "detail": str(e)}
        state["progress"] = f"Error: {str(e)[:100]}"
    finally:
        state["running"] = False
        state["step"] = "done"
        _notify_library_subs_safe()


def _fetch_lyrics_sync(limit=None, cancel_flag=None, progress_cb=None):
    """Fetch lyrics synchronously. Delegates to track_enrichment._fetch_lyrics_batch."""
    from track_enrichment import _fetch_lyrics_batch
    return _fetch_lyrics_batch(limit=limit, cancel_flag=cancel_flag, progress_cb=progress_cb)


@app.post("/enrich/start")
async def enrich_start(
    limit: Optional[int] = None,
    skip_embeddings: bool = False,
    skip_lastfm: bool = False,
    skip_audio_analysis: bool = False,
) -> Dict[str, Any]:
    """Start enrichment as a background task. Poll /enrich/status for progress."""
    with _enrich_lock:
        if _enrich_state["running"]:
            raise HTTPException(status_code=409, detail="Enrichment already running")
        _enrich_state.update(
            running=True, cancel_requested=False,
            step="starting", progress="Starting...", result=None,
        )

    t = threading.Thread(
        target=_enrich_worker,
        args=(limit, skip_embeddings, skip_lastfm, skip_audio_analysis),
        daemon=True,
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


@app.post("/search/similar")
async def search_similar(
    track_id: int,
    limit: Optional[int] = None,
    min_similarity: Optional[float] = None,
    artist: Optional[str] = None,
    genre: Optional[str] = None,
    is_lossless: Optional[bool] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> Dict[str, Any]:
    """Find tracks similar to a given track by audio embedding similarity."""
    from database import get_db_context
    from search import search_similar_tracks

    filters = {}
    if artist:
        filters["artist"] = artist
    if genre:
        filters["genre"] = genre
    if is_lossless is not None:
        filters["is_lossless"] = is_lossless
    if year_from:
        filters["year_from"] = year_from
    if year_to:
        filters["year_to"] = year_to

    try:
        with get_db_context() as db:
            result = search_similar_tracks(
                db, track_id, limit=limit, min_similarity=min_similarity, filters=filters
            )
            if "error" in result:
                raise HTTPException(status_code=404, detail=result["error"])
            return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Similar search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/search/text")
async def search_text(
    query: str,
    limit: Optional[int] = None,
    min_similarity: Optional[float] = None,
    artist: Optional[str] = None,
    genre: Optional[str] = None,
    is_lossless: Optional[bool] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> Dict[str, Any]:
    """Search tracks by text description using CLAP text-to-audio embeddings."""
    from database import get_db_context
    from search import search_by_text

    filters = {}
    if artist:
        filters["artist"] = artist
    if genre:
        filters["genre"] = genre
    if is_lossless is not None:
        filters["is_lossless"] = is_lossless
    if year_from:
        filters["year_from"] = year_from
    if year_to:
        filters["year_to"] = year_to

    try:
        with get_db_context() as db:
            return search_by_text(
                db, query, limit=limit, min_similarity=min_similarity, filters=filters
            )
    except Exception as e:
        logger.error(f"Text search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/search/lyrics")
async def search_lyrics(
    query: str,
    limit: Optional[int] = None,
    min_similarity: Optional[float] = None,
) -> Dict[str, Any]:
    """Search tracks by lyrics content similarity."""
    from database import get_db_context
    from search import search_by_lyrics

    try:
        with get_db_context() as db:
            return search_by_lyrics(
                db, query, limit=limit, min_similarity=min_similarity
            )
    except Exception as e:
        logger.error(f"Lyrics search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/search/artists")
async def api_search_artists(
    query: str,
    limit: int = 10,
    min_similarity: Optional[float] = None,
) -> Dict[str, Any]:
    """Search artists by biography similarity."""
    from database import get_db_context
    from search import search_artists_by_bio

    try:
        with get_db_context() as db:
            return search_artists_by_bio(
                db, query, limit=limit,
                min_similarity=min_similarity or 0.3,
            )
    except Exception as e:
        logger.error(f"Artist search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/search/albums")
async def api_search_albums(
    query: str,
    limit: int = 10,
    min_similarity: Optional[float] = None,
) -> Dict[str, Any]:
    """Search albums by description similarity."""
    from database import get_db_context
    from search import search_albums_by_info

    try:
        with get_db_context() as db:
            return search_albums_by_info(
                db, query, limit=limit,
                min_similarity=min_similarity or 0.3,
            )
    except Exception as e:
        logger.error(f"Album search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/search/genres")
async def api_search_genres(
    query: str,
    limit: int = 10,
    min_similarity: Optional[float] = None,
) -> Dict[str, Any]:
    """Search genres by description similarity."""
    from database import get_db_context
    from search import search_genres_by_description

    try:
        with get_db_context() as db:
            return search_genres_by_description(
                db, query, limit=limit,
                min_similarity=min_similarity or 0.3,
            )
    except Exception as e:
        logger.error(f"Genre search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/search/metadata")
async def search_metadata(
    artist: Optional[str] = None,
    album: Optional[str] = None,
    genre: Optional[str] = None,
    is_lossless: Optional[bool] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> Dict[str, Any]:
    """Search tracks by metadata filters only."""
    from database import get_db_context
    from search import search_by_metadata

    filters = {}
    if artist:
        filters["artist"] = artist
    if album:
        filters["album"] = album
    if genre:
        filters["genre"] = genre
    if is_lossless is not None:
        filters["is_lossless"] = is_lossless
    if year_from:
        filters["year_from"] = year_from
    if year_to:
        filters["year_to"] = year_to

    try:
        with get_db_context() as db:
            return search_by_metadata(db, filters=filters, limit=limit, offset=offset)
    except Exception as e:
        logger.error(f"Metadata search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/search/features")
async def search_features(
    bpm_min: Optional[float] = None,
    bpm_max: Optional[float] = None,
    key: Optional[str] = None,
    mode: Optional[str] = None,
    instrument: Optional[str] = None,
    vocalist: Optional[str] = None,
    gender: Optional[str] = None,
    danceable: Optional[str] = None,
    energy_min: Optional[float] = None,
    energy_max: Optional[float] = None,
    artist: Optional[str] = None,
    genre: Optional[str] = None,
    is_lossless: Optional[bool] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Search tracks by audio features (BPM, key, instruments, vocal, danceability)."""
    from database import get_db_context
    from search import search_by_features

    filters = {}
    if bpm_min is not None:
        filters["bpm_min"] = bpm_min
    if bpm_max is not None:
        filters["bpm_max"] = bpm_max
    if key:
        filters["key"] = key
    if mode:
        filters["mode"] = mode
    if instrument:
        filters["instrument"] = instrument
    if vocalist:
        filters["vocalist"] = vocalist
    if gender:
        filters["gender"] = gender
    if danceable == "yes":
        filters["danceable"] = True
    elif danceable == "no":
        filters["not_danceable"] = True
    if energy_min is not None:
        filters["energy_min"] = energy_min
    if energy_max is not None:
        filters["energy_max"] = energy_max
    if artist:
        filters["artist"] = artist
    if genre:
        filters["genre"] = genre
    if is_lossless is not None:
        filters["is_lossless"] = is_lossless

    try:
        with get_db_context() as db:
            return search_by_features(db, filters=filters, limit=limit)
    except Exception as e:
        logger.error(f"Features search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# -- Last.fm Auth -------------------------------------------------------------

# Temporary storage for auth URL (one per server instance)
_lastfm_auth_state: Dict[str, Any] = {}


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
    """Complete Last.fm OAuth flow. Call after user authorized in browser."""
    skg = _lastfm_auth_state.get("skg")
    url = _lastfm_auth_state.get("url")
    if not skg or not url:
        raise HTTPException(status_code=400, detail="Auth flow not started. Call /lastfm/auth/start first.")
    try:
        session_key = skg.get_web_auth_session_key(url)
        _lastfm_auth_state.clear()
        return {"success": True, "session_key": session_key}
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Authorization failed. Make sure you allowed access in the browser. ({e})",
        )


# -- Routers & Static Files ---------------------------------------------------

from routers.player import router as player_router
from routers.chat import router as chat_router

app.include_router(player_router)
app.include_router(chat_router)

from routers.eq import router as eq_router
app.include_router(eq_router)

from routers.sync import router as sync_router
app.include_router(sync_router)

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

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


if __name__ == "__main__":
    import uvicorn

    from tls_gen import ensure_cert

    cert_path, key_path = ensure_cert(_Path(__file__).parent / "data" / "tls")
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        log_config=LOGGING_CONFIG,
        ssl_keyfile=str(key_path),
        ssl_certfile=str(cert_path),
    )
