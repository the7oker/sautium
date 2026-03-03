"""
REST API for HQPlayer control.

Mirrors MCP server patterns (lazy singleton, path conversion, tracker registration)
but exposed as HTTP endpoints for the Web UI.
"""

import logging
import threading
import time
from typing import Optional

import httpx
import psycopg2
import psycopg2.extras
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import settings
from hqplayer_client import HQPlayerClient, PlaybackState, format_time, file_path_to_uri

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/player", tags=["player"])

# -- Lazy singletons ----------------------------------------------------------

_hqp_client: Optional[HQPlayerClient] = None
_hqp_lock = threading.Lock()  # Prevent concurrent HQPlayer TCP commands
_db_conn: Optional[psycopg2.extensions.connection] = None


def _get_hqp() -> HQPlayerClient:
    """Get or create HQPlayer client (lazy, auto-reconnect). Must be called inside _hqp_lock."""
    global _hqp_client
    need_reconnect = _hqp_client is None or not _hqp_client.is_connected()

    # Detect stale connection (broken pipe) by checking socket health
    if not need_reconnect and _hqp_client and _hqp_client.socket:
        import select
        try:
            ready = select.select([_hqp_client.socket], [], [], 0)
            if ready[0]:
                # Data available on socket without request = connection closed by remote
                peek = _hqp_client.socket.recv(1, 0x02)  # MSG_PEEK
                if not peek:
                    logger.info("HQPlayer connection closed by remote, reconnecting...")
                    need_reconnect = True
        except Exception:
            need_reconnect = True

    if need_reconnect:
        if _hqp_client:
            try:
                _hqp_client.disconnect()
            except Exception:
                pass
        _hqp_client = HQPlayerClient(
            host=settings.hqplayer_host,
            port=settings.hqplayer_port,
            timeout=10.0,
        )
        if not _hqp_client.connect():
            _hqp_client = None
            raise ConnectionError(
                f"Cannot connect to HQPlayer at {settings.hqplayer_host}:{settings.hqplayer_port}"
            )
        logger.info("Reconnected to HQPlayer")
    return _hqp_client


def _reset_hqp():
    """Force-close HQPlayer client so next _get_hqp() reconnects."""
    global _hqp_client
    if _hqp_client:
        try:
            _hqp_client.disconnect()
        except Exception:
            pass
        _hqp_client = None


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


def _get_db() -> psycopg2.extensions.connection:
    """Get or create PostgreSQL connection (lazy, auto-reconnect)."""
    global _db_conn
    if _db_conn is None or _db_conn.closed:
        _db_conn = psycopg2.connect(settings.database_url)
        _db_conn.autocommit = True
    return _db_conn


def _db_query(sql: str, params=None) -> list[dict]:
    conn = _get_db()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def _db_query_one(sql: str, params=None) -> Optional[dict]:
    rows = _db_query(sql, params)
    return rows[0] if rows else None


def _convert_path(db_path: str) -> str:
    """Convert DB path (/music/...) to HQPlayer file URI."""
    win_path = db_path.replace("/music/", settings.hqplayer_music_path + "/", 1)
    return file_path_to_uri(win_path)


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


# -- Search -------------------------------------------------------------------

