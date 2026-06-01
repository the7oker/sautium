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
import time
from typing import Dict

from fastapi import APIRouter, HTTPException, Path as FPath
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy import text

from covers import (
    SENTINEL_COVER_ID,
    resolve_cover_for_folder,
    resolve_artist_photo,
    photo_cooldown_active,
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

# Global throttle for external artist-photo lookups (Deezer + Last.fm).
# Per-artist locks dedupe one artist, but 30 *different* chips still fire
# 30 concurrent external requests — enough to get the whole IP rate-
# limited (Last.fm bans hard on bursts). So every lookup, regardless of
# artist, passes single-file through this semaphore, paced by a minimum
# interval. A request that can't be served within the wait budget defers
# to a short-cached 404 instead of holding the browser connection open
# (and starving the browser's ~6-connection pool); the lazy <img> retries
# on the next render. This is rate-limiting, not polling — the work is
# event-driven off the request, the interval only bounds the source's load.
_photo_sem = asyncio.Semaphore(1)
_PHOTO_MIN_INTERVAL = 1.0
_PHOTO_WAIT_BUDGET = 2.5
_photo_last_call = 0.0


def _defer_cover() -> Response:
    """Cover/photo not resolved this time — cooldown active, throttle
    queue busy, or a transient external failure. Short cache so the lazy
    <img> retries on a later render rather than every frame. The frontend
    overlays art on a placeholder and removes the <img> on 404, so the
    user sees the placeholder meanwhile."""
    return Response(
        status_code=404,
        content=b"",
        headers={"Cache-Control": "public, max-age=60"},
    )


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
        # A transient Last.fm failure left the cover unresolved (NULL, not
        # SENTINEL). Defer so the next request retries once Last.fm recovers.
        return _defer_cover()

    return _serve_cover_bytes(str(resolved))


def _resolve_artist_sync(artist_id: str):
    with get_db_context() as db:
        return resolve_artist_photo(db, artist_id)


@router.get("/by-artist/{artist_id}")
async def get_cover_by_artist(artist_id: str = FPath(..., min_length=36, max_length=36)):
    """Lazy resolution of an artist photo.

    Fast path: artists.photo_cover_id is already set → serve bytes.
    Slow path: NULL → resolve via Deezer/Last.fm behind the global photo
    throttle. While a source has us in cooldown, or the throttle queue is
    busy beyond the wait budget, defer to a short-cached 404 rather than
    block — failures pin SENTINEL_COVER_ID so we don't re-fetch forever."""
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

    # A source recently rate-limited us — don't queue more work, defer.
    if photo_cooldown_active():
        return _defer_cover()

    lock = await _get_artist_lock(artist_id)
    async with lock:
        with get_db_context() as db:
            row = db.execute(
                text("SELECT photo_cover_id FROM artists WHERE id = :id"),
                {"id": artist_id},
            ).first()
        if row and row[0] is not None:
            return _serve_cover_bytes(str(row[0]))

        # Serialize external lookups process-wide. Bail to a deferred 404
        # rather than hold the connection if the queue is busy.
        try:
            await asyncio.wait_for(_photo_sem.acquire(), timeout=_PHOTO_WAIT_BUDGET)
        except asyncio.TimeoutError:
            return _defer_cover()
        try:
            if photo_cooldown_active():
                # Cooldown was armed while we queued — don't fire.
                return _defer_cover()
            global _photo_last_call
            gap = time.monotonic() - _photo_last_call
            if gap < _PHOTO_MIN_INTERVAL:
                await asyncio.sleep(_PHOTO_MIN_INTERVAL - gap)
            try:
                resolved = await run_in_threadpool(_resolve_artist_sync, artist_id)
            finally:
                _photo_last_call = time.monotonic()
        except Exception as e:
            logger.error(f"artist photo resolution failed for {artist_id}: {e}")
            raise HTTPException(status_code=500, detail="photo resolution failed")
        finally:
            _photo_sem.release()

    if resolved is None:
        return _defer_cover()
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
