"""
REST API for HQPlayer control.

Mirrors MCP server patterns (lazy singleton, path conversion, tracker registration)
but exposed as HTTP endpoints for the Web UI.
"""

import asyncio
import json
import logging
import threading
import time
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import settings
from db_pool import db_query as _db_query, db_query_one as _db_query_one, get_conn as _get_conn
from hqplayer_client import HQPlayerClient, PlaybackState, format_time, file_path_to_uri
from lrclib import LrclibService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/player", tags=["player"])

# -- SSE infrastructure -------------------------------------------------------

_latest_status: dict = {"state": "disconnected"}
_status_version: int = 0
_status_changed = threading.Event()
_sse_clients: list = []           # list of (asyncio.Event, asyncio.AbstractEventLoop)
_sse_clients_lock = threading.Lock()
_poller_thread: Optional[threading.Thread] = None
_poller_running = False

# Playlist cache. Refreshed by the status poller every PLAYLIST_REFRESH_EVERY
# ticks (and immediately when a write command marks it stale via
# _invalidate_playlist). Served instantly by /api/player/playlist so the UI
# never waits on HQPlayer for the queue panel.
#
# `_playlist_version` is monotonically bumped only when the cached payload
# actually changes (deep-equality on the serialized track list). The value
# is embedded in every status payload so SSE clients can detect playlist
# mutations and refetch deterministically — no setTimeout/polling on the
# client side. Covers both our own write endpoints and external mutations
# (HQPDesktop UI, MCP, other clients) that the poller picks up on its
# periodic full refresh.
_latest_playlist: dict = {"tracks": [], "count": 0}
_playlist_version: int = 0
_playlist_dirty: bool = True   # force refresh on first poll
PLAYLIST_REFRESH_EVERY = 5      # otherwise refresh every N status polls


def _wake_sse_clients():
    """Thread-safe: signal all SSE async generators to send new data."""
    with _sse_clients_lock:
        for evt, loop in _sse_clients:
            loop.call_soon_threadsafe(evt.set)


def _status_poller():
    """Background thread: poll HQPlayer every ~1s, update cache, wake SSE clients.
    Also refreshes the playlist cache every PLAYLIST_REFRESH_EVERY ticks (or
    immediately when _playlist_dirty was set by a write endpoint)."""
    global _latest_status, _status_version
    tick = 0
    while _poller_running:
        try:
            with _hqp_status_lock:
                try:
                    hqp = _get_hqp_status()
                    status = hqp.get_status()
                except (BrokenPipeError, ConnectionError, OSError):
                    _reset_hqp_status()
                    hqp = _get_hqp_status()
                    status = hqp.get_status()

                # Playlist cache refresh — share the status socket
                if _playlist_dirty or (tick % PLAYLIST_REFRESH_EVERY == 0):
                    _refresh_playlist_cache()

            if status is None:
                new_data = {"state": "unknown"}
            else:
                state_names = {
                    PlaybackState.STOPPED: "stopped",
                    PlaybackState.PAUSED: "paused",
                    PlaybackState.PLAYING: "playing",
                    PlaybackState.STOPREQ: "stopping",
                }
                new_data = {
                    "state": state_names.get(status.state, "unknown"),
                    "artist": status.artist,
                    "album": status.album,
                    "song": status.song,
                    "genre": status.genre,
                    "position": status.position,
                    "length": status.length,
                    "volume": status.volume,
                    "track_index": status.track_index,
                    "progress_percent": round(status.progress_percent, 1),
                    "position_formatted": format_time(status.position),
                    "length_formatted": format_time(status.length),
                    "playlist_version": _playlist_version,
                }

            if new_data != _latest_status:
                _latest_status = new_data
                _status_version += 1
                _wake_sse_clients()

        except Exception:
            if _latest_status.get("state") != "disconnected":
                _latest_status = {"state": "disconnected"}
                _status_version += 1
                _wake_sse_clients()

        # Wait longer when disconnected to avoid log spam
        poll_interval = 5.0 if _latest_status.get("state") == "disconnected" else 1.0
        _status_changed.wait(timeout=poll_interval)
        _status_changed.clear()
        tick += 1


def _notify_update():
    """Wake poller for an immediate re-poll after a command."""
    _status_changed.set()


def start_status_poller():
    """Start the background status polling thread."""
    global _poller_thread, _poller_running
    if _poller_thread and _poller_thread.is_alive():
        return
    _poller_running = True
    _poller_thread = threading.Thread(target=_status_poller, daemon=True, name="sse-poller")
    _poller_thread.start()
    logger.info("SSE status poller started")


def stop_status_poller():
    """Stop the background status polling thread."""
    global _poller_running
    _poller_running = False
    _status_changed.set()  # unblock wait
    if _poller_thread:
        _poller_thread.join(timeout=3)
    logger.info("SSE status poller stopped")


# -- Lazy singletons ----------------------------------------------------------
#
# Two independent HQPlayer connections so the background status poller and
# user-initiated commands never contend for the same socket / lock:
#
#   * `_hqp_status_client` + `_hqp_status_lock`
#       Owned exclusively by `_status_poller`. Fast 2 s socket timeout so a
#       lagging HQPlayer is detected quickly without freezing the rest of
#       the request flow.
#
#   * `_hqp_client` + `_hqp_lock`
#       Owned by all command endpoints (play / pause / next / play-track /
#       /api/player/playlist etc). 5 s timeout for write operations that
#       may take longer to acknowledge (e.g. play-album loads many URIs).
#
# HQPlayer's control API accepts multiple concurrent TCP clients, so the
# two sockets co-exist cleanly on the HQP side.

_hqp_client: Optional[HQPlayerClient] = None
_hqp_lock = threading.Lock()  # cmd commands

_hqp_status_client: Optional[HQPlayerClient] = None
_hqp_status_lock = threading.Lock()  # status poller


def _make_client(timeout: float) -> HQPlayerClient:
    return HQPlayerClient(
        host=settings.hqplayer_host,
        port=settings.hqplayer_port,
        timeout=timeout,
    )


