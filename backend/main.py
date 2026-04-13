"""
Sautium - FastAPI Application
Main entry point for the API server.
"""

import asyncio
import logging
import logging.config
import select
import threading
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional

import psycopg2
import psycopg2.extensions
try:
    import torch
except ImportError:
    torch = None
from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from config import settings, get_settings, LOGGING_CONFIG
from dht_service import DHTService, HAS_LIBTORRENT

# Configure logging
logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger(__name__)

# Global DHT service reference (set during lifespan)
_dht_service: DHTService | None = None
_dht_reannounce_task: asyncio.Task | None = None
_model_cleanup_task: asyncio.Task | None = None
_cover_worker_task: asyncio.Task | None = None
_cover_notify_event: asyncio.Event | None = None
_cover_listen_thread: threading.Thread | None = None
_cover_listen_running = False


async def _model_cleanup_loop():
    """Periodically unload idle ML models to free GPU memory."""
    import model_cache
    while True:
        await asyncio.sleep(60)
        model_cache.cleanup_idle()


# -- Cover worker ------------------------------------------------------------

_COVER_BATCH_SIZE = 20
_COVER_IDLE_TIMEOUT = 60  # seconds between wake-ups when queue is empty


def _cover_sweep_once() -> int:
    """Process up to _COVER_BATCH_SIZE pending media_files. Returns count processed.

    Runs in a worker thread (blocking DB + CPU work). Errors on individual
    rows are caught and the row is marked processed to avoid a retry loop.
    """
    from sqlalchemy import text as _text
    from database import get_db_context
    from covers import process_pending

    try:
        with get_db_context() as db:
            rows = db.execute(_text(
                "SELECT id FROM media_files "
                "WHERE cover_processed_at IS NULL "
                "ORDER BY id LIMIT :lim"
            ), {"lim": _COVER_BATCH_SIZE}).all()
    except Exception as e:
        logger.error(f"cover worker: batch query failed: {e}")
        return 0

    if not rows:
        return 0

    processed = 0
    for (mid,) in rows:
        try:
            with get_db_context() as db:
                process_pending(db, mid)
            processed += 1
        except Exception as e:
            logger.warning(f"cover worker: media_file {mid} failed: {e}")
            try:
                with get_db_context() as db:
                    db.execute(_text(
                        "UPDATE media_files SET cover_processed_at = now() "
                        "WHERE id = :mid"
                    ), {"mid": mid})
            except Exception as e2:
                logger.error(f"cover worker: failed to mark {mid} processed: {e2}")
    return processed


def _cover_listen_loop(event: asyncio.Event, loop: asyncio.AbstractEventLoop):
    """Background thread: LISTEN cover_pending, wake the worker task on NOTIFY."""
    global _cover_listen_running
    while _cover_listen_running:
        conn = None
        try:
            conn = psycopg2.connect(settings.database_url)
            conn.set_isolation_level(
                psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT
            )
            with conn.cursor() as cur:
                cur.execute("LISTEN cover_pending")
            while _cover_listen_running:
                ready = select.select([conn], [], [], 5)
                if ready[0]:
                    conn.poll()
                    if conn.notifies:
                        conn.notifies.clear()
                        loop.call_soon_threadsafe(event.set)
        except Exception as e:
            logger.debug(f"cover LISTEN error: {e}")
            if _cover_listen_running:
                import time
                time.sleep(1)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


async def _cover_worker_loop():
    """Drain pending covers, then react to LISTEN cover_pending notifications."""
    global _cover_notify_event, _cover_listen_thread, _cover_listen_running
    _cover_notify_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    _cover_listen_running = True
    _cover_listen_thread = threading.Thread(
        target=_cover_listen_loop,
        args=(_cover_notify_event, loop),
        daemon=True,
        name="cover-listen",
    )
    _cover_listen_thread.start()

    initial_total = 0
    initial_sweep = True
    try:
        while True:
            count = await asyncio.to_thread(_cover_sweep_once)
            if count > 0:
                initial_total += count
                if initial_sweep and initial_total % (10 * _COVER_BATCH_SIZE) == 0:
                    logger.info(
                        f"cover worker: processed {initial_total} media_files..."
                    )
                continue

            if initial_sweep:
                logger.info(
                    f"cover worker: initial sweep complete ({initial_total} processed)"
                )
                initial_sweep = False

            _cover_notify_event.clear()
            try:
                await asyncio.wait_for(
                    _cover_notify_event.wait(),
                    timeout=_COVER_IDLE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup and shutdown events."""
    # Startup
    logger.info(f"Starting {settings.app_name} v{settings.app_version}")

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

    # Start cover art worker (initial sweep + LISTEN cover_pending)
    global _cover_worker_task
    _cover_worker_task = asyncio.create_task(_cover_worker_loop())

    yield

    # Shutdown
    global _cover_listen_running
    _cover_listen_running = False
    if _cover_worker_task:
        _cover_worker_task.cancel()
        try:
            await _cover_worker_task
        except asyncio.CancelledError:
            pass
    if _cover_listen_thread:
        _cover_listen_thread.join(timeout=3)

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
async def root():
    """Redirect to Web UI."""
    return RedirectResponse(url="/static/index.html")


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

        def progress_cb(msg: str, stats: dict):
            state["progress"] = msg
            state["stats"] = dict(stats)

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

            if prune:
                state["progress"] = "Pruning missing files..."
                try:
                    from scanner import prune_missing_files
                    prune_stats = prune_missing_files(progress_cb=lambda msg: state.update(progress=msg), subpath=subpath)
                    result["prune"] = prune_stats
                    logger.info(f"Prune results: {prune_stats}")
                except Exception as e:
                    logger.error(f"Prune failed: {e}")
                    result["prune_error"] = str(e)

            state["progress"] = "Scan complete"
        state["result"] = result

    except Exception as e:
        logger.error(f"Scan worker failed: {e}", exc_info=True)
        state["progress"] = f"Scan failed: {str(e)[:200]}"
        state["result"] = {"error": str(e)}
    finally:
        state["running"] = False


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


def _enrich_worker(limit: Optional[int], skip_embeddings: bool,
                   skip_lastfm: bool, skip_audio_analysis: bool):
    """Background worker that delegates to run_parallel_enrichment."""
    from track_enrichment import run_parallel_enrichment

    state = _enrich_state

    def _progress_cb(msg):
        state["progress"] = msg

    def _cancel_flag():
        return state["cancel_requested"]

    try:
        state["step"] = "running"
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

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        log_config=LOGGING_CONFIG
    )
