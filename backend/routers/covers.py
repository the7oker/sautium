"""Cover art endpoints.

Routes:

  GET /api/covers/{cover_id}             — direct UUID lookup, instant.
  GET /api/covers/by-media/{media_file_id}
                                          — lazy resolution: extract from
                                            disk, fall back to Last.fm,
                                            cache result, mark sentinel
                                            on permanent failure.
  GET /api/covers/caa/{rg_mbid}          — a phantom album's Cover Art
                                            Archive front image, fronted
                                            with a cache policy the browser
                                            can use (see _art_lookup).
  GET /api/covers/art?url=…              — any other outside cover the
                                            browser was handed (a streamed
                                            track's provider art), fetched
                                            only from _ART_SOURCES.

Resolved covers carry an `immutable` Cache-Control. The sentinel 404 is
served with a short max-age so a manual rescan / new disk file can
unblock the next request.
"""

import asyncio
import logging
import re
import time
import uuid
from collections import OrderedDict
from typing import Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException, Path as FPath, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy import text

from api_cooldown import cooling_down
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


# Covers from outside the node: a phantom album's Cover Art Archive front
# (albums.cover_url is a CAA hotlink for the whole phantom layer) and a
# streamed track's provider art (playback.queue.resolved_artwork). The
# browser asks the node for every one of them, never the image host: a
# browser on the LAN would tell that host its address, and an app's web view
# loads only the node's origin. Hotlinked, CAA also answers 307 →
# archive.org 302 → 200 with no Cache-Control (heuristic freshness only) and
# its 404s carry none either — so a shelf of phantom albums re-walked the
# redirects on every render and re-asked CAA for every artless release
# group. Fronted here, the browser keeps a cover for a month and a miss for
# a day. The bytes stay in a bounded process cache — the art is its
# source's, no covers row is minted for it.
_CAA_FRONT = "https://coverartarchive.org/release-group/{rg}/front-500"
_ART_MAX_AGE = 30 * 86400       # CAA's URL is not content-addressed: no `immutable`
_ART_MISS_MAX_AGE = 86400
_ART_CACHE_ENTRIES = 512
_ART_CACHE_BYTES = 24 * 2**20   # ≈ 400 covers at CAA's front-500 and Deezer's 1000 px
_ART_MAX_BYTES = 2 * 2**20      # one image; a provider's largest is ~0.3 MB
_ART_MAX_HOPS = 3               # CAA takes two: archive.org, then one of its data nodes
_RASTER = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"})