def _ensure_connected(client: Optional[HQPlayerClient], timeout: float, label: str
                      ) -> HQPlayerClient:
    """Return a healthy HQPlayer client; reconnect if the cached one is stale.

    Caller must hold the appropriate lock. `client` is the previous instance
    (may be None). Returns the (possibly new) instance. Raises ConnectionError
    if the connection cannot be established.
    """
    need_reconnect = client is None or not client.is_connected()

    # Detect remote-side close by peeking the socket.
    if not need_reconnect and client and client.socket:
        import select
        try:
            ready = select.select([client.socket], [], [], 0)
            if ready[0]:
                peek = client.socket.recv(1, 0x02)  # MSG_PEEK
                if not peek:
                    logger.info(f"HQPlayer ({label}) closed by remote, reconnecting...")
                    need_reconnect = True
        except Exception:
            need_reconnect = True

    if need_reconnect:
        if client:
            try:
                client.disconnect()
            except Exception:
                pass
        client = _make_client(timeout=timeout)
        if not client.connect():
            raise ConnectionError(
                f"Cannot connect to HQPlayer at {settings.hqplayer_host}:{settings.hqplayer_port}"
            )
        logger.info(f"HQPlayer ({label}) connected")
    return client


def _get_hqp() -> HQPlayerClient:
    """Get or create HQPlayer command client. Must be called inside _hqp_lock."""
    global _hqp_client
    _hqp_client = _ensure_connected(_hqp_client, timeout=5.0, label="cmd")
    return _hqp_client


def _get_hqp_status() -> HQPlayerClient:
    """Get or create HQPlayer status-poller client. Must be called inside _hqp_status_lock."""
    global _hqp_status_client
    _hqp_status_client = _ensure_connected(_hqp_status_client, timeout=2.0, label="status")
    return _hqp_status_client


def _reset_hqp():
    """Force-close HQPlayer command client so next _get_hqp() reconnects."""
    global _hqp_client
    if _hqp_client:
        try:
            _hqp_client.disconnect()
        except Exception:
            pass
        _hqp_client = None


def _reset_hqp_status():
    """Force-close HQPlayer status client so next _get_hqp_status() reconnects."""
    global _hqp_status_client
    if _hqp_status_client:
        try:
            _hqp_status_client.disconnect()
        except Exception:
            pass
        _hqp_status_client = None


def _hqp_cmd(func):
    """Execute a function with HQPlayer client under lock. Auto-reconnects on broken pipe."""
    with _hqp_lock:
        try:
            hqp = _get_hqp()
            return func(hqp)
        except (BrokenPipeError, ConnectionError, OSError) as e:
            logger.warning(f"HQPlayer connection lost ({e}), reconnecting...")
            _reset_hqp()
            hqp = _get_hqp()
            return func(hqp)




def _register_playlist(track_ids: list[int]) -> bool:
    """Register playlist mapping with playback tracker daemon."""
    try:
        playlist_mapping = {str(i): tid for i, tid in enumerate(track_ids)}
        with httpx.Client(timeout=2.0) as client:
            resp = client.post(
                f"{settings.tracker_url}/playlist",
                json={"playlist": playlist_mapping},
            )
            resp.raise_for_status()
            logger.info(f"Registered playlist with tracker: {len(track_ids)} tracks")
            return True
    except Exception as e:
        logger.warning(f"Failed to register playlist with tracker: {e}")
        return False


# -- Request models -----------------------------------------------------------

class VolumeRequest(BaseModel):
    level: float

class SearchRequest(BaseModel):
    query: str
    limit: int = 30

class PlayTrackRequest(BaseModel):
    track_id: int

class PlayAlbumRequest(BaseModel):
    album_name: str
    artist_name: str = ""

class PlaySimilarRequest(BaseModel):
    track_id: int
    limit: int = 15

class PlayTracksRequest(BaseModel):
    track_ids: list[int]

class QueueNextRequest(BaseModel):
    track_id: int
    limit: int = 5
    exclude_ids: list[int] = []

class QueueTracksRequest(BaseModel):
    track_ids: list[int]

class JumpRequest(BaseModel):
    index: int  # 1-based, matches HQPlayer's select_track convention

class RemoveRequest(BaseModel):
    index: int  # 1-based — HQPlayer's PlaylistRemove convention

class ReorderRequest(BaseModel):
    order: list[int]  # full new order of media_file_ids, current track included at its original position


# -- Search -------------------------------------------------------------------

