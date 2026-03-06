#!/usr/bin/env python3
"""
MCP Server for HQPlayer control and music library search.

Exposes HQPlayer playback controls and music library search as MCP tools
that Claude can call directly via natural language.

Architecture:
  - HQPlayer Client: TCP XML → Windows host
  - PostgreSQL: psycopg2 direct → localhost (Docker port-forwarded)
  - FastAPI Backend: httpx → localhost:8000 (ML-heavy semantic search)

All logging goes to stderr (stdout is reserved for STDIO MCP transport).
"""

import logging
import os
import sys

import httpx
import psycopg2
import psycopg2.extras
from mcp.server.fastmcp import FastMCP

# -- HQPlayer client import (stdlib only, safe to import from backend) --------
backend_path = os.environ.get("BACKEND_PATH", os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, backend_path)
from hqplayer_client import HQPlayerClient, PlaybackState, format_time, file_path_to_uri

# -- Logging to stderr (NEVER stdout — would corrupt STDIO transport) ---------
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("hqplayer-mcp")

# -- Configuration from environment -------------------------------------------
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER", "musicai")
DB_PASSWORD = os.getenv("DB_PASSWORD", "supervisor")
DB_NAME = os.getenv("DB_NAME", "music_ai")
HQPLAYER_HOST = os.getenv("HQPLAYER_HOST", "172.26.80.1")
HQPLAYER_PORT = int(os.getenv("HQPLAYER_PORT", "4321"))
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
TRACKER_URL = os.getenv("TRACKER_URL", "http://localhost:8765")  # playback tracker daemon

# -- MCP Server ---------------------------------------------------------------
mcp = FastMCP(
    "HQPlayer DJ",
    instructions="Control HQPlayer playback and search the music library.",
)

# -- Lazy singletons ----------------------------------------------------------
_hqp_client: HQPlayerClient | None = None
_db_conn: psycopg2.extensions.connection | None = None


def _get_hqp() -> HQPlayerClient:
    """Get or create HQPlayer client (lazy, auto-reconnect)."""
    global _hqp_client
    if _hqp_client is None or not _hqp_client.is_connected():
        _hqp_client = HQPlayerClient(host=HQPLAYER_HOST, port=HQPLAYER_PORT, timeout=10.0)
        if not _hqp_client.connect():
            _hqp_client = None
            raise ConnectionError(
                f"Cannot connect to HQPlayer at {HQPLAYER_HOST}:{HQPLAYER_PORT}. "
                "Make sure HQPlayer Desktop is running."
            )
    return _hqp_client


def _get_db() -> psycopg2.extensions.connection:
    """Get or create PostgreSQL connection (lazy, auto-reconnect)."""
    global _db_conn
    if _db_conn is None or _db_conn.closed:
        _db_conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            dbname=DB_NAME,
        )
        _db_conn.autocommit = True
    return _db_conn


def _db_query(sql: str, params: dict | tuple | None = None) -> list[dict]:
    """Execute SQL query and return list of dicts."""
    conn = _get_db()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def _db_query_one(sql: str, params: dict | None = None) -> dict | None:
    """Execute SQL query and return single dict or None."""
    rows = _db_query(sql, params)
    return rows[0] if rows else None


def _register_playlist(track_ids: list[int]) -> bool:
    """Register playlist mapping with playback tracker daemon.

    Args:
        track_ids: List of track IDs in playlist order

    Returns:
        True if successfully registered, False otherwise
    """
    try:
        # Build playlist mapping: index → track_id
        playlist_mapping = {str(i): track_id for i, track_id in enumerate(track_ids)}

        with httpx.Client(timeout=2.0) as client:
            response = client.post(
                f"{TRACKER_URL}/playlist",
                json={"playlist": playlist_mapping}
            )
            response.raise_for_status()
            logger.info(f"📋 Registered playlist with tracker: {len(track_ids)} tracks")
            return True
    except Exception as e:
        logger.warning(f"Failed to register playlist with tracker: {e}")
        logger.warning("Play counts will not be tracked for this session")
        return False


def _format_track(row: dict) -> str:
    """Format a track dict as a readable string."""
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
    """Format a list of tracks as readable text."""
    if not rows:
        return header + "\nNo tracks found." if header else "No tracks found."
    lines = []
    if header:
        lines.append(header)
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. {_format_track(row)}")
    return "\n".join(lines)