# What the node fetches outside art from: each source by the one URL shape it
# hands out, matched whole, and the hosts its redirects may lead to (None: a
# redirect is no art — Deezer's leads to its own blank placeholder). The
# browser can make the node fetch nothing else: no open proxy. A provider
# plugin whose art lives elsewhere shows no cover until its host is added
# here.
_ART_SOURCES = {
    "caa": (re.compile(r"https://coverartarchive\.org/release-group/[0-9a-f]{8}-[0-9a-f]{4}-"
                       r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/front-500"),
            re.compile(r"(?:[a-z0-9-]+\.)*archive\.org")),
    # Deezer's catalog art (cover_xl / cover_big off its public API), for the
    # core resolve and the bring-your-own lossless provider alike; the CDN's
    # older name is in phantoms minted before 2026-06-12.
    "deezer": (re.compile(r"https://(?:cdn-images|e-cdns-images)\.dzcdn\.net/images/cover/"
                          r"[0-9a-f]{32}/\d{2,4}x\d{2,4}-\d{6}-\d{1,3}-\d-\d\.jpg"), None),
    # The demo channel's matched video (streaming/youtube.py builds the URL).
    "youtube": (re.compile(r"https://i\.ytimg\.com/vi/[A-Za-z0-9_-]{11}/hqdefault\.jpg"), None),
}

_art_cache: "OrderedDict[str, tuple[int, str, bytes]]" = OrderedDict()
_art_cache_bytes = 0
_art_inflight: Dict[str, tuple[str, asyncio.Task]] = {}   # url -> (source, its fetch)
_refused_hosts: set = set()     # logged once each; capped — the route is open to the LAN

# Circuit breaker per source. CAA's images live on archive.org, which goes
# offline for hours at a time (2026-09-12: 503 "Temporarily Offline", 30 s
# connect stalls). Without a breaker every phantom cover on a cold node cost
# a 10 s upstream timeout, held one of the browser's ~6 connections to the
# origin meanwhile (artist photos share that pool), and was retried a minute
# later. Tripped by _ART_TRIP_FAILURES failures in a row — archive.org
# throws sporadic 503s while healthy, one proves nothing — the breaker lets
# ONE request through at a time as the probe and answers every other miss
# at once with the deferred 404; the first definite answer closes it. No
# timer and no api_cooldown row: a probe is the recovery signal here,
# whereas a rate-limit ban is what api_cooldown waits out.
_ART_TRIP_FAILURES = 3
_art_failures: Dict[str, int] = {}  # source -> consecutive upstream failures; a definite answer resets
_art_open: set = set()              # sources whose breaker is open


def _art_source(url: str) -> Optional[str]:
    """The source whose URL shape this is, or None — not one the node fetches."""
    for name, (shape, _hops) in _ART_SOURCES.items():
        if shape.fullmatch(url):
            return name
    return None


async def _art_fetch(source: str, url: str) -> tuple[int, str, bytes]:
    """(status, mime, body): 200 with the image; 404 for no art at that URL —
    the host's 404, a redirect off the source's hosts, a body that is no
    raster image or is over the size cap; 502 for anything else
    (unreachable, a 5xx) — never raises. Nothing of the person goes
    upstream: the node asks with no cookie, no referrer and its own
    address. While the source's breaker is open every probe fails the same
    way: the state change is the warning, the probes are debug noise."""
    hops = _ART_SOURCES[source][1]
    quiet = logging.DEBUG if source in _art_open else logging.WARNING
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            for _ in range(_ART_MAX_HOPS + 1):
                async with client.stream("GET", url) as resp:
                    nxt = resp.next_request
                    if nxt is not None:
                        if (hops is None or nxt.url.scheme != "https"
                                or not hops.fullmatch(nxt.url.host)):
                            logger.debug("%s redirects %s off its hosts, to %s", source, url, nxt.url)
                            return 404, "", b""
                        url = str(nxt.url)
                        continue
                    if resp.status_code in (404, 410):
                        return 404, "", b""
                    if resp.status_code != 200:
                        logger.log(quiet, "%s answered %s for %s", source, resp.status_code, url)
                        return 502, "", b""
                    mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if mime not in _RASTER:
                        logger.warning("%s served %r, not an image, for %s", source, mime, url)
                        return 404, "", b""
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if len(body) > _ART_MAX_BYTES:
                            logger.warning("%s cover over %d bytes: %s", source, _ART_MAX_BYTES, url)
                            return 404, "", b""
                    return (200, mime, bytes(body)) if body else (404, "", b"")
    except httpx.HTTPError as e:
        logger.log(quiet, "%s fetch failed for %s: %s %s", source, url, type(e).__name__, e)
        return 502, "", b""
    logger.warning("%s redirects more than %d times for %s", source, _ART_MAX_HOPS, url)
    return 404, "", b""


def _art_settle(source: str, status: int) -> None:
    """Feed one upstream outcome to the source's breaker."""
    if status in (200, 404):
        if source in _art_open:
            logger.info("%s reachable again after %d failed fetches", source, _art_failures[source])
            _art_open.discard(source)
        _art_failures[source] = 0
        return
    failures = _art_failures[source] = _art_failures.get(source, 0) + 1
    if source not in _art_open and failures >= _ART_TRIP_FAILURES:
        _art_open.add(source)
        logger.warning("%s unreachable (%d fetches failed in a row) — "
                       "probing one request at a time", source, failures)


def _art_remember(url: str, result: tuple[int, str, bytes]) -> None:
    global _art_cache_bytes
    _art_cache[url] = result
    _art_cache_bytes += len(result[2])
    while len(_art_cache) > _ART_CACHE_ENTRIES or _art_cache_bytes > _ART_CACHE_BYTES:
        _art_cache_bytes -= len(_art_cache.popitem(last=False)[1][2])


async def _art_fetch_settled(source: str, url: str) -> tuple[int, str, bytes]:
    try:
        result = await _art_fetch(source, url)
    finally:
        del _art_inflight[url]
    _art_settle(source, result[0])
    if result[0] in (200, 404):
        _art_remember(url, result)
    return result


async def _art_lookup(source: str, url: str) -> tuple[int, str, bytes]:
    """Cached, single-flight: a shelf on two devices asks the source once per
    cover. Only definite answers (200/404) are remembered. The fetch is a
    task of its own, so a browser that goes away mid-fetch strands no one
    waiting on the same cover."""
    hit = _art_cache.get(url)
    if hit is not None:
        _art_cache.move_to_end(url)
        return hit
    pending = _art_inflight.get(url)
    if pending is None:
        if source in _art_open and any(s == source for s, _ in _art_inflight.values()):
            return 503, "", b""     # the probe is already in flight
        pending = _art_inflight[url] = (source, asyncio.create_task(_art_fetch_settled(source, url)))
    return await asyncio.shield(pending[1])


def _art_response(result: tuple[int, str, bytes]) -> Response:
    status, mime, body = result
    if status == 200:
        return Response(content=body, media_type=mime,
                        headers={"Cache-Control": f"public, max-age={_ART_MAX_AGE}",
                                 "X-Content-Type-Options": "nosniff"})
    if status == 404:
        return _no_art()
    return _defer_cover()   # upstream trouble: the lazy <img> retries on a later render


def _no_art() -> Response:
    return Response(status_code=404, content=b"",
                    headers={"Cache-Control": f"public, max-age={_ART_MISS_MAX_AGE}"})


@router.get("/caa/{rg_mbid}")
async def get_cover_caa(rg_mbid: str = FPath(..., min_length=36, max_length=36)):
    try:
        rg = str(uuid.UUID(rg_mbid))
    except ValueError:
        raise HTTPException(status_code=404, detail="not a release group id")
    return _art_response(await _art_lookup("caa", _CAA_FRONT.format(rg=rg)))


@router.get("/art")
async def get_cover_art(url: str = Query(...)):
    """An outside cover the browser was handed — a streamed track's provider
    art, any phantom cover — fetched by the node if its URL has one of
    _ART_SOURCES' shapes; any other is no art, and nothing is fetched."""
    source = _art_source(url)
    if source is None:
        host = re.match(r"[a-z]+://([a-z0-9.-]{1,100})", url, re.IGNORECASE)
        host = host.group(1).lower() if host else "?"
        if host not in _refused_hosts and len(_refused_hosts) < 32:
            _refused_hosts.add(host)
            logger.info("cover refused: %s is not an image host the node fetches from", host)
        return _no_art()
    return _art_response(await _art_lookup(source, url))


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
    if cooling_down('deezer'):
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
            if cooling_down('deezer'):
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
