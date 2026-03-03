"""Register all tools in the global REGISTRY.

Handler functions mirror the MCP server logic (mcp/hqplayer_server.py),
but run in-process inside the FastAPI backend.
"""

import logging
import os
from typing import Optional

import httpx
import psycopg2
import psycopg2.extras

from config import settings
from tools.registry import REGISTRY, ToolDef, ToolParam

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy singletons (same pattern as MCP server)
# ---------------------------------------------------------------------------

_db_conn: Optional[psycopg2.extensions.connection] = None
_hqp_client = None


def _get_db():
    global _db_conn
    if _db_conn is None or _db_conn.closed:
        _db_conn = psycopg2.connect(settings.database_url)
        _db_conn.autocommit = True
    return _db_conn


def _db_query(sql, params=None):
    conn = _get_db()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def _db_query_one(sql, params=None):
    rows = _db_query(sql, params)
    return rows[0] if rows else None


def _get_hqp():
    global _hqp_client
    from hqplayer_client import HQPlayerClient
    if _hqp_client is None or not _hqp_client.is_connected():
        _hqp_client = HQPlayerClient(
            host=settings.hqplayer_host,
            port=settings.hqplayer_port,
            timeout=10.0,
        )
        if not _hqp_client.connect():
            _hqp_client = None
            raise ConnectionError(
                f"Cannot connect to HQPlayer at {settings.hqplayer_host}:{settings.hqplayer_port}. "
                "Make sure HQPlayer Desktop is running."
            )
    return _hqp_client


def _convert_path(db_path: str) -> str:
    from hqplayer_client import file_path_to_uri
    win_path = db_path.replace("/music/", settings.hqplayer_music_path + "/", 1)
    return file_path_to_uri(win_path)