# =============================================================================
# PLAYBACK CONTROL (6 tools)
# =============================================================================

@mcp.tool()
def hqplayer_play() -> str:
    """Start or resume HQPlayer playback."""
    try:
        hqp = _get_hqp()
        ok = hqp.play()
        return "Playback started." if ok else "Failed to start playback."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def hqplayer_pause() -> str:
    """Pause HQPlayer playback."""
    try:
        hqp = _get_hqp()
        ok = hqp.pause()
        return "Playback paused." if ok else "Failed to pause."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def hqplayer_stop() -> str:
    """Stop HQPlayer playback."""
    try:
        hqp = _get_hqp()
        ok = hqp.stop()
        return "Playback stopped." if ok else "Failed to stop."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def hqplayer_next() -> str:
    """Skip to the next track in HQPlayer."""
    try:
        hqp = _get_hqp()
        ok = hqp.next()
        return "Skipped to next track." if ok else "Failed to skip."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def hqplayer_previous() -> str:
    """Go back to the previous track in HQPlayer."""
    try:
        hqp = _get_hqp()
        ok = hqp.previous()
        return "Went to previous track." if ok else "Failed to go back."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def hqplayer_get_status() -> str:
    """Get current HQPlayer status: track info, position, state, volume."""
    try:
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


# =============================================================================
# VOLUME CONTROL (3 tools)
# =============================================================================

@mcp.tool()
def hqplayer_volume_up() -> str:
    """Increase HQPlayer volume by one step."""
    try:
        hqp = _get_hqp()
        ok = hqp.volume_up()
        return "Volume increased." if ok else "Failed to change volume."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def hqplayer_volume_down() -> str:
    """Decrease HQPlayer volume by one step."""
    try:
        hqp = _get_hqp()
        ok = hqp.volume_down()
        return "Volume decreased." if ok else "Failed to change volume."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def hqplayer_set_volume(level: float) -> str:
    """Set HQPlayer volume to an exact level (dB, typically -100 to 0).

    Args:
        level: Volume level in dB (e.g. -10.0)
    """
    try:
        hqp = _get_hqp()
        ok = hqp.set_volume(level)
        return f"Volume set to {level}." if ok else "Failed to set volume."
    except Exception as e:
        return f"Error: {e}"


# =============================================================================
# LIBRARY SEARCH (4 tools)
# =============================================================================

