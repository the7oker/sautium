#!/usr/bin/env python3
"""
MCP Server for the Sautium AI assistant: library search + playback.

Exposes library search (metadata, semantic, lyrics), playback on whichever
output the user selected (HQPlayer, DLNA, local device, browser), the
MusicBrainz-catalog tools, and — while HQPlayer is the selected output —
that device's own transport/DSP controls (the hqplayer_* tools).

Architecture:
  - HQPlayer Client: TCP XML → Windows host (device tools only, lazy)
  - PostgreSQL: psycopg2 direct → localhost (Docker port-forwarded)
  - FastAPI Backend: httpx → localhost:8000 (ML-heavy semantic search)

All logging goes to stderr (stdout is reserved for STDIO MCP transport).
"""

import hashlib
import hmac
import json
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
import assistant_queries as aq
from hqplayer_client import HQPlayerClient, PlaybackState, format_time

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
# MCP environment. The secret sits beside the node's identity — same resolution
# the backend uses (config.p2p_identity_dir), reached through the environment
# rather than an import for the same reason.

_API_SECRET: bytes | None = None


def _api_secret() -> bytes:
    global _API_SECRET
    if _API_SECRET is None:
        identity_dir = os.environ.get("P2P_IDENTITY_DIR") or os.path.join(
            backend_path, "data", "node_identity")
        with open(os.path.join(identity_dir, ".api_secret"), encoding="ascii") as f:
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


