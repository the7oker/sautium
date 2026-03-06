"""
Music AI DJ - FastAPI Application
Main entry point for the API server.
"""

import logging
import logging.config
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional

import psycopg2
try:
    import torch
except ImportError:
    torch = None
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from config import settings, get_settings, LOGGING_CONFIG

# Configure logging
logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger(__name__)


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

    yield

    # Shutdown
    stop_status_poller()
    logger.info("Shutting down application")


# Initialize FastAPI app
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="AI-powered music library management and recommendation system",
    lifespan=lifespan,
)


def test_db_connection() -> bool:
    """Test PostgreSQL connection."""
    conn = psycopg2.connect(settings.database_url)
    cursor = conn.cursor()
    cursor.execute("SELECT version();")
    version = cursor.fetchone()
    cursor.close()
    conn.close()
    logger.debug(f"PostgreSQL version: {version[0]}")
    return True


@app.get("/")
async def root():
    """Redirect to Web UI."""
    return RedirectResponse(url="/static/index.html")


@app.get("/health")
async def health_check() -> Dict[str, Any]:
    """Detailed health check including database and GPU."""
    health_status = {
        "status": "healthy",
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

    return health_status


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
    }


@app.get("/stats")
async def get_stats() -> Dict[str, Any]:
    """Get library statistics."""
    from sqlalchemy import text
    from database import get_db_context

    defaults = {
        "total_artists": 0, "total_albums": 0, "total_tracks": 0,
        "total_media_files": 0, "tracks_with_embeddings": 0,
        "tracks_with_lyrics": 0, "total_duration_seconds": 0,
        "total_file_size_bytes": 0, "unique_genres": 0,
    }
    try:
        with get_db_context() as db:
            result = db.execute(text("SELECT * FROM library_stats")).fetchone()

            if result:
                row = dict(result._mapping)
                dur = row.get("total_duration_seconds")
                row["total_duration_seconds"] = float(dur) if dur else 0
                return {**defaults, **row}
            else:
                return defaults
    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Scanning endpoints

@app.post("/scan")
async def scan_library_endpoint(
    limit: Optional[int] = None,
    skip_existing: bool = True,
    subpath: Optional[str] = None
) -> Dict[str, Any]:
    """
    Scan music library and import metadata to database.

    Args:
        limit: Maximum number of files to scan (for testing).
        skip_existing: Skip files already in database.
        subpath: Optional subdirectory within library to scan.

    Returns:
        Statistics about the scan.
    """
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

import threading

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
    """Background worker that runs all enrichment steps sequentially."""
    import time as _time

    state = _enrich_state
    result_parts = {}

    try:
        # --- Step 1: Track enrichment (embeddings, Last.fm, audio analysis) ---
        state["step"] = "enrich"
        state["progress"] = "Enriching tracks..."

        # Auto-skip Last.fm if API key not configured
        _skip_lastfm = skip_lastfm or not settings.lastfm_api_key

        # Log GPU status
        try:
            import torch
            if torch.cuda.is_available():
                gpu_name = torch.cuda.get_device_name(0)
                logger.info(f"GPU available: {gpu_name}")
                state["progress"] = f"GPU: {gpu_name}"
            else:
                logger.warning("CUDA not available — embeddings will be slow on CPU")
                state["progress"] = "Warning: no CUDA GPU, embeddings will be slow"
        except ImportError:
            logger.warning("torch not installed — audio embeddings will fail")

        if state["cancel_requested"]:
            return

        from track_enrichment import TrackEnrichmentPipeline
        pipeline = TrackEnrichmentPipeline(
            skip_embeddings=skip_embeddings,
            skip_lastfm=_skip_lastfm,
            skip_audio_analysis=skip_audio_analysis,
        )
        enrich_stats = pipeline.enrich_tracks(
            limit=limit,
            cancel_flag=lambda: state["cancel_requested"],
            progress_cb=lambda msg: state.update(progress=msg),
        )
        result_parts["enrich"] = enrich_stats
        state["progress"] = f"Enriched {enrich_stats.get('processed', 0)} tracks"

        if state["cancel_requested"]:
            return

        # --- Step 2: Fetch lyrics ---
        state["step"] = "lyrics"
        state["progress"] = "Fetching lyrics..."

        lyrics_stats = _fetch_lyrics_sync(
            limit=None,
            cancel_flag=lambda: state["cancel_requested"],
            progress_cb=lambda msg: state.update(progress=msg),
        )
        result_parts["lyrics"] = lyrics_stats
        state["progress"] = f"Lyrics: {lyrics_stats.get('found', 0)} found"

        if state["cancel_requested"]:
            return

        # --- Step 3: Lyrics embeddings ---
        state["step"] = "lyrics_embeddings"
        state["progress"] = "Generating lyrics embeddings..."

        from lyrics_embeddings import generate_lyrics_embeddings
        lyrics_emb_stats = generate_lyrics_embeddings(
            limit=None,
            progress_cb=lambda msg: state.update(progress=msg),
        )
        result_parts["lyrics_embeddings"] = lyrics_emb_stats

        # Build summary
        parts = []
        enrich_s = result_parts.get("enrich", {})
        parts.append(f"Enriched: {enrich_s.get('processed', 0)}")
        lyrics_s = result_parts.get("lyrics", {})
        parts.append(f"Lyrics: {lyrics_s.get('found', 0)} found")
        lem_s = result_parts.get("lyrics_embeddings", {})
        parts.append(f"Lyrics emb: {lem_s.get('success', 0)}")
        state["progress"] = " | ".join(parts)

        state["result"] = {"success": True, "statistics": result_parts}

    except Exception as e:
        logger.error(f"Enrichment failed: {e}", exc_info=True)
        state["result"] = {"success": False, "detail": str(e)}
        state["progress"] = f"Error: {str(e)[:100]}"
    finally:
        state["running"] = False
        state["step"] = "done"


