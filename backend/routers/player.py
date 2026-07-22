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

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import track_similarity
from config import settings
from db_pool import (
    db_query as _db_query,
    db_query_one as _db_query_one,
)
from ensemble_instruments import present_instruments
from hqplayer_client import PlaybackState, format_time
from lrclib import LrclibService
from playback import queue as queue_mod
from playback import sessions
from playback.base import ReorderPlan
from playback.hqp_backend import _get_hqp, _hqp_lock
from playback.manager import manager
from playback.queue import resolved_artwork as _resolved_artwork
from playback.queue import resolved_durations as _resolved_durations
from playback.sessions import _SESSION_ORIGINS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/player", tags=["player"])

def _hqp_configured() -> bool:
    """An HQPlayer endpoint is actually configured — env-enabled (Docker
    .env) or a host saved from the Web UI. Nodes without HQPlayer skip the
    1s poll loop entirely; the settings PUT starts it the moment a host is
    saved (precursor of the per-output-backend status loop, HARDWARE-TIERS
    §2.6)."""
    from config import settings as app_settings
    if app_settings.hqplayer_enabled:
        return True
    try:
        from routers.settings import _read
        return bool(_read("hqplayer.host"))
    except Exception:
        return False


def start_status_poller():
    """Activate the HQPlayer output backend (no-op without a configured
    endpoint — see _hqp_configured). Name kept for the main.py and
    settings.py call sites."""
    if not _hqp_configured():
        logger.info("No HQPlayer configured — playback output idle")
        return
    manager.activate("hqplayer")


def stop_status_poller():
    """Detach the active backend WITHOUT touching playback — clearing the
    HQPlayer host or shutting the app down must leave an externally-
    running player alone (the next start adopts its queue)."""
    manager.activate(None, stop_old=False)


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
    # Session origin hint: album "Play all" sends 'album' (+ origin_album_id)
    # so a played-whole-album isn't labelled "Mix". Defaults to 'mix'.
    origin: Optional[str] = None
    origin_album_id: Optional[str] = None

class QueueTracksRequest(BaseModel):
    track_ids: list[int]

class JumpRequest(BaseModel):
    index: int  # 1-based, matches HQPlayer's select_track convention

class RemoveRequest(BaseModel):
    index: int  # 1-based — HQPlayer's PlaylistRemove convention

class ReorderRequest(BaseModel):
    order: list[int]  # full new order of media_file_ids, current track included at its original position

class SeekRequest(BaseModel):
    position: float  # seconds into the current track


# -- Outputs -------------------------------------------------------------------

@router.get("/outputs")
def get_outputs(rescan: bool = False):
    """Available playback outputs + the active selection — feeds the Output
    picker. Local devices appear only where the backend runs natively
    (PortAudio finds nothing inside the Docker container). `rescan=1`
    reinitializes PortAudio to pick up hot-plugged devices — rescan() itself
    refuses while any stream is open, since reinit would invalidate it."""
    from playback.local import devices as local_devices
    from routers.settings import _read
    from config import settings as app_settings

    if rescan:
        local_devices.rescan()

    outputs = [{
        "type": "hqplayer",
        "available": _hqp_configured(),
        "host": _read("hqplayer.host") or app_settings.hqplayer_host,
        "port": _read("hqplayer.port") or app_settings.hqplayer_port,
    }]
    devices = local_devices.list_devices()
    if devices:
        outputs.append({"type": "local", "available": True, "devices": devices})

    try:
        import async_upnp_client  # noqa: F401 — presence check only
        dlna_available = True
    except ImportError:
        dlna_available = False
    persisted_renderer = _read("output.dlna_renderer")
    renderers = {**_dlna_discovered,
                 **{u: {**r, "pinned": True} for u, r in _dlna_pins().items()}}
    # The renderer of the ACTIVE dlna output is configuration — it stays
    # listed even when unreachable (like the HQPlayer row when HQP is off).
    # Once another output is selected it must earn its place via scan/pin.
    if persisted_renderer and _read("output.type") == "dlna":
        renderers.setdefault(persisted_renderer.get("udn"), persisted_renderer)
    outputs.append({
        "type": "dlna",
        "available": dlna_available,
        "renderers": list(renderers.values()),
    })
    outputs.append({"type": "browser", "available": True})

    backend = manager.active
    return {
        "active": {
            # Configured output, not just the attached one: with lazy
            # (re)attach the connection may not exist between sessions,
            # but the user's selection must stay visible in the picker.
            "type": backend.id if backend else _read("output.type"),
            "label": backend.label if backend else None,
            "attached": backend is not None,
            "device_id": _read("output.local_device"),
            "exclusive": bool(_read("output.local_exclusive")),
            "renderer_udn": (persisted_renderer or {}).get("udn"),
            "renderer_attached": getattr(backend, "renderer_attached", None),
        },
        "outputs": outputs,
    }


# -- DLNA discovery --------------------------------------------------------------

# Two renderer tiers served by /outputs. Discovered = the LAST scan's
# snapshot, replaced wholesale each Rescan so powered-off devices drop out
# (in-memory by design). Pinned = manual adds — deliberate config: on
# Docker nodes the scan is blind (multicast dies at the bridge, and the
# NAT drops unicast M-SEARCH replies), so pins are the ONLY population
# mechanism and live in user_settings to survive restarts.
_dlna_discovered: dict[str, dict] = {}


def _dlna_pins() -> dict[str, dict]:
    from routers.settings import _read
    return {r["udn"]: r for r in (_read("output.dlna_pinned") or [])}


def _dlna_save_pins(pins: dict[str, dict]) -> None:
    from routers.settings import _write
    _write("output.dlna_pinned", list(pins.values()))


class DlnaAddRequest(BaseModel):
    url: str   # renderer IP — or a full device-description URL


def _msearch_location(ip: str, timeout: float = 3.0) -> Optional[str]:
    """Unicast SSDP M-SEARCH straight at `ip`. Description URLs are
    device-invented (random port + path), but UDP 1900 IS the standard —
    so users enter a bare IP and we resolve the LOCATION ourselves.
    Prefers a MediaRenderer answer, falls back to any root device."""
    import socket as sock
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
           'MAN: "ssdp:discover"\r\nMX: 1\r\nST: ssdp:all\r\n\r\n').encode()
    s = sock.socket(sock.AF_INET, sock.SOCK_DGRAM)
    s.settimeout(timeout)
    fallback = None
    try:
        s.sendto(msg, (ip, 1900))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = s.recvfrom(4096)
            except OSError:
                break
            if addr[0] != ip:
                continue
            loc = st = ""
            for line in data.decode(errors="replace").splitlines():
                low = line.lower()
                if low.startswith("location:"):
                    loc = line.split(":", 1)[1].strip()
                elif low.startswith("st:"):
                    st = line.split(":", 1)[1].strip()
            if loc and "MediaRenderer" in st:
                return loc
            if loc and fallback is None:
                fallback = loc
    finally:
        s.close()
    return fallback


async def _dlna_describe(location: str) -> dict:
    """Fetch a device description and resolve the MediaRenderer in it →
    {udn, location, name, model}. Rejects descriptions without a renderer:
    multi-device hosts exist (the KANN serves its AK Connect media SERVER
    on the same port) and a pinned server can never play."""
    from async_upnp_client.aiohttp import AiohttpRequester
    from async_upnp_client.client_factory import UpnpFactory
    factory = UpnpFactory(AiohttpRequester(), non_strict=True)
    device = await factory.async_create_device(location)
    candidates = [device, *device.embedded_devices.values()]
    renderer = next((d for d in candidates
                     if ":MediaRenderer:" in (d.device_type or "")), None)
    if renderer is None:
        raise HTTPException(
            status_code=422,
            detail=f"'{device.friendly_name}' is {device.device_type or 'not a renderer'}"
                   " — no MediaRenderer at this address (a media-server "
                   "description of the same device is a common decoy)")
    return {
        "udn": renderer.udn,
        "location": location,
        "name": renderer.friendly_name,
        "model": renderer.model_name,
    }