def _backend_post(path: str, body: dict, timeout: float = 30.0) -> dict:
    """Signed POST to the FastAPI backend (JSON body covered by the signature)."""
    payload = json.dumps(body).encode("utf-8")
    ts = str(int(time.time()))
    canonical = f"POST\n{path}\n{ts}\n{hashlib.sha256(payload).hexdigest()}"
    sig = hmac.new(_api_secret(), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    with httpx.Client(base_url=BACKEND_URL, timeout=timeout, verify=False) as client:
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
# -- MCP Server ---------------------------------------------------------------
mcp = FastMCP(
    "Sautium Assistant",
    instructions="Search the music library and control playback on the "
                 "user's selected output.",
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
        # Every query here is an index probe plus a small rerank; PG's JIT
        # compiles the big per-row score expressions for ~0.6s of pure
        # overhead and never pays for itself (measured on search_tracks:
        # 719ms -> 126ms). Same call the discovery engine makes per query.
        with _db_conn.cursor() as cur:
            cur.execute("SET jit = off")
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


# -- Canonical identity --------------------------------------------------------
# Every tool speaks the track/album UUID, never media_files.id: the UUID is the
# one identity an owned file and a not-owned (streamable) row both carry, so a
# search result can always be fed straight into a play/queue tool. Which of the
# two it is only decides HOW the backend reaches the audio — never whether the
# assistant may name it. The queries and formatters are shared with the
# API-provider tool surface (backend/assistant_queries.py).


def _valid_uuids(ids: list) -> list[str]:
    return aq.valid_uuids(ids)


def _owned_media_file(track_uuid: str):
    return aq.owned_media_file(_db_query, track_uuid)


def _format_track_list(rows: list[dict], header: str = "") -> str:
    return aq.format_track_list(rows, header)


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
    corpus: str = "owned",
) -> str:
    """Search the catalog by metadata (artist, album, genre, or free text query).

    All parameters are optional. The query field searches across artist, album, and title.
    Tolerant to typos and misspellings (uses fuzzy trigram matching), and to script:
    a Latin query finds Cyrillic/CJK names.

    Args:
        query: Free text search across artist, album, and track title
        artist: Filter by artist name (fuzzy match, typo-tolerant)
        album: Filter by album name (fuzzy match, typo-tolerant)
        genre: Filter by genre (partial match)
        limit: Maximum number of results (default 20)
        corpus: 'owned' (default, files on disk) | 'all' (adds not-owned tracks,
            which play by streaming — same tools, same IDs)
    """
    try:
        if not any([query, artist, album, genre]):
            return "Provide at least one of query / artist / album / genre."
        rows = aq.search_tracks(_db_query, query=query, artist=artist, album=album,
                                genre=genre, limit=limit, corpus=corpus)
        scope = "" if corpus == "all" else " in the library"
        return _format_track_list(rows, f"Search results{scope} ({len(rows)} tracks):")
    except Exception as e:
        return f"Error searching tracks: {e}"


@mcp.tool()
def search_similar(
    track_id: str,
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
        track_id: Track UUID of the source track (owned or not-owned — either
            works, the seed only needs to be analysed)
        limit: Maximum number of similar tracks (default 15)
        vocalist: 'vocal' | 'instrumental' (optional)
        gender: 'male' | 'female' | 'mixed' (optional)
        genres: Genre names to require, OR within the list (optional)
        instruments: Broad instrument names to require (optional)
        corpus: 'owned' (default, files on disk) | 'all' (adds not-owned tracks,
            which play by streaming)
    """
    try:
        seed = _valid_uuids([track_id])
        if not seed:
            return f"'{track_id}' is not a track UUID. Search first, then pass the ID it returned."
        track_row = _db_query_one("""
            SELECT t.id::text AS tid, t.title, a.name AS artist,
                   EXISTS (SELECT 1 FROM embeddings e WHERE e.track_id = t.id) AS analysed
            FROM tracks t
            JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
            JOIN artists a ON a.id = ta.artist_id
            WHERE t.id = %(track_id)s::uuid
        """, {"track_id": seed[0]})
        if not track_row:
            return f"Track {track_id} not found."
        if not track_row["analysed"]:
            return (f"{track_row['artist']} - {track_row['title']} has no audio analysis yet, "
                    "so nothing can be matched against it. Use search_semantic instead.")

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
        return aq.format_artist_list(
            rows, f"Artist search for '{query}' ({len(rows)} results):")
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
        return aq.format_album_list(
            rows, f"Album search for '{query}' ({len(rows)} results):")
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
        return aq.format_genre_list(
            rows, f"Genre search for '{query}' ({len(rows)} results):")
    except httpx.ConnectError:
        return "Error: Cannot connect to backend."
    except Exception as e:
        return f"Error in genre search: {e}"


# -- MusicBrainz dump (beyond-library resolution) ------------------------------

@mcp.tool()
def mb_resolve(artist_name: str, album_title: str = "") -> str:
    """Resolve an artist that is NOT in the library (neither owned nor phantom)
    against the full MusicBrainz catalog and materialize them as streamable
    phantom entities. On dump-less nodes that know MB peers this transparently
    searches the PEER NETWORK and fetches the artist's signed catalog slice
    before minting — same path the Discovery screen uses. Returns JSON:
      status "ok"       -> artist_id (+ album_id when album_title matched) —
                           real UUIDs for your SAUTIUM_BLOCKS artist/album cards.
                           source "peer_network" = resolved via P2P, not a
                           local dump.
      status "not_found"-> the name is not in the catalog; do not tile it.
      status "rate_limited" -> peer search is cooling down; retry_in_s says
                           when — tell the user to ask again, do NOT claim
                           the artist is missing.
      status "peer_search_in_progress" -> the node is still discovering MB
                           peers on the network (the pulsing MusicBrainz
                           chip in Discovery) — usually settles within a
                           minute or two of node start. Say the lookup is
                           warming up and offer to retry; do NOT offer the
                           dump download and do NOT claim the artist is
                           missing.
      status "no_dump"  -> no local dump AND peer discovery found nothing;
                           payload carries download_gb/required_gb/free_gb/
                           can_fit for quoting the user before any download.

    Costs seconds per artist (mints the whole discography; a peer slice fetch
    adds more) — call it for at most ~3 artists per reply, only after library
    SQL found nothing. When the dump is absent it returns instantly, so it is
    always a safe first probe: call it and report facts — never ask the user
    whether the catalog "is supported" and never ask permission to check.

    Args:
        artist_name: Artist to resolve (any script; fuzzy, alias-aware)
        album_title: Optionally pin one specific album of that artist
    """
    q = (artist_name or "").strip()
    if not q:
        return json.dumps({"status": "error", "detail": "artist_name required"})
    try:
        data = _backend_get("/api/discovery/mb-search", {"q": q, "limit": 5})
        if data.get("cooldown"):
            # Every known peer is inside its burst budget — a retry-later,
            # not a miss. Collapsing this into not_found made the agent
            # tell users an artist "isn't in the catalog".
            return json.dumps({
                "status": "rate_limited",
                "retry_in_s": data["cooldown"],
            })
        if not data.get("available"):
            # Same state machine the Discovery chip renders: 'searching'
            # (the pulsing button) = P2P is up but the first peer-probe
            # round hasn't completed — a retry-later, not "no catalog".
            mb_state = (_backend_get("/api/discovery/mb-status", {})
                        or {}).get("state")
            if mb_state == "searching":
                return json.dumps({
                    "status": "peer_search_in_progress",
                    "note": ("MB peer discovery is still running; it "
                             "usually settles within a minute or two of "
                             "node start — suggest retrying shortly"),
                })
            st = _backend_get("/api/settings/musicbrainz/status", {})
            upd = st.get("update", {})
            if upd.get("running"):
                # Mid-operation the disk budget is decision-data for a
                # decision already made — and misleading (a fully-downloaded
                # archive reads as "0.1 GB left", which one model turned
                # into "small, should be quick"). Report progress only.
                return json.dumps({
                    "status": "no_dump",
                    "dump_running": True,
                    "dump_progress": upd.get("progress"),
                    "pct": upd.get("pct"),
                    "note": ("download+load already in progress; the "
                             "database-load phase after the download alone "
                             "takes tens of minutes — report the progress, "
                             "NEVER promise it will finish quickly"),
                }, ensure_ascii=False)
            disk = st.get("disk", {})
            return json.dumps({
                "status": "no_dump",
                "dump_running": False,
                **{k: disk.get(k) for k in
                   ("download_gb", "required_gb", "free_gb", "can_fit")},
            })
        artists = data.get("artists") or []
        remote = bool(data.get("remote"))
        if not artists:
            return json.dumps({"status": "not_found", "query": q,
                               **({"source": "peer_network"} if remote else {})})
        best = artists[0]
        out = {
            "status": "ok",
            "artist": {"name": best["name"], "comment": best.get("comment"),
                       "release_groups": best.get("rg_count")},
            # Namesake guard: MB disambiguation lines of the runners-up, so a
            # wrong-artist match is visible to the agent before it tiles.
            "alternatives": [{"name": a["name"], "comment": a.get("comment")}
                             for a in artists[1:4]],
        }
        if remote:
            out["source"] = "peer_network"
        rg_gid = None
        if album_title.strip():
            if remote:
                # Peer search is artist-only by design — album pinning needs
                # the slice imported first. The mint below does exactly that,
                # and the post-mint lookup further down fills album_id from
                # the freshly minted local rows, so the agent still gets a
                # tileable album card in one tool call.
                pass
            else:
                alb = _backend_get("/api/discovery/mb-search",
                                   {"q": album_title.strip(), "limit": 10})
                match = next((r for r in alb.get("albums") or []
                              if r.get("artist_gid") == best["gid"]), None)
                if match:
                    rg_gid = match["gid"]
                    out["album"] = {"title": match["title"], "year": match.get("year")}
                else:
                    out["album_status"] = "album_not_found"
        # Big discographies stream tracklists for every release group — give
        # the mint far more than the default 30s. artist_name is the P2P
        # slice key: on a dump-less node the backend fetches this artist's
        # signed slice by NAME before minting (same contract the Web UI's
        # mintMbTile uses) — without it a peer-found artist dies with 404
        # unknown_artist.
        minted = _backend_post("/api/discovery/mb-mint",
                               {"artist_gid": best["gid"], "rg_gid": rg_gid,
                                "artist_name": best["name"]},
                               timeout=180.0)
        out["artist_id"] = minted.get("artist_id")
        if rg_gid:
            # None here = canon declined the group; fall back to the artist card.
            out["album_id"] = minted.get("album_id")
        elif remote and album_title.strip() and out.get("artist_id"):
            # Remote path skipped the pre-mint album pin (peer search is
            # artist-only) — the mint just imported the slice and minted
            # the discography, so the album exists locally NOW. Resolve it
            # here so the reply carries a streamable album card instead of
            # a "go query SQL" hint the model may skip.
            row = _db_query_one(
                """
                SELECT a.id, a.title, a.release_year
                FROM albums a
                JOIN album_artists aa ON aa.album_id = a.id
                WHERE aa.artist_id = %(aid)s::uuid
                  AND (lower(a.title) = lower(%(t)s)
                       OR a.title ILIKE '%%' || %(t)s || '%%')
                ORDER BY (lower(a.title) = lower(%(t)s)) DESC
                LIMIT 1
                """,
                {"aid": out["artist_id"], "t": album_title.strip()},
            )
            if row:
                out["album"] = {"title": row["title"],
                                "year": row.get("release_year")}
                out["album_id"] = str(row["id"])
            else:
                out["album_status"] = "album_not_found"
        return json.dumps(out, ensure_ascii=False)
    except httpx.ConnectError:
        return "Error: Cannot connect to backend."
    except Exception as e:
        return f"Error resolving against MusicBrainz: {e}"


@mcp.tool()
def mb_dump_status() -> str:
    """MusicBrainz dump state: loaded/version, live download+load progress
    (phase, pct), and — only while NO operation is running — the disk budget
    (download_gb/required_gb/free_gb/can_fit) for offering mb_dump_download.
    Use for "how is the download going?" follow-ups. Duration discipline:
    the database-load phase after the download takes tens of minutes; NEVER
    estimate remaining time from download numbers — report phase + pct."""
    try:
        st = _backend_get("/api/settings/musicbrainz/status", {})
        upd = st.get("update") or {}
        out = {
            "loaded": st.get("loaded"),
            "version": st.get("version"),
            "update": upd,
        }
        if upd.get("running"):
            # The budget is decision-data for an offer that's already been
            # accepted — mid-operation it misleads (see mb_resolve).
            out["note"] = ("operation in progress; the load phase takes tens "
                           "of minutes — report progress, never promise "
                           "quick completion")
        else:
            out["disk"] = st.get("disk")
        return json.dumps(out, ensure_ascii=False)
    except httpx.ConnectError:
        return "Error: Cannot connect to backend."
    except Exception as e:
        return f"Error reading dump status: {e}"


@mcp.tool()
def mb_dump_download(confirm: bool = False) -> str:
    """Start the background MusicBrainz dump download+load (~7 GB download,
    ~30 GB disk total, tens of minutes). Fire-and-forget: returns immediately —
    NEVER wait for completion in the same reply; progress lives in More →
    Library → MusicBrainz database, or via mb_dump_status. Call ONLY with
    confirm=true, ONLY after the
    user explicitly agreed in this conversation to the quoted size, and never
    when the disk budget said can_fit=false (the backend refuses then anyway).

    Args:
        confirm: Must be true; the explicit-user-consent latch.
    """
    if not confirm:
        return json.dumps({"status": "refused",
                           "detail": "requires confirm=true after explicit user consent"})
    try:
        _backend_post("/api/settings/musicbrainz/update", {})
    except httpx.ConnectError:
        return "Error: Cannot connect to backend."
    except RuntimeError as e:
        # 409 already-running / 507 insufficient-disk, with the backend's reason.
        return json.dumps({"status": "error", "detail": str(e)}, ensure_ascii=False)
    return json.dumps({"status": "started",
                       "note": "background job; check later via mb_dump_status "
                               "or in More → Library → MusicBrainz database"})


@mcp.tool()
def get_lyrics(track_id: str) -> str:
    """Get the full lyrics text for a specific track.

    Use this when the user asks what a song is about, to quote lyrics,
    or to analyze lyrical content of a specific track.

    Args:
        track_id: Track UUID (owned or not-owned — lyrics are keyed on the
            track, not on a file)
    """
    try:
        tid = _valid_uuids([track_id])
        if not tid:
            return f"'{track_id}' is not a track UUID."
        row = aq.track_lyrics(_db_query, tid[0])

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
def get_track_info(track_id: str) -> str:
    """Get full details about a specific track including audio features.

    Args:
        track_id: Track UUID. Works for a not-owned track too — it has no file
            facts (sample rate, path), but title/album/analysis are the same.
    """
    try:
        tid = _valid_uuids([track_id])
        if not tid:
            return f"'{track_id}' is not a track UUID."
        row = aq.track_info(_db_query, tid[0])
        if not row:
            return f"Track {track_id} not found."
        return aq.format_track_info(row, aq.track_features(_db_query, tid[0]))
    except Exception as e:
        return f"Error getting track info: {e}"


# =============================================================================
# SMART PLAY (5 tools)
# =============================================================================
#
# Everything here takes canonical UUIDs and routes on what the catalog holds:
# a track with a file plays from disk, one without streams from a provider.
# The assistant never has to ask which it is — that question belongs to the
# backend, and asking it here is what used to make not-owned music unplayable.

@mcp.tool()
def play_track(track_id: str) -> str:
    """Play one track on the user's chosen output (replaces the queue).

    Args:
        track_id: Track UUID. A track with no file in the library plays too —
            it streams (Deezer lossless / YouTube), which takes a few seconds
            longer to start.
    """
    try:
        tid = _valid_uuids([track_id])
        if not tid:
            return f"'{track_id}' is not a track UUID. Search first and pass the ID it returned."
        mf = _owned_media_file(tid[0])
        if mf is not None:
            r = _backend_post("/api/player/play-track", {"track_id": mf})
            if not r.get("ok"):
                return f"Could not play track: {r.get('detail') or r}"
            return (f"Now playing: {r.get('artist')} - {r.get('title')}\n"
                    f"Album: {r.get('album')}")

        r = _backend_post("/api/player/play-phantom-track", {"track_id": tid[0]}, timeout=90.0)
        if not r.get("track_count"):
            return "No streaming provider has that track, so it cannot be played."
        return (f"Now streaming: {r.get('artist')} - {r.get('title')}\n"
                f"Album: {r.get('album')} | via {r.get('provider')}")
    except Exception as e:
        return f"Error playing track: {e}"


@mcp.tool()
def play_album(album: str, artist_name: str = "") -> str:
    """Play a whole album on the user's chosen output (replaces the queue).

    Args:
        album: Album UUID (preferred — the ID search_albums and SQL return), or
            an album title to fuzzy-match. A title only matches OWNED albums;
            for an album that isn't in the library pass its UUID.
        artist_name: Optional artist to disambiguate same-titled albums
    """
    try:
        aid = _valid_uuids([album])
        body = ({"album_id": aid[0]} if aid
                else {"album_name": album, "artist_name": artist_name})
        r = _backend_post("/api/player/play-album", body, timeout=180.0)
        if not r.get("ok"):
            return f"Could not play album: {r.get('detail') or r}"
        if r.get("provider"):            # streamed (not in the library)
            missing = len(r.get("missing") or [])
            tail = f" ({missing} track(s) no provider has)" if missing else ""
            return (f"Now streaming the album via {r['provider']}: "
                    f"{r.get('track_count', '?')} tracks{tail}")
        return (f"Now playing album: {r.get('artist')} - {r.get('album')}"
                f" ({r.get('track_count', '?')} tracks)")
    except Exception as e:
        return f"Error playing album: {e}"


@mcp.tool()
def play_similar(track_id: str, limit: int = 10) -> str:
    """Play a station of tracks acoustically similar to the seed, on the user's
    chosen output (replaces the queue). Results mix owned files with not-owned
    tracks, which stream.

    Args:
        track_id: Track UUID of the seed (must be analysed — any owned track is,
            and so is a not-owned one that arrived with analysis)
        limit: How many similar tracks to queue (default 10)
    """
    try:
        tid = _valid_uuids([track_id])
        if not tid:
            return f"'{track_id}' is not a track UUID."
        r = _backend_post("/api/player/play-similar",
                          {"track_id": tid[0], "limit": limit}, timeout=90.0)
        if not r.get("ok"):
            return f"Could not start similar playback: {r.get('detail') or r}"
        tracks = r.get("tracks") or [{}]
        streamed = sum(1 for t in tracks if t.get("is_owned") is False)
        tail = f" ({streamed} of them streamed)" if streamed else ""
        return (f"Now playing: {tracks[0].get('artist')} - {tracks[0].get('title')}\n"
                f"Queued {r.get('count', '?')} tracks by acoustic similarity{tail}.")
    except Exception as e:
        return f"Error playing similar tracks: {e}"


@mcp.tool()
def add_to_queue(ids: list[str]) -> str:
    """Append tracks and/or albums to the current queue, in the given order,
    without clearing what is already there. This is how a playlist is built:
    pass ten album UUIDs and they queue back to back.

    Owned entities are queued immediately; not-owned ones stream in behind
    their provider lookup, so the queue keeps growing after this returns.

    Args:
        ids: Track and/or album UUIDs, in the order to append (max 50)
    """
    try:
        valid = _valid_uuids(ids)
        if not valid:
            return "No valid UUIDs given. Pass the IDs the search tools returned."
        kinds = aq.entity_kinds(_db_query, valid)
        items = [{"kind": kinds[i], "id": i} for i in valid if i in kinds]
        if not items:
            return "None of those IDs exist in the catalog."
        r = _backend_post("/api/player/queue-entities", {"items": items}, timeout=60.0)
        parts = []
        if r.get("queued"):
            parts.append(f"{r['queued']} track(s) from the library")
        if r.get("streaming"):
            parts.append(f"{r['streaming']} streaming in behind them")
        missing = len(r.get("not_found") or [])
        tail = f" ({missing} had nothing playable)" if missing else ""
        return "Queued: " + (", ".join(parts) or "nothing") + tail
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
