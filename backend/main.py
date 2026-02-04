"""
Music AI DJ - FastAPI Application
Main entry point for the API server.
"""

import logging
import logging.config
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional

import psycopg2
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

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
    if torch.cuda.is_available():
        gpu_name = torch.cuda.get_device_name(0)
        gpu_memory = torch.cuda.get_device_properties(0).total_memory / 1e9
        logger.info(f"GPU available: {gpu_name} ({gpu_memory:.1f} GB)")
    else:
        logger.warning("No GPU detected. Audio embedding will be slow.")

    # Test database connection
    try:
        test_db_connection()
        logger.info("Database connection successful")
    except Exception as e:
        logger.error(f"Database connection failed: {e}")

    yield

    # Shutdown
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
async def root() -> Dict[str, Any]:
    """Health check endpoint."""
    return {
        "status": "healthy",
        "app": settings.app_name,
        "version": settings.app_version,
    }


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
    if torch.cuda.is_available():
        health_status["checks"]["gpu"] = {
            "available": True,
            "name": torch.cuda.get_device_name(0),
            "memory_gb": round(
                torch.cuda.get_device_properties(0).total_memory / 1e9, 1
            )
        }
    else:
        health_status["checks"]["gpu"] = {
            "available": False
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

    try:
        with get_db_context() as db:
            result = db.execute(text("SELECT * FROM library_stats")).fetchone()

            if result:
                return {
                    "total_artists": result[0],
                    "total_albums": result[1],
                    "total_tracks": result[2],
                    "tracks_with_embeddings": result[3],
                    "total_duration_seconds": float(result[4]) if result[4] else 0,
                    "total_file_size_bytes": result[5] or 0,
                    "unique_genres": result[6],
                }
            else:
                return {
                    "total_artists": 0,
                    "total_albums": 0,
                    "total_tracks": 0,
                    "tracks_with_embeddings": 0,
                    "total_duration_seconds": 0,
                    "total_file_size_bytes": 0,
                    "unique_genres": 0,
                }
    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Scanning endpoints

@app.post("/scan")
async def scan_library_endpoint(
    limit: Optional[int] = None,
    skip_existing: bool = True
) -> Dict[str, Any]:
    """
    Scan music library and import metadata to database.

    Args:
        limit: Maximum number of files to scan (for testing).
        skip_existing: Skip files already in database.

    Returns:
        Statistics about the scan.
    """
    from scanner import scan_library as do_scan

    try:
        logger.info(f"Starting library scan (limit={limit}, skip_existing={skip_existing})")
        stats = do_scan(limit=limit, skip_existing=skip_existing)
        return {
            "success": True,
            "statistics": stats
        }
    except Exception as e:
        logger.error(f"Scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/embeddings/generate")
async def generate_embeddings():
    """Generate audio embeddings (Step 1.3)."""
    raise HTTPException(
        status_code=501,
        detail="Embedding generation will be implemented in Step 1.3"
    )


@app.post("/search/similar")
async def search_similar():
    """Search for similar tracks (Step 1.4)."""
    raise HTTPException(
        status_code=501,
        detail="Similarity search will be implemented in Step 1.4"
    )


@app.post("/search/query")
async def search_query():
    """Natural language search with AI (Step 1.5)."""
    raise HTTPException(
        status_code=501,
        detail="AI-powered search will be implemented in Step 1.5"
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_config=LOGGING_CONFIG
    )