def _fetch_lyrics_sync(limit=None, cancel_flag=None, progress_cb=None):
    """Fetch lyrics synchronously. Used by enrichment worker."""
    import time as _time
    from database import get_db_context
    from sqlalchemy import text

    use_lrclib = True
    use_genius = bool(settings.genius_access_token)

    lrclib_service = None
    genius_service = None

    if use_lrclib:
        from lrclib import LrclibService
        lrclib_service = LrclibService()
    if use_genius:
        from genius import GeniusService
        genius_service = GeniusService(settings.genius_access_token)

    stats = {"processed": 0, "found": 0, "not_found": 0, "errors": 0}

    with get_db_context() as db:
        query_sql = """
            SELECT t.id as track_id, t.title,
                   a.name as artist,
                   al.title as album,
                   mf.duration_seconds
            FROM tracks t
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a ON ta.artist_id = a.id
            JOIN media_files mf ON mf.track_id = t.id AND mf.is_analysis_source = true
            JOIN album_variants av ON mf.album_variant_id = av.id
            JOIN albums al ON av.album_id = al.id
            LEFT JOIN external_metadata em
                ON em.entity_type = 'track'
                AND em.entity_id = t.id::text
                AND em.source = 'lrclib'
                AND em.metadata_type = 'lyrics'
            WHERE em.id IS NULL
            ORDER BY t.title
        """
        if limit:
            query_sql += f" LIMIT {limit}"

        rows = db.execute(text(query_sql)).fetchall()
        total_lyrics = len(rows)
        logger.info(f"Lyrics: {total_lyrics} tracks to process")
        lyrics_start = _time.time()

        for i, row in enumerate(rows):
            if cancel_flag and cancel_flag():
                break

            stats["processed"] += 1

            if progress_cb and i % 5 == 0:
                elapsed = _time.time() - lyrics_start
                if i > 0:
                    eta = elapsed / i * (total_lyrics - i)
                    eta_str = f", ETA {int(eta)}s"
                else:
                    eta_str = ""
                progress_cb(f"Lyrics {i+1}/{total_lyrics}{eta_str}")
            found = False

            if use_lrclib and lrclib_service:
                try:
                    result = lrclib_service.fetch_and_store(
                        db, row.track_id, row.title, row.artist,
                        album_name=row.album,
                        duration=int(row.duration_seconds) if row.duration_seconds else None,
                    )
                    if result and result.get("status") not in ("not_found", "error"):
                        found = True
                    _time.sleep(0.1)
                except Exception as e:
                    logger.debug(f"LRCLIB failed for {row.artist} - {row.title}: {e}")

            if not found and use_genius and genius_service:
                try:
                    result = genius_service.fetch_and_store(
                        db, row.track_id, row.title, row.artist,
                    )
                    if result and result.get("status") not in ("not_found", "error"):
                        found = True
                    _time.sleep(1.0)
                except Exception as e:
                    logger.debug(f"Genius failed for {row.artist} - {row.title}: {e}")

            if found:
                stats["found"] += 1
            else:
                stats["not_found"] += 1

            db.commit()

    return stats


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
