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
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
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
MUSIC_WINDOWS_PATH = os.getenv("MUSIC_WINDOWS_PATH", "E:/Music")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

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


def _db_query(sql: str, params: dict | None = None) -> list[dict]:
    """Execute SQL query and return list of dicts."""
    conn = _get_db()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or {})
        return [dict(row) for row in cur.fetchall()]


def _db_query_one(sql: str, params: dict | None = None) -> dict | None:
    """Execute SQL query and return single dict or None."""
    rows = _db_query(sql, params)
    return rows[0] if rows else None


def _convert_path(db_path: str) -> str:
    """Convert DB path (/music/...) to HQPlayer file URI (file:///E:/Music/...)."""
    win_path = db_path.replace("/music/", MUSIC_WINDOWS_PATH + "/", 1)
    return file_path_to_uri(win_path)


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
    if row.get("quality_source"):
        extras.append(f"Quality: {row['quality_source']}")
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

    Args:
        query: Free text search across artist, album, and track title
        artist: Filter by artist name (partial match)
        album: Filter by album name (partial match)
        genre: Filter by genre (partial match)
        limit: Maximum number of results (default 20)
    """
    try:
        conditions = ["1=1"]
        params: dict = {"limit": limit}

        if query:
            conditions.append(
                "(a2.name ILIKE %(query)s OR al.title ILIKE %(query)s OR t.title ILIKE %(query)s)"
            )
            params["query"] = f"%{query}%"
        if artist:
            conditions.append("a2.name ILIKE %(artist)s")
            params["artist"] = f"%{artist}%"
        if album:
            conditions.append("al.title ILIKE %(album)s")
            params["album"] = f"%{album}%"
        if genre:
            conditions.append("g.name ILIKE %(genre)s")
            params["genre"] = f"%{genre}%"

        where = " AND ".join(conditions)

        sql = f"""
            SELECT DISTINCT t.id, t.title, a2.name as artist, al.title as album,
                   g.name as genre, al.quality_source,
                   t.duration_seconds, t.track_number
            FROM tracks t
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a2 ON ta.artist_id = a2.id
            JOIN albums al ON t.album_id = al.id
            LEFT JOIN track_genres tg ON t.id = tg.track_id
            LEFT JOIN genres g ON tg.genre_id = g.id
            WHERE {where}
            ORDER BY a2.name, al.title, t.track_number
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
        sql = """
            WITH target AS (
                SELECT e.vector
                FROM tracks t
                JOIN embeddings e ON t.embedding_id = e.id
                WHERE t.id = %(track_id)s
            )
            SELECT t.id, t.title, a2.name as artist, al.title as album,
                   g.name as genre, al.quality_source, t.duration_seconds,
                   1 - (e.vector <=> (SELECT vector FROM target)) as similarity
            FROM tracks t
            JOIN embeddings e ON t.embedding_id = e.id
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a2 ON ta.artist_id = a2.id
            JOIN albums al ON t.album_id = al.id
            LEFT JOIN track_genres tg ON t.id = tg.track_id
            LEFT JOIN genres g ON tg.genre_id = g.id
            WHERE t.id != %(track_id)s
            ORDER BY e.vector <=> (SELECT vector FROM target)
            LIMIT %(limit)s
        """
        rows = _db_query(sql, {"track_id": track_id, "limit": limit})

        # Get source track info
        source = _db_query_one("""
            SELECT t.title, a2.name as artist
            FROM tracks t
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a2 ON ta.artist_id = a2.id
            WHERE t.id = %(track_id)s
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
def get_track_info(track_id: int) -> str:
    """Get full details about a specific track including audio features.

    Args:
        track_id: The track ID from the database
    """
    try:
        row = _db_query_one("""
            SELECT t.id, t.title, t.track_number, t.disc_number,
                   t.duration_seconds, t.sample_rate, t.bit_depth,
                   t.file_path,
                   a2.name as artist, al.title as album,
                   al.release_year, al.quality_source,
                   g.name as genre
            FROM tracks t
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a2 ON ta.artist_id = a2.id
            JOIN albums al ON t.album_id = al.id
            LEFT JOIN track_genres tg ON t.id = tg.track_id
            LEFT JOIN genres g ON tg.genre_id = g.id
            WHERE t.id = %(track_id)s
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
        lines.append(f"Quality: {row.get('quality_source', 'Unknown')}")
        if row.get("sample_rate"):
            lines.append(f"Sample rate: {row['sample_rate']} Hz / {row.get('bit_depth', '?')}-bit")
        lines.append(f"ID: {row['id']}")

        # Audio features
        af = _db_query_one("""
            SELECT bpm, key, mode, energy_db, danceability, vocal_instrumental, instruments
            FROM audio_features WHERE track_id = %(track_id)s
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
            SELECT t.file_path, t.title, a2.name as artist, al.title as album
            FROM tracks t
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a2 ON ta.artist_id = a2.id
            JOIN albums al ON t.album_id = al.id
            WHERE t.id = %(track_id)s
        """, {"track_id": track_id})

        if not row:
            return f"Track with ID {track_id} not found."

        uri = _convert_path(row["file_path"])
        hqp = _get_hqp()

        # Stop, clear playlist, add track, select first, play
        hqp.stop()
        hqp.playlist_add(uri, clear=True)
        hqp.select_track(0)
        hqp.play()

        return f"Now playing: {row['artist']} - {row['title']}\nAlbum: {row['album']}"
    except Exception as e:
        return f"Error playing track: {e}"


@mcp.tool()
def play_album(album_name: str, artist_name: str = "") -> str:
    """Find an album and play all its tracks on HQPlayer.

    Args:
        album_name: Album name (partial match supported)
        artist_name: Optional artist name to narrow the search
    """
    try:
        conditions = ["al.title ILIKE %(album)s"]
        params: dict = {"album": f"%{album_name}%"}

        if artist_name:
            conditions.append("a2.name ILIKE %(artist)s")
            params["artist"] = f"%{artist_name}%"

        where = " AND ".join(conditions)

        rows = _db_query(f"""
            SELECT t.id, t.file_path, t.title, t.track_number,
                   a2.name as artist, al.title as album
            FROM tracks t
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a2 ON ta.artist_id = a2.id
            JOIN albums al ON t.album_id = al.id
            WHERE {where}
            ORDER BY t.disc_number, t.track_number
        """, params)

        if not rows:
            return f"Album '{album_name}' not found."

        hqp = _get_hqp()

        # Stop, clear playlist, add all tracks, select first, play
        hqp.stop()
        first_uri = _convert_path(rows[0]["file_path"])
        hqp.playlist_add(first_uri, clear=True)

        for row in rows[1:]:
            uri = _convert_path(row["file_path"])
            hqp.playlist_add(uri)

        hqp.select_track(0)
        hqp.play()

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
        sql = """
            WITH target AS (
                SELECT e.vector
                FROM tracks t
                JOIN embeddings e ON t.embedding_id = e.id
                WHERE t.id = %(track_id)s
            )
            SELECT t.id, t.file_path, t.title, a2.name as artist, al.title as album,
                   1 - (e.vector <=> (SELECT vector FROM target)) as similarity
            FROM tracks t
            JOIN embeddings e ON t.embedding_id = e.id
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a2 ON ta.artist_id = a2.id
            JOIN albums al ON t.album_id = al.id
            WHERE t.id != %(track_id)s
            ORDER BY e.vector <=> (SELECT vector FROM target)
            LIMIT %(limit)s
        """
        rows = _db_query(sql, {"track_id": track_id, "limit": limit})

        if not rows:
            return "No similar tracks found."

        hqp = _get_hqp()

        # Stop, clear playlist, add all tracks, select first, play
        hqp.stop()
        first_uri = _convert_path(rows[0]["file_path"])
        hqp.playlist_add(first_uri, clear=True)

        for row in rows[1:]:
            uri = _convert_path(row["file_path"])
            hqp.playlist_add(uri)

        hqp.select_track(0)
        hqp.play()

        # Get source track info
        source = _db_query_one("""
            SELECT t.title, a2.name as artist
            FROM tracks t
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a2 ON ta.artist_id = a2.id
            WHERE t.id = %(track_id)s
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
            SELECT t.id, t.file_path, t.title, a2.name as artist
            FROM tracks t
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a2 ON ta.artist_id = a2.id
            WHERE t.id IN ({placeholders})
            ORDER BY array_position(ARRAY[{placeholders}]::int[], t.id)
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