@router.get("/search")
async def search_tracks(q: str = "", limit: int = 20):
    """Two-stage search grouped by albums: exact ILIKE first, fuzzy trigram fallback."""
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
               g.name as genre, mf.is_lossless
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN album_variants av ON mf.album_variant_id = av.id
        JOIN albums al ON av.album_id = al.id
        LEFT JOIN track_genres tg ON t.id = tg.track_id
        LEFT JOIN genres g ON tg.genre_id = g.id
        WHERE {where_exact}
        ORDER BY a.name, al.title, mf.disc_number, mf.track_number
    """, params)

    if not rows:
        # Stage 2: Fuzzy trigram fallback
        params = {"query": q, "query_like": f"%{q}%", "limit": limit * 10}
        rows = _db_query("""
            SELECT mf.id, t.title, mf.track_number, mf.disc_number, mf.duration_seconds,
                   a.name as artist, av.id as album_id, al.title as album,
                   g.name as genre, mf.is_lossless,
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
            LEFT JOIN track_genres tg ON t.id = tg.track_id
            LEFT JOIN genres g ON tg.genre_id = g.id
            WHERE similarity(a.name, %(query)s) > 0.25
               OR similarity(al.title, %(query)s) > 0.25
               OR similarity(t.title, %(query)s) > 0.25
            ORDER BY _score DESC, a.name, al.title, mf.disc_number, mf.track_number
        """, params)

    # Group by album (merge multi-disc albums, but keep lossless/lossy separate)
    albums_dict = {}
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

@router.get("/playlist")
def get_playlist():
    """Get current playlist from HQPlayer with track details from DB."""
    try:
        with _hqp_lock:
            hqp = _get_hqp()
            hqp_tracks = hqp.get_playlist()
        logger.info(f"HQPlayer returned {len(hqp_tracks)} tracks")

        if not hqp_tracks:
            logger.info("Playlist is empty")
            return {"tracks": [], "count": 0}

        # Extract URIs and convert back to DB paths
        tracks_with_info = []
        for idx, hqp_track in enumerate(hqp_tracks):
            uri = hqp_track["uri"]
            logger.debug(f"Processing track {idx}: URI={uri}")

            # Convert file://E:/Music/... or file:///E:/Music/... back to /music/...
            if uri.startswith("file:///"):
                win_path = uri[8:]  # Remove file:///
            elif uri.startswith("file://"):
                win_path = uri[7:]  # Remove file://
            else:
                logger.warning(f"Unsupported URI format: {uri}")
                continue

            # Replace backslashes with forward slashes and decode percent-encoded brackets
            win_path = win_path.replace("\\", "/")
            win_path = win_path.replace("%5B", "[").replace("%5D", "]")
            db_path = win_path.replace(settings.hqplayer_music_path + "/", "/music/", 1)
            logger.debug(f"  Converted: {uri} -> {db_path}")

            # Query DB for track info
            row = _db_query_one("""
                SELECT mf.id, t.title, mf.track_number, a.name as artist
                FROM media_files mf
                JOIN tracks t ON mf.track_id = t.id
                JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
                JOIN artists a ON ta.artist_id = a.id
                WHERE mf.file_path = %(path)s
            """, {"path": db_path})

            logger.debug(f"  DB lookup: {'found' if row else 'not found'}")

            if row:
                tracks_with_info.append({
                    "id": row["id"],
                    "title": row["title"],
                    "track_number": row["track_number"],
                    "artist": row["artist"],
                    "index": idx,
                })
            else:
                # Fallback to HQPlayer metadata
                logger.warning(f"Track not found in DB, using HQPlayer metadata")
                tracks_with_info.append({
                    "id": None,
                    "title": hqp_track["song"] or "Unknown",
                    "track_number": None,
                    "artist": hqp_track["artist"] or "Unknown",
                    "index": idx,
                })

        logger.info(f"Returning {len(tracks_with_info)} tracks to client")
        return {"tracks": tracks_with_info, "count": len(tracks_with_info)}

    except Exception as e:
        logger.error(f"Get playlist failed: {e}")
        return {"tracks": [], "count": 0}


@router.get("/status")
def get_status():
    """Get current HQPlayer status. Returns {state: 'disconnected'} on connection failure."""
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


@router.post("/play")
def play():
    try:
        return {"ok": _hqp_cmd(lambda h: h.play())}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/pause")
def pause():
    try:
        return {"ok": _hqp_cmd(lambda h: h.pause())}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/stop")
def stop():
    try:
        return {"ok": _hqp_cmd(lambda h: h.stop())}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/next")
def next_track():
    try:
        return {"ok": _hqp_cmd(lambda h: h.next())}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/previous")
def previous_track():
    try:
        return {"ok": _hqp_cmd(lambda h: h.previous())}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/volume/up")
def volume_up():
    try:
        return {"ok": _hqp_cmd(lambda h: h.volume_up())}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/volume/down")
def volume_down():
    try:
        return {"ok": _hqp_cmd(lambda h: h.volume_down())}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/volume")
def set_volume(req: VolumeRequest):
    try:
        return {"ok": _hqp_cmd(lambda h: h.set_volume(req.level))}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# -- Smart play ----------------------------------------------------------------

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
        uri = _convert_path(row["file_path"])
        with _hqp_lock:
            hqp = _get_hqp()
            hqp.stop()
            hqp.playlist_add(uri, clear=True)
            hqp.play()
        _register_playlist([req.track_id])

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
            hqp.playlist_add(_convert_path(rows[0]["file_path"]), clear=True)
            for row in rows[1:]:
                hqp.playlist_add(_convert_path(row["file_path"]))
            hqp.play()

        track_ids = [r["id"] for r in rows]
        _register_playlist(track_ids)

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
            hqp.playlist_add(_convert_path(rows[0]["file_path"]), clear=True)
            for row in rows[1:]:
                hqp.playlist_add(_convert_path(row["file_path"]))
            hqp.play()

        track_ids = [r["id"] for r in rows]
        _register_playlist(track_ids)

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

    placeholders = ", ".join(str(int(tid)) for tid in req.track_ids)
    rows = _db_query(f"""
        SELECT mf.id, mf.file_path, t.title, a.name as artist, al.title as album
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN album_variants av ON mf.album_variant_id = av.id
        JOIN albums al ON av.album_id = al.id
        WHERE mf.id IN ({placeholders})
        ORDER BY array_position(ARRAY[{placeholders}]::int[], mf.id)
    """)

    if not rows:
        raise HTTPException(status_code=404, detail="No tracks found")

    try:
        with _hqp_lock:
            hqp = _get_hqp()
            logger.info(f"play-tracks: stopping playback")
            hqp.stop()
            first_path = _convert_path(rows[0]["file_path"])
            logger.info(f"play-tracks: adding first track (clear=True): {first_path}")
            result = hqp.playlist_add(first_path, clear=True)
            logger.info(f"play-tracks: playlist_add result: {result}")
            for i, row in enumerate(rows[1:], 2):
                path = _convert_path(row["file_path"])
                hqp.playlist_add(path)
            logger.info(f"play-tracks: added {len(rows)} tracks total")
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

        track_ids = [r["id"] for r in rows]
        _register_playlist(track_ids)

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