def _register_playlist(track_ids: list[int]) -> bool:
    try:
        playlist_mapping = {str(i): tid for i, tid in enumerate(track_ids)}
        with httpx.Client(timeout=2.0) as client:
            resp = client.post(
                f"{settings.tracker_url}/playlist",
                json={"playlist": playlist_mapping},
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.warning(f"Failed to register playlist: {e}")
        return False


def _format_track(row: dict) -> str:
    from hqplayer_client import format_time
    parts = []
    if row.get("artist"):
        parts.append(row["artist"])
    if row.get("title"):
        parts.append(row["title"])
    line = " - ".join(parts) if parts else f"Track #{row.get('id', '?')}"

    extras = []
    if row.get("album"):
        extras.append(f"Album: {row['album']}")
    if row.get("genre"):
        extras.append(f"Genre: {row['genre']}")
    if row.get("duration_seconds"):
        extras.append(f"Duration: {format_time(float(row['duration_seconds']))}")
    if row.get("is_lossless") is not None:
        extras.append(f"Quality: {'Lossless' if row['is_lossless'] else 'Lossy'}")
    if row.get("similarity") is not None:
        extras.append(f"Similarity: {float(row['similarity']):.2%}")
    if row.get("id"):
        extras.append(f"ID: {row['id']}")
    if extras:
        line += "\n  " + " | ".join(extras)
    return line


def _format_track_list(rows: list[dict], header: str = "") -> str:
    if not rows:
        return header + "\nNo tracks found." if header else "No tracks found."
    lines = []
    if header:
        lines.append(header)
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. {_format_track(row)}")
    return "\n".join(lines)


# ===========================================================================
# Handler functions
# ===========================================================================

def _h_execute_query(sql: str) -> str:
    from tools.execute_query import execute_query
    return execute_query(sql)


def _h_search_tracks(
    query: str = "",
    artist: str = "",
    album: str = "",
    genre: str = "",
    limit: int = 20,
) -> str:
    try:
        conditions = ["1=1"]
        params: dict = {"limit": limit}
        order_scores: list[str] = []

        if query:
            conditions.append(
                "(similarity(a.name, %(query)s) > 0.1 "
                "OR similarity(al.title, %(query)s) > 0.1 "
                "OR similarity(t.title, %(query)s) > 0.1 "
                "OR a.name ILIKE %(query_like)s "
                "OR al.title ILIKE %(query_like)s "
                "OR t.title ILIKE %(query_like)s)"
            )
            params["query"] = query
            params["query_like"] = f"%{query}%"
            order_scores.append(
                "GREATEST(similarity(a.name, %(query)s), "
                "similarity(al.title, %(query)s), "
                "similarity(t.title, %(query)s))"
            )
        if artist:
            conditions.append(
                "(similarity(a.name, %(artist)s) > 0.15 OR a.name ILIKE %(artist_like)s)"
            )
            params["artist"] = artist
            params["artist_like"] = f"%{artist}%"
            order_scores.append("similarity(a.name, %(artist)s)")
        if album:
            conditions.append(
                "(similarity(al.title, %(album)s) > 0.15 OR al.title ILIKE %(album_like)s)"
            )
            params["album"] = album
            params["album_like"] = f"%{album}%"
            order_scores.append("similarity(al.title, %(album)s)")
        if genre:
            conditions.append("g.name ILIKE %(genre_like)s")
            params["genre_like"] = f"%{genre}%"

        where = " AND ".join(conditions)
        score_expr = f"GREATEST({', '.join(order_scores)})" if order_scores else "0"

        sql = f"""
            SELECT * FROM (
                SELECT DISTINCT ON (mf.id)
                       mf.id, t.title, a.name as artist, al.title as album,
                       g.name as genre,
                       mf.duration_seconds, mf.track_number,
                       {score_expr} as _score
                FROM media_files mf
                JOIN tracks t ON mf.track_id = t.id
                JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
                JOIN artists a ON ta.artist_id = a.id
                JOIN album_variants av ON mf.album_variant_id = av.id
                JOIN albums al ON av.album_id = al.id
                LEFT JOIN track_genres tg ON t.id = tg.track_id
                LEFT JOIN genres g ON tg.genre_id = g.id
                WHERE {where}
                ORDER BY mf.id, _score DESC
            ) sub
            ORDER BY _score DESC, artist, album, track_number
            LIMIT %(limit)s
        """
        rows = _db_query(sql, params)
        return _format_track_list(rows, f"Search results ({len(rows)} tracks):")
    except Exception as e:
        return f"Error searching tracks: {e}"


def _h_search_similar(track_id: int, limit: int = 15) -> str:
    try:
        # First, get the track_id for the given media_file id
        track_row = _db_query_one("""
            SELECT track_id FROM media_files WHERE id = %(track_id)s
        """, {"track_id": track_id})
        if not track_row:
            return f"Track with ID {track_id} not found."
        db_track_id = track_row["track_id"]

        sql = """
            WITH target AS (
                SELECT e.vector
                FROM embeddings e
                WHERE e.track_id = %(db_track_id)s
                LIMIT 1
            )
            SELECT sub.id, sub.title, sub.artist, sub.album,
                   sub.genre, sub.duration_seconds, sub.similarity
            FROM (
                SELECT DISTINCT ON (t2.id)
                       t2.id as track_id, t2.title,
                       1 - (e2.vector <=> (SELECT vector FROM target)) as similarity
                FROM tracks t2
                JOIN embeddings e2 ON e2.track_id = t2.id
                WHERE t2.id != %(db_track_id)s
                ORDER BY t2.id, e2.vector <=> (SELECT vector FROM target)
            ) track_matches
            JOIN LATERAL (
                SELECT mf.id, mf.duration_seconds,
                       a.name as artist, al.title as album, g.name as genre
                FROM media_files mf
                JOIN track_artists ta ON track_matches.track_id = ta.track_id AND ta.role = 'primary'
                JOIN artists a ON ta.artist_id = a.id
                JOIN album_variants av ON mf.album_variant_id = av.id
                JOIN albums al ON av.album_id = al.id
                LEFT JOIN track_genres tg ON track_matches.track_id = tg.track_id
                LEFT JOIN genres g ON tg.genre_id = g.id
                WHERE mf.track_id = track_matches.track_id
                ORDER BY mf.id
                LIMIT 1
            ) sub ON true
            ORDER BY track_matches.similarity DESC
            LIMIT %(limit)s
        """
        rows = _db_query(sql, {"db_track_id": db_track_id, "limit": limit})

        source = _db_query_one("""
            SELECT t.title, a.name as artist
            FROM media_files mf
            JOIN tracks t ON mf.track_id = t.id
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a ON ta.artist_id = a.id
            WHERE mf.id = %(track_id)s
        """, {"track_id": track_id})

        header = f"Tracks similar to: {source['artist']} - {source['title']}" if source else "Similar tracks"
        return _format_track_list(rows, f"{header} ({len(rows)} results):")
    except Exception as e:
        return f"Error finding similar tracks: {e}"


def _h_search_semantic(query: str, limit: int = 15) -> str:
    try:
        backend_url = os.getenv("BACKEND_URL", "http://localhost:8000")
        with httpx.Client(base_url=backend_url, timeout=30.0) as client:
            resp = client.post("/search/text", params={"query": query, "limit": limit})
            resp.raise_for_status()
            data = resp.json()
        rows = data.get("results", [])
        return _format_track_list(rows, f"Semantic search for '{query}' ({len(rows)} results):")
    except httpx.ConnectError:
        return "Error: Cannot connect to backend for semantic search."
    except Exception as e:
        return f"Error in semantic search: {e}"


def _h_search_lyrics(query: str, limit: int = 15) -> str:
    try:
        import os
        backend_url = os.getenv("BACKEND_URL", "http://localhost:8000")
        with httpx.Client(base_url=backend_url, timeout=30.0) as client:
            resp = client.get("/search/lyrics", params={"query": query, "limit": limit})
            resp.raise_for_status()
            data = resp.json()
        rows = data.get("results", [])
        return _format_track_list(rows, f"Lyrics search for '{query}' ({len(rows)} results):")
    except httpx.ConnectError:
        return "Error: Cannot connect to backend for lyrics search."
    except Exception as e:
        return f"Error in lyrics search: {e}"


def _h_get_track_info(track_id: int) -> str:
    try:
        from hqplayer_client import format_time
        row = _db_query_one("""
            SELECT mf.id, t.title, mf.track_number, mf.disc_number,
                   mf.duration_seconds, mf.sample_rate, mf.bit_depth,
                   mf.file_path, mf.is_lossless,
                   a.name as artist, al.title as album,
                   al.release_year,
                   g.name as genre
            FROM media_files mf
            JOIN tracks t ON mf.track_id = t.id
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a ON ta.artist_id = a.id
            JOIN album_variants av ON mf.album_variant_id = av.id
            JOIN albums al ON av.album_id = al.id
            LEFT JOIN track_genres tg ON t.id = tg.track_id
            LEFT JOIN genres g ON tg.genre_id = g.id
            WHERE mf.id = %(track_id)s
        """, {"track_id": track_id})

        if not row:
            return f"Track with ID {track_id} not found."

        lines = [
            f"{row['artist']} - {row['title']}",
            f"Album: {row['album']}",
        ]
        if row.get("release_year"):
            lines.append(f"Year: {row['release_year']}")
        if row.get("genre"):
            lines.append(f"Genre: {row['genre']}")
        if row.get("track_number"):
            disc = f" (Disc {row['disc_number']})" if row.get("disc_number") and row["disc_number"] > 1 else ""
            lines.append(f"Track: #{row['track_number']}{disc}")
        if row.get("duration_seconds"):
            lines.append(f"Duration: {format_time(float(row['duration_seconds']))}")
        if row.get("is_lossless") is not None:
            lines.append(f"Quality: {'Lossless' if row['is_lossless'] else 'Lossy'}")
        if row.get("sample_rate"):
            lines.append(f"Sample rate: {row['sample_rate']} Hz / {row.get('bit_depth', '?')}-bit")
        lines.append(f"ID: {row['id']}")

        af = _db_query_one("""
            SELECT bpm, key, mode, energy_db, danceability, vocal_instrumental, instruments
            FROM audio_features WHERE track_id = (SELECT track_id FROM media_files WHERE id = %(track_id)s)
        """, {"track_id": track_id})

        if af:
            lines.append("")
            lines.append("Audio Features:")
            if af.get("bpm"):
                lines.append(f"  BPM: {float(af['bpm']):.1f}")
            if af.get("key"):
                lines.append(f"  Key: {af['key']} {af.get('mode', '')}")
            if af.get("energy_db") is not None:
                lines.append(f"  Energy: {float(af['energy_db']):.1f} dB")
            if af.get("danceability") is not None:
                lines.append(f"  Danceability: {float(af['danceability']):.2f}")
            if af.get("vocal_instrumental"):
                lines.append(f"  Type: {af['vocal_instrumental']}")
            if af.get("instruments"):
                instr = af["instruments"]
                if isinstance(instr, list):
                    lines.append(f"  Instruments: {', '.join(instr)}")

        return "\n".join(lines)
    except Exception as e:
        return f"Error getting track info: {e}"


# -- Playback handlers -------------------------------------------------------

def _h_play_track(track_id: int) -> str:
    try:
        row = _db_query_one("""
            SELECT mf.file_path, t.title, a.name as artist, al.title as album
            FROM media_files mf
            JOIN tracks t ON mf.track_id = t.id
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a ON ta.artist_id = a.id
            JOIN album_variants av ON mf.album_variant_id = av.id
            JOIN albums al ON av.album_id = al.id
            WHERE mf.id = %(track_id)s
        """, {"track_id": track_id})
        if not row:
            return f"Track with ID {track_id} not found."

        uri = _convert_path(row["file_path"])
        hqp = _get_hqp()
        hqp.stop()
        hqp.playlist_add(uri, clear=True)
        hqp.select_track(0)
        hqp.play()
        _register_playlist([track_id])
        return f"Now playing: {row['artist']} - {row['title']}\nAlbum: {row['album']}"
    except Exception as e:
        return f"Error playing track: {e}"


def _h_play_album(album_name: str, artist_name: str = "") -> str:
    try:
        match_conditions = [
            "(similarity(al.title, %(album)s) > 0.15 OR al.title ILIKE %(album_like)s)"
        ]
        match_params: dict = {"album": album_name, "album_like": f"%{album_name}%"}
        order_parts = ["similarity(al.title, %(album)s)"]

        if artist_name:
            match_conditions.append(
                "(similarity(a.name, %(artist)s) > 0.15 OR a.name ILIKE %(artist_like)s)"
            )
            match_params["artist"] = artist_name
            match_params["artist_like"] = f"%{artist_name}%"
            order_parts.append("similarity(a.name, %(artist)s)")

        match_where = " AND ".join(match_conditions)
        order_expr = " + ".join(order_parts)

        best_album = _db_query_one(f"""
            SELECT DISTINCT al.id, al.title as album, a.name as artist
            FROM albums al
            JOIN album_variants av ON av.album_id = al.id
            JOIN media_files mf ON mf.album_variant_id = av.id
            JOIN tracks t ON mf.track_id = t.id
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a ON ta.artist_id = a.id
            WHERE {match_where}
            ORDER BY {order_expr} DESC
            LIMIT 1
        """, match_params)

        if not best_album:
            return f"Album '{album_name}' not found."

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
            return f"Album '{album_name}' not found."

        hqp = _get_hqp()
        hqp.stop()
        first_uri = _convert_path(rows[0]["file_path"])
        hqp.playlist_add(first_uri, clear=True)
        for row in rows[1:]:
            uri = _convert_path(row["file_path"])
            hqp.playlist_add(uri)
        hqp.select_track(0)
        hqp.play()

        track_ids = [row["id"] for row in rows]
        _register_playlist(track_ids)

        album_title = rows[0]["album"]
        artist = rows[0]["artist"]
        track_list = "\n".join(
            f"  {r.get('track_number', i+1)}. {r['title']}" for i, r in enumerate(rows)
        )
        return f"Playing album: {artist} - {album_title} ({len(rows)} tracks)\n{track_list}"
    except Exception as e:
        return f"Error playing album: {e}"


def _h_play_similar(track_id: int, limit: int = 10) -> str:
    try:
        # First, get the track_id for the given media_file id
        track_row = _db_query_one("""
            SELECT track_id FROM media_files WHERE id = %(track_id)s
        """, {"track_id": track_id})
        if not track_row:
            return f"Track with ID {track_id} not found."
        db_track_id = track_row["track_id"]

        sql = """
            WITH target AS (
                SELECT e.vector
                FROM embeddings e
                WHERE e.track_id = %(db_track_id)s
                LIMIT 1
            )
            SELECT sub.id, sub.file_path, sub.title, sub.artist, sub.album, sub.similarity
            FROM (
                SELECT DISTINCT ON (t2.id)
                       t2.id as track_id, t2.title,
                       1 - (e2.vector <=> (SELECT vector FROM target)) as similarity
                FROM tracks t2
                JOIN embeddings e2 ON e2.track_id = t2.id
                WHERE t2.id != %(db_track_id)s
                ORDER BY t2.id, e2.vector <=> (SELECT vector FROM target)
            ) track_matches
            JOIN LATERAL (
                SELECT mf.id, mf.file_path,
                       a.name as artist, al.title as album
                FROM media_files mf
                JOIN track_artists ta ON track_matches.track_id = ta.track_id AND ta.role = 'primary'
                JOIN artists a ON ta.artist_id = a.id
                JOIN album_variants av ON mf.album_variant_id = av.id
                JOIN albums al ON av.album_id = al.id
                WHERE mf.track_id = track_matches.track_id
                ORDER BY mf.id
                LIMIT 1
            ) sub ON true
            ORDER BY track_matches.similarity DESC
            LIMIT %(limit)s
        """
        rows = _db_query(sql, {"db_track_id": db_track_id, "limit": limit})
        if not rows:
            return "No similar tracks found."

        hqp = _get_hqp()
        hqp.stop()
        first_uri = _convert_path(rows[0]["file_path"])
        hqp.playlist_add(first_uri, clear=True)
        for row in rows[1:]:
            uri = _convert_path(row["file_path"])
            hqp.playlist_add(uri)
        hqp.select_track(0)
        hqp.play()

        track_ids = [row["id"] for row in rows]
        _register_playlist(track_ids)

        source = _db_query_one("""
            SELECT t.title, a.name as artist
            FROM media_files mf
            JOIN tracks t ON mf.track_id = t.id
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a ON ta.artist_id = a.id
            WHERE mf.id = %(track_id)s
        """, {"track_id": track_id})

        header = f"Playing {len(rows)} tracks similar to: {source['artist']} - {source['title']}" if source else f"Playing {len(rows)} similar tracks"
        track_list = "\n".join(
            f"  {i+1}. {r['artist']} - {r['title']} ({float(r['similarity']):.0%})"
            for i, r in enumerate(rows)
        )
        return f"{header}\n{track_list}"
    except Exception as e:
        return f"Error playing similar tracks: {e}"


def _h_add_to_queue(track_ids: list[int]) -> str:
    try:
        if not track_ids:
            return "No track IDs provided."
        placeholders = ", ".join(str(int(tid)) for tid in track_ids)
        rows = _db_query(f"""
            SELECT mf.id, mf.file_path, t.title, a.name as artist
            FROM media_files mf
            JOIN tracks t ON mf.track_id = t.id
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a ON ta.artist_id = a.id
            WHERE mf.id IN ({placeholders})
            ORDER BY array_position(ARRAY[{placeholders}]::int[], mf.id)
        """)
        if not rows:
            return "None of the specified tracks were found."
        hqp = _get_hqp()
        added = []
        for row in rows:
            uri = _convert_path(row["file_path"])
            hqp.playlist_add(uri)
            added.append(f"{row['artist']} - {row['title']}")
        return f"Added {len(added)} tracks to queue:\n" + "\n".join(f"  {i+1}. {t}" for i, t in enumerate(added))
    except Exception as e:
        return f"Error adding to queue: {e}"


# -- HQPlayer control handlers -----------------------------------------------

def _h_hqplayer_play() -> str:
    try:
        return "Playback started." if _get_hqp().play() else "Failed to start playback."
    except Exception as e:
        return f"Error: {e}"


def _h_hqplayer_pause() -> str:
    try:
        return "Playback paused." if _get_hqp().pause() else "Failed to pause."
    except Exception as e:
        return f"Error: {e}"


def _h_hqplayer_stop() -> str:
    try:
        return "Playback stopped." if _get_hqp().stop() else "Failed to stop."
    except Exception as e:
        return f"Error: {e}"


def _h_hqplayer_next() -> str:
    try:
        return "Skipped to next track." if _get_hqp().next() else "Failed to skip."
    except Exception as e:
        return f"Error: {e}"


def _h_hqplayer_previous() -> str:
    try:
        return "Went to previous track." if _get_hqp().previous() else "Failed to go back."
    except Exception as e:
        return f"Error: {e}"


def _h_hqplayer_get_status() -> str:
    try:
        from hqplayer_client import PlaybackState, format_time
        hqp = _get_hqp()
        status = hqp.get_status()
        if status is None:
            return "Could not get HQPlayer status."
        state_names = {
            PlaybackState.STOPPED: "Stopped",
            PlaybackState.PAUSED: "Paused",
            PlaybackState.PLAYING: "Playing",
            PlaybackState.STOPREQ: "Stopping",
        }
        lines = [f"State: {state_names.get(status.state, 'Unknown')}"]
        if status.artist or status.song:
            lines.append(f"Track: {status.artist} - {status.song}")
        if status.album:
            lines.append(f"Album: {status.album}")
        if status.genre:
            lines.append(f"Genre: {status.genre}")
        lines.append(f"Position: {format_time(status.position)} / {format_time(status.length)}")
        lines.append(f"Volume: {status.volume}")
        lines.append(f"Track index: {status.track_index}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def _h_hqplayer_volume_up() -> str:
    try:
        return "Volume increased." if _get_hqp().volume_up() else "Failed to change volume."
    except Exception as e:
        return f"Error: {e}"


def _h_hqplayer_volume_down() -> str:
    try:
        return "Volume decreased." if _get_hqp().volume_down() else "Failed to change volume."
    except Exception as e:
        return f"Error: {e}"


def _h_hqplayer_set_volume(level: float) -> str:
    try:
        ok = _get_hqp().set_volume(level)
        return f"Volume set to {level}." if ok else "Failed to set volume."
    except Exception as e:
        return f"Error: {e}"


def _h_hqplayer_get_settings() -> str:
    try:
        hqp = _get_hqp()
        lines = []
        info = hqp.get_info()
        if info:
            lines.append(f"HQPlayer: {info.get('product', '')} v{info.get('version', '')}")
            lines.append(f"Engine: {info.get('engine', '')}")
            lines.append("")
        filters = hqp.get_filters()
        if filters:
            lines.append(f"Available filters ({len(filters)}):")
            for f in filters:
                lines.append(f"  [{f['index']}] {f['name']}")
        modes = hqp.get_modes()
        if modes:
            lines.append(f"\nOutput modes ({len(modes)}):")
            for m in modes:
                lines.append(f"  [{m['index']}] {m['name']}")
        rates = hqp.get_rates()
        if rates:
            lines.append(f"\nSample rates ({len(rates)}):")
            for r in rates:
                rate_khz = r['rate'] / 1000
                lines.append(f"  [{r['index']}] {rate_khz:.1f} kHz")
        return "\n".join(lines) if lines else "No settings info available."
    except Exception as e:
        return f"Error getting settings: {e}"


def _h_hqplayer_set_filter(filter_name: str) -> str:
    try:
        hqp = _get_hqp()
        filters = hqp.get_filters()
        if not filters:
            return "Could not retrieve filter list from HQPlayer."
        match = None
        for f in filters:
            if f["name"].lower() == filter_name.lower():
                match = f
                break
        if match is None:
            for f in filters:
                if filter_name.lower() in f["name"].lower():
                    match = f
                    break
        if match is None:
            available = ", ".join(f["name"] for f in filters)
            return f"Filter '{filter_name}' not found. Available filters: {available}"
        ok = hqp.set_filter(match["index"])
        return f"Filter set to: {match['name']}" if ok else f"Failed to set filter to {match['name']}."
    except Exception as e:
        return f"Error setting filter: {e}"


# ===========================================================================
# Register all tools
# ===========================================================================

def register_all():
    """Register all 20 tools in the global REGISTRY."""

    REGISTRY.register(ToolDef(
        name="execute_query",
        description="Execute a read-only SQL SELECT query against the music database. "
                    "Use this to answer questions that require custom queries not covered by other tools. "
                    "Only SELECT and WITH queries are allowed. Results limited to 100 rows.",
        parameters=[
            ToolParam("sql", "string", "SQL SELECT query to execute", required=True),
        ],
        handler=_h_execute_query,
    ))

    REGISTRY.register(ToolDef(
        name="search_tracks",
        description="Search music library by metadata (artist, album, genre, or free text query). "
                    "All parameters are optional. Tolerant to typos (fuzzy trigram matching).",
        parameters=[
            ToolParam("query", "string", "Free text search across artist, album, and track title", required=False, default=""),
            ToolParam("artist", "string", "Filter by artist name (fuzzy match)", required=False, default=""),
            ToolParam("album", "string", "Filter by album name (fuzzy match)", required=False, default=""),
            ToolParam("genre", "string", "Filter by genre (partial match)", required=False, default=""),
            ToolParam("limit", "integer", "Maximum number of results (default 20)", required=False, default=20),
        ],
        handler=_h_search_tracks,
    ))

    REGISTRY.register(ToolDef(
        name="search_similar",
        description="Find tracks with similar sound/audio to a given track using AI audio embeddings (CLAP).",
        parameters=[
            ToolParam("track_id", "integer", "The ID of the source track", required=True),
            ToolParam("limit", "integer", "Maximum number of similar tracks to return (default 15)", required=False, default=15),
        ],
        handler=_h_search_similar,
    ))

    REGISTRY.register(ToolDef(
        name="search_semantic",
        description="Search music library by natural language description using AI semantic understanding. "
                    "Uses CLAP text-to-audio embeddings. E.g. 'energetic rock', 'calm piano music'.",
        parameters=[
            ToolParam("query", "string", "Natural language description of the music you want", required=True),
            ToolParam("limit", "integer", "Maximum number of results (default 15)", required=False, default=15),
        ],
        handler=_h_search_semantic,
    ))

    REGISTRY.register(ToolDef(
        name="search_lyrics",
        description="Search tracks by lyrics content using AI semantic understanding. "
                    "Finds songs whose lyrics match a description. "
                    "E.g. 'songs about love', 'rain and sadness', 'protest and freedom', 'dancing in the moonlight'.",
        parameters=[
            ToolParam("query", "string", "Description of lyrical content to search for", required=True),
            ToolParam("limit", "integer", "Maximum number of results (default 15)", required=False, default=15),
        ],
        handler=_h_search_lyrics,
    ))

    REGISTRY.register(ToolDef(
        name="get_track_info",
        description="Get full details about a specific track including audio features.",
        parameters=[
            ToolParam("track_id", "integer", "The track ID from the database", required=True),
        ],
        handler=_h_get_track_info,
    ))

    REGISTRY.register(ToolDef(
        name="play_track",
        description="Play a specific track by its database ID on HQPlayer.",
        parameters=[
            ToolParam("track_id", "integer", "The track ID from the database", required=True),
        ],
        handler=_h_play_track,
    ))

    REGISTRY.register(ToolDef(
        name="play_album",
        description="Find an album and play all its tracks on HQPlayer. Tolerant to typos (fuzzy matching).",
        parameters=[
            ToolParam("album_name", "string", "Album name (fuzzy match, typo-tolerant)", required=True),
            ToolParam("artist_name", "string", "Optional artist name to narrow the search", required=False, default=""),
        ],
        handler=_h_play_album,
    ))

    REGISTRY.register(ToolDef(
        name="play_similar",
        description="Find tracks similar to the given track and play them on HQPlayer.",
        parameters=[
            ToolParam("track_id", "integer", "Source track ID to find similar tracks for", required=True),
            ToolParam("limit", "integer", "Number of similar tracks to queue (default 10)", required=False, default=10),
        ],
        handler=_h_play_similar,
    ))

    REGISTRY.register(ToolDef(
        name="add_to_queue",
        description="Add tracks to the current HQPlayer playlist/queue by their IDs.",
        parameters=[
            ToolParam("track_ids", "array", "List of track IDs to add to the queue", required=True, items_type="integer"),
        ],
        handler=_h_add_to_queue,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_play",
        description="Start or resume HQPlayer playback.",
        parameters=[],
        handler=_h_hqplayer_play,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_pause",
        description="Pause HQPlayer playback.",
        parameters=[],
        handler=_h_hqplayer_pause,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_stop",
        description="Stop HQPlayer playback.",
        parameters=[],
        handler=_h_hqplayer_stop,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_next",
        description="Skip to the next track in HQPlayer.",
        parameters=[],
        handler=_h_hqplayer_next,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_previous",
        description="Go back to the previous track in HQPlayer.",
        parameters=[],
        handler=_h_hqplayer_previous,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_get_status",
        description="Get current HQPlayer status: track info, position, state, volume.",
        parameters=[],
        handler=_h_hqplayer_get_status,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_volume_up",
        description="Increase HQPlayer volume by one step.",
        parameters=[],
        handler=_h_hqplayer_volume_up,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_volume_down",
        description="Decrease HQPlayer volume by one step.",
        parameters=[],
        handler=_h_hqplayer_volume_down,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_set_volume",
        description="Set HQPlayer volume to an exact level (dB, typically -100 to 0).",
        parameters=[
            ToolParam("level", "number", "Volume level in dB (e.g. -10.0)", required=True),
        ],
        handler=_h_hqplayer_set_volume,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_get_settings",
        description="Get current HQPlayer DSP settings: filters, output mode, sample rate.",
        parameters=[],
        handler=_h_hqplayer_get_settings,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_set_filter",
        description="Set HQPlayer upsampling filter by name. Use hqplayer_get_settings first to see available filter names.",
        parameters=[
            ToolParam("filter_name", "string", "Name of the filter to set (e.g. 'poly-sinc-gauss-xla')", required=True),
        ],
        handler=_h_hqplayer_set_filter,
    ))


# Auto-register on import
register_all()