@router.get("/search")
async def search_tracks(q: str = "", limit: int = 20):
    """Two-stage search grouped by albums: exact ILIKE first, fuzzy trigram fallback."""
    limit = min(limit, 100)
    q = q.strip()
    if not q:
        return {"albums": [], "count": 0}

    # Stage 1: Exact ILIKE — split into words, all must match in at least one field
    words = q.split()
    word_conditions = []
    params: dict = {"limit": limit * 10}  # Get more tracks for grouping
    for i, word in enumerate(words):
        key = f"w{i}"
        params[key] = f"%{word}%"
        word_conditions.append(
            f"(a.name ILIKE %({key})s OR al.title ILIKE %({key})s OR t.title ILIKE %({key})s)"
        )
    where_exact = " AND ".join(word_conditions)

    rows = _db_query(f"""
        SELECT mf.id, t.title, mf.track_number, mf.disc_number, mf.duration_seconds,
               a.name as artist, av.id as album_id, al.title as album,
               (SELECT g.name FROM track_genres tg JOIN genres g ON tg.genre_id = g.id
                WHERE tg.track_id = t.id LIMIT 1) as genre,
               mf.is_lossless, t.id as track_id
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN album_variants av ON mf.album_variant_id = av.id
        JOIN albums al ON av.album_id = al.id
        WHERE {where_exact}
        ORDER BY a.name, al.title, mf.disc_number, mf.track_number
    """, params)

    if not rows:
        # Stage 2: Fuzzy trigram fallback
        params = {"query": q, "query_like": f"%{q}%", "limit": limit * 10}
        rows = _db_query("""
            SELECT mf.id, t.title, mf.track_number, mf.disc_number, mf.duration_seconds,
                   a.name as artist, av.id as album_id, al.title as album,
                   (SELECT g.name FROM track_genres tg JOIN genres g ON tg.genre_id = g.id
                    WHERE tg.track_id = t.id LIMIT 1) as genre,
                   mf.is_lossless, t.id as track_id,
                   GREATEST(
                       similarity(a.name, %(query)s),
                       similarity(al.title, %(query)s),
                       similarity(t.title, %(query)s)
                   ) as _score
            FROM media_files mf
            JOIN tracks t ON mf.track_id = t.id
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a ON ta.artist_id = a.id
            JOIN album_variants av ON mf.album_variant_id = av.id
            JOIN albums al ON av.album_id = al.id
            WHERE similarity(a.name, %(query)s) > 0.25
               OR similarity(al.title, %(query)s) > 0.25
               OR similarity(t.title, %(query)s) > 0.25
            ORDER BY _score DESC, a.name, al.title, mf.disc_number, mf.track_number
        """, params)

    # Group by album (merge multi-disc albums, but keep lossless/lossy separate)
    # Deduplicate tracks that appear in multiple album_variants of the same album
    albums_dict = {}
    seen_tracks = {}  # key -> set of track_ids already added
    for row in rows:
        key = (row["artist"], row["album"], row["is_lossless"])
        if key not in albums_dict:
            albums_dict[key] = {
                "artist": row["artist"],
                "album": row["album"],
                "album_id": row["album_id"],
                "genre": row["genre"],
                "is_lossless": row["is_lossless"],
                "tracks": [],
            }
            seen_tracks[key] = set()
        track_id = row.get("track_id")
        if track_id and track_id in seen_tracks[key]:
            continue
        if track_id:
            seen_tracks[key].add(track_id)
        albums_dict[key]["tracks"].append({
            "id": row["id"],
            "title": row["title"],
            "track_number": row["track_number"],
            "disc_number": row["disc_number"],
            "duration_seconds": row["duration_seconds"],
        })

    # Calculate totals and limit to requested album count
    albums = []
    for i, album_data in enumerate(list(albums_dict.values())[:limit]):
        album_data["album_id"] = i  # Unique index per group for DOM IDs (DB album_id may collide)
        album_data["track_count"] = len(album_data["tracks"])
        album_data["total_duration"] = sum(t["duration_seconds"] or 0 for t in album_data["tracks"])
        albums.append(album_data)

    return {"albums": albums, "count": len(albums)}


# -- Transport controls -------------------------------------------------------

def _build_playlist_payload(hqp_tracks: list) -> dict:
    """Convert HQPlayer raw playlist into the JSON shape served to the UI.
    Pure transform — no HQPlayer or socket I/O."""
    if not hqp_tracks:
        return {"tracks": [], "count": 0}

    # Convert URIs to DB paths in bulk
    path_to_idx: dict[str, list[int]] = {}
    idx_to_hqp: dict[int, dict] = {}
    for idx, hqp_track in enumerate(hqp_tracks):
        uri = hqp_track["uri"]
        idx_to_hqp[idx] = hqp_track

        if uri.startswith("file:///"):
            db_path = uri[8:]
        elif uri.startswith("file://"):
            db_path = uri[7:]
        else:
            continue

        db_path = db_path.replace("\\", "/")
        db_path = db_path.replace("%5B", "[").replace("%5D", "]")
        path_to_idx.setdefault(db_path, []).append(idx)

    all_paths = list(path_to_idx.keys())
    db_rows_by_path: dict[str, dict] = {}
    if all_paths:
        rows_batch = _db_query("""
            SELECT mf.id, mf.file_path, t.title, mf.track_number,
                   mf.duration_seconds, mf.cover_id::text AS cover_id,
                   a.name as artist
            FROM media_files mf
            JOIN tracks t ON mf.track_id = t.id
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a ON ta.artist_id = a.id
            WHERE mf.file_path = ANY(%(paths)s)
        """, {"paths": all_paths})
        for r in rows_batch:
            db_rows_by_path[r["file_path"]] = r

    tracks_with_info = []
    for idx in range(len(hqp_tracks)):
        hqp_track = idx_to_hqp.get(idx)
        if hqp_track is None:
            continue

        row = None
        for path, indices in path_to_idx.items():
            if idx in indices:
                row = db_rows_by_path.get(path)
                break

        if row:
            tracks_with_info.append({
                "id": row["id"],
                "title": row["title"],
                "track_number": row["track_number"],
                "artist": row["artist"],
                "duration_seconds": (
                    float(row["duration_seconds"])
                    if row["duration_seconds"] is not None else None
                ),
                "cover_id": row["cover_id"],
                "index": idx,
            })
        else:
            tracks_with_info.append({
                "id": None,
                "title": hqp_track["song"] or "Unknown",
                "track_number": None,
                "artist": hqp_track["artist"] or "Unknown",
                "duration_seconds": None,
                "cover_id": None,
                "index": idx,
            })

    return {"tracks": tracks_with_info, "count": len(tracks_with_info)}


def _refresh_playlist_cache():
    """Pull current playlist from HQPlayer (status socket) and update cache.
    Caller must hold _hqp_status_lock. Bumps `_playlist_version` only when
    the new payload differs from the cached one — clients use the version
    bump as a deterministic signal to refetch."""
    global _latest_playlist, _playlist_dirty, _playlist_version
    try:
        hqp = _get_hqp_status()
        hqp_tracks = hqp.get_playlist()
        new_payload = _build_playlist_payload(hqp_tracks)
        if new_payload != _latest_playlist:
            _latest_playlist = new_payload
            _playlist_version += 1
        _playlist_dirty = False
    except (BrokenPipeError, ConnectionError, OSError) as e:
        logger.debug(f"Playlist refresh failed ({e})")
        _reset_hqp_status()


def _invalidate_playlist():
    """Mark playlist cache stale; the poller will pull a fresh copy on its
    next tick. Called from write endpoints (play_track / play_album / etc)
    after the user mutates the queue."""
    global _playlist_dirty
    _playlist_dirty = True
    _status_changed.set()  # wake poller immediately


