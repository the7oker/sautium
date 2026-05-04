"""Cover art endpoints.

Two routes:

  GET /api/covers/{cover_id}             — direct UUID lookup, instant.
  GET /api/covers/by-media/{media_file_id}
                                          — lazy resolution: extract from
                                            disk, fall back to Last.fm,
                                            cache result, mark sentinel
                                            on permanent failure.

Resolved covers carry an `immutable` Cache-Control. The sentinel 404 is
served with a short max-age so a manual rescan / new disk file can
unblock the next request.
"""

import asyncio
import logging
from typing import Dict

from fastapi import APIRouter, HTTPException, Path as FPath
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy import text

from covers import (
    SENTINEL_COVER_ID,
    resolve_cover_for_folder,
    resolve_artist_photo,
    _split_folder,
)
from database import get_db_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/covers", tags=["covers"])

# Single-flight: while one request is resolving covers for a folder, any
# concurrent /api/covers/by-media/<id> for siblings of the same folder
# waits on the same lock. After the leader finishes, the followers
# re-check the DB (cover_id is now populated) and serve directly.
_folder_locks: Dict[str, asyncio.Lock] = {}
_folder_locks_master = asyncio.Lock()

# Per-artist single-flight: a page rendering 30 artist chips otherwise
# triggers 30 simultaneous Last.fm scrapes for the same set; the lock
# collapses concurrent first-time requests for one artist into a
# single fetch.
_artist_locks: Dict[str, asyncio.Lock] = {}
_artist_locks_master = asyncio.Lock()


async def _get_folder_lock(folder_key: str) -> asyncio.Lock:
    async with _folder_locks_master:
        lock = _folder_locks.get(folder_key)
        if lock is None:
            lock = asyncio.Lock()
            _folder_locks[folder_key] = lock
        return lock


async def _get_artist_lock(artist_id: str) -> asyncio.Lock:
    async with _artist_locks_master:
        lock = _artist_locks.get(artist_id)
        if lock is None:
            lock = asyncio.Lock()
            _artist_locks[artist_id] = lock
        return lock


def _serve_cover_bytes(cover_id: str) -> Response:
    """Look up cover bytes by UUID; raise 404 with short cache on sentinel /
    missing, return Response with immutable cache on success."""
    with get_db_context() as db:
        row = db.execute(
            text("SELECT data, content_hash FROM covers WHERE id = :id"),
            {"id": cover_id},
        ).first()

    if not row:
        raise HTTPException(status_code=404, detail="cover not found")

    data, content_hash = row

    # Sentinel row carries empty bytes — serve as 404 with short cache.
    if cover_id == str(SENTINEL_COVER_ID) or not data:
        # Headers on HTTPException don't propagate easily; raise with a
        # custom Response instead.
        return Response(
            status_code=404,
            content=b"",
            headers={"Cache-Control": "public, max-age=300"},
        )

    etag = f'"{content_hash.hex()[:16]}"'
    return Response(
        content=bytes(data),
        media_type="image/webp",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": etag,
        },
    )


def _resolve_sync(media_file_id: int):
    """Folder-level resolution under one DB transaction. Runs in a worker
    thread so the FastAPI event loop is not blocked on I/O."""
    with get_db_context() as db:
        cover_id = resolve_cover_for_folder(db, media_file_id)
        # get_db_context auto-commits on clean exit.
    return cover_id


@router.get("/by-media/{media_file_id}")
async def get_cover_by_media(media_file_id: int):
    """Lazy cover resolution for a media_file.

    Fast path: media_files.cover_id is already set → serve bytes.
    Slow path: cover_id is NULL → enter folder-wide resolution under a
    per-folder lock; first caller does the work, siblings benefit from
    the populated cover_id and serve fast on retry.
    """
    # Fast path — single round-trip if cover already resolved.
    with get_db_context() as db:
        row = db.execute(
            text("SELECT cover_id, file_path FROM media_files WHERE id = :id"),
            {"id": media_file_id},
        ).first()

    if not row:
        raise HTTPException(status_code=404, detail="media_file not found")

    cover_id, file_path = row
    if cover_id is not None:
        return _serve_cover_bytes(str(cover_id))

    # Slow path — single-flight per folder.
    folder_prefix, _ = _split_folder(file_path or "")
    lock_key = folder_prefix or f"mf:{media_file_id}"
    lock = await _get_folder_lock(lock_key)

    async with lock:
        # Re-check after acquiring lock — sibling request may have just
        # finished resolution and populated our cover_id.
        with get_db_context() as db:
            row = db.execute(
                text("SELECT cover_id FROM media_files WHERE id = :id"),
                {"id": media_file_id},
            ).first()
        if row and row[0] is not None:
            return _serve_cover_bytes(str(row[0]))

        try:
            resolved = await run_in_threadpool(_resolve_sync, media_file_id)
        except Exception as e:
            logger.error(f"cover resolution failed for media_file {media_file_id}: {e}")
            raise HTTPException(status_code=500, detail="cover resolution failed")

    if resolved is None:
        # Shouldn't happen — resolve_cover_for_folder always returns
        # at least SENTINEL_COVER_ID. Defensive 404 with short cache
        # so a retry can recover after a code fix.
        return Response(
            status_code=404,
            content=b"",
            headers={"Cache-Control": "public, max-age=60"},
        )

    return _serve_cover_bytes(str(resolved))


def _resolve_artist_sync(artist_id: str):
    with get_db_context() as db:
        return resolve_artist_photo(db, artist_id)


@router.get("/by-artist/{artist_id}")
async def get_cover_by_artist(artist_id: str = FPath(..., min_length=36, max_length=36)):
    """Lazy resolution of an artist photo.

    Fast path: artists.photo_cover_id is already set → serve bytes.
    Slow path: NULL → scrape Last.fm under a per-artist lock; failures
    pin SENTINEL_COVER_ID so we don't re-scrape on every request."""
    with get_db_context() as db:
        row = db.execute(
            text("SELECT photo_cover_id FROM artists WHERE id = :id"),
            {"id": artist_id},
        ).first()

    if not row:
        raise HTTPException(status_code=404, detail="artist not found")

    cover_id = row[0]
    if cover_id is not None:
        return _serve_cover_bytes(str(cover_id))

    lock = await _get_artist_lock(artist_id)
    async with lock:
        with get_db_context() as db:
            row = db.execute(
                text("SELECT photo_cover_id FROM artists WHERE id = :id"),
                {"id": artist_id},
            ).first()
        if row and row[0] is not None:
            return _serve_cover_bytes(str(row[0]))

        try:
            resolved = await run_in_threadpool(_resolve_artist_sync, artist_id)
        except Exception as e:
            logger.error(f"artist photo resolution failed for {artist_id}: {e}")
            raise HTTPException(status_code=500, detail="photo resolution failed")

    if resolved is None:
        return Response(
            status_code=404,
            content=b"",
            headers={"Cache-Control": "public, max-age=60"},
        )
    return _serve_cover_bytes(str(resolved))


@router.get("/{cover_id}")
async def get_cover(cover_id: str = FPath(..., min_length=36, max_length=36)):
    """Serve WebP cover bytes by UUID. Long-cache headers."""
    try:
        return _serve_cover_bytes(cover_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"cover lookup failed for {cover_id}: {e}")
        raise HTTPException(status_code=500, detail="cover lookup failed")