@mcp.tool()
def search_tracks(
    query: str = "",
    artist: str = "",
    album: str = "",
    genre: str = "",
    limit: int = 20,
) -> str:
    """Search music library by metadata (artist, album, genre, or free text query).

    All parameters are optional. The query field searches across artist, album, and title.
    Tolerant to typos and misspellings (uses fuzzy trigram matching).

    Args:
        query: Free text search across artist, album, and track title
        artist: Filter by artist name (fuzzy match, typo-tolerant)
        album: Filter by album name (fuzzy match, typo-tolerant)
        genre: Filter by genre (partial match)
        limit: Maximum number of results (default 20)
    """
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

        if order_scores:
            score_expr = f"GREATEST({', '.join(order_scores)})"
        else:
            score_expr = "0"

        sql = f"""
            SELECT * FROM (
                SELECT DISTINCT ON (mf.id)
                       mf.id, t.title, a.name as artist, al.title as album,
                       g.name as genre, mf.is_lossless,
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


@mcp.tool()
def search_similar(track_id: int, limit: int = 15) -> str:
    """Find tracks with similar sound/audio to a given track using AI audio embeddings (CLAP).

    Args:
        track_id: The ID of the source track
        limit: Maximum number of similar tracks to return (default 15)
    """
    try:
        # Get track_id for the given media file
        track_row = _db_query_one("""
            SELECT track_id FROM media_files WHERE id = %(track_id)s
        """, {"track_id": track_id})
        if not track_row:
            return f"Track with ID {track_id} not found."
        db_track_id = track_row["track_id"]

        sql = """
            WITH target AS (
                SELECT e.vector FROM embeddings e WHERE e.track_id = %(db_track_id)s LIMIT 1
            )
            SELECT sub.id, sub.title, sub.artist, sub.album,
                   sub.genre, sub.is_lossless, sub.duration_seconds,
                   track_matches.similarity
            FROM (
                SELECT DISTINCT ON (t2.id) t2.id as track_id, t2.title,
                       1 - (e2.vector <=> (SELECT vector FROM target)) as similarity
                FROM tracks t2
                JOIN embeddings e2 ON e2.track_id = t2.id
                WHERE t2.id != %(db_track_id)s
                ORDER BY t2.id, e2.vector <=> (SELECT vector FROM target)
            ) track_matches
            JOIN LATERAL (
                SELECT mf.id, mf.duration_seconds, mf.is_lossless,
                       a.name as artist, al.title as album, g.name as genre,
                       t.title
                FROM media_files mf
                JOIN tracks t ON mf.track_id = track_matches.track_id
                JOIN track_artists ta ON track_matches.track_id = ta.track_id AND ta.role = 'primary'
                JOIN artists a ON ta.artist_id = a.id
                JOIN album_variants av ON mf.album_variant_id = av.id
                JOIN albums al ON av.album_id = al.id
                LEFT JOIN track_genres tg ON track_matches.track_id = tg.track_id
                LEFT JOIN genres g ON tg.genre_id = g.id
                WHERE mf.track_id = track_matches.track_id
                ORDER BY mf.id LIMIT 1
            ) sub ON true
            ORDER BY track_matches.similarity DESC
            LIMIT %(limit)s
        """
        rows = _db_query(sql, {"db_track_id": db_track_id, "limit": limit})

        # Get source track info
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


@mcp.tool()
def search_semantic(query: str, limit: int = 15) -> str:
    """Search music library by natural language description using AI semantic understanding.

    Uses CLAP text-to-audio embeddings to find tracks matching a description like
    'energetic rock', 'calm piano music', 'heavy bass electronic'.
    Requires the FastAPI backend to be running (ML models in Docker).

    Args:
        query: Natural language description of the music you want
        limit: Maximum number of results (default 15)
    """
    try:
        with httpx.Client(base_url=BACKEND_URL, timeout=30.0) as client:
            resp = client.post(
                "/search/text",
                params={"query": query, "limit": limit},
            )
            resp.raise_for_status()
            data = resp.json()

        rows = data.get("results", [])
        return _format_track_list(rows, f"Semantic search for '{query}' ({len(rows)} results):")
    except httpx.ConnectError:
        return (
            "Error: Cannot connect to FastAPI backend at "
            f"{BACKEND_URL}. Make sure the Docker backend is running."
        )
    except Exception as e:
        return f"Error in semantic search: {e}"


@mcp.tool()
def search_lyrics(query: str, limit: int = 15) -> str:
    """Search tracks by lyrics content using AI semantic understanding.

    Finds songs whose lyrics match a description.
    E.g. 'songs about love', 'rain and sadness', 'protest and freedom', 'dancing in the moonlight'.

    Args:
        query: Description of lyrical content to search for
        limit: Maximum number of results (default 15)
    """
    try:
        with httpx.Client(base_url=BACKEND_URL, timeout=30.0) as client:
            resp = client.post(
                "/search/lyrics",
                params={"query": query, "limit": limit},
            )
            resp.raise_for_status()
            data = resp.json()

        rows = data.get("results", [])
        return _format_track_list(rows, f"Lyrics search for '{query}' ({len(rows)} results):")
    except httpx.ConnectError:
        return "Error: Cannot connect to backend for lyrics search."
    except Exception as e:
        return f"Error in lyrics search: {e}"


@mcp.tool()
def get_lyrics(track_id: int) -> str:
    """Get the full lyrics text for a specific track.

    Use this when the user asks what a song is about, to quote lyrics,
    or to analyze lyrical content of a specific track.

    Args:
        track_id: The track ID from the database
    """
    try:
        row = _db_query_one("""
            SELECT t.title, a.name as artist, tl.source, tl.plain_lyrics, tl.instrumental
            FROM media_files mf
            JOIN tracks t ON mf.track_id = t.id
            JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
            JOIN artists a ON a.id = ta.artist_id
            LEFT JOIN track_lyrics tl ON tl.track_id = t.id
            WHERE mf.id = %(track_id)s
            ORDER BY CASE tl.source WHEN 'lrclib' THEN 1 WHEN 'genius' THEN 2 ELSE 3 END
            LIMIT 1
        """, {"track_id": track_id})

        if not row:
            return f"Track {track_id} not found."
        if row.get("instrumental"):
            return f"{row['artist']} - {row['title']}: instrumental track (no lyrics)."
        if not row.get("plain_lyrics"):
            return f"{row['artist']} - {row['title']}: lyrics not available."

        return (
            f"{row['artist']} - {row['title']} [source: {row['source']}]\n\n"
            f"{row['plain_lyrics']}"
        )
    except Exception as e:
        return f"Error getting lyrics: {e}"


@mcp.tool()
def get_track_info(track_id: int) -> str:
    """Get full details about a specific track including audio features.

    Args:
        track_id: The track ID from the database
    """
    try:
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
        lines.append(f"Quality: {'Lossless' if row.get('is_lossless') else 'Lossy'}")
        if row.get("sample_rate"):
            lines.append(f"Sample rate: {row['sample_rate']} Hz / {row.get('bit_depth', '?')}-bit")
        lines.append(f"ID: {row['id']}")

        # Audio features
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


# =============================================================================
# SMART PLAY (4 tools)
# =============================================================================

@mcp.tool()
def play_track(track_id: int) -> str:
    """Play a specific track by its database ID on HQPlayer.

    Args:
        track_id: The track ID from the database
    """
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

        uri = file_path_to_uri(row["file_path"])
        hqp = _get_hqp()

        # Stop, clear playlist, add track, select first, play
        hqp.stop()
        hqp.playlist_add(uri, clear=True)
        hqp.select_track(0)
        hqp.play()

        # Register playlist with tracker
        _register_playlist([track_id])

        return f"Now playing: {row['artist']} - {row['title']}\nAlbum: {row['album']}"
    except Exception as e:
        return f"Error playing track: {e}"


@mcp.tool()
def play_album(album_name: str, artist_name: str = "") -> str:
    """Find an album and play all its tracks on HQPlayer.
    Tolerant to typos and misspellings (uses fuzzy trigram matching).

    Args:
        album_name: Album name (fuzzy match, typo-tolerant)
        artist_name: Optional artist name to narrow the search (fuzzy match)
    """
    try:
        # First, find the best matching album using trigram similarity
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

        # Find the best matching album (by name + optional artist)
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

        # Now get all tracks from that specific album
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

        # Stop, clear playlist, add all tracks, select first, play
        hqp.stop()
        first_uri = file_path_to_uri(rows[0]["file_path"])
        hqp.playlist_add(first_uri, clear=True)

        for row in rows[1:]:
            uri = file_path_to_uri(row["file_path"])
            hqp.playlist_add(uri)

        hqp.select_track(0)
        hqp.play()

        # Register playlist with tracker
        track_ids = [row["id"] for row in rows]
        _register_playlist(track_ids)

        album_title = rows[0]["album"]
        artist = rows[0]["artist"]
        track_list = "\n".join(
            f"  {r.get('track_number', i+1)}. {r['title']}" for i, r in enumerate(rows)
        )

        return (
            f"Playing album: {artist} - {album_title} ({len(rows)} tracks)\n"
            f"{track_list}"
        )
    except Exception as e:
        return f"Error playing album: {e}"


@mcp.tool()
def play_similar(track_id: int, limit: int = 10) -> str:
    """Find tracks similar to the given track and play them on HQPlayer.

    Args:
        track_id: Source track ID to find similar tracks for
        limit: Number of similar tracks to queue (default 10)
    """
    try:
        # Get track_id for the given media file
        track_row = _db_query_one("""
            SELECT track_id FROM media_files WHERE id = %(track_id)s
        """, {"track_id": track_id})
        if not track_row:
            return f"Track with ID {track_id} not found."
        db_track_id = track_row["track_id"]

        sql = """
            WITH target AS (
                SELECT e.vector FROM embeddings e WHERE e.track_id = %(db_track_id)s LIMIT 1
            )
            SELECT sub.id, sub.file_path, sub.title, sub.artist, sub.album,
                   track_matches.similarity
            FROM (
                SELECT DISTINCT ON (t2.id) t2.id as track_id, t2.title,
                       1 - (e2.vector <=> (SELECT vector FROM target)) as similarity
                FROM tracks t2
                JOIN embeddings e2 ON e2.track_id = t2.id
                WHERE t2.id != %(db_track_id)s
                ORDER BY t2.id, e2.vector <=> (SELECT vector FROM target)
            ) track_matches
            JOIN LATERAL (
                SELECT mf.id, mf.file_path, mf.duration_seconds,
                       a.name as artist, al.title as album, g.name as genre,
                       t.title
                FROM media_files mf
                JOIN tracks t ON mf.track_id = track_matches.track_id
                JOIN track_artists ta ON track_matches.track_id = ta.track_id AND ta.role = 'primary'
                JOIN artists a ON ta.artist_id = a.id
                JOIN album_variants av ON mf.album_variant_id = av.id
                JOIN albums al ON av.album_id = al.id
                LEFT JOIN track_genres tg ON track_matches.track_id = tg.track_id
                LEFT JOIN genres g ON tg.genre_id = g.id
                WHERE mf.track_id = track_matches.track_id
                ORDER BY mf.id LIMIT 1
            ) sub ON true
            ORDER BY track_matches.similarity DESC
            LIMIT %(limit)s
        """
        rows = _db_query(sql, {"db_track_id": db_track_id, "limit": limit})

        if not rows:
            return "No similar tracks found."

        hqp = _get_hqp()

        # Stop, clear playlist, add all tracks, select first, play
        hqp.stop()
        first_uri = file_path_to_uri(rows[0]["file_path"])
        hqp.playlist_add(first_uri, clear=True)

        for row in rows[1:]:
            uri = file_path_to_uri(row["file_path"])
            hqp.playlist_add(uri)

        hqp.select_track(0)
        hqp.play()

        # Register playlist with tracker
        track_ids = [row["id"] for row in rows]
        _register_playlist(track_ids)

        # Get source track info
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


@mcp.tool()
def add_to_queue(track_ids: list[int]) -> str:
    """Add tracks to the current HQPlayer playlist/queue by their IDs.

    Args:
        track_ids: List of track IDs to add to the queue
    """
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
            uri = file_path_to_uri(row["file_path"])
            hqp.playlist_add(uri)
            added.append(f"{row['artist']} - {row['title']}")

        return f"Added {len(added)} tracks to queue:\n" + "\n".join(f"  {i+1}. {t}" for i, t in enumerate(added))
    except Exception as e:
        return f"Error adding to queue: {e}"


# =============================================================================
# DSP SETTINGS (2 tools)
# =============================================================================

@mcp.tool()
def hqplayer_get_settings() -> str:
    """Get current HQPlayer DSP settings: filters, output mode, sample rate."""
    try:
        hqp = _get_hqp()

        lines = []

        # Get info
        info = hqp.get_info()
        if info:
            lines.append(f"HQPlayer: {info.get('product', '')} v{info.get('version', '')}")
            lines.append(f"Engine: {info.get('engine', '')}")
            lines.append("")

        # Filters
        filters = hqp.get_filters()
        if filters:
            lines.append(f"Available filters ({len(filters)}):")
            for f in filters:
                lines.append(f"  [{f['index']}] {f['name']}")

        # Modes
        modes = hqp.get_modes()
        if modes:
            lines.append(f"\nOutput modes ({len(modes)}):")
            for m in modes:
                lines.append(f"  [{m['index']}] {m['name']}")

        # Rates
        rates = hqp.get_rates()
        if rates:
            lines.append(f"\nSample rates ({len(rates)}):")
            for r in rates:
                rate_khz = r['rate'] / 1000
                lines.append(f"  [{r['index']}] {rate_khz:.1f} kHz")

        return "\n".join(lines) if lines else "No settings info available."
    except Exception as e:
        return f"Error getting settings: {e}"


@mcp.tool()
def hqplayer_set_filter(filter_name: str) -> str:
    """Set HQPlayer upsampling filter by name.

    Use hqplayer_get_settings first to see available filter names.

    Args:
        filter_name: Name of the filter to set (e.g. 'poly-sinc-gauss-xla')
    """
    try:
        hqp = _get_hqp()
        filters = hqp.get_filters()

        if not filters:
            return "Could not retrieve filter list from HQPlayer."

        # Find filter by name (case-insensitive, partial match)
        match = None
        for f in filters:
            if f["name"].lower() == filter_name.lower():
                match = f
                break

        if match is None:
            # Try partial match
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


# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