@router.post("/outputs/dlna/scan")
async def dlna_scan():
    """SSDP-search the LAN for MediaRenderer devices (~3 s). Works from
    native deployments; the Docker bridge has no LAN multicast — use
    /outputs/dlna/add with the renderer's description URL there."""
    try:
        from async_upnp_client.search import async_search
    except ImportError:
        raise HTTPException(status_code=501, detail="async-upnp-client not installed")

    found: dict[str, str] = {}

    async def _on_response(headers) -> None:
        loc = headers.get("location") or headers.get("LOCATION")
        usn = headers.get("usn") or headers.get("USN") or loc
        if loc:
            found[usn] = str(loc)

    # Search from EVERY private interface: on a multi-homed host (Wi-Fi +
    # WSL/Hyper-V virtual adapters) the default-route multicast often egresses
    # a virtual NIC and never reaches the LAN — the KANN was invisible to a
    # single default search while answering an interface-bound one.
    from tls_gen import detect_private_host_ips
    sources = [None] + [(ip, 0) for ip in detect_private_host_ips()]
    for source in sources:
        try:
            await async_search(
                _on_response, timeout=2, source=source,
                search_target="urn:schemas-upnp-org:device:MediaRenderer:1")
        except Exception as e:
            logger.debug("SSDP search on %s failed: %s", source, e)

    renderers = []
    fresh: dict[str, dict] = {}
    for loc in sorted(set(found.values())):
        try:
            info = await _dlna_describe(loc)
        except Exception as e:
            logger.debug("DLNA describe failed for %s: %s", loc, e)
            continue
        fresh[info["udn"]] = info
        renderers.append(info)
    _dlna_discovered.clear()
    _dlna_discovered.update(fresh)
    return {"renderers": renderers}


@router.post("/outputs/dlna/add")
async def dlna_add(req: DlnaAddRequest):
    """Register a renderer by IP (unicast M-SEARCH resolves its description
    URL — the path multicast-less deployments use) or by a full
    device-description URL."""
    raw = req.url.strip()
    if not raw.lower().startswith(("http://", "https://")):
        ip = raw.split("/")[0].split(":")[0]
        location = await asyncio.to_thread(_msearch_location, ip)
        if not location:
            raise HTTPException(
                status_code=502,
                detail=f"no UPnP device answered at {ip}:1900 — is the "
                       "renderer's network mode (e.g. AK Connect) enabled?")
        raw = location
    try:
        info = await _dlna_describe(raw)
    except ImportError:
        raise HTTPException(status_code=501, detail="async-upnp-client not installed")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"renderer not reachable: {e}")
    pins = _dlna_pins()
    pins[info["udn"]] = info
    _dlna_save_pins(pins)
    return info


class DlnaRemoveRequest(BaseModel):
    udn: str


@router.post("/outputs/dlna/remove")
async def dlna_remove(req: DlnaRemoveRequest):
    """Unpin a manually added renderer (and drop it from the last scan
    snapshot so it disappears immediately)."""
    pins = _dlna_pins()
    removed = pins.pop(req.udn, None) is not None
    _dlna_save_pins(pins)
    _dlna_discovered.pop(req.udn, None)
    return {"ok": True, "removed": removed}


# -- Browser output (per-tab renderer channel) -------------------------------------

class BrowserEventRequest(BaseModel):
    tab: str
    event: str                        # playing|paused|ended|advanced|timeupdate|error
    position: Optional[float] = None
    duration: Optional[float] = None
    queue_index: Optional[int] = None  # the slot the tab currently renders
    epoch: Optional[int] = None       # load-directive epoch the event belongs to


def _browser_backend():
    backend = manager.active
    if backend is None or backend.id != "browser":
        raise HTTPException(status_code=409, detail="browser output is not active")
    return backend


@router.get("/browser/channel")
async def browser_channel(tab: str):
    """SSE command channel for the renderer tab. Directives (load/play/
    pause/seek/volume/stop/queue/released) flow down; the newest subscriber
    displaces the previous one (takeover)."""
    backend = _browser_backend()
    loop = asyncio.get_event_loop()
    channel: asyncio.Queue = asyncio.Queue()
    backend.attach_tab(tab, channel, loop)

    async def event_generator():
        try:
            while True:
                try:
                    directive = await asyncio.wait_for(channel.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(directive)}\n\n"
                if directive.get("cmd") == "released":
                    return
        except asyncio.CancelledError:
            pass
        finally:
            backend.detach_tab(tab)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/browser/event")
def browser_event(req: BrowserEventRequest):
    """<audio> element events from the renderer tab (element callbacks —
    timeupdate throttled to ~1/s client-side, not polling)."""
    backend = _browser_backend()
    payload = backend.on_client_event(req.tab, req.event, req.position,
                                      req.duration, req.queue_index, req.epoch)
    return {"ok": True, **(payload or {})}


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
               (SELECT g.name FROM album_genres ag JOIN genres g ON g.id = ag.genre_id
                WHERE ag.album_id = av.album_id ORDER BY ag.count DESC NULLS LAST LIMIT 1) as genre,
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
                   (SELECT g.name FROM album_genres ag JOIN genres g ON g.id = ag.genre_id
                    WHERE ag.album_id = av.album_id ORDER BY ag.count DESC NULLS LAST LIMIT 1) as genre,
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

@router.get("/playlist")
def get_playlist():
    """The canonical queue, serialized. Always instant — the queue lives
    here; no player round-trip, no cache."""
    return manager.queue.payload()


@router.get("/status/stream")
async def status_stream():
    """SSE endpoint: pushes status updates in real-time."""
    loop = asyncio.get_event_loop()
    evt = asyncio.Event()

    async def event_generator():
        last_version = -1
        try:
            manager.sse_register(evt, loop)

            # Send current status immediately
            yield f"data: {json.dumps(manager.latest_status)}\n\n"
            last_version = manager.status_version

            while True:
                try:
                    await asyncio.wait_for(evt.wait(), timeout=15.0)
                    evt.clear()
                except asyncio.TimeoutError:
                    # Keepalive
                    yield ": keepalive\n\n"
                    continue

                if manager.status_version != last_version:
                    last_version = manager.status_version
                    yield f"data: {json.dumps(manager.latest_status)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            manager.sse_unregister(evt)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/preview-events")
async def preview_events_stream():
    """SSE: bare 'refresh' pings whenever phantom-preview state changes (a track
    starts/finishes buffering, or enrichment commits key·bpm). No payload — the
    open album page already knows its own id and re-fetches /api/albums/{id} for
    a single consistent snapshot (features + buffering), so there's no split
    source to race. Coalesced server-side; the client debounces its re-fetch."""
    from streaming.events import preview_events

    async def event_generator():
        q = preview_events.subscribe()
        try:
            while True:
                try:
                    await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                # Drain any coalesced pings so one re-fetch covers the burst.
                while not q.empty():
                    q.get_nowait()
                yield "data: refresh\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            preview_events.unsubscribe(q)

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
    """Last status pushed by the active output backend — always instant,
    never hits the player in the request path."""
    return manager.latest_status


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
            "process_speed": status.process_speed,
            "track_index": status.track_index,
            "progress_percent": round(status.progress_percent, 1),
            "position_formatted": format_time(status.position),
            "length_formatted": format_time(status.length),
        }
    except Exception:
        return {"state": "disconnected"}


def _stream_quality(provider_id: Optional[str]) -> str:
    """'lossless' / 'lossy' for the serving provider — the preview analog of the
    owned file-quality badge (a stream has no file bit-depth)."""
    if provider_id:
        try:
            from streaming import service as streaming_service
            for p in streaming_service.providers_preferred():
                if p.manifest.id == provider_id:
                    return "lossless" if p.manifest.lossless else "lossy"
        except Exception:
            pass
        if provider_id == "deezer":   # registry not ready — known-provider fallback
            return "lossless"
    return "lossy"