def _force_refresh_playlist_after_write():
    """Synchronously refresh the playlist cache after a write so the
    response carries the new state. The async invalidate-and-wake-poller
    pattern races with optimistic UI: the frontend issues fetchPlaylist
    immediately on response, hits the still-stale cache, and overwrites
    the optimistic update with old data — a visible revert. Doing the
    refresh here on the status socket (different lock + connection from
    the cmd path) blocks the response by ~50–150ms but guarantees that
    any cache reader after this point sees the post-mutation state."""
    try:
        with _hqp_status_lock:
            _refresh_playlist_cache()
    except Exception as e:
        logger.warning(f"force playlist refresh failed: {e}")
    _status_changed.set()  # propagate the bumped playlist_version via SSE


@router.get("/playlist")
def get_playlist():
    """Return last cached playlist. Refreshed by the status poller every
    PLAYLIST_REFRESH_EVERY ticks and immediately on write-command-driven
    invalidation. Always instant — never blocks on HQPlayer."""
    return _latest_playlist


@router.get("/status/stream")
async def status_stream():
    """SSE endpoint: pushes status updates in real-time."""
    loop = asyncio.get_event_loop()
    evt = asyncio.Event()

    async def event_generator():
        last_version = -1
        try:
            with _sse_clients_lock:
                _sse_clients.append((evt, loop))

            # Send current status immediately
            yield f"data: {json.dumps(_latest_status)}\n\n"
            last_version = _status_version

            while True:
                try:
                    await asyncio.wait_for(evt.wait(), timeout=15.0)
                    evt.clear()
                except asyncio.TimeoutError:
                    # Keepalive
                    yield ": keepalive\n\n"
                    continue

                if _status_version != last_version:
                    last_version = _status_version
                    yield f"data: {json.dumps(_latest_status)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            with _sse_clients_lock:
                _sse_clients[:] = [(e, l) for e, l in _sse_clients if e is not evt]

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/status")
def get_status():
    """Return last cached HQPlayer status. Cache is maintained by the
    background _status_poller (1 s tick) on its own dedicated socket so
    this endpoint never hits HQPlayer in the request path — it's always
    instant regardless of HQPlayer responsiveness."""
    return _latest_status


# Legacy synchronous status (rarely needed; UI uses cached /status above
# and SSE for live updates). Kept for tools that explicitly want a fresh
# read direct from HQPlayer.
@router.get("/status/fresh")
def get_status_fresh():
    """Force a fresh HQPlayer status read. Slower (may hang up to socket
    timeout if HQPlayer is unresponsive)."""
    try:
        with _hqp_lock:
            hqp = _get_hqp()
            status = hqp.get_status()
        if status is None:
            return {"state": "unknown"}

        state_names = {
            PlaybackState.STOPPED: "stopped",
            PlaybackState.PAUSED: "paused",
            PlaybackState.PLAYING: "playing",
            PlaybackState.STOPREQ: "stopping",
        }

        return {
            "state": state_names.get(status.state, "unknown"),
            "artist": status.artist,
            "album": status.album,
            "song": status.song,
            "genre": status.genre,
            "position": status.position,
            "length": status.length,
            "volume": status.volume,
            "track_index": status.track_index,
            "progress_percent": round(status.progress_percent, 1),
            "position_formatted": format_time(status.position),
            "length_formatted": format_time(status.length),
        }
    except Exception:
        return {"state": "disconnected"}


@router.get("/now-playing-detail")
def now_playing_detail(media_file_id: int):
    """Aggregated rich payload for the Now Playing screen.

    Combines media-file metadata (format, sample rate, bit depth, cover),
    track-level audio features (BPM, key, mode, energy, instruments),
    album info, and the top genres into one roundtrip. Called by the
    frontend whenever the playing track changes.
    """
    row = _db_query_one("""
        SELECT mf.id AS media_file_id,
               mf.track_id::text AS track_id,
               mf.cover_id::text AS cover_id,
               mf.file_format,
               mf.is_lossless,
               mf.sample_rate,
               mf.bit_depth,
               mf.duration_seconds,
               t.title,
               af.bpm,
               af.key,
               af.mode,
               af.energy,
               af.energy_db,
               af.danceability,
               af.instruments,
               af.moods,
               av.album_id::text AS album_id,
               al.title AS album_title,
               al.release_year AS year
        FROM media_files mf
        JOIN tracks t ON t.id = mf.track_id
        LEFT JOIN audio_features af ON af.track_id = t.id
        JOIN album_variants av ON av.id = mf.album_variant_id
        JOIN albums al ON al.id = av.album_id
        WHERE mf.id = %(id)s
    """, {"id": media_file_id})

    if not row:
        raise HTTPException(status_code=404, detail="media_file not found")

    row["genres"] = _db_query("""
        SELECT g.id::text, g.name
        FROM track_genres tg
        JOIN genres g ON g.id = tg.genre_id
        WHERE tg.track_id = %(t)s::uuid
        ORDER BY g.name
        LIMIT 3
    """, {"t": row["track_id"]})

    row["primary_artist"] = _db_query_one("""
        SELECT a.id::text, a.name
        FROM artists a
        JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
        WHERE ta.track_id = %(t)s::uuid
        LIMIT 1
    """, {"t": row["track_id"]})

    sr = row.get("sample_rate") or 0
    bd = row.get("bit_depth") or 0
    if row.get("is_lossless") and sr >= 48000 and bd >= 24:
        row["quality"] = "hi-res"
    elif row.get("is_lossless"):
        row["quality"] = "lossless"
    else:
        row["quality"] = "lossy"

    return row


@router.post("/play")
def play():
    try:
        result = {"ok": _hqp_cmd(lambda h: h.play())}
        _notify_update()
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/pause")
def pause():
    try:
        result = {"ok": _hqp_cmd(lambda h: h.pause())}
        _notify_update()
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/stop")
def stop():
    try:
        result = {"ok": _hqp_cmd(lambda h: h.stop())}
        _notify_update()
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/next")
def next_track():
    try:
        result = {"ok": _hqp_cmd(lambda h: h.next())}
        _notify_update()
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/previous")
def previous_track():
    try:
        result = {"ok": _hqp_cmd(lambda h: h.previous())}
        _notify_update()
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/volume/up")
def volume_up():
    try:
        result = {"ok": _hqp_cmd(lambda h: h.volume_up())}
        _notify_update()
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/volume/down")
def volume_down():
    try:
        result = {"ok": _hqp_cmd(lambda h: h.volume_down())}
        _notify_update()
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/volume")
def set_volume(req: VolumeRequest):
    try:
        result = {"ok": _hqp_cmd(lambda h: h.set_volume(req.level))}
        _notify_update()
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# -- Smart play ----------------------------------------------------------------

