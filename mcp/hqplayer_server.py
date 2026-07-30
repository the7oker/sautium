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

import hashlib
import hmac
import logging
import os
import sys
import time
import urllib.parse

import httpx
import psycopg2
import psycopg2.extras
from mcp.server.fastmcp import FastMCP

# -- HQPlayer client import (stdlib only, safe to import from backend) --------
backend_path = os.environ.get("BACKEND_PATH", os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, backend_path)
from ensemble_instruments import present_instruments
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
BACKEND_URL = os.getenv("BACKEND_URL", "https://localhost:8000")


# -- Signed backend HTTP -------------------------------------------------------
# The backend requires HMAC-SHA256 request signatures (backend/auth_hmac.py).
# Reimplemented stdlib-only: importing auth_hmac would drag starlette into the
# MCP environment. The secret file is shared via BACKEND_PATH (bind-mounted).

_API_SECRET: bytes | None = None


def _api_secret() -> bytes:
    global _API_SECRET
    if _API_SECRET is None:
        path = os.path.join(backend_path, "data", ".api_secret")
        with open(path, encoding="ascii") as f:
            _API_SECRET = f.read().strip().encode("ascii")
    return _API_SECRET


def _backend_get(path: str, params: dict) -> dict:
    """Signed GET to the FastAPI backend. The query string is built explicitly
    so the signature covers the exact URL httpx sends (param order matters)."""
    clean = {k: v for k, v in params.items() if v not in (None, "", [], ())}
    qs = urllib.parse.urlencode(clean, doseq=True)
    path_q = f"{path}?{qs}" if qs else path
    ts = str(int(time.time()))
    canonical = f"GET\n{path_q}\n{ts}\n{hashlib.sha256(b'').hexdigest()}"
    sig = hmac.new(_api_secret(), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    with httpx.Client(base_url=BACKEND_URL, timeout=30.0, verify=False) as client:
        resp = client.get(path_q, headers={"x-sautium-ts": ts, "x-sautium-sig": sig})
        resp.raise_for_status()
        return resp.json()


def _backend_post(path: str, body: dict) -> dict:
    """Signed POST to the FastAPI backend (JSON body covered by the signature)."""
    import json as _json
    payload = _json.dumps(body).encode("utf-8")
    ts = str(int(time.time()))
    canonical = f"POST\n{path}\n{ts}\n{hashlib.sha256(payload).hexdigest()}"
    sig = hmac.new(_api_secret(), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    with httpx.Client(base_url=BACKEND_URL, timeout=30.0, verify=False) as client:
        resp = client.post(path, content=payload,
                           headers={"x-sautium-ts": ts, "x-sautium-sig": sig,
                                    "content-type": "application/json"})
        if resp.status_code >= 400:
            # Surface the backend's own reason. raise_for_status() reports the
            # status and the URL, which tells an agent nothing it can act on —
            # "No track IDs provided" does.
            try:
                detail = resp.json().get("detail")
            except Exception:
                detail = None
            raise RuntimeError(detail or f"backend returned {resp.status_code}")
        return resp.json()


def _discovery(target: str, **params) -> list[dict]:
    """One composite discovery-engine query → one target's results. Same engine
    as the Web UI's Discovery search (routers/discovery.py /api/discovery/search)."""
    if "limit" in params:
        params["limit"] = min(int(params["limit"]), 30)
    data = _backend_get("/api/discovery/search", {"target": target, **params})
    return data.get("results", [])
TRACKER_URL = os.getenv("TRACKER_URL", "http://localhost:8765")  # playback tracker daemon

# -- MCP Server ---------------------------------------------------------------
mcp = FastMCP(
    "HQPlayer DJ",
    instructions="Control HQPlayer playback and search the music library.",
)

# -- Lazy singletons ----------------------------------------------------------
_hqp_client: HQPlayerClient | None = None
_db_conn: psycopg2.extensions.connection | None = None


def _active_output() -> str:
    """Which output the node is playing through, per the backend."""
    try:
        return (_backend_get("/api/settings/output", {}) or {}).get("type") or ""
    except Exception:
        return ""          # backend unreachable: don't block on a guess


def _get_hqp() -> HQPlayerClient:
    """Get or create HQPlayer client (lazy, auto-reconnect).

    Refuses when HQPlayer is not the chosen output. Every tool that commands
    HQPlayer comes through here, so this is the one place the rule has to
    exist. It exists because the tools are named for what they do and an agent
    will reach for one: asking to change a filter, or to skip a track, while
    the sound is going to a phone over DLNA would reconfigure — or start —
    a device in another room, with the canonical queue none the wiser."""
    active = _active_output()
    if active and active != "hqplayer":
        raise ConnectionError(
            f"HQPlayer is not the active audio output (currently: {active}). "
            "Its transport and DSP controls are unavailable. Use play_track / "
            "play_album / play_similar / add_to_queue, which play through "
            "whatever output the user has chosen."
        )
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
                       (SELECT g.name FROM album_genres ag JOIN genres g ON g.id = ag.genre_id
                        WHERE ag.album_id = av.album_id ORDER BY ag.count DESC NULLS LAST LIMIT 1) as genre,
                       mf.is_lossless,
                       mf.duration_seconds, mf.track_number,
                       {score_expr} as _score
                FROM media_files mf
                JOIN tracks t ON mf.track_id = t.id
                JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
                JOIN artists a ON ta.artist_id = a.id
                JOIN album_variants av ON mf.album_variant_id = av.id
                JOIN albums al ON av.album_id = al.id
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
def search_similar(
    track_id: int,
    limit: int = 15,
    vocalist: str = "",
    gender: str = "",
    genres: list[str] = [],
    instruments: list[str] = [],
    corpus: str = "owned",
) -> str:
    """Find tracks similar in sound to a given track (CLAP audio embeddings),
    optionally narrowed by hard filters — one composite engine query
    ("more like this, but instrumental / only Trip-Hop / only by this artist's
    genre").

    Args:
        track_id: The media file ID of the source track
        limit: Maximum number of similar tracks (default 15)
        vocalist: 'vocal' | 'instrumental' (optional)
        gender: 'male' | 'female' | 'mixed' (optional)
        genres: Genre names to require, OR within the list (optional)
        instruments: Broad instrument names to require (optional)
        corpus: 'owned' (default, playable files) | 'all' (adds phantom discography)
    """
    try:
        track_row = _db_query_one("""
            SELECT mf.track_id::text AS tid, t.title, a.name AS artist
            FROM media_files mf
            JOIN tracks t ON t.id = mf.track_id
            JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
            JOIN artists a ON a.id = ta.artist_id
            WHERE mf.id = %(track_id)s
        """, {"track_id": track_id})
        if not track_row:
            return f"Track with ID {track_id} not found."

        rows = _discovery("track", seed_track_id=track_row["tid"], limit=limit,
                          vocalist=vocalist, gender=gender, genres=genres,
                          instruments=instruments, corpus=corpus)
        header = f"Tracks similar to: {track_row['artist']} - {track_row['title']}"
        return _format_track_list(rows, f"{header} ({len(rows)} results):")
    except Exception as e:
        return f"Error finding similar tracks: {e}"


@mcp.tool()
def search_semantic(
    query: str,
    limit: int = 15,
    vocalist: str = "",
    gender: str = "",
    genres: list[str] = [],
    instruments: list[str] = [],
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    corpus: str = "owned",
) -> str:
    """Search tracks by a SOUND description (CLAP text→audio), optionally with
    hard filters — one discovery-engine query (the same engine as the Web UI's
    "Sound · AI" scope).

    The query describes how the music SOUNDS ("dark ambient with rain",
    "energetic rock with guitar solos") — it is NOT a name/title search (use
    search_tracks for names) and NOT a lyrics search (use search_lyrics).
    Every filter is AND-composed. Example: query='romantic saxophone',
    gender='female', vocalist='vocal'.

    Args:
        query: Natural language SOUND description ("energetic rock", "calm piano")
        limit: Maximum number of results (default 15)
        vocalist: 'vocal' | 'instrumental' (optional)
        gender: 'male' | 'female' | 'mixed' (optional)
        genres: Genre names to require, OR within the list (optional)
        instruments: Broad instrument names ("saxophone", "piano") (optional)
        bpm_min: Lower BPM bound (optional)
        bpm_max: Upper BPM bound (optional)
        corpus: 'owned' (default, playable files) | 'all' (adds phantom discography)
    """
    try:
        rows = _discovery("track", sound=query, limit=limit, vocalist=vocalist,
                          gender=gender, genres=genres, instruments=instruments,
                          bpm_min=bpm_min, bpm_max=bpm_max, corpus=corpus)
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
        rows = _discovery("track", lyrics=query, limit=limit)
        return _format_track_list(rows, f"Lyrics search for '{query}' ({len(rows)} results):")
    except httpx.ConnectError:
        return "Error: Cannot connect to backend for lyrics search."
    except Exception as e:
        return f"Error in lyrics search: {e}"


@mcp.tool()
def search_artists(query: str, limit: int = 10, by_bio: bool = False) -> str:
    """Search artists by NAME (default) or by biography description. Returns
    artists, not tracks.

    by_bio=False: fuzzy name/alias match — 'Madonna', 'мадонна', 'Floex'.
    by_bio=True: AI search in biographies — 'British rock band from the 70s',
    'female jazz vocalist', 'German electronic producer'.

    Args:
        query: Artist name, or (with by_bio=True) a description of the artist
        limit: Maximum number of artists (default 10)
        by_bio: Search biographies instead of names (default False)
    """
    try:
        params = {"scope": "bio"} if by_bio else {}
        rows = _discovery("artist", q=query, limit=limit, **params)
        lines = [f"Artist search for '{query}' ({len(rows)} results):"]
        for r in rows:
            tags = []
            if r.get("gender") and r["gender"] != "unknown":
                tags.append(r["gender"])
            if r.get("is_vocalist") and r["is_vocalist"] != "unknown":
                tags.append(r["is_vocalist"])
            if r.get("is_owned") is False:
                tags.append("phantom")
            tag_str = f" [{'/'.join(tags)}]" if tags else ""
            sim = f"{r['similarity']:.2f}" if r.get("similarity") is not None else " —  "
            lines.append(f"  {sim} | {r['artist']}{tag_str}")
        return "\n".join(lines) if rows else f"No artists found for '{query}'"
    except httpx.ConnectError:
        return "Error: Cannot connect to backend."
    except Exception as e:
        return f"Error in artist search: {e}"


@mcp.tool()
def search_albums(query: str, limit: int = 10) -> str:
    """Search albums by description. Returns albums, not tracks.

    Use for queries about album concept, style, or context.
    E.g. 'concept album about war', 'live recording', 'debut album with orchestral arrangements'.

    Args:
        query: Description to search for in album descriptions
        limit: Maximum number of albums (default 10)
    """
    try:
        rows = _discovery("album", q=query, limit=limit)
        lines = [f"Album search for '{query}' ({len(rows)} results):"]
        for r in rows:
            year_str = f" ({r['year']})" if r.get("year") else ""
            phantom = " [phantom]" if r.get("is_owned") is False else ""
            sim = f"{r['similarity']:.2f}" if r.get("similarity") is not None else " —  "
            lines.append(f"  {sim} | {r.get('artist') or '?'} — {r['album']}{year_str}{phantom}")
        return "\n".join(lines) if rows else f"No albums found for '{query}'"
    except httpx.ConnectError:
        return "Error: Cannot connect to backend."
    except Exception as e:
        return f"Error in album search: {e}"


@mcp.tool()
def search_genres(query: str, limit: int = 10) -> str:
    """Search genres by description. Returns genres, not tracks.

    Use for queries about music characteristics or style.
    E.g. 'heavy distorted guitars', 'African rhythms', 'minimalist repetitive compositions'.

    Args:
        query: Description to search for in genre descriptions
        limit: Maximum number of genres (default 10)
    """
    try:
        rows = _discovery("genre", q=query, limit=limit)
        lines = [f"Genre search for '{query}' ({len(rows)} results):"]
        for r in rows:
            sim = f"{r['similarity']:.2f}" if r.get("similarity") is not None else " —  "
            lines.append(f"  {sim} | {r['genre']} ({r.get('album_count', 0)} albums)")
        return "\n".join(lines) if rows else f"No genres found for '{query}'"
    except httpx.ConnectError:
        return "Error: Cannot connect to backend."
    except Exception as e:
        return f"Error in genre search: {e}"


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
                   (SELECT g.name FROM album_genres ag JOIN genres g ON g.id = ag.genre_id
                    WHERE ag.album_id = av.album_id ORDER BY ag.count DESC NULLS LAST LIMIT 1) as genre
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
                # storage is the raw top-20 score distribution — show only
                # labels above their read thresholds
                instr = present_instruments(af["instruments"])
                top = sorted(instr.items(), key=lambda x: -x[1])[:5]
                if top:
                    lines.append(
                        "  Instruments: " + ", ".join(f"{k} ({v:.2f})" for k, v in top)
                    )

        return "\n".join(lines)
    except Exception as e:
        return f"Error getting track info: {e}"


# =============================================================================
# SMART PLAY (4 tools)
# =============================================================================

@mcp.tool()
def play_track(track_id: int) -> str:
    """Play a specific track by its media-file ID on the user's chosen output.

    Args:
        track_id: media_files.id from the database
    """
    try:
        r = _backend_post("/api/player/play-track", {"track_id": track_id})
        if not r.get("ok"):
            return f"Could not play track {track_id}: {r.get('detail') or r}"
        return (f"Now playing: {r.get('artist')} - {r.get('title')}\n"
                f"Album: {r.get('album')}")
    except Exception as e:
        return f"Error playing track: {e}"


@mcp.tool()
def play_album(album_name: str, artist_name: str = "") -> str:
    """Find an album and play all of it on the user's chosen output.

    Args:
        album_name: Album title (partial match works)
        artist_name: Optional artist to disambiguate same-titled albums
    """
    try:
        r = _backend_post("/api/player/play-album",
                          {"album_name": album_name, "artist_name": artist_name})
        if not r.get("ok"):
            return f"Could not play album: {r.get('detail') or r}"
        return (f"Now playing album: {r.get('artist')} - {r.get('album')}"
                f" ({r.get('track_count', '?')} tracks)")
    except Exception as e:
        return f"Error playing album: {e}"


@mcp.tool()
def play_similar(track_id: int, limit: int = 10) -> str:
    """Play a track and queue acoustically similar ones after it, on the
    user's chosen output.

    Args:
        track_id: media_files.id of the seed track
        limit: How many similar tracks to queue (default 10)
    """
    try:
        r = _backend_post("/api/player/play-similar",
                          {"track_id": track_id, "limit": limit})
        if not r.get("ok"):
            return f"Could not start similar playback: {r.get('detail') or r}"
        first = (r.get("tracks") or [{}])[0]
        return (f"Now playing: {first.get('artist')} - {first.get('title')}\n"
                f"Queued {r.get('count', '?')} tracks by acoustic similarity.")
    except Exception as e:
        return f"Error playing similar tracks: {e}"


@mcp.tool()
def add_to_queue(track_ids: list[int]) -> str:
    """Append tracks to the current queue on the user's chosen output,
    without clearing what is already there.

    Args:
        track_ids: media_files.id values, in the order to append
    """
    try:
        r = _backend_post("/api/player/queue-tracks", {"track_ids": track_ids})
        return f"Added {r.get('count', len(track_ids))} track(s) to the queue."
    except Exception as e:
        return f"Error adding to queue: {e}"

@mcp.tool()
def hqplayer_get_settings() -> str:
    """Get current HQPlayer DSP settings: filters, dither/shapers, output mode, sample rate."""
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
                desc = f.get("description", "")
                suffix = f" — {desc}" if desc else ""
                lines.append(f"  [{f['index']}] {f['name']}{suffix}")

        # Shapers / Dithers
        shapers = hqp.get_shapers()
        if shapers:
            lines.append(f"\nAvailable dither/shapers ({len(shapers)}):")
            for s in shapers:
                lines.append(f"  [{s['index']}] {s['name']}")

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
        if not ok:
            return f"Failed to set filter to {match['name']}."
        desc = match.get("description", "")
        return f"Filter set to: {match['name']}" + (f" — {desc}" if desc else "")
    except Exception as e:
        return f"Error setting filter: {e}"


@mcp.tool()
def hqplayer_set_shaper(shaper_name: str) -> str:
    """Set HQPlayer dither/noise shaper by name.

    Use hqplayer_get_settings first to see available shaper names.

    Args:
        shaper_name: Name of the dither/shaper to set (e.g. 'NS9')
    """
    try:
        hqp = _get_hqp()
        shapers = hqp.get_shapers()

        if not shapers:
            return "Could not retrieve shaper list from HQPlayer."

        # Find shaper by name (case-insensitive, exact match first)
        match = None
        for s in shapers:
            if s["name"].lower() == shaper_name.lower():
                match = s
                break

        if match is None:
            # Try partial match
            for s in shapers:
                if shaper_name.lower() in s["name"].lower():
                    match = s
                    break

        if match is None:
            available = ", ".join(s["name"] for s in shapers)
            return f"Shaper '{shaper_name}' not found. Available shapers: {available}"

        ok = hqp.set_shaping(match["index"])
        return f"Dither/shaper set to: {match['name']}" if ok else f"Failed to set shaper to {match['name']}."
    except Exception as e:
        return f"Error setting shaper: {e}"


# =============================================================================
# CONVOLUTION & MATRIX PROFILE TOOLS
# =============================================================================

@mcp.tool()
def hqplayer_set_convolution(enabled: bool) -> str:
    """Enable or disable HQPlayer convolution engine.

    Convolution must be pre-configured in HQPlayer GUI with impulse response files.
    This tool only toggles convolution on/off during playback.

    Args:
        enabled: True to enable convolution, False to disable
    """
    try:
        hqp = _get_hqp()
        ok = hqp.set_convolution(enabled)
        state = "enabled" if enabled else "disabled"
        return f"Convolution {state}." if ok else f"Failed to {state} convolution."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def hqplayer_list_matrix_profiles() -> str:
    """List all saved HQPlayer Matrix Processor profiles.

    Matrix profiles contain EQ, convolution, and channel routing settings.
    """
    try:
        hqp = _get_hqp()
        profiles = hqp.matrix_list_profiles()
        if not profiles:
            return "No matrix profiles found. Create profiles in HQPlayer GUI (Settings → Matrix → save profile name)."
        lines = [f"Available matrix profiles ({len(profiles)}):"]
        for p in profiles:
            lines.append(f"  - {p}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def hqplayer_get_matrix_profile() -> str:
    """Get the currently active HQPlayer Matrix Processor profile name."""
    try:
        hqp = _get_hqp()
        profile = hqp.matrix_get_profile()
        if profile is None:
            return "Could not get current matrix profile."
        return f"Current matrix profile: '{profile}'" if profile else "No matrix profile active (using default)."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def hqplayer_set_matrix_profile(profile_name: str) -> str:
    """Set HQPlayer Matrix Processor profile by name.

    Use hqplayer_list_matrix_profiles first to see available profiles.
    Matrix profiles contain EQ settings, convolution filters, and channel routing.

    Args:
        profile_name: Name of the matrix profile to activate
    """
    try:
        hqp = _get_hqp()

        # Verify profile exists
        profiles = hqp.matrix_list_profiles()
        if profiles and profile_name not in profiles:
            # Try case-insensitive match
            match = None
            for p in profiles:
                if p.lower() == profile_name.lower():
                    match = p
                    break
            if match is None:
                for p in profiles:
                    if profile_name.lower() in p.lower():
                        match = p
                        break
            if match is None:
                available = ", ".join(profiles)
                return f"Profile '{profile_name}' not found. Available: {available}"
            profile_name = match

        ok = hqp.matrix_set_profile(profile_name)
        return f"Matrix profile set to: '{profile_name}'" if ok else f"Failed to set matrix profile to '{profile_name}'."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def hqplayer_get_dsp_state() -> str:
    """Get current HQPlayer DSP processing state.

    Returns active filter index, shaper index, mode, convolution on/off,
    and current matrix profile. Complements hqplayer_get_settings which lists
    available options.
    """
    try:
        hqp = _get_hqp()
        state = hqp.get_state()
        if not state:
            return "Could not get DSP state from HQPlayer."

        active_filter = next(
            (f for f in hqp.get_filters() if f["index"] == state["filter"]), None
        )
        filter_line = f"  Active filter index: {state['filter']}"
        if active_filter:
            filter_line += f" ({active_filter['name']})"
            if active_filter.get("description"):
                filter_line += f" — {active_filter['description']}"

        lines = ["Current DSP state:"]
        lines.append(f"  Convolution: {'ON' if state['convolution'] else 'OFF'}")
        lines.append(f"  Matrix profile: '{state['matrix_profile']}'" if state['matrix_profile'] else "  Matrix profile: (none)")
        lines.append(filter_line)
        lines.append(f"  Active shaper index: {state['shaper']}")
        lines.append(f"  Active mode index: {state['mode']}")
        lines.append(f"  Active rate index: {state['rate']}")
        lines.append(f"  Phase invert: {'ON' if state['invert'] else 'OFF'}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def generate_eq_preset(name: str, filters_json: str, description: str = "") -> str:
    """Generate a parametric EQ preset file for HQPlayer Matrix Processor.

    Creates a Room EQ Wizard (REW) format .txt file that can be loaded into
    HQPlayer's Matrix Processor via the Browse button. Also provides inline
    HQPlayer pipeline syntax.

    Use this when the user asks for EQ adjustments (de-essing, warmth, bass boost, etc.)
    that HQPlayer cannot do natively through its filter/shaper settings.

    Args:
        name: Preset name (e.g. "de-ess", "warm-vocal", "bass-boost")
        filters_json: JSON array of filter objects. Each filter has:
            - type: "peak", "lowshelf"/"lshelf", "highshelf"/"hshelf", "highpass"/"hp", "lowpass"/"lp", "notch"
            - freq: Frequency in Hz
            - gain: Gain in dB (for peak and shelf types)
            - q: Q factor (higher = narrower band). Default 0.707
            Example: [{"type":"peak","freq":6500,"gain":-3,"q":4},{"type":"hshelf","freq":9000,"gain":-2,"q":0.7}]
        description: Human-readable description of what this EQ does
    """
    try:
        import json as _json
        filters = _json.loads(filters_json)
        if not isinstance(filters, list) or not filters:
            return "Error: filters_json must be a non-empty JSON array of filter objects."

        # Validate filters
        for i, f in enumerate(filters):
            if "type" not in f or "freq" not in f:
                return f"Error: filter {i+1} must have 'type' and 'freq' fields."

        # Import generator (backend path already in sys.path)
        from eq_generator import save_eq_preset
        result = save_eq_preset(
            filters=filters,
            name=name,
            description=description,
        )
        return result["instructions"]
    except _json.JSONDecodeError as e:
        return f"Error parsing filters_json: {e}"
    except Exception as e:
        return f"Error generating EQ preset: {e}"


# =============================================================================
# GEAR ADVISOR (3 tools)
# =============================================================================

@mcp.tool()
def gear_advisor_report() -> str:
    """The user's audio-gear upgrade advisor report: listening axes from
    their library, plateau diagnosis of owned electronics (where money is
    measurably dead), and researched candidate transducers with delta rows
    vs owned gear. THIS is the source of truth for gear advice — never
    advise purchases from general knowledge when this data exists."""
    try:
        import json as _json
        return _json.dumps(_backend_get("/api/profile/gear/advisor", {}), ensure_ascii=False)
    except Exception as e:
        return f"Error fetching advisor report: {e}"


@mcp.tool()
def gear_system_report() -> str:
    """Deterministic pair-compatibility matrix over the user's gear park:
    SPL headroom, damping, gain staging, format chains, measured caveats
    and community pair-synergy notes, each with provenance tiers."""
    try:
        import json as _json
        return _json.dumps(_backend_get("/api/profile/gear/system", {}), ensure_ascii=False)
    except Exception as e:
        return f"Error fetching system report: {e}"


@mcp.tool()
def gear_add_candidate(brand: str, model: str, category: str) -> str:
    """Add a gear model to the user's wishlist (status 'want') and kick off
    background research: specs, community sentiment, measured caveats, and
    pair-synergy notes against the user's owned sources. Research lands in
    ~2 minutes; results appear in the Upgrade advisor and via
    gear_advisor_report. Use when the user asks to consider/compare gear
    that is not in the catalog yet. category is one of: headphones, iems,
    dac, amp, player, streamer, speakers, power_amp, preamp,
    integrated_amp, turntable, cartridge, phono_stage (cables and power
    products are not tracked — no analyzable physics). Vinyl: a low-output
    MC cartridge (Lyra-class) needs a phono stage with an MC input — add
    them together, like electrostats with energizers. For electrostatic headphones
    ALWAYS also add the energizer/amp candidates — conventional amps cannot
    drive them, and the pair research will surface which energizers the
    community actually rates for that model."""
    try:
        res = _backend_post("/api/profile/gear", {
            "brand": brand, "model": model, "category": category, "status": "want",
        })
        return (f"Queued: {brand} {model} ({category}) added as 'want' "
                f"(gear_model_id {res.get('gear_model_id')}). Background research "
                "started — specs, sentiment and pair-synergy vs the user's sources "
                "will be cached in ~2 minutes. Tell the user results will appear in "
                "the Upgrade advisor, and re-check gear_advisor_report before giving "
                "verdicts on this model.")
    except Exception as e:
        return f"Error adding candidate: {e}"


# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