def _preview_now_playing_detail(track_id: str, provider: Optional[str]) -> dict:
    """Now Playing detail for a streamed phantom track (no media_file). Same shape
    as the owned path, sourced by track_id: MB tracklist metadata + CAA cover +
    whatever audio_features the preview enrichment has produced so far (key/BPM/
    energy fill in live as the stream is analysed). Album is the track's first
    release that carries art; quality is the stream source, not a file bit-depth."""
    row = _db_query_one("""
        SELECT t.id::text AS track_id, t.title,
               al.id::text AS album_id, al.title AS album_title,
               al.release_year AS year, al.cover_url,
               af.bpm, af.key, af.mode, af.energy, af.energy_db,
               af.danceability, af.instruments, af.moods
        FROM tracks t
        JOIN album_tracks atr ON atr.track_id = t.id
        JOIN albums al ON al.id = atr.album_id
        LEFT JOIN audio_features af ON af.track_id = t.id
        WHERE t.id = %(tid)s::uuid
        ORDER BY (al.cover_url IS NOT NULL) DESC, al.id
        LIMIT 1
    """, {"tid": track_id})
    if not row:
        raise HTTPException(status_code=404, detail="track not found")
    row["media_file_id"] = None
    row["cover_id"] = None
    row["instruments"] = present_instruments(row.get("instruments"))
    row["genres"] = _db_query("""
        SELECT g.id::text, g.name
        FROM album_genres ag JOIN genres g ON g.id = ag.genre_id
        WHERE ag.album_id = %(a)s::uuid
        GROUP BY g.id, g.name
        ORDER BY MAX(ag.count) DESC NULLS LAST, g.name
        LIMIT 3
    """, {"a": row["album_id"]})
    row["primary_artist"] = _db_query_one("""
        SELECT a.id::text, a.name
        FROM artists a
        JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
        WHERE ta.track_id = %(t)s::uuid
        LIMIT 1
    """, {"t": track_id})
    row["quality"] = _stream_quality(provider)
    row["provider_cover_url"] = _resolved_artwork.get(track_id)
    return row


@router.get("/now-playing-detail")
def now_playing_detail(media_file_id: int = None, track_id: str = None,
                       provider: str = None):
    """Aggregated rich payload for the Now Playing screen.

    Combines media-file metadata (format, sample rate, bit depth, cover),
    track-level audio features (BPM, key, mode, energy, instruments),
    album info, and the top genres into one roundtrip. Called by the
    frontend whenever the playing track changes. A streamed phantom track has no
    media_file — it is fetched by ``track_id`` (+ the serving ``provider`` for the
    quality badge) and served the same shape via _preview_now_playing_detail.
    """
    if track_id:
        return _preview_now_playing_detail(track_id, provider)
    if not media_file_id:
        raise HTTPException(status_code=400, detail="media_file_id or track_id required")
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

    row["instruments"] = present_instruments(row.get("instruments"))
    row["genres"] = _db_query("""
        SELECT g.id::text, g.name
        FROM album_genres ag
        JOIN genres g ON g.id = ag.genre_id
        WHERE ag.album_id = %(a)s::uuid
        GROUP BY g.id, g.name
        ORDER BY MAX(ag.count) DESC NULLS LAST, g.name
        LIMIT 3
    """, {"a": row["album_id"]})

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
        return {"ok": manager.ensure_active().play()}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/pause")
def pause():
    try:
        return {"ok": manager.backend().pause()}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/stop")
def stop():
    try:
        return {"ok": manager.backend().stop()}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/next")
def next_track():
    try:
        return {"ok": manager.ensure_active().next()}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/previous")
def previous_track():
    try:
        return {"ok": manager.ensure_active().previous()}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/volume/up")
def volume_up():
    try:
        return {"ok": manager.backend().volume_up()}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/volume/down")
def volume_down():
    try:
        return {"ok": manager.backend().volume_down()}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/volume")
def set_volume(req: VolumeRequest):
    try:
        return {"ok": manager.backend().set_volume(req.level)}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/seek")
def seek(req: SeekRequest):
    """Seek within the current track (outputs whose capabilities().seek is
    true — the built-in engine and HQPlayer both support it)."""
    if req.position < 0:
        raise HTTPException(status_code=400, detail="position must be >= 0")
    try:
        return {"ok": manager.backend().seek(int(req.position))}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# -- Smart play ----------------------------------------------------------------

@router.post("/remove")
def remove(req: RemoveRequest):
    """Remove a queue slot by 1-based index (HQPlayer's PlaylistRemove
    convention). The play_count for the removed slot is unaffected —
    tracking records past plays, not pending queue contents."""
    if req.index < 1:
        raise HTTPException(status_code=400, detail="index must be >= 1")
    try:
        ok = manager.remove(req.index)
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

    current_items = manager.queue.snapshot()
    current_ids = [it.media_file_id for it in current_items]

    status_idx = manager.latest_status.get("track_index") or 0
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

    items_by_id = {}
    for it in current_items:
        items_by_id.setdefault(it.media_file_id, it)

    plan = ReorderPlan(
        seamless=seamless,
        status_idx=status_idx,
        new_status_idx=new_status_idx,
        old_before=old_before,
        new_before=new_before,
        old_after=old_after,
        new_after=new_after,
        order=req.order,
        items_by_id=items_by_id,
        position=int(manager.latest_status.get("position") or 0),
        resume=manager.latest_status.get("state") in ("playing", "paused"),
    )
    try:
        result = manager.apply_reorder(plan)
        return {"ok": True, "anchor_index": new_status_idx, **result}
    except Exception as e:
        logger.error(f"reorder failed: {e}", exc_info=True)
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
        ok = manager.jump(req.index)
        return {"ok": ok, "index": req.index}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


def _filler_append(item, gen: int) -> Optional[bool]:
    """Append one filler item, retrying through control-link blips — a DHT
    announce window can drop a single add while the audio stream itself rides
    on, and silently losing an already-fetched track leaves a "buffering"
    ghost in the album UI. None = generation superseded (caller must stop);
    False = dropped after retries."""
    for attempt in range(4):
        if attempt:
            time.sleep(5)
        added = manager.append([item], generation=gen)
        if added is None:
            return None
        if added:
            return True
    logger.warning("filler: dropped %s — %s after add retries",
                   item.artist, item.title)
    return False


def _owned_filler(items: list, gen: int) -> None:
    """Append the rest of an owned set as each item is ready — file:// instantly,
    an m4a after its in-memory transcode (inside the HQP mirror) — in order,
    stopping if a new playback supersedes this one (generation)."""
    for item in items:
        if _filler_append(item, gen) is None:
            return


def _add_owned(rows: list, *, clear_first: bool, position: str = "end") -> int:
    """THE single path for queueing owned tracks. Rows (id, ...) become
    full-metadata QueueItems; the active backend mirrors them as needed
    (HQPlayer: file:// URIs, m4a via in-memory FLAC transcode). When any
    track needs transcoding the queue is rolled in — the first track is
    added (and, for a replace, played) now, the rest fill in behind via a
    background filler — so a slow transcode never blocks the add. A native-
    only set takes the fast add-everything-at-once path. ``clear_first``
    replaces the queue (and starts playback); otherwise tracks append
    (``position`` next|end). Returns the count queued (rolling appends
    finish async)."""
    from streaming.local import TRANSCODE_FORMATS
    items = queue_mod.items_for_media_ids([r["id"] for r in rows])
    needs_roll = any((it.source.get("format") or "").upper() in TRANSCODE_FORMATS
                     for it in items)

    if not needs_roll:
        if clear_first:
            added, _gen = manager.replace_queue(items, play=True, probe_first=True)
        else:
            added = manager.append(items, position) or 0
        return added

    # Rolling: add/play the first now, background-fill the rest in order.
    first, rest = items[0], items[1:]
    if clear_first:
        added, gen = manager.replace_queue([first], play=True)
    else:
        added = manager.append([first], position) or 0
        gen = manager.queue.generation
    if added and rest:
        threading.Thread(target=_owned_filler, args=(rest, gen),
                         daemon=True, name="owned-fill").start()
    return len(items) if added else 0