@router.post("/remove")
def remove(req: RemoveRequest):
    """Remove a track from the current HQPlayer playlist by index.

    `index` is 1-based to match HQPlayer's `PlaylistRemove`. After
    removal the playlist cache is invalidated so the next status
    poll refreshes it; the playback-tracker mapping naturally
    becomes stale (indices shift) but is rebuilt by the next
    play-track / play-album call. The play_count for the removed
    slot is unaffected — tracker records past plays, not pending
    queue contents.

    Reorder is intentionally not implemented. HQPlayer's Control
    API exposes add (append-only), clear and remove — there is no
    insert-at-index or move primitive. Implementing reorder would
    require a clear+rebuild round-trip that interrupts the
    currently playing track. If the protocol gains a move
    operation, expose it here.
    """
    if req.index < 1:
        raise HTTPException(status_code=400, detail="index must be >= 1")
    try:
        ok = _hqp_cmd(lambda h: h.playlist_remove(req.index))
        _invalidate_playlist()
        return {"ok": ok, "index": req.index}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/reorder")
def reorder(req: ReorderRequest):
    """Reorder the playlist around the currently playing track.

    HQPlayer's Control API only exposes append (PlaylistAdd) and
    remove (PlaylistRemove) — no insert, no move. The trick is to
    never touch the slot HQPlayer is reading from: as long as we
    only append to the end and remove non-current slots, audio
    plays through the rebuild without a glitch.

    Effects of each primitive on the current slot's index K:
      append(uri)         — playlist grows; K unchanged.
      remove(i), i  < K   — playlist shrinks before K; K -= 1.
      remove(i), i == K   — current slot deleted; AUDIO CUTS OFF.
      remove(i), i  > K   — playlist shrinks after K; K unchanged.

    So K can shift LEFT (via removes before it) or stay; it cannot
    shift RIGHT, and tracks cannot be inserted before it. That
    constrains what reorders can be done seamlessly:

      new_before must be a subsequence of old_before (we can only
      drop tracks from the front; we cannot insert or permute) AND
      new_after must equal (old_after ∪ tracks dropped from before),
      with arbitrary ordering.

    When the request fits these constraints we run the zero-
    interrupt path. Anything else (swapping tracks inside before,
    pulling a track from after into before, shifting current right)
    is impossible without an insert primitive, so we fall back to
    a full clear+rebuild + select_track + seek (~300 ms gap).
    """
    if not req.order:
        raise HTTPException(status_code=400, detail="order is empty")

    try:
        with _hqp_status_lock:
            _refresh_playlist_cache()
        current_payload = _latest_playlist
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"playlist read failed: {e}")
    current_tracks = current_payload.get("tracks") or []
    current_ids = [t.get("id") for t in current_tracks]

    status_idx = _latest_status.get("track_index") or 0
    if status_idx < 1 or status_idx > len(current_ids):
        raise HTTPException(
            status_code=409,
            detail="reorder requires a currently playing track; use /play-tracks",
        )

    if len(req.order) != len(current_ids):
        raise HTTPException(
            status_code=400,
            detail="order length must match current playlist length",
        )

    from collections import Counter
    if Counter(req.order) != Counter(current_ids):
        raise HTTPException(
            status_code=400,
            detail="order must be a permutation of the current playlist",
        )

    current_id = current_ids[status_idx - 1]
    new_status_idx = req.order.index(current_id) + 1

    old_before = current_ids[:status_idx - 1]
    old_after = current_ids[status_idx:]
    new_before = req.order[:new_status_idx - 1]
    new_after = req.order[new_status_idx:]

    if old_before == new_before and old_after == new_after:
        return {
            "ok": True, "removed": 0, "added": 0,
            "anchor_index": status_idx, "interrupted": False,
        }

    # Zero-interrupt feasibility: new_before must be a subsequence
    # of old_before (we can only drop tracks, never reorder or
    # add to before), and new_after's multiset must match what we
    # have available — original after-segment plus any tracks the
    # before-shrink drops out.
    def is_subsequence(needle: list, haystack: list) -> bool:
        j = 0
        for h in haystack:
            if j < len(needle) and needle[j] == h:
                j += 1
        return j == len(needle)

    expected_after = Counter(old_after) + Counter(old_before) - Counter(new_before)
    seamless = (
        is_subsequence(new_before, old_before)
        and Counter(new_after) == expected_after
    )

    # Resolve file paths for tracks we may need to append. Seamless
    # path only appends new_after; interrupt path rebuilds everything.
    ids_needing_paths = new_after if seamless else req.order
    rows = _db_query("""
        SELECT mf.id, mf.file_path
        FROM media_files mf
        WHERE mf.id = ANY(%(ids)s)
    """, {"ids": ids_needing_paths})
    path_by_id = {r["id"]: r["file_path"] for r in rows}
    missing = [mid for mid in ids_needing_paths if mid not in path_by_id]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"unknown media_file_ids: {missing[:5]}",
        )

    if seamless:
        # Sequence (current at slot K = status_idx throughout, then
        # shifts left as we shrink the before-segment in step 3):
        #
        # 1. Empty the after-segment by repeatedly removing slot K+1.
        #    K never changes: every remove targets a slot above K.
        # 2. Append new_after in target order. All appends land at the
        #    very end, after the current slot, so K stays put.
        # 3. Walk old_before slot by slot. Tracks present in new_before
        #    (matched in subsequence order) stay; the rest are removed
        #    by remove(slot). Each kept track advances our cursor;
        #    each removal keeps the cursor where it is (since slots
        #    above shift down to take its place). When the last
        #    "drop" track is removed, K has shifted down by exactly
        #    (len(old_before) - len(new_before)), landing on
        #    new_status_idx as required.
        try:
            with _hqp_lock:
                hqp = _get_hqp()
                for _ in range(len(old_after)):
                    hqp.playlist_remove(status_idx + 1)
                for mid in new_after:
                    hqp.playlist_add(file_path_to_uri(path_by_id[mid]))
                cursor = 1
                new_before_remaining = list(new_before)
                for old_track in old_before:
                    if (new_before_remaining
                            and new_before_remaining[0] == old_track):
                        new_before_remaining.pop(0)
                        cursor += 1
                    else:
                        hqp.playlist_remove(cursor)
            _register_playlist(req.order)
            _force_refresh_playlist_after_write()
            return {
                "ok": True,
                "removed": len(old_after) + (len(old_before) - len(new_before)),
                "added": len(new_after),
                "anchor_index": new_status_idx,
                "interrupted": False,
            }
        except Exception as e:
            logger.error(f"reorder (seamless) failed: {e}", exc_info=True)
            raise HTTPException(status_code=503, detail=str(e))

    # Fallback: clear+rebuild. Required for cases that need an
    # insert (swap inside before, after→before crossing, current
    # right-shift). hqp.stop() is mandatory — HQPlayer ignores
    # PlaylistAdd(clear=True) while playing, silently appending
    # instead, which would double every track and land
    # select_track on the wrong slot.
    position = int(_latest_status.get("position") or 0)
    prev_state = _latest_status.get("state")
    should_resume = prev_state in ("playing", "paused")
    try:
        with _hqp_lock:
            hqp = _get_hqp()
            hqp.stop()
            hqp.playlist_add(
                file_path_to_uri(path_by_id[req.order[0]]), clear=True)
            for mid in req.order[1:]:
                hqp.playlist_add(file_path_to_uri(path_by_id[mid]))
            hqp.select_track(new_status_idx)
            if position > 0:
                hqp.seek(position)
            if should_resume:
                hqp.play()
        _register_playlist(req.order)
        _force_refresh_playlist_after_write()
        return {
            "ok": True,
            "removed": len(current_ids),
            "added": len(req.order),
            "anchor_index": new_status_idx,
            "interrupted": True,
        }
    except Exception as e:
        logger.error(f"reorder (rebuild) failed: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/jump")