@router.post("/play-track")
def play_track(req: PlayTrackRequest):
    """Replace the queue with a single track and play it."""
    row = _db_query_one("""
        SELECT mf.id, mf.file_path, mf.file_format, t.title, a.name as artist, al.title as album
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

    sessions.rotate_session(manager.queue, 'track',
                            seed_media_file_id=req.track_id)

    try:
        added = _add_owned([row], clear_first=True)
        _exit_radio_mode()
        if not added:
            raise HTTPException(
                status_code=503,
                detail="The playback output did not accept the track — try again.",
            )
        return {
            "ok": True,
            "artist": row["artist"],
            "title": row["title"],
            "album": row["album"],
        }
    except HTTPException:
        raise
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

    sessions.rotate_session(
        manager.queue,
        'album',
        origin_album_id=str(best_album["id"]),
        seed_media_file_id=rows[0]["id"],
    )

    try:
        items = queue_mod.items_for_media_ids([r["id"] for r in rows])
        added, _gen = manager.replace_queue(items, play=True)
        _exit_radio_mode()

        if added < len(rows):
            raise HTTPException(
                status_code=503,
                detail=f"HQPlayer added {added} of {len(rows)} tracks "
                       "(connection unstable). Try again.",
            )
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
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# -- Phantom-album streaming buffer policy ------------------------------------
_PHANTOM_LEAD_CAP_SECONDS = 240.0    # never pre-buffer more than ~4 min of audio
_PHANTOM_LEAD_TRACK_TIMEOUT = 60.0   # cap the per-track wait while pre-buffering


def _phantom_lead_seconds(rtf: float, queries) -> float:
    """How much contiguous audio to buffer before starting playback, from the
    measured real-time factor (wall-fetch seconds per audio second of track 0 —
    the channel-speed signal). Fetching is now SEQUENTIAL (one stream), so a
    single stream keeps up iff rtf <= 1:

      rtf <= 1   one stream outruns playback → start at once (lead 0), ASAP.
      rtf  > 1   a per-track deficit of (rtf-1)·duration accrues → pre-buffer
                 proportional to the deficit (≈ one track at rtf≈2), capped so the
                 user never waits more than the cap; a very slow channel still
                 eventually drains, but the opening plays seamlessly.
    """
    if rtf <= 1.0:
        return 0.0
    durs = [q.duration for q in queries if q.duration]
    d_avg = (sum(durs) / len(durs)) if durs else 60.0
    return min(_PHANTOM_LEAD_CAP_SECONDS, d_avg * min(rtf - 1.0, 1.0))


def _phantom_filler(proxy, tokens: list, start_index: int, gen: int) -> None:
    """Rolling-append the rest of a phantom set as each track finishes
    fetching, in order — the backend only ever receives a ready buffer.
    Waits are UNBOUNDED: the fetch pipeline is globally sequential, so a
    token's turn can be tens of minutes away behind other queued albums —
    "not fetched yet" is not an error (a per-track timeout here silently
    dropped every track whose turn hadn't come, so a queued album arrived
    as just its last few tracks). A track is skipped only when its fetch
    actually failed; a replaced session wakes every waiter (superseded)
    and the generation guard stops the filler."""
    for j in range(start_index, len(tokens)):
        try:
            e = proxy.wait_ready(tokens[j], timeout=None)
        except KeyError:
            return   # set dropped: a new session replaced these tokens
        if e.audio is None:
            continue   # fetch failed on every provider (or superseded) — skip
        item = queue_mod.item_for_proxy_token(tokens[j])
        if item is None:
            continue
        if _filler_append(item, gen) is None:
            return   # user moved to another queue → stop appending


def _phantom_insert_next(proxy, tokens: list, gen: int) -> None:
    """Wait for EVERY queued token (unbounded, same rationale as the filler),
    then seamless-insert the whole block after the playing slot — 'next'
    needs the block in hand, and on a backlogged channel readiness is far
    away, so this runs off the request thread. The generation guard inside
    append() discards the block if the queue was replaced meanwhile."""
    ready_items = []
    for tok in tokens:
        try:
            e = proxy.wait_ready(tok, timeout=None)
        except KeyError:
            return   # set dropped: a new session replaced these tokens
        if e.audio is None:
            continue
        item = queue_mod.item_for_proxy_token(tok)
        if item is not None:
            ready_items.append(item)
    if ready_items:
        manager.append(ready_items, "next", generation=gen)


def _parallel_resolve(provider, queries: list) -> list:
    """Resolve every query on a provider concurrently (the fast availability
    pass, no downloads). Returns a list parallel to `queries` of ResolvedSource
    / None (None = not found on this provider)."""
    from concurrent.futures import ThreadPoolExecutor

    def one(q):
        try:
            return provider.resolve(q)
        except Exception:
            return None

    if not queries:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(queries)),
                            thread_name_prefix="resolve") as ex:
        return list(ex.map(one, queries))


def _resolve_waterfall(queries: list) -> list:
    """Per-track lossless-first fallback CHAIN ``[(provider, source_id), ...]``.
    Each track resolves to its best provider (Deezer FLAC before YouTube lossy);
    the chain then appends LOWER-preference providers (source_id=None, resolved
    lazily inside fetch) as DOWNLOAD-failure fallbacks — so a track Deezer has but
    can't serve in FLAC (region/licensing) still streams from YouTube instead of
    just dropping. An empty chain == no provider resolved it (truly unavailable)."""
    from streaming import service as streaming_service

    provs = streaming_service.providers_preferred()
    best = [None] * len(queries)            # index into provs that resolved
    sids = [None] * len(queries)
    durs = [None] * len(queries)            # provider-reported duration (s), for length_ms backfill
    arts = [None] * len(queries)            # provider album art, for the CAA-cover fallback
    pending = list(range(len(queries)))
    for pi, prov in enumerate(provs):
        if not pending:
            break
        if not prov.supports_resolve:
            continue                        # can't pre-resolve; appears only as a lazy fallback
        res = _parallel_resolve(prov, [queries[i] for i in pending])
        still = []
        for i, r in zip(pending, res):
            if r is not None:
                best[i], sids[i], durs[i], arts[i] = pi, r.source_id, r.duration, r.artwork_url
            else:
                still.append(i)
        pending = still

    for q, d, art in zip(queries, durs, arts):
        # Only MB-length-less tracks — the virtual duration is a FALLBACK, never an
        # override of a known MB length (that is the canonical display; the resolved
        # source may be a mismatch at a different length).
        if d and d > 0 and q.track_id and not q.duration:
            _resolved_durations[q.track_id] = float(d)
        if art and q.track_id:
            _resolved_artwork[q.track_id] = art

    chains = []
    for i in range(len(queries)):
        if best[i] is not None:
            chain = [(provs[best[i]], sids[i])]
            chain += [(provs[pi], None) for pi in range(best[i] + 1, len(provs))]
        else:
            chain = [(p, None) for p in provs if not p.supports_resolve]
        chains.append(chain)
    return chains


def _provider_label(items: list) -> Optional[str]:
    """Preferred provider id(s) for a session — 'deezer', 'youtube', or
    'deezer+youtube'. From each track's chain head; the actual served provider may
    differ if a download fell back, but this is just the informational label."""
    ids = sorted({chain[0][0].manifest.id for _q, chain in items if chain})
    return "+".join(ids) if ids else None


def _mix_quality(n_lossless: int, n_lossy: int) -> Optional[str]:
    """Album-level quality from the per-track lossless/lossy split — drives the
    badge so it stops claiming pure 'Lossless' when most tracks stream from a
    lossy fallback. Returns the dominant tier (mostly_* when the album mixes both)."""
    if not (n_lossless or n_lossy):
        return None
    if not n_lossy:
        return "lossless"
    if not n_lossless:
        return "lossy"
    return "mostly_lossless" if n_lossless > n_lossy else "mostly_lossy"


def _artist_alts(name: str) -> tuple:
    """MB-canonical alternates (name + aliases) for a credited artist name, for
    provider resolve retries. Empty without the local MB dump (optional layer)."""
    import mb_backend as mb
    if not name or not mb.LOCAL_DUMP:
        return ()
    return tuple(mb.artist_alt_names(name))


def _phantom_track_query(track_id: str):
    """Build a TrackQuery for one phantom track from album_tracks, or None."""
    from streaming.base import TrackQuery
    row = _db_query_one("""
        SELECT t.title, atr.length_ms, al.title AS album, al.cover_url,
               (SELECT ar.name FROM track_artists ta
                  JOIN artists ar ON ar.id = ta.artist_id
                WHERE ta.track_id = t.id
                ORDER BY (ta.role = 'primary') DESC LIMIT 1) AS artist
        FROM tracks t
        JOIN album_tracks atr ON atr.track_id = t.id
        JOIN albums al ON al.id = atr.album_id
        WHERE t.id = %(tid)s::uuid
        ORDER BY (al.cover_url IS NOT NULL) DESC, (atr.length_ms IS NOT NULL) DESC, al.id
        LIMIT 1
    """, {"tid": track_id})
    if not row or not row["title"]:
        return None
    return TrackQuery(
        artist=row["artist"] or "", title=row["title"], album=row["album"],
        artist_alts=_artist_alts(row["artist"]),
        duration=(float(row["length_ms"]) / 1000.0 if row["length_ms"] else None),
        track_id=track_id, cover_url=row["cover_url"])


def _phantom_album_queries(album_id: str) -> list:
    """Ordered TrackQuery list for a phantom album's tracklist (album_tracks)."""
    from streaming.base import TrackQuery
    rows = _db_query("""
        SELECT t.id::text AS track_id, t.title, al.title AS album, al.cover_url,
               atr.length_ms,
               (SELECT ar.name FROM track_artists ta
                  JOIN artists ar ON ar.id = ta.artist_id
                WHERE ta.track_id = t.id
                ORDER BY (ta.role = 'primary') DESC LIMIT 1) AS artist
        FROM album_tracks atr
        JOIN tracks t ON t.id = atr.track_id
        JOIN albums al ON al.id = atr.album_id
        WHERE atr.album_id = %(album_id)s
        ORDER BY atr.disc, atr.position
    """, {"album_id": album_id})
    alts = {n: _artist_alts(n) for n in {r["artist"] for r in rows if r["artist"]}}
    return [
        TrackQuery(
            artist=r["artist"] or "", title=r["title"], album=r["album"],
            artist_alts=alts.get(r["artist"], ()),
            duration=(float(r["length_ms"]) / 1000.0 if r["length_ms"] else None),
            track_id=r["track_id"], cover_url=r["cover_url"])
        for r in rows if r["title"]
    ]


# Phantom-availability cache: track ids a provider can't resolve, per
# (album_id, provider_id). One resolve pass is tens of provider searches — far
# too slow to redo on every album-page view, so cache it (catalogs are stable).
_availability_cache: dict[tuple, tuple] = {}   # key -> (timestamp, frozenset(unavailable))
_AVAILABILITY_TTL_S = 3600.0


@router.get("/phantom-availability/{album_id}")
def phantom_availability(album_id: str) -> dict:
    """Track ids of a phantom album NO provider can stream, so the album page can
    dim + disable them up front (no need to stream first). Tries every provider
    (lossless-first); a track is unavailable only if none has it, and available if
    ANY of its tracklist rows resolves (the same title can appear at several
    durations — one match is enough). Cached per album + provider-set."""
    from streaming import service as streaming_service
    if not streaming_service.is_enabled():
        return {"unavailable": []}
    provs = streaming_service.providers_preferred()
    if not provs:
        return {"unavailable": []}

    key = (album_id, tuple(p.manifest.id for p in provs))
    now = time.time()
    hit = _availability_cache.get(key)
    if hit and now - hit[0] < _AVAILABILITY_TTL_S:
        unavailable, quality, durations = hit[1], hit[2], hit[3]
    else:
        queries = _phantom_album_queries(album_id)
        chains = _resolve_waterfall(queries)
        # Per distinct track_id, keep the BEST quality among its rows (the same
        # title at several durations may resolve on different providers). Quality
        # is the chain HEAD (preferred provider); a download-failure fallback can
        # still drop it to lossy at stream time.
        best_lossless = {}
        for q, chain in zip(queries, chains):
            if not chain or not q.track_id:
                continue
            best_lossless[q.track_id] = (best_lossless.get(q.track_id, False)
                                         or chain[0][0].manifest.lossless)
        all_ids = {q.track_id for q in queries if q.track_id}
        unavailable = frozenset(all_ids - set(best_lossless))
        quality = _mix_quality(sum(best_lossless.values()),
                               sum(1 for v in best_lossless.values() if not v))
        # DISPLAY-only durations the resolve recovered for MB-length-less tracks
        # (not persisted — see _resolved_durations).
        durations = {t: _resolved_durations[t] for t in all_ids
                     if t in _resolved_durations}
        _availability_cache[key] = (now, unavailable, quality, durations)

    return {"unavailable": [{"track_id": t} for t in unavailable],
            "quality": quality, "durations": durations}


class PlayPhantomAlbumRequest(BaseModel):
    album_id: str             # UUID of a phantom (not-owned) album
    position: str = "end"     # queue endpoint only: 'next' | 'end'


@router.post("/play-phantom-album")
def play_phantom_album(req: PlayPhantomAlbumRequest):
    """Stream a phantom (not-in-library) album onto HQPlayer.

    Resolves the album's MusicBrainz tracklist to a streaming provider, serves
    the audio through the in-memory media proxy as plain-http URLs, and feeds
    those into HQPlayer's NATIVE playlist exactly like owned ``file://`` tracks
    (proven: HQPlayer plays http FLAC sustained alongside local files).

    Now-Playing shows HQPlayer's generic stream label for these tracks until
    provider-sourced metadata is wired into the status poller (next step)."""
    from streaming import service as streaming_service

    if not streaming_service.is_enabled():
        raise HTTPException(status_code=503, detail="Streaming preview is disabled")

    queries = _phantom_album_queries(req.album_id)
    if not queries:
        raise HTTPException(status_code=404, detail="Phantom album not found or has no tracklist")

    proxy = streaming_service.get_proxy()
    if proxy is None:
        raise HTTPException(status_code=503, detail="No streaming provider available")

    # Per-track resolve waterfall → a lossless-first fallback CHAIN per track
    # (Deezer FLAC, then YouTube). The UI greys out only tracks NO provider can
    # stream; the chain also covers download-time failures (Deezer resolves but
    # serves no FLAC) by falling through to YouTube during fetch.
    chains = _resolve_waterfall(queries)
    items = [(q, ch) for q, ch in zip(queries, chains) if ch]
    missing = [q for q, ch in zip(queries, chains) if not ch]
    missing_payload = [{"track_id": q.track_id, "title": q.title} for q in missing]

    if not items:
        # No provider has any track — leave HQPlayer untouched; the UI disables
        # Stream all and greys out every row.
        return {
            "ok": True, "provider": None,
            "album": queries[0].album, "artist": queries[0].artist,
            "track_count": 0, "requested": len(queries), "missing": missing_payload,
        }

    avail_q = [q for q, _ch in items]
    tokens = proxy.start_session(items)        # priority-fetches track 0

    # Adaptive rolling buffer. Track 0 is fetched alone (full bandwidth) for the
    # fastest safe start; its measured real-time factor sizes how much audio we
    # pre-buffer before playing so transitions don't gap on a slow channel; the
    # tail then streams in via a background filler that appends each track as it
    # lands. The UI shows a buffering state until this returns.
    try:
        proxy.wait_ready(tokens[0])
    except (TimeoutError, KeyError):
        pass
    rtf = proxy.fetch_rtf(tokens[0]) or 1.0
    proxy.prefetch_from(1)                     # fan out the tail concurrently
    lead_target = _phantom_lead_seconds(rtf, avail_q)

    # Event-driven pre-buffer: block on each boundary track's ready event in
    # order until the contiguous buffered audio covers the lead (or the album
    # ends, or a track is too slow — then start with what we have).
    prefix, buffered, next_index = proxy.ready_lead(0)
    while (not prefix or buffered < lead_target) and next_index < len(tokens):
        try:
            proxy.wait_ready(tokens[next_index], timeout=_PHANTOM_LEAD_TRACK_TIMEOUT)
        except (TimeoutError, KeyError):
            break
        prefix, buffered, next_index = proxy.ready_lead(0)

    logger.info("phantom buffer: rtf=%.2f lead=%.0fs → start %d/%d (avail %d, missing %d)",
                rtf, lead_target, len(prefix), len(queries), len(avail_q), len(missing))

    if not prefix:
        raise HTTPException(status_code=502, detail="No tracks could be fetched from the provider")

    # Snapshot the prior queue into a session, then open a phantom-album one —
    # streamed albums land in the Home shelf like owned albums (origin_album_id
    # is the phantom album's UUID; the seed is its first available track).
    sessions.rotate_session(manager.queue, 'album',
                            origin_album_id=str(req.album_id),
                            seed_track_id=avail_q[0].track_id)

    prefix_items = [it for it in (queue_mod.item_for_proxy_token(t) for t in prefix)
                    if it is not None]
    try:
        added, gen = manager.replace_queue(prefix_items, play=True)
        _exit_radio_mode()

        if not added:
            raise HTTPException(
                status_code=503,
                detail="The playback output did not accept the preview — try again.",
            )
        # Roll the remaining tracks in as they finish fetching (background).
        if next_index < len(tokens):
            threading.Thread(
                target=_phantom_filler, args=(proxy, list(tokens), next_index, gen),
                daemon=True, name="phantom-filler").start()
        return {
            "ok": True,
            "album": avail_q[0].album,
            "artist": avail_q[0].artist,
            "provider": _provider_label(items),
            "track_count": added,                        # streaming now
            "buffering": max(0, len(avail_q) - added),   # rolling in via the filler
            "requested": len(queries),                   # tracklist size
            "missing": missing_payload,                  # not found on ANY provider
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


class PlayPhantomTrackRequest(BaseModel):
    track_id: str             # UUID of a phantom (not-owned) track
    position: str = "end"     # queue endpoint only: 'next' | 'end'


@router.post("/play-phantom-track")
def play_phantom_track(req: PlayPhantomTrackRequest):
    """Stream a single phantom track onto HQPlayer (replaces the queue), the
    streaming counterpart of clicking an owned track. Resolves the track via the
    preferred provider; returns track_count=0 + the track in `missing` when the
    provider has no match (the UI greys the row)."""
    from streaming import service as streaming_service

    if not streaming_service.is_enabled():
        raise HTTPException(status_code=503, detail="Streaming preview is disabled")

    q = _phantom_track_query(req.track_id)
    if q is None:
        raise HTTPException(status_code=404, detail="Phantom track not found")
    missing = [{"track_id": req.track_id, "title": q.title}]
    chain = _resolve_waterfall([q])[0]        # Deezer lossless first, YouTube fallback
    if not chain:
        return {"ok": True, "provider": None,
                "track_count": 0, "requested": 1, "missing": missing}

    proxy = streaming_service.get_proxy()
    tokens = proxy.start_session([(q, chain)])
    try:
        e = proxy.wait_ready(tokens[0])
    except (TimeoutError, KeyError):
        e = None
    if e is None or e.audio is None:
        raise HTTPException(status_code=502, detail="This track isn't available to stream right now.")

    # A single streamed track is a 'track'-origin session seeded by its UUID.
    sessions.rotate_session(manager.queue, 'track',
                            seed_track_id=req.track_id)

    try:
        item = queue_mod.item_for_proxy_token(tokens[0])
        added, _gen = manager.replace_queue([item] if item else [], play=True)
        _exit_radio_mode()
        if not added:
            raise HTTPException(
                status_code=503,
                detail="The playback output did not accept the preview — try again.")
        return {"ok": True, "provider": e.provider.manifest.id, "track_count": 1,
                "requested": 1, "missing": [], "artist": q.artist, "album": q.album}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/queue-phantom-track")
def queue_phantom_track(req: PlayPhantomTrackRequest):
    """Append one phantom track to the HQPlayer queue (streamed, no replace).
    Same async model as the album endpoint: the response reports the resolve
    outcome, and the track rolls into the queue when its (sequential, possibly
    backlogged) fetch turn completes — an in-request wait here timed out and
    502'd whenever other albums were still fetching ahead of it."""
    from streaming import service as streaming_service
    if not streaming_service.is_enabled():
        raise HTTPException(status_code=503, detail="Streaming preview is disabled")

    q = _phantom_track_query(req.track_id)
    if q is None:
        raise HTTPException(status_code=404, detail="Phantom track not found")
    chain = _resolve_waterfall([q])[0]        # Deezer lossless first, YouTube fallback
    if not chain:
        return {"ok": True, "provider": None, "track_count": 0, "requested": 1,
                "missing": [{"track_id": req.track_id, "title": q.title}]}

    if manager.active is None:
        raise HTTPException(status_code=503,
                            detail="No active playback output — select one and try again.")
    proxy = streaming_service.get_proxy()
    tokens = proxy.add_tracks([(q, chain)])
    gen = manager.queue.generation
    if req.position == "next":
        threading.Thread(target=_phantom_insert_next, args=(proxy, list(tokens), gen),
                         daemon=True, name="phantom-queue-next").start()
    else:
        threading.Thread(target=_phantom_filler, args=(proxy, list(tokens), 0, gen),
                         daemon=True, name="phantom-queue").start()
    return {"ok": True, "provider": chain[0][0].manifest.id, "track_count": 1,
            "requested": 1, "missing": []}


@router.post("/queue-phantom-album")
def queue_phantom_album(req: PlayPhantomAlbumRequest):
    """Append a phantom album to the HQPlayer queue (streamed, rolling-append)."""
    from streaming import service as streaming_service
    if not streaming_service.is_enabled():
        raise HTTPException(status_code=503, detail="Streaming preview is disabled")

    queries = _phantom_album_queries(req.album_id)
    if not queries:
        raise HTTPException(status_code=404, detail="Phantom album not found or has no tracklist")

    proxy = streaming_service.get_proxy()
    if proxy is None:
        raise HTTPException(status_code=503, detail="No streaming provider available")

    chains = _resolve_waterfall(queries)      # Deezer lossless first, YouTube fallback
    items = [(q, ch) for q, ch in zip(queries, chains) if ch]
    missing = [q for q, ch in zip(queries, chains) if not ch]
    missing_payload = [{"track_id": q.track_id, "title": q.title} for q in missing]
    if not items:
        return {"ok": True, "provider": None, "track_count": 0,
                "requested": len(queries), "missing": missing_payload}

    avail_q = [q for q, _ch in items]
    # Both positions roll in from a background worker — fetching is sequential
    # and possibly backlogged behind earlier albums, so readiness can be far
    # away. Fail loudly NOW if no output is active — otherwise we'd return ok
    # and silently drop everything. (This used to probe HQPlayer directly — a
    # pre-refactor leftover that 503'd every DLNA/local/browser node even
    # though their canonical append cannot be "unreachable".)
    if manager.active is None:
        raise HTTPException(status_code=503,
                            detail="No active playback output — select one and try again.")
    tokens = proxy.add_tracks(items)
    # A queue-append doesn't start a new session, so capture the CURRENT
    # generation; the workers abort if the user replaces the queue meanwhile.
    gen = manager.queue.generation
    if req.position == "next":
        threading.Thread(target=_phantom_insert_next, args=(proxy, list(tokens), gen),
                         daemon=True, name="phantom-queue-next").start()
    else:
        # 'end' → roll each available track into the back of the queue as it lands.
        threading.Thread(target=_phantom_filler, args=(proxy, list(tokens), 0, gen),
                         daemon=True, name="phantom-queue").start()
    return {"ok": True, "provider": _provider_label(items),
            "track_count": len(avail_q), "requested": len(queries),
            "missing": missing_payload}


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

    sessions.rotate_session(manager.queue, 'radio',
                            seed_media_file_id=req.track_id)

    try:
        items = queue_mod.items_for_media_ids([r["id"] for r in rows])
        added, _gen = manager.replace_queue(items, play=True)
        _exit_radio_mode()

        if added < len(rows):
            raise HTTPException(
                status_code=503,
                detail=f"HQPlayer added {added} of {len(rows)} tracks "
                       "(connection unstable). Try again.",
            )
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
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/play-tracks")
def play_tracks(req: PlayTracksRequest):
    """Play multiple tracks by IDs."""
    if not req.track_ids:
        raise HTTPException(status_code=400, detail="No track IDs provided")
    if req.origin is not None and req.origin not in _SESSION_ORIGINS:
        raise HTTPException(status_code=400, detail=f"invalid origin: {req.origin}")

    rows = _db_query("""
        SELECT mf.id, mf.file_path, mf.file_format, t.title, a.name as artist, al.title as album
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

    sessions.rotate_session(
        manager.queue,
        req.origin or 'mix',
        origin_album_id=req.origin_album_id,
        seed_media_file_id=rows[0]["id"],
    )

    try:
        added = _add_owned(rows, clear_first=True)
        _exit_radio_mode()
        if added < len(rows):
            raise HTTPException(
                status_code=503,
                detail=f"HQPlayer added {added} of {len(rows)} tracks "
                       "(connection unstable). Try again.",
            )
        return {
            "ok": True,
            "count": len(rows),
            "tracks": [
                {"id": r["id"], "title": r["title"], "artist": r["artist"]}
                for r in rows
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"play-tracks failed: {e}")
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/queue-tracks")
def queue_tracks(req: QueueTracksRequest):
    """Append the given tracks to the current queue, in order, without
    clearing. Used by the per-track "+" button and the "Queue album"
    action — these expect "add exactly these tracks", not the
    similarity-driven batches Radio Mode appends."""
    if not req.track_ids:
        raise HTTPException(status_code=400, detail="No track IDs provided")

    rows = _db_query("""
        SELECT mf.id, mf.file_path, mf.file_format, t.title, a.name as artist, al.title as album
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
        added = _add_owned(rows, clear_first=False, position="end")
        if added < len(rows):
            raise HTTPException(
                status_code=503,
                detail=f"HQPlayer added {added} of {len(rows)} tracks "
                       "(connection unstable). Try again.",
            )
        return {
            "ok": True,
            "count": added,
            "tracks": [
                {"id": r["id"], "title": r["title"], "artist": r["artist"], "album": r["album"]}
                for r in rows
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# -- Radio mode (mixed: owned + streamed phantom) -----------------------------
#
# Radio drifts: each batch is CLAP-similar to the CURRENTLY playing track, mixing
# owned (file://) and a capped number of phantom (streamed) tracks. The status
# observer refills near the end of the queue, so radio runs on. Memory stays bounded by the
# model itself — only the current batch's few phantoms are buffered at once (a
# fraction of a single streamed album), freed as they play and the next batch fills.

_RADIO_BATCH_SIZE = 10          # tracks per fill
_RADIO_MAX_PHANTOM = 4          # cap streamed tracks per batch — owned ones between
                                # them give the single-lane fetcher free buffer time,
                                # so a slow stream can't starve the playhead
_RADIO_PHANTOM_EVERY = 2        # place a phantom only after this many owned (spacing)
_RADIO_REFILL_AT = 3            # refill when <= this many tracks remain ahead
_RADIO_ARTIST_CAP = 2           # tracks per artist within one pool — raw KNN
                                # clusters hard (a Tangerine Dream seed put Edgar
                                # Froese in 10 of the top 30) and radio must not
                                # collapse into a single-artist run
_RADIO_JITTER = 0.2             # ORDER BY score * (1 + JITTER*random()): reshuffles
                                # near-ties so the same seed never yields the same
                                # station twice, while distant pool members still
                                # rank behind close ones in sparse library corners
_radio_played: set = set()      # track UUIDs added this session — never repeat
_radio_refilling = False        # one fill thread at a time
_radio_last_artist: Optional[str] = None   # artist of the last queued track — the
                                # next batch avoids opening with the same artist


def _radio_similar(seed_uuid: str, exclude: set, limit: int) -> list:
    """Mixed (owned + phantom) audio-similar to the seed — the shared two-tier
    scorer (track_similarity.similar_tracks: mean-KNN recall pool, segment
    chamfer + BPM/energy/genre continuity rerank) with radio's artist cap and
    jitter. Rows carry what the batch builder needs: identity + file fields
    (owned) or the MB tracklist fields (phantom)."""
    return track_similarity.similar_tracks(
        seed_uuid, exclude=exclude, limit=limit,
        artist_cap=_RADIO_ARTIST_CAP, jitter=_RADIO_JITTER)


def _radio_interleave(owned: list, phantom: list, prev_artist: Optional[str]) -> list:
    """Order one batch: phantoms spaced out between owned tracks (never first,
    never adjacent — owned tracks between streams give the single-lane fetcher
    buffer time), and consecutive same-artist rows avoided when another candidate
    can take the slot. `prev_artist` is the artist already at the queue tail, so
    a batch doesn't open by echoing the currently playing artist — with a drifting
    seed its own artist is usually the nearest neighbour."""
    owned, phantom = list(owned), list(phantom)
    batch, owned_since, last_artist = [], 0, prev_artist
    while len(batch) < _RADIO_BATCH_SIZE and (owned or phantom):
        take_phantom = not owned or (phantom and owned_since >= _RADIO_PHANTOM_EVERY)
        src = phantom if take_phantom else owned
        pick = next((i for i, r in enumerate(src) if r["artist"] != last_artist), 0)
        row = src.pop(pick)
        batch.append(row)
        owned_since = 0 if take_phantom else owned_since + 1
        last_artist = row["artist"]
    return batch


def _radio_build_batch(seed_uuid: str) -> list:
    """Pick a mixed batch from the seed, cap + space out the phantoms (owned tracks
    between them buffer the next stream), resolve the phantom chains and submit them
    to the proxy. Returns ordered items for the rolling appender (owned: file fields,
    turned into a URI — and transcoded if m4a — at append time; phantom: a proxy
    token). Phantoms that no provider resolves are simply dropped."""
    from streaming import service as streaming_service
    from streaming.base import TrackQuery

    rows = _radio_similar(seed_uuid, _radio_played, _RADIO_BATCH_SIZE * 3)
    owned = [r for r in rows if r["is_owned"]]
    phantom = [r for r in rows if not r["is_owned"]]
    proxy = streaming_service.get_proxy() if streaming_service.is_enabled() else None
    if proxy is None:
        phantom = []
    phantom = phantom[:_RADIO_MAX_PHANTOM]

    batch_rows = _radio_interleave(owned, phantom, _radio_last_artist)

    # Resolve the batch's phantoms (one waterfall pass) and submit the resolvable
    # ones to the proxy for sequential buffering; keep a token per batch position.
    p_positions = [i for i, r in enumerate(batch_rows) if not r["is_owned"]]
    p_queries = [TrackQuery(
        artist=batch_rows[i]["artist"] or "", title=batch_rows[i]["title"],
        album=batch_rows[i]["phantom_album"] or "",
        artist_alts=_artist_alts(batch_rows[i]["artist"]),
        duration=(float(batch_rows[i]["length_ms"]) / 1000.0
                  if batch_rows[i]["length_ms"] else None),
        track_id=batch_rows[i]["track_id"]) for i in p_positions]
    chains = _resolve_waterfall(p_queries) if p_queries else []
    avail = [(q, ch) for q, ch in zip(p_queries, chains) if ch]
    avail_pos = [pos for pos, ch in zip(p_positions, chains) if ch]
    tokens = proxy.add_tracks(avail) if avail else []
    pos_token = dict(zip(avail_pos, tokens))

    batch = []
    for i, r in enumerate(batch_rows):
        if r["is_owned"]:
            batch.append({"kind": "owned", "media_file_id": r["media_file_id"],
                          "file_path": r["file_path"], "file_format": r["file_format"],
                          "track_uuid": r["track_id"], "artist": r["artist"]})
        elif i in pos_token:
            batch.append({"kind": "phantom", "token": pos_token[i],
                          "track_uuid": r["track_id"], "artist": r["artist"]})
        # a phantom no provider resolved → dropped (radio keeps flowing)
    return batch


def _radio_append_batch(batch: list, gen: int) -> None:
    """Rolling-append a batch: owned rows go to the HQPlayer queue at once, a
    phantom row waits for the proxy to buffer it then appends its http URI — in
    batch order, so the playhead never reaches a not-yet-ready stream. A phantom
    that never buffers is skipped. Stops if radio is turned off or a new playback
    session supersedes this one (generation)."""
    global _radio_last_artist
    from streaming import service as streaming_service
    proxy = streaming_service.get_proxy()
    for entry in batch:
        if not manager.radio_mode or manager.queue.generation != gen:
            return
        if entry["kind"] == "phantom":
            if proxy is None:
                continue
            try:
                e = proxy.wait_ready(entry["token"])
            except (TimeoutError, KeyError):
                continue                       # never buffered → skip
            if e is None or e.audio is None:
                continue
            item = queue_mod.item_for_proxy_token(entry["token"])
        else:
            owned = queue_mod.items_for_media_ids([entry["media_file_id"]])
            item = owned[0] if owned else None
        if item is None:
            continue
        if _filler_append(item, gen) is None:
            return
        _radio_played.add(entry["track_uuid"])
        _radio_last_artist = entry["artist"]


def _radio_fill(seed_uuid: str, gen: int) -> None:
    """Pick + append one radio batch from the seed. Background thread (the phantom
    resolve + buffer is slow); clears the one-at-a-time guard on exit."""
    global _radio_refilling
    try:
        if manager.radio_mode and manager.queue.generation == gen:
            batch = _radio_build_batch(seed_uuid)
            if batch:
                _radio_append_batch(batch, gen)
    except Exception:
        logger.exception("radio fill failed")
    finally:
        _radio_refilling = False


def _radio_refill_observer(new_data: dict, item) -> None:
    """Mixed-radio refill: when the playhead nears the end of the radio
    queue, append another drifting batch (owned + a few streamed phantoms,
    seeded from the current track) so radio runs on. Registered as a
    manager status observer — runs on every status tick. Background —
    the phantom resolve+buffer is slow; one fill at a time."""
    global _radio_refilling
    if not manager.radio_mode or _radio_refilling:
        return
    idx = new_data.get("track_index")
    qlen = len(manager.queue)
    if isinstance(idx, int) and qlen > 0 and idx >= qlen - _RADIO_REFILL_AT:
        seed = item.track_id if item else None
        if seed:
            _radio_refilling = True
            threading.Thread(
                target=_radio_fill, args=(seed, manager.queue.generation),
                daemon=True, name="radio-refill").start()


manager.subscribe_status(_radio_refill_observer)


@router.get("/similar/{track_uuid}")
def similar_tracks(track_uuid: str, limit: int = 7):
    """Now Playing 'Similar' shelf — the same two-tier scorer radio drifts on
    (track_similarity.similar_tracks), deterministic (no jitter). Mixed rows:
    owned ones carry media_file_id (play by file), phantom ones track_id +
    cover_url (stream). `similarity` is 1 - the ranking score (chamfer +
    BPM/energy/genre continuity), so the shown numbers are monotonic with
    the list order — the bare chamfer cosine wasn't."""
    rows = track_similarity.similar_tracks(track_uuid, limit=limit)
    for r in rows:
        del r["file_path"], r["file_format"], r["phantom_album"], r["length_ms"]
    return {"results": rows}


class RadioStartRequest(BaseModel):
    track_id: Optional[int] = None    # media_file_id of an owned seed
    track_uuid: Optional[str] = None  # track UUID of a phantom (streamed) seed


@router.post("/radio/start")
def radio_start(req: RadioStartRequest):
    """Start drifting radio from a seed: keep the current track playing, clear what
    follows, and fill the queue in the background with a mixed (owned + streamed
    phantom) CLAP-similar batch. The poller refills near the end so it runs on.
    Async — the phantom resolve + buffer is slow; the toggle returns at once and the
    queue grows behind the seed (owned instantly, phantoms as they buffer)."""
    global _radio_played, _radio_refilling, _radio_last_artist

    # Seed by track UUID (a streamed phantom row has no media_file) or by
    # media_file_id (owned). Either way radio keys on the track's CLAP embedding.
    if req.track_uuid:
        seed_uuid = req.track_uuid
    elif req.track_id:
        seed = _db_query_one(
            "SELECT track_id::text AS tid FROM media_files WHERE id = %(id)s",
            {"id": req.track_id})
        if not seed:
            raise HTTPException(status_code=404, detail="Track not found")
        seed_uuid = seed["tid"]
    else:
        raise HTTPException(status_code=400, detail="track_id or track_uuid required")

    # Radio needs the seed's audio embedding to find similar tracks. A just-started
    # phantom may not be analysed yet — fail clearly instead of an empty station.
    # The same lookup grabs the seed's artist: the first batch avoids opening with
    # it (the playing track's own artist is usually its nearest neighbour).
    seed_row = _db_query_one("""
        SELECT a.name AS artist
        FROM tracks t
        JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
        JOIN artists a ON a.id = ta.artist_id
        WHERE t.id = %(t)s::uuid
          AND EXISTS (SELECT 1 FROM embeddings e WHERE e.track_id = t.id)
    """, {"t": seed_uuid})
    if not seed_row:
        raise HTTPException(
            status_code=409,
            detail="This track isn't analysed yet — play it a moment, then start radio.")

    # seed_uuid is resolved for BOTH an owned (req.track_id) and a phantom
    # (req.track_uuid) seed — pass it as the logical seed so a radio started
    # from a streamed track gets a proper session card (previously this passed
    # the owned-only req.track_id, which was None for a phantom seed).
    sessions.rotate_session(manager.queue, 'radio', seed_track_id=seed_uuid,
                            seed_media_file_id=req.track_id)
    # Clear everything around the reading slot — the seed plays on while the
    # batch flows in behind. New generation supersedes any prior filler.
    gen = manager.clear_for_radio()

    _radio_played = {seed_uuid}
    _radio_last_artist = seed_row["artist"]
    _radio_refilling = True                 # hold the refill until the first batch lands
    manager.set_radio_mode(True)            # flips the UI toggle via SSE at once

    threading.Thread(target=_radio_fill, args=(seed_uuid, gen),
                     daemon=True, name="radio-fill").start()
    return {"ok": True, "seed_id": req.track_id}


@router.post("/radio/stop")
def radio_stop():
    """Flip radio mode off. The queue is left alone — radio's
    'replace' behaviour only happens on start, and turning it off
    just means future track-ends won't trigger an append."""
    _exit_radio_mode()
    return {"ok": True, "radio_mode": False}


def _exit_radio_mode() -> None:
    """Drop the radio flag and wake SSE so the Now Playing toggle
    snaps off immediately. Called from every endpoint that
    replaces the queue with explicit user-picked content (play-
    track, play-album, play-tracks, play-similar). Append-only
    paths like queue-tracks don't touch the flag — they extend
    the radio rather than ending it."""
    manager.set_radio_mode(False)


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