def jump(req: JumpRequest):
    """Jump to a specific position in the current HQPlayer playlist.

    `index` is 1-based to match HQPlayer's own `select_track` API and
    the SSE `track_index` field. Used by the Queue sheet to play a
    specific track without rebuilding the playlist."""
    if req.index < 1:
        raise HTTPException(status_code=400, detail="index must be >= 1")
    try:
        ok = _hqp_cmd(lambda h: h.select_track(req.index))
        if ok:
            _hqp_cmd(lambda h: h.play())
        _notify_update()
        return {"ok": ok, "index": req.index}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/play-track")
def play_track(req: PlayTrackRequest):
    """Clear playlist, add single track, play, register with tracker."""
    row = _db_query_one("""
        SELECT mf.file_path, t.title, a.name as artist, al.title as album
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN album_variants av ON mf.album_variant_id = av.id
        JOIN albums al ON av.album_id = al.id
        WHERE mf.id = %(track_id)s
    """, {"track_id": req.track_id})

    if not row:
        raise HTTPException(status_code=404, detail="Track not found")

    try:
        uri = file_path_to_uri(row["file_path"])
        with _hqp_lock:
            hqp = _get_hqp()
            hqp.stop()
            hqp.playlist_add(uri, clear=True)
            # Register BEFORE play() so the tracker has the index→track_id
            # map by the time HQPlayer emits its first status update —
            # otherwise the now-playing scrobble is silently dropped.
            _register_playlist([req.track_id])
            hqp.play()
        _invalidate_playlist()
        _notify_update()

        return {
            "ok": True,
            "artist": row["artist"],
            "title": row["title"],
            "album": row["album"],
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/play-album")
def play_album(req: PlayAlbumRequest):
    """Fuzzy-match album, load all tracks, play."""
    match_conditions = [
        "(similarity(al.title, %(album)s) > 0.15 OR al.title ILIKE %(album_like)s)"
    ]
    match_params: dict = {"album": req.album_name, "album_like": f"%{req.album_name}%"}
    order_parts = ["similarity(al.title, %(album)s)"]

    if req.artist_name:
        match_conditions.append(
            "(similarity(a.name, %(artist)s) > 0.15 OR a.name ILIKE %(artist_like)s)"
        )
        match_params["artist"] = req.artist_name
        match_params["artist_like"] = f"%{req.artist_name}%"
        order_parts.append("similarity(a.name, %(artist)s)")

    match_where = " AND ".join(match_conditions)
    order_expr = " + ".join(order_parts)

    best_album = _db_query_one(f"""
        SELECT al.id, al.title as album, a.name as artist, {order_expr} as _score
        FROM albums al
        JOIN album_variants av ON av.album_id = al.id
        JOIN media_files mf ON mf.album_variant_id = av.id
        JOIN tracks t ON mf.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        WHERE {match_where}
        GROUP BY al.id, al.title, a.name
        ORDER BY _score DESC
        LIMIT 1
    """, match_params)

    if not best_album:
        raise HTTPException(status_code=404, detail=f"Album '{req.album_name}' not found")

    rows = _db_query("""
        SELECT mf.id, mf.file_path, t.title, mf.track_number,
               a.name as artist, al.title as album
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN album_variants av ON mf.album_variant_id = av.id
        JOIN albums al ON av.album_id = al.id
        WHERE al.id = %(album_id)s
        ORDER BY mf.disc_number, mf.track_number
    """, {"album_id": best_album["id"]})

    if not rows:
        raise HTTPException(status_code=404, detail="Album has no tracks")

    try:
        with _hqp_lock:
            hqp = _get_hqp()
            hqp.stop()
            hqp.playlist_add(file_path_to_uri(rows[0]["file_path"]), clear=True)
            for row in rows[1:]:
                hqp.playlist_add(file_path_to_uri(row["file_path"]))
            track_ids = [r["id"] for r in rows]
            _register_playlist(track_ids)
            hqp.play()

        _invalidate_playlist()
        _notify_update()

        return {
            "ok": True,
            "artist": rows[0]["artist"],
            "album": rows[0]["album"],
            "track_count": len(rows),
            "tracks": [
                {"id": r["id"], "title": r["title"], "track_number": r.get("track_number")}
                for r in rows
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/play-similar")
def play_similar(req: PlaySimilarRequest):
    """Find similar tracks via pgvector cosine search, queue and play."""
    # Get track_id from the media_file
    source = _db_query_one("""
        SELECT mf.id, t.id as db_track_id
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        WHERE mf.id = %(track_id)s
    """, {"track_id": req.track_id})

    if not source:
        raise HTTPException(status_code=404, detail="Track not found")

    rows = _db_query("""
        WITH target AS (
            SELECT e.vector
            FROM embeddings e
            WHERE e.track_id = %(db_track_id)s
        )
        SELECT mf_rep.id, mf_rep.file_path, t.title, a.name as artist,
               mf_rep.album_title as album,
               1 - (e.vector <=> (SELECT vector FROM target)) as similarity
        FROM tracks t
        JOIN embeddings e ON e.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN LATERAL (
            SELECT mf.id, mf.file_path, mf.duration_seconds, mf.track_number,
                   mf.sample_rate, mf.bit_depth, mf.is_lossless,
                   al.title as album_title
            FROM media_files mf
            JOIN album_variants av ON mf.album_variant_id = av.id
            JOIN albums al ON av.album_id = al.id
            WHERE mf.track_id = t.id
            ORDER BY mf.is_analysis_source DESC, mf.id
            LIMIT 1
        ) mf_rep ON true
        WHERE t.id != %(db_track_id)s
        ORDER BY e.vector <=> (SELECT vector FROM target)
        LIMIT %(limit)s
    """, {"db_track_id": source["db_track_id"], "limit": req.limit})

    if not rows:
        raise HTTPException(status_code=404, detail="No similar tracks found")

    try:
        with _hqp_lock:
            hqp = _get_hqp()
            hqp.stop()
            hqp.playlist_add(file_path_to_uri(rows[0]["file_path"]), clear=True)
            for row in rows[1:]:
                hqp.playlist_add(file_path_to_uri(row["file_path"]))
            track_ids = [r["id"] for r in rows]
            _register_playlist(track_ids)
            hqp.play()

        _invalidate_playlist()
        _notify_update()

        return {
            "ok": True,
            "count": len(rows),
            "tracks": [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "artist": r["artist"],
                    "album": r["album"],
                    "similarity": round(float(r["similarity"]), 3),
                }
                for r in rows
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/play-tracks")
def play_tracks(req: PlayTracksRequest):
    """Play multiple tracks by IDs."""
    if not req.track_ids:
        raise HTTPException(status_code=400, detail="No track IDs provided")

    rows = _db_query("""
        SELECT mf.id, mf.file_path, t.title, a.name as artist, al.title as album
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN album_variants av ON mf.album_variant_id = av.id
        JOIN albums al ON av.album_id = al.id
        WHERE mf.id = ANY(%(ids)s)
        ORDER BY array_position(%(ids)s, mf.id)
    """, {"ids": req.track_ids})

    if not rows:
        raise HTTPException(status_code=404, detail="No tracks found")

    try:
        with _hqp_lock:
            hqp = _get_hqp()
            logger.info(f"play-tracks: stopping playback")
            hqp.stop()
            first_path = file_path_to_uri(rows[0]["file_path"])
            logger.info(f"play-tracks: adding first track (clear=True): {first_path}")
            result = hqp.playlist_add(first_path, clear=True)
            logger.info(f"play-tracks: playlist_add result: {result}")
            for i, row in enumerate(rows[1:], 2):
                path = file_path_to_uri(row["file_path"])
                hqp.playlist_add(path)
            logger.info(f"play-tracks: added {len(rows)} tracks total")
            track_ids = [r["id"] for r in rows]
            _register_playlist(track_ids)
            hqp.play()

            # Verify playback started; if first track fails (e.g. [Vinyl] path),
            # try skipping to next tracks until one plays
            time.sleep(0.5)
            status = hqp.get_status()
            if status and status.state == PlaybackState.STOPPED and len(rows) > 1:
                logger.warning("play-tracks: first track didn't start, trying next tracks")
                for skip_idx in range(2, min(len(rows) + 1, 6)):  # try up to 5 tracks
                    hqp.select_track(skip_idx)
                    hqp.play()
                    time.sleep(0.5)
                    status = hqp.get_status()
                    if status and status.state != PlaybackState.STOPPED:
                        logger.info(f"play-tracks: track {skip_idx} started successfully")
                        break

        _invalidate_playlist()
        _notify_update()

        return {
            "ok": True,
            "count": len(rows),
            "tracks": [
                {"id": r["id"], "title": r["title"], "artist": r["artist"]}
                for r in rows
            ],
        }
    except Exception as e:
        logger.error(f"play-tracks failed: {e}")
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/queue-tracks")
def queue_tracks(req: QueueTracksRequest):
    """Append the given tracks to the current queue, in order, without
    clearing. Used by the per-track "+" button and the "Queue album"
    action — these expect "add exactly these tracks", not the
    similarity-driven Radio Mode that `/queue-next` provides."""
    if not req.track_ids:
        raise HTTPException(status_code=400, detail="No track IDs provided")

    rows = _db_query("""
        SELECT mf.id, mf.file_path, t.title, a.name as artist, al.title as album
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN album_variants av ON mf.album_variant_id = av.id
        JOIN albums al ON av.album_id = al.id
        WHERE mf.id = ANY(%(ids)s)
        ORDER BY array_position(%(ids)s, mf.id)
    """, {"ids": req.track_ids})

    if not rows:
        raise HTTPException(status_code=404, detail="No tracks found")

    try:
        with _hqp_lock:
            hqp = _get_hqp()
            for row in rows:
                hqp.playlist_add(file_path_to_uri(row["file_path"]))
        _invalidate_playlist()
        return {
            "ok": True,
            "count": len(rows),
            "tracks": [
                {"id": r["id"], "title": r["title"], "artist": r["artist"], "album": r["album"]}
                for r in rows
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/queue-next")
def queue_next(req: QueueNextRequest):
    """Append similar tracks to the current queue (Radio Mode)."""
    source = _db_query_one("""
        SELECT mf.id, t.id as db_track_id
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        WHERE mf.id = %(track_id)s
    """, {"track_id": req.track_id})

    if not source:
        raise HTTPException(status_code=404, detail="Track not found")

    exclude_clause = ""
    params = {"db_track_id": source["db_track_id"], "limit": req.limit}
    if req.exclude_ids:
        exclude_clause = "AND mf_rep.id != ALL(%(exclude_ids)s)"
        params["exclude_ids"] = req.exclude_ids

    rows = _db_query(f"""
        WITH target AS (
            SELECT e.vector
            FROM embeddings e
            WHERE e.track_id = %(db_track_id)s
        )
        SELECT mf_rep.id, mf_rep.file_path, t.title, a.name as artist,
               mf_rep.album_title as album
        FROM tracks t
        JOIN embeddings e ON e.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN LATERAL (
            SELECT mf.id, mf.file_path, al.title as album_title
            FROM media_files mf
            JOIN album_variants av ON mf.album_variant_id = av.id
            JOIN albums al ON av.album_id = al.id
            WHERE mf.track_id = t.id
            ORDER BY mf.is_analysis_source DESC, mf.id
            LIMIT 1
        ) mf_rep ON true
        WHERE t.id != %(db_track_id)s
          {exclude_clause}
        ORDER BY e.vector <=> (SELECT vector FROM target)
        LIMIT %(limit)s
    """, params)

    if not rows:
        return {"ok": True, "count": 0, "tracks": []}

    try:
        with _hqp_lock:
            hqp = _get_hqp()
            for row in rows:
                hqp.playlist_add(file_path_to_uri(row["file_path"]))

        _invalidate_playlist()

        return {
            "ok": True,
            "count": len(rows),
            "tracks": [
                {"id": r["id"], "title": r["title"], "artist": r["artist"], "album": r["album"]}
                for r in rows
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# -- Lyrics -------------------------------------------------------------------

@router.get("/lyrics/{media_file_id}")
def get_lyrics(media_file_id: int):
    """
    Get lyrics for a track by media_file_id. Lazy-fetch from LRCLIB if not cached.

    Returns parsed synced_lyrics (list of {time_ms, text}) or null.
    Never returns an error — gracefully returns null fields.
    """
    # Get track info from DB
    row = _db_query_one("""
        SELECT mf.id, t.id as track_id, t.title, a.name as artist,
               al.title as album, mf.duration_seconds
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN album_variants av ON mf.album_variant_id = av.id
        JOIN albums al ON av.album_id = al.id
        WHERE mf.id = %(media_file_id)s
    """, {"media_file_id": media_file_id})

    if not row:
        return {
            "track_id": None, "artist": None, "title": None,
            "source": None, "instrumental": False,
            "plain_lyrics": None, "synced_lyrics": None,
        }

    track_id = str(row["track_id"])

    # Check track_lyrics table (fast path)
    lyrics_row = _db_query_one("""
        SELECT source, plain_lyrics, synced_lyrics, instrumental
        FROM track_lyrics
        WHERE track_id = %(track_id)s
        ORDER BY
            CASE WHEN synced_lyrics IS NOT NULL THEN 0 ELSE 1 END,
            created_at DESC
        LIMIT 1
    """, {"track_id": track_id})

    if lyrics_row:
        synced = None
        if lyrics_row["synced_lyrics"]:
            synced = LrclibService.parse_lrc(lyrics_row["synced_lyrics"])
        return {
            "track_id": track_id,
            "artist": row["artist"],
            "title": row["title"],
            "source": lyrics_row["source"],
            "instrumental": lyrics_row["instrumental"],
            "plain_lyrics": lyrics_row["plain_lyrics"],
            "synced_lyrics": synced,
        }

    # Check external_metadata for previous failed fetch
    meta_row = _db_query_one("""
        SELECT fetch_status FROM external_metadata
        WHERE entity_type = 'track' AND entity_id = %(track_id)s
          AND source = 'lrclib' AND metadata_type = 'lyrics'
    """, {"track_id": track_id})

    if meta_row and meta_row["fetch_status"] == "not_found":
        return {
            "track_id": track_id,
            "artist": row["artist"],
            "title": row["title"],
            "source": None, "instrumental": False,
            "plain_lyrics": None, "synced_lyrics": None,
        }

    # On-demand fetch from LRCLIB
    try:
        from database import get_db_context

        duration = int(row["duration_seconds"]) if row["duration_seconds"] else None

        with LrclibService() as service, get_db_context() as db:
            result = service.fetch_and_store(
                db,
                track_id=row["track_id"],
                track_name=row["title"],
                artist_name=row["artist"],
                album_name=row["album"],
                duration=duration,
            )

        if result["status"] == "not_found":
            return {
                "track_id": track_id,
                "artist": row["artist"],
                "title": row["title"],
                "source": None, "instrumental": False,
                "plain_lyrics": None, "synced_lyrics": None,
            }

        data = result.get("data", {})
        synced = None
        if data.get("syncedLyrics"):
            synced = LrclibService.parse_lrc(data["syncedLyrics"])

        return {
            "track_id": track_id,
            "artist": row["artist"],
            "title": row["title"],
            "source": "lrclib",
            "instrumental": data.get("instrumental", False),
            "plain_lyrics": data.get("plainLyrics"),
            "synced_lyrics": synced,
        }

    except Exception as e:
        logger.error(f"Lyrics fetch failed for media_file {media_file_id}: {e}")
        return {
            "track_id": track_id,
            "artist": row["artist"],
            "title": row["title"],
            "source": None, "instrumental": False,
            "plain_lyrics": None, "synced_lyrics": None,
        }
