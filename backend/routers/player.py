"""
REST API for HQPlayer control.

Mirrors MCP server patterns (lazy singleton, path conversion, tracker registration)
but exposed as HTTP endpoints for the Web UI.
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import track_similarity
import zero_shot
from config import settings
from db_pool import (
    db_query as _db_query,
    db_query_one as _db_query_one,
)
from ensemble_instruments import present_instruments
from lrclib import LrclibService
from playback import queue as queue_mod
from playback import sessions
from playback.base import OutputRefused, ReorderPlan, new_id
from playback.dlna_backend import AnswerRefused, read_answer
from playback import hqp_link
from playback.hqp_link import Building, Dropped, NotAnswering
from playback.hqp_meter import meter as hqp_meter
from playback import substitute
from playback.manager import (NotHere, OutputHeld, QueueChanged, active_hqp_endpoint, manager,
                              reach_key, said_with)
from playback.queue import resolved_artwork as _resolved_artwork
from playback.queue import resolved_durations as _resolved_durations
from playback.sessions import _SESSION_ORIGINS
from serving import EventStream
from sql_queries import ALBUM_FILES, HERE, TRACK_FILES, best_rip_order, here, owned_rank, rip_quality

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/player", tags=["player"])

def _hqp_configured() -> bool:
    """The person chose an HQPlayer (hqp_library.driven). The environment's
    HQPLAYER_ENABLED is no choice: an install older than the Output picker
    had it saved as one, once (db_migrate's hqp_choice_v1)."""
    import hqp_library
    return hqp_library.driven() is not None


def start_status_poller():
    """Attach the HQPlayer backend — and with it the 1s status poll — only when
    HQPlayer is the chosen output.

    Being *configured* used to be enough, which meant a node whose owner listens
    through the browser or a DLNA renderer still opened a socket to HQPlayer
    every second, and saving an HQPlayer address could take the output over from
    whatever was selected. Configuration is not consent."""
    from routers.settings import _read
    if _read("output.type") != "hqplayer":
        logger.info("HQPlayer is not the selected output — not polling it")
        return
    if not _hqp_configured():
        logger.info("No HQPlayer configured — playback output idle")
        return
    manager.activate("hqplayer")


def stop_status_poller():
    """Detach the active backend WITHOUT touching playback — clearing the
    HQPlayer host or shutting the app down must leave an externally-
    running player alone (the next start adopts its queue). A lent output
    is detached already: the app stopping while a benchmark still puts
    HQPlayer back (hqp_benchmark.shutdown) leaves it to that."""
    if manager.held is None:
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
    # A caller holding the canonical album UUID (the AI assistant, which reads
    # the catalog, not the filesystem) sends album_id — owned or phantom, the
    # endpoint routes it. The fuzzy name path stays for callers that only have
    # what the user said, and stays OWNED-only: a trigram scan of 3M phantom
    # titles is the seq-scan the discovery engine exists to avoid.
    album_id: Optional[str] = None
    album_name: str = ""
    artist_name: str = ""

class PlaySimilarRequest(BaseModel):
    track_id: str          # canonical track UUID — owned or phantom seed
    limit: int = 15

class PlayTracksRequest(BaseModel):
    track_ids: list[int]
    # Session origin hint: album "Play all" sends 'album' (+ origin_album_id)
    # so a played-whole-album isn't labelled "Mix". Defaults to 'mix'.
    origin: Optional[str] = None
    origin_album_id: Optional[str] = None

class QueueTracksRequest(BaseModel):
    track_ids: list[int]
    position: str = "end"   # 'next' | 'end'

class EntityRef(BaseModel):
    kind: str              # 'track' | 'album'
    id: str                # canonical UUID — owned or phantom, caller needn't know

class QueueEntitiesRequest(BaseModel):
    items: list[EntityRef] = Field(..., max_length=50)
    # play-entities only: assembling a playlist is not the same act as putting
    # one on, and the user says which ("build me a set" vs "play these").
    autoplay: bool = True
    # queue-entities only: 'next' | 'end' — the whole list, owned and
    # streamed slots alike, goes there at once
    position: str = "end"

class PlaySessionRequest(BaseModel):
    session_id: str

class QueueSessionRequest(BaseModel):
    session_id: str
    position: str = "end"   # 'next' | 'end'

class JumpRequest(BaseModel):
    index: int  # the queue's 1-based slot, as the status's track_index names it
    # The track the client saw in that slot: the tap names it, never the slot
    # alone — a queue played from elsewhere, or a mutation in flight (a new
    # queue handed over, a new order), moves slots under the sheet
    track_id: Optional[str] = None

class RemoveRequest(BaseModel):
    index: int  # the queue's 1-based slot (HQPlayer's entry differs past a gap)
    # The identity the client saw in that slot. A queue that moved under
    # the tap (radio refill, another device, the client's own optimistic
    # re-render) makes the removal refuse instead of taking whatever now
    # sits at the index — an index alone cannot be made idempotent.
    track_id: Optional[str] = None

class ReorderRequest(BaseModel):
    # Full new order of universal track UUIDs (owned + phantom), current
    # track included at its original position. media_file_ids can't key
    # this: phantoms have none, so radio queues were unreorderable.
    order: list[str]

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

    if rescan:
        local_devices.rescan()

    outputs = [{
        "type": "hqplayer",
        "available": True,
        "configured": _hqp_configured(),
        "hqplayers": _hqplayer_entries(),
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
    outputs.append({
        "type": "dlna",
        "available": dlna_available,
        # The renderer of the ACTIVE dlna output is configuration — it stays
        # listed even when unreachable (like the HQPlayer row when HQP is
        # off). Once another output is selected it must earn its place via
        # scan/pin.
        "renderers": _dlna_rows(persisted_renderer if _read("output.type") == "dlna" else None),
    })
    outputs.append({"type": "browser", "available": True})

    return {
        "active": {
            # Configured output, not just the attached one: with lazy
            # (re)attach the connection may not exist between sessions,
            # but the user's selection must stay visible in the picker.
            # Same dict the status payload carries — one source of truth.
            **manager.output_info,
            "device_id": _read("output.local_device"),
            "exclusive": bool(_read("output.local_exclusive")),
            "renderer_udn": udn_key((persisted_renderer or {}).get("udn")),
            "stream_quality": _read("output.stream_quality") or "lossless",
        },
        "outputs": outputs,
    }


# -- DLNA discovery --------------------------------------------------------------

# Two renderer tiers served by /outputs. Discovered = the LAST scan's
# snapshot, replaced wholesale each Rescan so powered-off devices drop out
# (in-memory by design). Pinned = manual adds — deliberate config, kept in
# user_settings so they survive restarts.
#
# Pins used to be the ONLY way to populate a Docker node, because multicast
# dies at the bridge. The unicast sweep changed that, but less than it looks,
# and the boundary is a single sharp one: SSDP answers come back from whatever
# source port the device chose, and the bridge's NAT is port-restricted for
# UDP, so only devices replying from 1900 traverse it.
#
# Measured on this network, same request from WSL and from the container:
#   router          replies from 1900   → found from both
#   KANN ULTRA      replies from 40201  → found from WSL, invisible to the container
#   BubbleUPnP      replies from 35510  → same
#
# Both renderers here answer unicast perfectly well; the container simply never
# hears them. MX and the collection window are NOT the factor — the KANN
# answers identically at MX=1/1.5s and MX=2/3.5s. So the sweep earns its place
# for 1900-replying devices, and on Docker pinning a full description URL
# remains the way in for the rest; backend/dlna_locate.py, run where no NAT is
# in the path, prints one.
#
# Both snapshots are REPLACED, never mutated: /outputs runs on a worker
# thread and iterates them while the scan lands in a thread of its own, and a
# dict changing size under an iteration is a RuntimeError. A rebind is
# atomic and a reader keeps the dict it started with.
_dlna_discovered: dict[str, dict] = {}


def udn_key(udn: Optional[str]) -> Optional[str]:
    """A renderer's identity as one string: a UDN is a UUID, and a UUID
    reads the same in either case (RFC 4122)."""
    return udn.strip().lower() if udn else udn


def _dlna_pins() -> dict[str, dict]:
    from routers.settings import _read
    return {udn_key(r["udn"]): r for r in (_read("output.dlna_pinned") or [])}


def _dlna_rows(active: Optional[dict]) -> list:
    """One picker row per renderer, by UDN: where the last scan reached it,
    with every other address it was pinned or chosen at kept behind as a
    fallback, never as a row of its own. A phone added by its LAN address
    and found by a scan through the tunnel was listed twice, and the first
    taps chose the address that no longer answered."""
    rows: dict[str, dict] = {}
    pins = _dlna_pins()
    for record in [*_dlna_discovered.values(), *pins.values(), *([active] if active else [])]:
        key = udn_key(record.get("udn"))
        if not key:
            continue
        seen = rows.get(key)
        # The record seen first wins the row (the scan's, then the pin's);
        # the others add their addresses behind its own
        rows[key] = (remember_location(record) if seen is None
                     else {**seen, "locations": [*seen["locations"], *(
                         loc for loc in remember_location(record)["locations"]
                         if loc not in seen["locations"])][:4]})
        rows[key]["udn"] = key
    for key, row in rows.items():
        if key in pins:
            row["pinned"] = True
    return list(rows.values())


# -- HQPlayer discovery -----------------------------------------------------------
#
# HQPlayer answers its own discovery datagram (hqp_library.discover) on the
# control port, so the sweep that asks every LAN address for a renderer asks
# it for an HQPlayer in the same breath. The last scan's snapshot, like the
# renderers'; the HQPlayers the owner chose or added are rows in
# hqp_endpoints (hqp_library.register) and are listed whether or not the box
# is on — configuration, like a pinned renderer.
_hqp_discovered: dict[tuple, dict] = {}


def _hqp_key(host: str, port: int) -> tuple:
    import hqp_library
    return hqp_library.address_key(host, port)


def _hqplayer_entries() -> list:
    """Every HQPlayer the picker can offer: the registered rows, what the
    last scan found, and the configured address itself (kept listed like
    the active renderer, so the selection stays visible while its box is
    off). A registered row keeps its own address as the one to dial — on a
    Docker node that is host.docker.internal for a Desktop the scan sees at
    the LAN address."""
    import hqp_library
    from auth_hmac import is_own_address
    entries: dict[tuple, dict] = {}
    for r in _db_query("""
        SELECT e.id, e.name, e.host, e.port, e.product, e.library_hash IS NOT NULL AS synced,
               (SELECT count(*) FROM album_variants av JOIN hqp_library_files f ON f.album_variant_id = av.id
                 WHERE av.hqp_endpoint_id = e.id) AS files,
               (SELECT count(*) FROM hqp_dsp_speed s WHERE s.hqp_endpoint_id = e.id) AS measured,
               (SELECT count(*) FROM hqp_ab_sittings a WHERE a.hqp_endpoint_id = e.id) AS compared
          FROM hqp_endpoints e WHERE e.host IS NOT NULL ORDER BY e.id"""):
        entries[_hqp_key(r["host"], r["port"])] = {
            "endpoint_id": r["id"], "name": r["name"], "host": r["host"], "port": int(r["port"]),
            "product": r["product"],
            "embedded": hqp_library.is_embedded(r["product"]) if r["product"] else None,
            "here": is_own_address(r["host"]), "known": True, "seen": False, "control": None,
            "synced": bool(r["synced"]), "files": int(r["files"] or 0),
            "measured": int(r["measured"] or 0), "compared": int(r["compared"] or 0),
            "configured": False}
    for (host, port), d in _hqp_discovered.items():
        key = _hqp_key(host, port)
        e = entries.get(key)
        if e is None:
            entries[key] = {"endpoint_id": d.get("endpoint_id"), "name": d["name"], "host": host, "port": port,
                            "product": d["product"], "embedded": hqp_library.is_embedded(d["product"]),
                            "here": key[0] == "here", "known": False, "seen": True, "control": d["control"],
                            "synced": False, "files": 0, "measured": 0, "compared": 0,
                            "configured": False}
        else:
            e["seen"] = True
            e["control"] = d["control"]
            e["name"] = e["name"] or d["name"]
            if not e["product"]:
                e["product"] = d["product"]
                e["embedded"] = hqp_library.is_embedded(d["product"])
    address = hqp_library.driven()
    if address is not None:
        host, port = address
        key = _hqp_key(host, port)
        e = entries.get(key)
        if e is None:
            entries[key] = {"endpoint_id": None, "name": None, "host": host, "port": port, "product": None,
                            "embedded": None, "here": key[0] == "here", "known": False, "seen": False,
                            "control": None, "synced": False, "files": 0, "measured": 0,
                            "compared": 0, "configured": True}
        else:
            e["configured"] = True
    for e in entries.values():
        e["label"] = hqp_library.label(e["name"], e["product"])
    # A stable order — this machine first, then by label and address — so a
    # tap never reshuffles the list under the finger.
    return sorted(entries.values(), key=lambda e: (not e["here"], e["label"].lower(), e["host"]))


def remember_location(record: dict, seen: dict | None = None) -> dict:
    """Fold a freshly seen address into a renderer's history.

    A renderer's identity is its UDN; its address is weather. The same phone
    is on the LAN in the evening and on a tunnel at lunchtime, and a record
    that stores one address turns the other case into "device unavailable".
    So every address we have ever reached this UDN at is kept, newest first,
    and attach walks the list until one answers."""
    known = list(record.get("locations") or [])
    current = record.get("location")
    if current and current not in known:
        known.insert(0, current)
    for loc in ([seen["location"]] if seen and seen.get("location") else []):
        if loc in known:
            known.remove(loc)
        known.insert(0, loc)
    merged = {**record, **(seen or {})}
    merged["locations"] = known[:4]      # a device with five past addresses is not a device we can wait on
    if known:
        merged["location"] = known[0]
    return merged


def note_renderer_location(udn: str, location: str) -> None:
    """Promote the address a renderer actually answered on, wherever that
    renderer is remembered — its pin, the chosen output — the address it
    had kept behind it. Called from the DLNA backend after an attach lands
    on something other than the stored address, and by a scan for every
    remembered renderer it reaches: the play press then tries the address
    that answers now (a phone back on the LAN from a tunnel failed its
    first Play on the tunnel address it was chosen at)."""
    global _dlna_discovered
    from routers.settings import _read, _write
    key = udn_key(udn)
    pins = _dlna_pins()
    if key in pins and pins[key].get("location") != location:
        pins[key] = remember_location(pins[key], {"location": location})
        _dlna_save_pins(pins)
    active = _read("output.dlna_renderer")
    if active and udn_key(active.get("udn")) == key and active.get("location") != location:
        _write("output.dlna_renderer",
               remember_location(active, {"location": location}))
    if key in _dlna_discovered:
        _dlna_discovered = {**_dlna_discovered,
                            key: remember_location(_dlna_discovered[key], {"location": location})}


def _without_location(record: dict, location: str) -> dict:
    """`record` with `location` no longer among its addresses: the next one
    it is known at becomes its own (remember_location's order)."""
    known = [loc for loc in dict.fromkeys([record.get("location"), *(record.get("locations") or [])])
             if loc and loc != location]
    return {**record, "location": known[0] if known else None, "locations": known}


def forget_renderer_location(udn: str, location: str) -> None:
    """Drop an address a renderer is remembered at — its pin, the chosen
    output, the last scan's row — once another device answers there
    (DlnaBackend._create_device). Kept, it was probed at every press, and a
    LAN address that answered drew the renderer off its tunnel each time,
    only for its attach to find the other device and land back."""
    global _dlna_discovered
    from routers.settings import _read, _write
    key = udn_key(udn)

    def remembered(record: dict) -> bool:
        return location in (record.get("location"), *(record.get("locations") or []))
    pins = _dlna_pins()
    if key in pins and remembered(pins[key]):
        pins[key] = _without_location(pins[key], location)
        _dlna_save_pins(pins)
    active = _read("output.dlna_renderer")
    if active and udn_key(active.get("udn")) == key and remembered(active):
        _write("output.dlna_renderer", _without_location(active, location))
    if key in _dlna_discovered and remembered(_dlna_discovered[key]):
        _dlna_discovered = {**_dlna_discovered,
                            key: _without_location(_dlna_discovered[key], location)}


def _dlna_save_pins(pins: dict[str, dict]) -> None:
    from routers.settings import _write
    _write("output.dlna_pinned", list(pins.values()))


class AddDeviceRequest(BaseModel):
    address: str   # an IP or host name (an HQPlayer's control port is 4321, a renderer's SSDP 1900) — or a full device-description URL


def _rehost(location: str, ip: str) -> Optional[str]:
    """Point a description URL at the address that actually answered.

    SSDP devices advertise a LOCATION built from their own interface address,
    which is only reachable by whoever shares that segment. Reach one across a
    VPN — a phone on mobile data, joined to the same tailnet — and it answers
    on the tunnel while naming its carrier-side address, which resolves to
    nothing here. The port and path are the device's to choose; the host is
    ours to know, because we just spoke to it. None for a LOCATION that is
    no URL — a port that is no number, a bracket left open, no host: that
    answer's failure alone."""
    from urllib.parse import urlparse, urlunparse
    try:
        parts = urlparse(location)
        hostname, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not hostname:
        return None
    if hostname == ip:
        return location
    host = f"[{ip}]" if ":" in ip else ip
    netloc = f"{host}:{port}" if port else host
    return urlunparse(parts._replace(netloc=netloc))


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
            described = _rehost(loc, ip) if loc else None
            if described and "MediaRenderer" in st:
                return described
            if described and fallback is None:
                fallback = described
    finally:
        s.close()
    return fallback


# What a scan waits for: a device's description — the one document a scan
# or an address added by hand reads (_dlna_describe); its services' are the
# attach's business. Its size and encoding are the attach's own rule
# (dlna_backend.read_answer).
_DESCRIBE_TIMEOUT_S = 5.0
_LOCATIONS_PER_HOST = 4
_LOCATIONS = 64
_DESCRIBE_DEADLINE_S = 20.0


class DescriptionRefused(AnswerRefused):
    """A description the scan does not take: its shape, its size, its URL."""


async def _fetch_description(location: str) -> str:
    """The device description at `location`: one plain-HTTP GET, no
    redirect followed, within _DESCRIBE_TIMEOUT_S, read and decoded as the
    attach reads it (dlna_backend.read_answer), so the scan lists what the
    attach can read."""
    from urllib.parse import urlparse

    from aiohttp import ClientSession, ClientTimeout

    if urlparse(location).scheme != "http":
        raise DescriptionRefused("a description is a plain-HTTP GET")
    async with ClientSession(auto_decompress=False) as session:
        async with session.get(location, headers={"Accept-Encoding": "identity"},
                               allow_redirects=False,
                               timeout=ClientTimeout(total=_DESCRIBE_TIMEOUT_S)) as r:
            if r.status != 200:
                raise DescriptionRefused(f"the description answered {r.status}")
            return await read_answer(r)


_UPNP_DEVICE = "{urn:schemas-upnp-org:device-1-0}"
_DESCRIBED = ("deviceType", "friendlyName", "manufacturer", "modelName", "UDN")
_TEXT_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")
# A UDN is a renderer's identity — stored, sent in each row, compared — so it
# is taken whole or not at all ("uuid:" and a UUID, 41 characters, in practice)
_UDN_FORM = re.compile("[^\x00-\x1f\x7f-\x9f\u2028\u2029]{1,128}")


def _described_devices(document: str) -> list:
    """The root device of a description and the devices it embeds directly,
    read as the attach's library reads them (async_upnp_client's UpnpFactory
    and UpnpDevice): in the UPnP device namespace and no deeper, one embedded
    device of a type — the last — and each word from the first element of
    its name. Read so, the scan lists no renderer the attach would not find
    there."""
    from defusedxml.ElementTree import fromstring
    top = fromstring(document).find(f"{_UPNP_DEVICE}device")
    if top is None:
        return []
    embedded = {device.findtext(f"{_UPNP_DEVICE}deviceType", ""): device
                for device in top.findall(f"{_UPNP_DEVICE}deviceList/{_UPNP_DEVICE}device")}
    return [{word: device.findtext(f"{_UPNP_DEVICE}{word}", "").strip() for word in _DESCRIBED}
            for device in (top, *embedded.values())]


def _described_text(value: str) -> str:
    """A device's word about itself as it is stored and shown: no control or
    line-separating character, 200 characters at most."""
    return _TEXT_CONTROL.sub("", value)[:200]


def _multicast_location(location: str, source: Optional[str]) -> Optional[str]:
    """A multicast answer's LOCATION as the scan describes it: plain HTTP,
    on the address the answer came from (_rehost), as the unicast sweep
    does — or none."""
    if not source or not location.lower().startswith("http://"):
        return None
    return _rehost(location, source.split("%", 1)[0].strip("[]"))


def _scan_locations(found: dict, remembered: set) -> list:
    """The addresses a scan describes: plain HTTP, _LOCATIONS_PER_HOST per
    host and _LOCATIONS in all, those of the hosts the node remembers a
    renderer at first, then in order."""
    from urllib.parse import urlparse
    hosts = {urlparse(loc).hostname for loc in remembered if loc}
    per_host: dict = {}
    out = []
    for loc in sorted(set(found.values()), key=lambda loc: (urlparse(loc).hostname not in hosts, loc)):
        host = urlparse(loc).hostname
        if urlparse(loc).scheme != "http" or per_host.get(host, 0) >= _LOCATIONS_PER_HOST:
            continue
        per_host[host] = per_host.get(host, 0) + 1
        out.append(loc)
        if len(out) >= _LOCATIONS:
            break
    return out


def _describe_all(locations: list) -> dict:
    """Each location's renderer (_dlna_describe), described at once in an
    event loop of this thread's own — a description is parsed off the
    server's loop — within _DESCRIBE_DEADLINE_S: what has not answered by
    then is left out, as is one that fails."""
    async def run() -> dict:
        tasks = {loc: asyncio.ensure_future(_dlna_describe(loc)) for loc in locations}
        if tasks:
            await asyncio.wait(tasks.values(), timeout=_DESCRIBE_DEADLINE_S)
        out = {}
        for loc, task in tasks.items():
            if not task.done():
                task.cancel()
                logger.debug("DLNA describe of %s did not end within the scan", loc)
            elif task.exception() is not None:
                logger.debug("DLNA describe failed for %s: %s", loc, task.exception())
            else:
                out[loc] = task.result()
        return out
    return asyncio.run(run())


async def _dlna_describe(location: str) -> dict:
    """Fetch a device description and resolve the MediaRenderer in it →
    {udn, location, name, model}. Rejects descriptions without a renderer:
    multi-device hosts exist (the KANN serves its AK Connect media SERVER
    on the same port) and a pinned server can never play. Only the
    description is read (_fetch_description): what a scan needs of a device
    is in it, and the service documents are the attach's to fetch."""
    devices = _described_devices(await _fetch_description(location))
    if not devices:
        raise DescriptionRefused("no device in the description")
    renderer = next((d for d in devices if ":MediaRenderer:" in d["deviceType"]), None)
    if renderer is None:
        root = devices[0]
        raise HTTPException(
            status_code=422,
            detail=f"'{_described_text(root['friendlyName'])}' is "
                   f"{_described_text(root['deviceType']) or 'not a renderer'}"
                   " — no MediaRenderer at this address (a media-server "
                   "description of the same device is a common decoy)")
    if not _UDN_FORM.fullmatch(renderer["UDN"]):
        raise DescriptionRefused("a renderer without a UDN of its form")
    return {
        "udn": renderer["UDN"],
        "location": location,
        "name": _described_text(renderer["friendlyName"]),
        "model": _described_text(renderer["modelName"]),
        "manufacturer": _described_text(renderer["manufacturer"]),
    }


_TAILSCALE_BINARIES = (
    r"C:\Program Files\Tailscale\tailscale.exe",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/usr/local/bin/tailscale",
    "/usr/bin/tailscale",
    "tailscale",
)


def _tailnet_peers() -> list:
    """Online tailnet peers, so the scan can reach a renderer that is not on
    any local segment.

    Sweeping a tunnel the way we sweep a LAN is not an option: the range is
    100.64.0.0/10, four million addresses, and peers land anywhere in it with
    no subnet structure — two devices in the same tailnet here sit in
    different /24s. But Tailscale already knows exactly who is reachable, so
    the sweep is a handful of addresses rather than a search.

    Only a natively-run backend can ask: there is no CLI in the container, and
    even given the address the bridge's NAT drops SSDP replies from the
    ephemeral ports phone renderers answer on."""
    import subprocess
    for binary in _TAILSCALE_BINARIES:
        try:
            out = subprocess.run([binary, "status", "--json"],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode != 0:
            continue
        try:
            peers = (json.loads(out.stdout).get("Peer") or {}).values()
        except json.JSONDecodeError:
            return []
        return [ip for p in peers if p.get("Online")
                for ip in (p.get("TailscaleIPs") or [])[:1] if ":" not in ip]
    return []


def _lan_subnets() -> list:
    """The /24 worth sweeping with unicast — the real LAN, and only it.

    Every candidate costs 254 probes, so this must not be "every private
    address the host has". A developer machine carries Hyper-V, WSL, two
    VMware adapters and a few link-local ones, none of which has ever hosted a
    renderer; sweeping them all took 45s, long enough to read as a hang.

    The default route names the LAN exactly, which is the native answer. A
    container cannot use it — its route ends at the bridge — so there the
    host's address has to be supplied, and SAUTIUM_HOST_IPS is where it lives.
    """
    import ipaddress
    from streaming.media_host import _source_address_toward
    explicit = [s.strip() for s in os.getenv("SAUTIUM_HOST_IPS", "").split(",")
                if s.strip()]
    seeds = explicit or [ip for ip in [_source_address_toward("8.8.8.8")] if ip]
    if not seeds:
        from tls_gen import detect_private_host_ips
        seeds = detect_private_host_ips()
    nets = []
    for ip in seeds:
        try:
            addr = ipaddress.ip_address(ip)
            net = ipaddress.ip_network(f"{ip}/24", strict=False)
        except ValueError:
            continue
        # Link-local means DHCP failed on that adapter; nothing is configured
        # there to answer. CGNAT is a tunnel, where a /24 is meaningless — its
        # members are enumerated by _tailnet_peers(), not searched for.
        if (addr.is_link_local or net in nets
                or addr in ipaddress.ip_network("100.64.0.0/10")):
            continue
        nets.append(net)
    return nets


def _sweep_probe(ip: str) -> tuple:
    """One address of the unicast sweep (_unicast_sweep): an M-SEARCH and an
    HQPlayer `<discover/>` sent to it and its answers read for 1.5 s — the
    renderers it names, as (usn, location) pairs, and the HQPlayer it is, if
    any."""
    import select as sel
    import socket as sock

    import hqp_library
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
           'MAN: "ssdp:discover"\r\nMX: 1\r\n'
           'ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n').encode()
    s = sock.socket(sock.AF_INET, sock.SOCK_DGRAM)
    d = sock.socket(sock.AF_INET, sock.SOCK_DGRAM)
    hits, hqp = [], None
    try:
        s.sendto(msg, (ip, 1900))
        d.sendto(b"<discover/>\n", (ip, 4321))
        started = time.time()
        deadline = started + 1.5
        asked_again = False
        while time.time() < deadline:
            ready, _, _ = sel.select([s, d], [], [], max(0.0, min(deadline, started + 0.6) - time.time()) or 0.05)
            if hqp is None and not asked_again and time.time() >= started + 0.6:
                # one datagram can go missing (seen live: the Pi answered one
                # sweep and not the next); a second ask halfway costs nothing
                d.sendto(b"<discover/>\n", (ip, 4321))
                asked_again = True
            for r in ready:
                try:
                    data, addr = r.recvfrom(4096)
                except OSError:
                    continue
                if addr[0] != ip:
                    continue
                if r is d:
                    hqp = hqp_library.parse_discover(data) or hqp
                    continue
                loc = usn = ""
                for line in data.decode(errors="replace").splitlines():
                    low = line.lower()
                    if low.startswith("location:"):
                        loc = line.split(":", 1)[1].strip()
                    elif low.startswith("usn:"):
                        usn = line.split(":", 1)[1].strip()
                described = _rehost(loc, ip) if loc.lower().startswith("http://") else None
                if described:
                    hits.append((usn or loc, described))
    except OSError:
        pass
    finally:
        s.close()
        d.close()
    return hits, hqp


async def _unicast_sweep(found: dict, found_hqp: dict) -> None:
    """M-SEARCH every address of the LAN, one at a time rather than by
    multicast — and ask each for an HQPlayer in the same breath (the
    `<discover/>` datagram HQPlayer Client broadcasts; hqp_library.discover).

    Multicast is the protocol's answer and it is the wrong one here: it dies
    at the docker bridge, so a containerised node has never discovered a
    single renderer and pinning by hand was the only way in. Unicast crosses
    the bridge like any other UDP, which the router answering this sweep from
    inside the container proves. 254 addresses, two datagrams each, is a
    rounding error next to a user waiting for a device list."""
    loop = asyncio.get_running_loop()
    targets = ([str(h) for net in _lan_subnets() for h in net.hosts()]
               + await asyncio.to_thread(_tailnet_peers))
    with ThreadPoolExecutor(max_workers=64) as pool:
        # every probe ends before the pool is left: one that fails is its
        # address's failure, never the sweep's
        results = await asyncio.gather(
            *(loop.run_in_executor(pool, _sweep_probe, ip) for ip in targets), return_exceptions=True)
    for ip, result in zip(targets, results):
        if isinstance(result, Exception):
            logger.debug("unicast probe of %s failed: %s", ip, result)
            continue
        hits, hqp = result
        for usn, loc in hits:
            found[(usn, loc)] = loc
        if hqp is not None:
            found_hqp[ip] = hqp


# One Output scan at a time, whoever asks for it: the picker, or a play
# press whose renderer answered at none of its known addresses (scan_once)
_scan_lock = threading.Lock()
_scan_running: Optional[Future] = None


def _scan() -> Future:
    """The scan under way, or one started now: _scan_run in an event loop
    of its own thread, which no waiter that walks away can cancel."""
    global _scan_running
    with _scan_lock:
        if _scan_running is None or _scan_running.done():
            future: Future = Future()
            future.set_running_or_notify_cancel()

            def run() -> None:
                try:
                    future.set_result(asyncio.run(_scan_run()))
                except BaseException as e:
                    future.set_exception(e)
            threading.Thread(target=run, daemon=True, name="output-scan").start()
            _scan_running = future
        return _scan_running


def scan_once() -> dict:
    """The Output scan's answer, from a thread that waits for it — the one
    under way, or a new one (_scan)."""
    return _scan().result()


@router.post("/outputs/scan")
async def scan_outputs():
    """Find what plays on the network — MediaRenderer devices and HQPlayers.
    One scan at a time; concurrent callers wait for the one already
    running instead of starting another.

    Opening the Output picker starts a scan by itself, so pressing Rescan
    while that is in flight used to run two. Both end by replacing the
    discovered set wholesale, so the LAST one to finish won — and the button,
    which tracks only its own request, went back to idle while the other scan
    was still going. That is what "the device appeared after Rescan said it
    was done" was: the two racing, not the device being slow. Two concurrent
    sweeps of the whole subnet also make discovery less reliable, not more,
    since a dozing phone renderer answers one M-SEARCH and drops the other."""
    return await asyncio.wrap_future(_scan())


async def _scan_run():
    """SSDP multicast from every local interface, then a unicast sweep for the
    containerised case where multicast cannot leave the bridge — the sweep
    finds the HQPlayers too."""
    import hqp_library
    try:
        from async_upnp_client.search import async_search
    except ImportError:
        raise HTTPException(status_code=501, detail="async-upnp-client not installed")

    # Every address an answer names, by the USN it declared and the address:
    # two answers under one USN — one device on two networks, or another
    # device claiming its UDN — are two addresses to describe, never one
    # put in the other's place
    found: dict[tuple, str] = {}
    found_hqp: dict[str, dict] = {}

    async def _on_response(headers) -> None:
        loc = headers.get("location") or headers.get("LOCATION")
        usn = headers.get("usn") or headers.get("USN") or loc
        described = _multicast_location(str(loc), headers.get("_host")) if loc else None
        if described:
            found[(usn, described)] = described

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

    await _unicast_sweep(found, found_hqp)

    # Each box found is asked over control (GetInfo + the library hash) —
    # all of them at once: one that is slow or closes its port at once must
    # not hold the scan for the others.
    # Whatever one box answers is that box's failure, never the scan's.
    async def _resolve(host: str) -> tuple:
        try:
            # Its link says first whether HQPlayer takes anything now — a
            # build, one it did not finish, a silence: busy, never asked into
            with hqp_link.probing(host, 4321) as link:
                link.refuse()
                return host, await asyncio.to_thread(hqp_library.resolve_endpoint, host, 4321), None
        except Exception as e:
            return host, None, e
    hqps: dict[tuple, dict] = {}
    configured = hqp_library.driven()
    for host, resolved, err in await asyncio.gather(*(_resolve(h) for h in found_hqp)):
        facts = found_hqp[host]
        entry = {"name": facts["name"], "product": facts["product"], "endpoint_id": None,
                 "control": True}
        if isinstance(err, (Building, Dropped, NotAnswering)):
            # Busy, not gone: the link of the HQPlayer driven refused the
            # question — it builds or did not finish a build, is lent to the
            # benchmark, or answers nothing. Its row is read by address;
            # nothing is asked.
            ep = await asyncio.to_thread(hqp_library.endpoint_by_address, host, 4321)
            if ep is not None:
                entry["endpoint_id"] = ep["id"]
        elif err is not None:
            # Answers discovery, refuses control: an Embedded past its trial
            # half-hour looks exactly like this (the daemon still answers the
            # datagram while the control port closes at once) — listed as
            # not answering, not registered. The same verdict the selected
            # row gets from its live check, so a box reads the same whether
            # it is the output or not.
            logger.info("an HQPlayer the scan found answers discovery but not control (%s)",
                        type(err).__name__)
            logger.debug("its name and address: %r at %s: %s", facts["name"], host, err)
            entry["control"] = False
        else:
            ep, how, _facts = resolved
            try:
                if ep is not None:
                    # the row learns what the box says about itself (a row
                    # minted from an address alone takes HQPlayer's own name);
                    # the same library at a new address (a DHCP lease later)
                    # has its row follow it — a match by an alias of its own
                    # address keeps the address it was registered at
                    ep = await asyncio.to_thread(hqp_library.ensure_endpoint, host, 4321, resolved)
                elif configured and _hqp_key(host, 4321) == _hqp_key(*configured):
                    # the configured address is the owner's choice: its row
                    # comes on first contact, at the address the owner gave
                    ep = await asyncio.to_thread(hqp_library.register, *configured)
            except Exception as e:
                logger.warning("an HQPlayer the scan found could not be registered: %s", e)
                logger.debug("its address: %s", host)
                ep = None
            if ep is not None:
                entry["endpoint_id"] = ep["id"]
        hqps[(host, 4321)] = entry
    global _hqp_discovered, _dlna_discovered
    _hqp_discovered = hqps

    fresh: dict[str, dict] = {}
    reached: dict[str, list] = {}      # every address this scan described each renderer at
    pins = _dlna_pins()
    known = {**_dlna_discovered, **pins}
    from playback.dlna_backend import known_locations
    from routers.settings import _read
    active = _read("output.dlna_renderer") or {}
    chosen = udn_key(active.get("udn"))
    remembered = {loc for record in (*known.values(), active) for loc in known_locations(record)}
    locations = _scan_locations(found, remembered)
    described = await asyncio.to_thread(_describe_all, locations)
    # Where the node knows each renderer it holds — pinned, or the output
    held = {key: set(known_locations(record)) for key, record in pins.items()}
    if chosen:
        held.setdefault(chosen, set()).update(known_locations(active))
    answers: dict[str, list] = {}
    for loc in locations:
        info = described.get(loc)
        if info is None or _is_hqplayer_renderer(info, found_hqp):
            continue
        answers.setdefault(udn_key(info["udn"]), []).append(info)
    for key, infos in answers.items():
        # A renderer the node holds takes no new address while one it is
        # known at answers with its UDN: UPnP authenticates no device, and a
        # claim of the UDN — public in the renderer's own announcements — is
        # all another device on the network needs. Only a renderer absent
        # from where it was known moves where it is found now.
        mine = held.get(key)
        if mine and any(info["location"] in mine for info in infos):
            elsewhere = [info["location"] for info in infos if info["location"] not in mine]
            if elsewhere:
                logger.info("a renderer the node holds answers where it is known; "
                            "%d other address(es) claiming it not taken", len(elsewhere))
                logger.debug("%s not taken at %s", key, ", ".join(elsewhere))
            infos = [info for info in infos if info["location"] in mine]
        reached[key] = [info["location"] for info in infos]
        # A scan finds the device where it is TODAY; whatever we knew about
        # where it used to be stays attached to it, so a phone found on the
        # LAN keeps its tunnel address as a fallback and vice versa. The
        # same renderer at another address in the same scan (a phone
        # answering on the LAN and through the tunnel): one record, the
        # address found first its own, the others behind it
        first, *rest = infos
        row = {**remember_location(known.get(key, {}), first), "udn": key}
        for info in rest:
            if info["location"] not in row["locations"]:
                row = {**row, "locations": [*row["locations"], info["location"]][:4]}
        fresh[key] = row
    _dlna_discovered = fresh
    if fresh:
        for key, info in fresh.items():
            if key in pins or key == chosen:
                note_renderer_location(key, info["location"])
        if chosen in reached:
            # The active output follows the renderer to where the scan
            # reached it, when its own address no longer answers
            try:
                await asyncio.to_thread(manager.renderer_reached, chosen, reached[chosen])
            except Exception as e:
                logger.warning("the output did not move to where the scan reached its renderer (%s)",
                               type(e).__name__)
                logger.debug("%s reached at %s: %s", fresh[chosen].get("name"), reached[chosen][0], e)
    renderers = list(fresh.values())
    return {"renderers": renderers, "hqplayers": _hqplayer_entries()}


def _is_hqplayer_renderer(info: dict, found_hqp: dict) -> bool:
    """HQPlayer is a UPnP renderer as well (manufacturer Signalyst — Desktop
    and HQPlayer OS alike); a box that answered as an HQPlayer is driven as
    one — its renderer would be the same DSP with no queue and no filter
    control, so it is not offered twice.

    One box answers on several addresses: the datagram on the LAN address the
    sweep probed, the interface-bound M-SEARCH on a virtual adapter's (a
    Desktop on a host with WSL/Hyper-V). The match is therefore by `_hqp_key`,
    under which every alias of this machine is one key — never by the
    literal address, which listed the Desktop's renderer as a second device."""
    from urllib.parse import urlparse
    if not (info.get("manufacturer") or "").lower().startswith("signalyst"):
        return False
    host = urlparse(info.get("location") or "").hostname
    if not host:
        return False
    key = _hqp_key(host, 4321)
    return any(_hqp_key(h, 4321) == key for h in found_hqp)


def _probe_hqplayer(host: str, port: int) -> Optional[dict]:
    """The HQPlayer at an address, registered as one the owner added — or
    None. The discovery datagram first; GetInfo over TCP where a firewall
    passes only that."""
    import hqp_library
    facts = hqp_library.discover(host, port)
    # the person's own look at this address: its link may dial it meanwhile
    with hqp_link.probing(host, port):
        if facts is None:
            try:
                info = hqp_library.get_info(host, port)
            except OSError:
                return None
            if not info.get("product"):
                return None
            facts = {"name": info["name"], "product": info["product"]}
        entry = {"kind": "hqplayer", "host": host, "port": port, "name": facts["name"],
                 "product": facts["product"], "embedded": hqp_library.is_embedded(facts["product"]),
                 "endpoint_id": None}
        ep = hqp_library.register(host, port)
    if ep is not None:
        entry["endpoint_id"], entry["name"] = ep["id"], ep["name"]
    return entry


@router.post("/outputs/add")
async def add_device(req: AddDeviceRequest):
    """Register whatever plays at an address, for what the scan cannot see
    (another subnet, a box that was off): an HQPlayer before a DLNA
    renderer, since a box that is both — HQPlayer OS is a renderer too —
    is driven as the HQPlayer. A bare IP or name is probed on both ports; a
    full device-description URL is a renderer by definition (unicast
    M-SEARCH resolves one from an IP otherwise)."""
    raw = req.address.strip()
    if not raw:
        raise HTTPException(status_code=400, detail="an address is required")
    if not raw.lower().startswith(("http://", "https://")):
        host, port = raw.split("/")[0], 4321
        if host.count(":") == 1:
            host, given = host.rsplit(":", 1)
            port = int(given) if given.isdigit() else 4321
        hqp = await asyncio.to_thread(_probe_hqplayer, host, port)
        if hqp is not None:
            return hqp
        location = await asyncio.to_thread(_msearch_location, host)
        if not location:
            raise HTTPException(
                status_code=502,
                detail=f"nothing answered at {host} — no HQPlayer on port {port} (is "
                       "'Allow control from network' on?) and no UPnP device on 1900 "
                       "(is the renderer's network mode, e.g. AK Connect, enabled?)")
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
    key = udn_key(info["udn"])
    prior = pins.get(key) or _dlna_discovered.get(key) or {}
    info = {**(remember_location(prior, info) if prior else remember_location(info)), "udn": key}
    pins[key] = info
    _dlna_save_pins(pins)
    return {"kind": "dlna", **info}


class DlnaRemoveRequest(BaseModel):
    udn: str


@router.post("/outputs/dlna/remove")
async def dlna_remove(req: DlnaRemoveRequest):
    """Forget a renderer entirely — pin, scan snapshot, and the selection that
    keeps an unreachable device listed. That last one used to be unremovable:
    the active renderer is re-added to every listing so a configured output
    stays visible while its device is off, which also meant a stale entry
    could not be got rid of by any means the UI offered."""
    from routers.settings import _read, _write
    key = udn_key(req.udn)
    pins = _dlna_pins()
    removed = pins.pop(key, None) is not None
    _dlna_save_pins(pins)
    global _dlna_discovered
    if key in _dlna_discovered:
        _dlna_discovered = {k: v for k, v in _dlna_discovered.items() if k != key}
        removed = True
    active = _read("output.dlna_renderer")
    if active and udn_key(active.get("udn")) == key:
        _write("output.dlna_renderer", None)
        removed = True
    # What its Seeks showed (DlnaBackend._remember_opus_seek) goes with it
    facts = _read("output.dlna_opus_seek") or {}
    if key in facts:
        _write("output.dlna_opus_seek", {k: v for k, v in facts.items() if k != key})
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
            backend.detach_tab(tab, channel)

    return EventStream(event_generator())


@router.post("/browser/event")
def browser_event(req: BrowserEventRequest):
    """<audio> element events from the renderer tab (element callbacks —
    timeupdate throttled to ~1/s client-side, not polling)."""
    backend = _browser_backend()
    payload = backend.on_client_event(req.tab, req.event, req.position,
                                      req.duration, req.queue_index, req.epoch)
    return {"ok": True, **(payload or {})}


# -- Search -------------------------------------------------------------------

@router.get("/playlist")
def get_playlist():
    """The canonical queue, serialized — each slot the active output does
    not hold named with why (a gap). Always instant — the queue lives here;
    no player round-trip, no cache."""
    return manager.playlist()


@router.get("/status/stream")
async def status_stream():
    """SSE endpoint: pushes status updates in real-time.

    MIGRATION BRIDGE — the current frontend holds ONE stream, /api/events;
    this endpoint stays because a stale SPA tab (pre-/api/events build)
    reconnects here, receives the frontend build stamp in the status
    payload and reloads itself onto the new frontend. Remove once no
    long-lived tabs predate 2026-08-11."""
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

    return EventStream(event_generator())


events_router = APIRouter(prefix="/api", tags=["events"])


@events_router.get("/events")
async def events_stream(tab: Optional[str] = Query(None, max_length=64,
                                                   pattern=r"^[A-Za-z0-9_-]+$")):
    """The ONE SSE connection a web-UI tab holds.

    Multiplexes every push channel — player status, phantom-preview pings,
    gear-research transitions, chat wakeups — as typed JSON messages {"t": kind, "d":
    payload}. Browsers cap an HTTP/1.1 origin at 6 connections for the
    WHOLE browser; three parallel streams per tab meant two tabs starved
    every other fetch into (pending) forever (observed 2026-08-11). New
    event kinds ride this channel for free — never add another standalone
    SSE endpoint for the web UI. Preview/research events are payload-free
    pings by design: the open screen re-fetches its own snapshot, so
    there is no split source to race.

    `tab` names the page (one id per page load, player.js). The first message
    of every connection is `hello`: the page re-states, on this stream, what
    it wants from it — the peak meter's interest (PUT /api/hqplayer/meter)
    is bound to the stream the page holds, and a reconnect of the same page
    takes it over (playback.hqp_meter)."""
    import mb_discovery
    from routers.artists import artist_sse_register, artist_sse_unregister
    from routers.discovery import (lb_sse_register, lb_sse_unregister,
                                   mb_sse_register, mb_sse_unregister)
    from routers.gear_models import (research_sse_register,
                                     research_sse_unregister)
    from routers.p2p import chat_sse_register, chat_sse_unregister
    from routers.settings import (_notices_state, identity_event, identity_sse_register,
                                  identity_sse_unregister, notices_sse_register,
                                  notices_sse_unregister)
    from streaming.events import preview_events

    loop = asyncio.get_event_loop()
    status_evt = asyncio.Event()
    research_evt = asyncio.Event()
    mb_evt = asyncio.Event()
    lb_evt = asyncio.Event()
    artist_evt = asyncio.Event()
    chat_evt = asyncio.Event()
    notice_evt = asyncio.Event()
    identity_evt = asyncio.Event()
    meter_evt = asyncio.Event()

    async def event_generator():
        last_version = -1
        preview_q = preview_events.subscribe()
        manager.sse_register(status_evt, loop)
        research_sse_register(research_evt, loop)
        mb_sse_register(mb_evt, loop)
        lb_sse_register(lb_evt, loop)
        artist_sse_register(artist_evt, loop)
        chat_sse_register(chat_evt, loop)
        notices_sse_register(notice_evt, loop)
        identity_sse_register(identity_evt, loop)
        meter = hqp_meter.register(tab, meter_evt, loop) if tab else None
        try:
            yield 'data: {"t": "hello"}\n\n'
            # The connect-time status carries the floor of this connection's
            # events (`event_floor`, an id of the kind theirs are, minted
            # now): what a status says happened at or before it — on this
            # one or on any later, an event gone from the status and back —
            # happened before this connection: state for the page to paint,
            # never news (`output_event`). The notices' copy below is flagged
            # for the same reason
            yield ("data: "
                   + json.dumps({"t": "status",
                                 "d": {**manager.latest_status, "event_floor": new_id()}})
                   + "\n\n")
            last_version = manager.status_version
            # What each streamed row of the queue shows of its stream — whole,
            # with the queue version it was read at; again whenever it moves
            last_slots = manager.slots_version
            yield "data: " + json.dumps({"t": "slots", "d": manager.slots_message()}) + "\n\n"
            # What is reachable here now — the recorded answer of the music
            # folder, as a key: at connect and whenever it changes (its
            # record wakes this stream). A screen that shows the owner's
            # files reads its payload again on a new key; the notice says
            # the same to the person, but may say it before the look has
            last_reach = reach_key()
            yield "data: " + json.dumps({"t": "reach", "d": {"key": last_reach}}) + "\n\n"
            # Notices travel as the whole active set, never as single
            # events: the client diffs it, so a tab that was asleep learns
            # what is true now rather than replaying what happened. The
            # connect-time copy is flagged so it paints state, not toasts.
            # Derived off the loop: its database reads and, one look at a
            # time across every stream, the music folder (scanner).
            yield ("data: "
                   + json.dumps({"t": "notice",
                                 "d": {**await asyncio.to_thread(_notices_state),
                                       "initial": True}})
                   + "\n\n")
            # The node's identity, as state at connect and as news after: the
            # page toasts only a change it saw happen
            yield ("data: "
                   + json.dumps({"t": "identity",
                                 "d": {**await asyncio.to_thread(identity_event),
                                       "initial": True}})
                   + "\n\n")
            while True:
                waiters = {
                    asyncio.create_task(status_evt.wait()): "status",
                    asyncio.create_task(research_evt.wait()): "research",
                    asyncio.create_task(mb_evt.wait()): "mb",
                    asyncio.create_task(lb_evt.wait()): "lb",
                    asyncio.create_task(artist_evt.wait()): "artist",
                    asyncio.create_task(chat_evt.wait()): "chat",
                    asyncio.create_task(notice_evt.wait()): "notice",
                    asyncio.create_task(identity_evt.wait()): "identity",
                    asyncio.create_task(preview_q.get()): "preview",
                }
                if meter is not None:
                    waiters[asyncio.create_task(meter_evt.wait())] = "meter"
                try:
                    done, pending = await asyncio.wait(
                        waiters, timeout=15.0,
                        return_when=asyncio.FIRST_COMPLETED)
                finally:
                    # Also when the page leaves during the wait (the stream is
                    # cancelled here): a waiter left pending was destroyed
                    # with the generator — "Task was destroyed but it is
                    # pending!" on every page reload.
                    for task in waiters:
                        task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if not done:
                    yield ": keepalive\n\n"
                    continue
                kinds = {waiters[task] for task in done}
                if "status" in kinds:
                    status_evt.clear()
                    if manager.status_version != last_version:
                        last_version = manager.status_version
                        yield ("data: "
                               + json.dumps({"t": "status",
                                             "d": manager.latest_status})
                               + "\n\n")
                    if manager.slots_version != last_slots:
                        last_slots = manager.slots_version
                        yield ("data: "
                               + json.dumps({"t": "slots", "d": manager.slots_message()})
                               + "\n\n")
                    if reach_key() != last_reach:
                        last_reach = reach_key()
                        yield "data: " + json.dumps({"t": "reach", "d": {"key": last_reach}}) + "\n\n"
                if "preview" in kinds:
                    # Drain coalesced pings so one re-fetch covers the burst.
                    while not preview_q.empty():
                        preview_q.get_nowait()
                    yield 'data: {"t": "preview"}\n\n'
                if "research" in kinds:
                    research_evt.clear()
                    yield 'data: {"t": "research"}\n\n'
                if "chat" in kinds:
                    chat_evt.clear()
                    yield 'data: {"t": "chat"}\n\n'
                if "mb" in kinds:
                    mb_evt.clear()
                    yield ("data: "
                           + json.dumps({"t": "mb", "d": mb_discovery.state()})
                           + "\n\n")
                if "lb" in kinds:
                    # Payload-free: a ListenBrainz slice landed — the open
                    # artist page re-fetches its own snapshot.
                    lb_evt.clear()
                    yield 'data: {"t": "lb"}\n\n'
                if "artist" in kinds:
                    # Payload-free: an opened composer's bio fetch ended — the
                    # open artist page re-fetches its own snapshot.
                    artist_evt.clear()
                    yield 'data: {"t": "artist"}\n\n'
                if "notice" in kinds:
                    notice_evt.clear()
                    notices = await asyncio.to_thread(_notices_state)
                    yield ("data: "
                           + json.dumps({"t": "notice", "d": notices})
                           + "\n\n")
                if "identity" in kinds:
                    identity_evt.clear()
                    yield ("data: "
                           + json.dumps({"t": "identity",
                                         "d": await asyncio.to_thread(identity_event)})
                           + "\n\n")
                if "meter" in kinds:
                    # Every reading the page has not had yet, in order: the
                    # meter offers them on this loop, so none slips between
                    # the clear and the drain.
                    meter_evt.clear()
                    for msg in meter.drain():
                        yield "data: " + json.dumps({"t": "meter", "d": msg}) + "\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            manager.sse_unregister(status_evt)
            research_sse_unregister(research_evt, loop)
            mb_sse_unregister(mb_evt, loop)
            lb_sse_unregister(lb_evt, loop)
            artist_sse_unregister(artist_evt, loop)
            chat_sse_unregister(chat_evt, loop)
            notices_sse_unregister(notice_evt, loop)
            identity_sse_unregister(identity_evt, loop)
            preview_events.unsubscribe(preview_q)
            if meter is not None:
                hqp_meter.unregister(meter)

    return EventStream(event_generator())


@router.get("/status")
def get_status():
    """Last status pushed by the active output backend — always instant,
    never hits the player in the request path."""
    return manager.latest_status


def _stream_quality(provider_id: Optional[str]) -> str:
    """'lossless' / 'lossy' for the serving provider — the preview analog of the
    owned file-quality badge (a stream has no file bit-depth)."""
    if provider_id:
        # The registry, not the ranking: a buffer fetched from a provider
        # that has stepped aside since is still that provider's audio
        from streaming import service as streaming_service
        prov = streaming_service.get_provider(provider_id)
        if prov is not None:
            return "lossless" if prov.manifest.lossless else "lossy"
        if provider_id == "deezer":   # registry not ready — known-provider fallback
            return "lossless"
    return "lossy"


def _preview_now_playing_detail(track_id: str, provider: Optional[str],
                                album_id: Optional[str] = None) -> dict:
    """Now Playing detail for a streamed phantom track (no media_file). Same shape
    as the owned path, sourced by track_id: MB tracklist metadata + CAA cover +
    whatever audio_features the preview enrichment has produced so far (key/BPM/
    energy fill in live as the stream is analysed). Quality is the stream source,
    not a file bit-depth.

    `album_id` is the album the QUEUE SLOT came from, carried down from the
    enqueue. It has to be: a canonical track belongs to every album whose
    tracklist names it, so a compilation or a later reissue would otherwise
    win the screen over the album the listener actually pressed play on. The
    ORDER BY below is the fallback for slots with no album context at all."""
    row = _db_query_one("""
        SELECT t.id::text AS track_id, t.title,
               al.id::text AS album_id, al.title AS album_title,
               al.release_year AS year, al.cover_url,
               af.bpm, af.key, af.mode, af.energy, af.energy_db,
               ti.instruments
        FROM tracks t
        JOIN album_tracks atr ON atr.track_id = t.id
        JOIN albums al ON al.id = atr.album_id
        LEFT JOIN audio_features af ON af.track_id = t.id
        LEFT JOIN track_instruments ti ON ti.track_id = t.id
        WHERE t.id = %(tid)s::uuid
          AND (%(aid)s::uuid IS NULL OR al.id = %(aid)s::uuid)
        ORDER BY (al.cover_url IS NOT NULL) DESC, al.id
        LIMIT 1
    """, {"tid": track_id, "aid": album_id})
    if not row and album_id:
        # The slot names an album this track is no longer on (a re-mint moved
        # the tracklist under it) — fall back rather than 404 the screen.
        return _preview_now_playing_detail(track_id, provider)
    if not row:
        raise HTTPException(status_code=404, detail="track not found")
    row["media_file_id"] = None
    row["cover_id"] = None
    row["instruments"] = present_instruments(row.get("instruments"))
    row.update(_zero_shot_detail(track_id))
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
    held = None if provider else _held_copy(track_id)
    if held:
        # Not a stream: a file held at the HQPlayer that is the output plays
        # natively there — its format and resolution are the badge, as an
        # owned file's are; the analysis is the track's, keyed by uuid.
        row.update(held)
    else:
        row["quality"] = _stream_quality(provider)
    row["provider_cover_url"] = _resolved_artwork.get(track_id)
    return row


def _zero_shot_detail(track_id: str) -> dict:
    """The Now Playing fields derived from the track's segments: the moods
    present and the danceability — null until the track has segments."""
    z = _db_query_one("""
        SELECT moods, round(danceability::numeric, 3)::float8 AS danceability
        FROM track_zero_shot WHERE track_id = %(t)s::uuid
    """, {"t": track_id}) or {}
    return {"danceability": z.get("danceability"),
            "moods": zero_shot.present_moods(z.get("moods")) or None}


def _held_copy(track_id: str) -> Optional[dict]:
    """The held file of this track that is playing: the one a queue slot of
    the track names (its path), else the best copy at the HQPlayer that is
    the output. None when nothing is held there — a phantom, or another
    output."""
    hqp_id = active_hqp_endpoint()
    paths = [it.source.get("path") for it in manager.queue.snapshot()
             if it.track_id == track_id and it.source.get("kind") == "hqp"]
    if hqp_id is None and not paths:
        return None
    return _db_query_one(f"""
        SELECT hf.file_format, hf.is_lossless, hf.sample_rate, hf.bit_depth,
               hf.duration_seconds,
               {rip_quality('hf.is_lossless', 'hf.sample_rate', 'hf.bit_depth')} AS quality
        FROM hqp_library_files hf
        JOIN album_variants av ON av.id = hf.album_variant_id
        WHERE hf.track_id = %(tid)s::uuid
          AND (hf.hqp_path = ANY(%(paths)s) OR av.hqp_endpoint_id = %(hqp)s)
        ORDER BY (hf.hqp_path = ANY(%(paths)s)) DESC,
                 hf.bit_depth DESC NULLS LAST, hf.sample_rate DESC NULLS LAST
        LIMIT 1
    """, {"tid": track_id, "paths": paths, "hqp": hqp_id})


# -- HQPlayer diagnostics (playback.hqp_diagnostics) ---------------------------
# Why a slot did not play on the HQPlayer output. In memory only; whole here,
# paths cut to their last two components on the way into a support bundle.

def _hqp_info() -> dict:
    b = manager.active
    return b.info if b is not None and b.id == "hqplayer" else {}


@router.get("/diagnostics/hqplayer")
def hqplayer_diagnostics():
    from hqplayer_client import HQPlayerClient
    from playback import hqp_diagnostics as diag
    b = manager.active
    owed = b.link.owed if b is not None and b.id == "hqplayer" else None
    return {"attempts": [diag.summary(a) for a in diag.attempts()],
            "failing": diag.failing_run(),
            "log_source": diag.log_source(settings.hqplayer_host, _hqp_info()),
            "client_errors": HQPlayerClient.last_errors(),
            # The add HQPlayer owes the answer to, while its mirror waits for it
            "owed": owed.view() if owed is not None else None}


@router.get("/diagnostics/hqplayer/log")
def hqplayer_log():
    """HQPlayer's own log, read now — never tailed in the background."""
    from playback import hqp_diagnostics as diag
    info = _hqp_info()
    return diag.read_log(diag.log_source(settings.hqplayer_host, info), info)


@router.get("/diagnostics/hqplayer/bundle")
def hqplayer_diagnostics_bundle():
    """The `playback` scope of a support warrant (desktop/diag_bundle.py)."""
    from playback import hqp_diagnostics as diag
    info = _hqp_info()
    return diag.bundle_view(diag.log_source(settings.hqplayer_host, info), info)


@router.get("/diagnostics/hqplayer/{attempt_id}")
def hqplayer_attempt(attempt_id: int):
    from playback import hqp_diagnostics as diag
    attempt = diag.find(attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="No such attempt (only the last 20 are kept)")
    return diag.public(attempt, redact=False)


@router.get("/now-playing-detail")
def now_playing_detail(media_file_id: int = None, track_id: str = None,
                       provider: str = None, album_id: str = None):
    """Aggregated rich payload for the Now Playing screen.

    Combines media-file metadata (format, sample rate, bit depth, cover),
    track-level audio features (BPM, key, mode, energy, instruments),
    album info, and the top genres into one roundtrip. Called by the
    frontend whenever the playing track changes. A streamed phantom track has no
    media_file — it is fetched by ``track_id`` (+ the serving ``provider`` for the
    quality badge) and served the same shape via _preview_now_playing_detail.
    """
    if track_id:
        return _preview_now_playing_detail(track_id, provider, album_id)
    if not media_file_id:
        raise HTTPException(status_code=400, detail="media_file_id or track_id required")
    row = _db_query_one(f"""
        SELECT mf.id AS media_file_id,
               mf.track_id::text AS track_id,
               mf.cover_id::text AS cover_id,
               mf.file_format,
               mf.is_lossless,
               mf.sample_rate,
               mf.bit_depth,
               {rip_quality('mf.is_lossless', 'mf.sample_rate', 'mf.bit_depth')} AS quality,
               mf.duration_seconds,
               t.title,
               af.bpm,
               af.key,
               af.mode,
               af.energy,
               af.energy_db,
               ti.instruments,
               av.album_id::text AS album_id,
               al.title AS album_title,
               al.release_year AS year
        FROM media_files mf
        JOIN tracks t ON t.id = mf.track_id
        LEFT JOIN audio_features af ON af.track_id = t.id
        LEFT JOIN track_instruments ti ON ti.track_id = t.id
        JOIN album_variants av ON av.id = mf.album_variant_id
        JOIN albums al ON al.id = av.album_id
        WHERE mf.id = %(id)s
    """, {"id": media_file_id})

    if not row:
        raise HTTPException(status_code=404, detail="media_file not found")

    row["instruments"] = present_instruments(row.get("instruments"))
    row.update(_zero_shot_detail(row["track_id"]))
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

    return row


def _transport(intent: bool, command: str, *args) -> dict:
    """One transport command on the active output — a play intent through
    the gate that (re)attaches it — and whether it was taken; one that was
    not carries the output's own words (`refusal`), which the assistant
    quotes and the shell says (reportPlaybackResult). While a play intent
    goes on behind the answer (HQPlayer taking the queue again, or a queue
    mutation in flight) `starts` names the track it will start — null when
    a Pause, a Stop or any newer intent that supersedes it dropped it."""
    try:
        b = manager.ensure_active() if intent else manager.backend()
        if command == "play":
            # the file that Play would start, looked at before the output
            # is asked; nothing of the queue opening here is said in words
            manager.before_start(b)
        elif command in ("next", "previous"):
            manager.before_step(b, 1 if command == "next" else -1)
        ok = getattr(b, command)(*args)
    except Exception as e:
        raise _output_error(e)
    return _starting(b, {"ok": True} if ok else _refused(b))


def _refused(b) -> dict:
    """A command the output did not take: its own words (`refusal`) and, when
    a play attempt watched it, that attempt (`attempt`), whose diagnosis says
    the same to every tab — the shell says it once."""
    attempt = b.refused_attempt()
    answer = {"ok": False, "refusal": b.refusal()}
    return answer if attempt is None else {**answer, "attempt": attempt}


def _starting(b, answer: dict) -> dict:
    """`answer`, with the track a play press will start (PlayerBackend.pressing)
    while one runs — or a queue mutation will, once it stands."""
    press = b.pressing()
    return answer if press is None else {**answer, "starts": press or None}


@router.post("/play")
def play():
    return _transport(True, "play")


@router.post("/pause")
def pause():
    return _transport(False, "pause")


@router.post("/stop")
def stop():
    return _transport(False, "stop")


@router.post("/next")
def next_track():
    return _transport(True, "next")


@router.post("/previous")
def previous_track():
    return _transport(True, "previous")


@router.post("/volume/up")
def volume_up():
    return _transport(False, "volume_up")


@router.post("/volume/down")
def volume_down():
    return _transport(False, "volume_down")


@router.post("/volume")
def set_volume(req: VolumeRequest):
    return _transport(False, "set_volume", req.level)


@router.post("/seek")
def seek(req: SeekRequest):
    """Seek within the current track (outputs whose capabilities().seek is
    true — the built-in engine and HQPlayer both support it); one the output
    did not take carries its words, as a transport command does."""
    if req.position < 0:
        raise HTTPException(status_code=400, detail="position must be >= 0")
    try:
        b = manager.backend()
        ok = b.seek(int(req.position))
    except Exception as e:
        raise _output_error(e)
    return _starting(b, {"ok": True} if ok else _refused(b))


# -- Smart play ----------------------------------------------------------------

@router.post("/remove")
def remove(req: RemoveRequest):
    """Remove the queue's slot at the 1-based `index` — the output finds its
    own entry for it (a gap of HQPlayer's has none). The play_count for the
    removed slot is unaffected —
    tracking records past plays, not pending queue contents. 409 is the
    slot no longer holding `track_id`; a removal the output refused is a
    503 in its words (_output_error), the slot still in the queue."""
    if req.index < 1:
        raise HTTPException(status_code=400, detail="index must be >= 1")
    try:
        ok = manager.remove(req.index, track_id=req.track_id)
    except Exception as e:
        raise _output_error(e)
    if not ok and req.track_id:
        raise HTTPException(status_code=409, detail="queue changed — refresh and try again")
    return {"ok": ok, "index": req.index}


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

    The plan is made on the queue as the route reads it, its slots the
    queue's items themselves (a track queued twice is two slots, taken in
    their order), and applied under the queue lock only to that order of it
    and the output it was made on: one a filler's append (or any edit that
    moved a slot) or an output switch overtook meanwhile is refused whole
    (409), the queue as it is. A step the output refuses is answered in its
    words (OutputRefused).
    """
    if not req.order:
        raise HTTPException(status_code=400, detail="order is empty")

    # The plan names the queue's order and the output it was made on:
    # applied under the queue lock only to those (a filler's append, an
    # output switch may land first)
    current_items = manager.queue.snapshot()

    status_idx = manager.latest_status.get("track_index") or 0
    if status_idx < 1 or status_idx > len(current_items):
        raise HTTPException(
            status_code=409,
            detail="reorder requires a currently playing track; use /play-tracks",
        )

    if len(req.order) != len(current_items):
        raise HTTPException(
            status_code=400,
            detail="order length must match current playlist length",
        )

    from collections import Counter
    if Counter(req.order) != Counter(it.track_id for it in current_items):
        raise HTTPException(
            status_code=400,
            detail="order must be a permutation of the current playlist",
        )

    # The sheet names tracks, the plan slots: a track queued twice takes its
    # copies in their order
    copies: dict = {}
    for it in current_items:
        copies.setdefault(it.track_id, []).append(it)
    order = [copies[track_id].pop(0) for track_id in req.order]

    current = current_items[status_idx - 1]
    new_status_idx = next(i for i, it in enumerate(order, 1) if it is current)

    old_before = current_items[:status_idx - 1]
    old_after = current_items[status_idx:]
    new_before = order[:new_status_idx - 1]
    new_after = order[new_status_idx:]

    def same(a: list, b: list) -> bool:
        return len(a) == len(b) and all(x is y for x, y in zip(a, b))

    if same(old_before, new_before) and same(old_after, new_after):
        return {
            "ok": True, "removed": 0, "added": 0,
            "anchor_index": status_idx, "interrupted": False,
        }

    # Zero-interrupt feasibility: new_before must be a subsequence
    # of old_before (we can only drop tracks, never reorder or
    # add to before), and new_after must hold exactly the original
    # after-segment plus what the before-shrink drops out.
    def is_subsequence(needle: list, haystack: list) -> bool:
        j = 0
        for h in haystack:
            if j < len(needle) and needle[j] is h:
                j += 1
        return j == len(needle)

    dropped = {id(it) for it in old_before} - {id(it) for it in new_before}
    seamless = (
        is_subsequence(new_before, old_before)
        and {id(it) for it in new_after} == {id(it) for it in old_after} | dropped
    )

    plan = ReorderPlan(
        seamless=seamless,
        status_idx=status_idx,
        new_status_idx=new_status_idx,
        old_before=old_before,
        new_before=new_before,
        old_after=old_after,
        new_after=new_after,
        order=order,
        position=int(manager.latest_status.get("position") or 0),
        # A paused HQPlayer is left stopped at its spot, never played to be
        # paused again: the play press resumes it there (HqpBackend._rebuild)
        resume=manager.latest_status.get("state") == "playing",
        items=current_items,
        output=manager.active,
    )
    try:
        result = manager.apply_reorder(plan)
        return {"ok": True, "anchor_index": new_status_idx, **result}
    except QueueChanged as e:
        raise HTTPException(status_code=409, detail=f"{e} — the queue shows it as it is now")
    except OutputRefused as e:
        # The output's answer, logged in its words where it was met
        raise _output_error(e)
    except Exception as e:
        # The output busy — held, building, silent, a newer intent first —
        # is news; anything else is a fault, with its traceback. Either way
        # without what the person's words could not do: it names a track
        status, why = refusal = _output_refusal(e)
        if status == 409:
            logger.warning("reorder not applied: %s", why)
        else:
            logger.error("reorder failed: %s", why, exc_info=True)
        raise _output_error(e, refusal)

@router.post("/jump")
def jump(req: JumpRequest):
    """Jump to a slot of the queue.

    `index` is the queue's 1-based slot, as the SSE `track_index` field
    names it — HQPlayer's entry for it is the output's to find (a slot it
    does not hold has none). Used by the Queue sheet to play a specific
    track without rebuilding the playlist; on a playlist that is no mirror
    the tap is a play press, answered at once with the track it will start
    (`starts`). A slot the output does not take — a gap of this HQPlayer —
    is answered with the output's own words (`refusal`), as every transport
    command is. The tap names the track the sheet showed (`track_id`): a
    sheet not refreshed yet — a replace or a radio from another device —
    plays that track wherever the queue holds it now, and one the queue no
    longer holds is refused in words. While HQPlayer takes a queue mutation
    (a new queue, a new order, a "play next", a removal) it plays the
    track wherever the mutation puts it — one the mutation takes away is
    refused."""
    if req.index < 1:
        raise HTTPException(status_code=400, detail="index must be >= 1")
    # A queue mutation in flight: an output that holds the person's words
    # finds the track among what it is handed (the queue commits only after
    # it); any other is handed the slot that holds the track now
    active = manager.active
    slot = (req.index if active is not None and active.holds_words and manager.queue.view()[2]
            else manager.queue.slot_of(req.index, req.track_id))
    if slot is None:
        return {"ok": False, "index": req.index, "refusal": "it is no longer in the queue"}
    try:
        # select() loads AND plays the slot (PlayerBackend contract): a
        # play() after it raced the DLNA load and resumed the OLD track for
        # the seconds the new one took to buffer
        b = manager.ensure_active()
        if not manager.queue.view()[2]:
            manager.before_start(b, slot)       # the file the tap would start, looked at first
        ok = b.select(slot, req.track_id)
    except Exception as e:
        raise _output_error(e)
    answer = {"ok": True, "index": slot} if ok else {**_refused(b), "index": slot}
    return _starting(b, answer)


def _add_owned(rows: list, *, clear_first: bool, position: str = "end",
               archive: Optional[Callable[[], None]] = None,
               left: Optional[list] = None) -> int:
    """THE single path for queueing owned tracks by the rows a route read:
    each row its file where one opens here now (`id`), else its track's
    stream, queued as the track itself (_reach_segments, which refuses rows
    none of which opens where nothing streams) — full-metadata QueueItems,
    committed in ONE mutation: every slot in its place at once, whatever the
    output makes of it: on HQPlayer an m4a or a CUE slice it is not handed
    yet waits behind its horizon and the fetch lane prepares it in the
    queue's order (playback.substitute), so a slow transcode never blocks
    the add and nothing tapped after it lands inside it. ``clear_first``
    replaces the queue (and starts playback) — ``archive``, the session
    rotation, once the output has taken it (PlaybackManager.replace_queue);
    otherwise tracks append (``position`` next|end). Returns the count
    queued; ``left`` takes what was left out, each with why — the caller
    expects ``len(rows) - len(left)`` slots."""
    items, out = _segment_items(*_reach_segments(rows))
    if left is not None:
        left.extend(out)
    if not items:
        return 0
    if clear_first:
        added, _gen = manager.replace_queue(items, play=True, archive=archive)
    else:
        added = manager.append(items, position) or 0
    return added


@router.post("/play-track")
def play_track(req: PlayTrackRequest):
    """Replace the queue with a single track and play it — its file where
    that opens here now, else a copy that does, else its stream
    (_add_owned)."""
    row = _db_query_one(_FILE_ROWS, {"ids": [req.track_id], "hqp_id": active_hqp_endpoint(),
                                     "gone": _gone()})
    if not row:
        raise HTTPException(status_code=404, detail="Track not found")

    try:
        left: list = []
        added = _add_owned([row], clear_first=True, left=left, archive=lambda: sessions.rotate_session(
            manager.queue, 'track', seed_media_file_id=req.track_id))
        if left:
            raise HTTPException(status_code=404, detail="Nothing in that list streams here")
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
        raise _output_error(e)


@router.post("/play-album")
def play_album(req: PlayAlbumRequest):
    """Play a whole album — by canonical UUID (owned or phantom) or fuzzy name.

    An album with no rip has nothing to load from disk, so it is handed to the
    streaming path: the same thing the album page does when the user presses
    Play on a not-owned release."""
    if req.album_id:
        best_album = _db_query_one(
            "SELECT al.id, al.title AS album FROM albums al WHERE al.id = %(id)s::uuid",
            {"id": req.album_id})
        if not best_album:
            raise HTTPException(status_code=404, detail="Album not found")
        return _play_album_rows(best_album)

    if not req.album_name:
        raise HTTPException(status_code=400, detail="album_id or album_name required")

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
        JOIN owned_files mf ON mf.album_variant_id = av.id
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

    return _play_album_rows(best_album)


def _play_album_rows(best_album: dict):
    """Replace the queue with one album's tracks and play: each track its
    best copy that opens here now, else its stream (_reach_segments); an
    album with no copy for this output is streamed whole."""
    album_id = str(best_album["id"])
    rows = _album_rows(album_id)

    if not rows:
        return play_phantom_album(PlayPhantomAlbumRequest(album_id=album_id))

    items, left = _segment_items(*_reach_segments(rows))
    if not items:
        raise HTTPException(status_code=404, detail="Nothing in that list streams here")
    try:
        added, _gen = manager.replace_queue(items, play=True, archive=lambda: sessions.rotate_session(
            manager.queue, 'album', origin_album_id=album_id,
            seed_media_file_id=next((r["id"] for r in rows if r["id"] is not None
                                     and r["location"] == "local"), None),
            seed_track_id=items[0].track_id if items[0].media_file_id is None else None))
        _exit_radio_mode()

        if added < len(items):
            raise HTTPException(
                status_code=503,
                detail=f"HQPlayer added {added} of {len(items)} tracks "
                       "(connection unstable). Try again.",
            )
        gone = {t["track_id"] for t in left}
        rows = [r for r in rows if r["track_id"] not in gone]
        return {
            "ok": True,
            "artist": rows[0]["artist"],
            "album": rows[0]["album"],
            "track_count": len(rows),
            "tracks": [
                {"id": r["id"] if r["location"] == "local" else None,
                 "track_id": r["track_id"], "location": r["location"],
                 "title": r["title"], "track_number": r.get("track_number")}
                for r in rows
            ],
            "not_streamable": left,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise _output_error(e)


def _parallel_resolve(provider, queries: list, wanted: list) -> list:
    """Resolve the `wanted` indices of `queries` on a provider (the fast
    availability pass, no downloads). Returns a list parallel to `wanted` of
    ResolvedSource / None (not on this provider) / ProviderUnavailable (no
    answer — the caller must not cache a miss).

    The rest of `queries` is the albums the wanted tracks sit on. A provider
    that declares `resolve_batch` reads all of it: a tracklist corroborates
    what a single track cannot — the lossless provider lines the album up against the
    catalog's, YouTube gates an unnamed channel on the album vouching for it.
    Acceptance stays here either way — a batch result is held to the same
    length test `resolve()` applies."""
    from concurrent.futures import ThreadPoolExecutor
    from streaming.base import ProviderUnavailable, fits_length

    if not wanted:
        return []
    batch = getattr(provider, "resolve_batch", None)
    if callable(batch):
        try:
            res = batch(queries, wanted)
        except ProviderUnavailable as e:
            return [e] * len(wanted)
        return [r if (r is None or isinstance(r, ProviderUnavailable)
                      or fits_length(queries[i], r.duration)) else None
                for i, r in zip(wanted, res)]

    def one(q):
        try:
            return provider.resolve(q)
        except ProviderUnavailable as e:
            return e                    # no answer — the caller must not cache a miss
        except Exception as e:
            # A provider bug is not "the track is nowhere": surface it, and keep
            # it out of the chain cache like any other non-answer.
            logger.warning("%s resolve crashed: %s", provider.manifest.id, e)
            return ProviderUnavailable(str(e))

    with ThreadPoolExecutor(max_workers=min(provider.resolve_workers, len(wanted)),
                            thread_name_prefix="resolve") as ex:
        return list(ex.map(one, [queries[i] for i in wanted]))


def _album_context(queries: list, pending: list) -> list:
    """The rest of every album a pending query sits on — its tracklist rows
    that are not among `queries` — appended to a resolve pass as EVIDENCE for
    the providers' batch tier, never as work: a provider's album lookup lines a
    whole tracklist up against the catalog's, and a YouTube archive channel
    is vouched for by the album's other tracks. Nothing here is returned or
    cached; a context row resolves on its own request, with its own album."""
    albums = {queries[i].album_id for i in pending if queries[i].album_id}
    if not albums:
        return []
    have = {(q.album_id, q.track_id) for q in queries}
    return [q for aid in albums for q in _phantom_album_queries(aid)
            if (aid, q.track_id) not in have]


# Resolved-source cache: (track_id, provider order) -> (timestamp, chain). One
# resolve pass is tens of provider searches — seconds — and the album page runs
# it (availability) moments before Play runs it AGAIN on the exact same tracks,
# that second one sitting squarely in front of the first sound. Source ids are
# stable catalogue facts, so one resolve serves both readers; a stale id simply
# fails its download and cascades down the chain. Keyed on the provider order
# too: a provider cooldown demotes/drops it (providers_preferred), and those
# thinner chains must not outlive the cooldown.
_chain_cache: dict[tuple, tuple] = {}
_CHAIN_TTL_S = 3600.0

# What a provider said it cannot serve, and why: (track_id, provider id) ->
# (timestamp, Refused code), written by the fetch lane when a link refuses
# with a reason (streaming.base.Refused — not available in this region, no
# recording of that length, no clip). A fact of the catalog, not of the
# hour, so it outlives the chain cache: a day, in memory, a restart forgets.
# The resolve leaves a refused provider out of THAT track's chain (never its
# album's: a region's licence is per track), so availability says the track
# does not stream here before anyone presses Play. A plain failure — a 403, a
# cut connection — says nothing of the track and is not remembered.
_refusals: dict[tuple, tuple] = {}
_REFUSAL_TTL_S = 24 * 3600.0
_refusals_lock = threading.Lock()


def note_refusal(query, provider, code: str) -> None:
    """The media proxy's hook (MediaProxy.link_refused): remembered, and the
    track's cached chains dropped — the next resolve builds it without that
    provider."""
    if not query.track_id:
        return
    with _refusals_lock:
        _refusals[(query.track_id, provider.manifest.id)] = (time.time(), code)
    for key in [k for k in list(_chain_cache) if k[0] == query.track_id]:
        _chain_cache.pop(key, None)


def _refusals_of(track_id: Optional[str]) -> dict:
    """provider id -> code of every refusal still remembered for the track."""
    if not track_id:
        return {}
    now = time.time()
    with _refusals_lock:
        for key, (ts, _code) in list(_refusals.items()):
            if now - ts >= _REFUSAL_TTL_S:
                _refusals.pop(key, None)
        return {pid: code for (tid, pid), (_ts, code) in _refusals.items() if tid == track_id}


def _refused_code(track_id: Optional[str]) -> Optional[str]:
    """Why the track does not stream here, by what was remembered: the code
    of the best provider that refused it (the preferred order), else of any;
    None when nothing refused it."""
    refused = _refusals_of(track_id)
    if not refused:
        return None
    from streaming import service as streaming_service
    for p in streaming_service.providers_preferred():
        if p.manifest.id in refused:
            return refused[p.manifest.id]
    return next(iter(refused.values()))


def _resolve_waterfall(queries: list, unanswered_ids: Optional[set] = None) -> list:
    """Per-track lossless-first fallback CHAIN ``[(provider, source_id), ...]``.
    Each track resolves to its best provider (lossless before YouTube lossy);
    the chain then appends LOWER-preference providers (source_id=None, resolved
    lazily inside fetch) as DOWNLOAD-failure fallbacks — so a track the lossless provider has but
    can't serve in FLAC (region/licensing) still streams from YouTube instead of
    just dropping. An empty chain == no provider resolved it (truly unavailable).
    Cached per track (see _chain_cache) — including the empty chain, or every
    play would re-search every provider for a track known to be nowhere. A
    track some provider could not ANSWER for (timeout, quota) is not cached:
    its chain is built from the providers that did answer, and the next
    resolve asks the silent one again; `unanswered_ids`, when given, is told
    those tracks' ids."""
    from streaming import demo
    from streaming import service as streaming_service
    from streaming.base import ProviderUnavailable

    provs = streaming_service.providers_preferred()
    # A track whose demo listen is spent (streaming/demo.py) has no
    # demo-limited provider in ITS order: the pass asks only the providers
    # that may still serve it, so its chain holds their real answer — never a
    # lazy link the fetch would have refused, which is what a post-filter of
    # a cached chain would leave, and what availability would then report as
    # streamable on a guess. The order is the cache key, so spending the
    # listen is a cache miss, not a stale chain.
    spent = demo.consumed(q.track_id for q in queries)
    # A provider that refused the track with a reason is left out of ITS
    # order the same way (_refusals), so its chain is the others' answer
    refused = {q.track_id: _refusals_of(q.track_id) for q in queries if q.track_id}

    def admits(p, q) -> bool:
        return demo.admits(p, q.track_id, spent) and p.manifest.id not in refused.get(q.track_id, {})

    def order_for(q) -> tuple:
        return tuple(p.manifest.id for p in provs if admits(p, q))

    now = time.time()
    for key, (ts, _chain) in list(_chain_cache.items()):
        if now - ts >= _CHAIN_TTL_S:
            _chain_cache.pop(key, None)

    chains = [None] * len(queries)
    pending = []                            # indices still to resolve
    for i, q in enumerate(queries):
        hit = _chain_cache.get((q.track_id, order_for(q))) if q.track_id else None
        if hit is not None:
            chains[i] = hit[1]
        else:
            pending.append(i)

    # Every provider sees the albums the pending tracks sit on (the rows
    # `queries` lacks come from the catalog); only `unresolved` is asked for.
    pool = queries + _album_context(queries, pending) if pending else queries
    best = {}                               # index -> index into provs that resolved
    sids, durs, arts = {}, {}, {}
    unanswered = set()                      # a provider gave no answer — not a miss
    silence = {}                            # provider id -> the first reason it gave none
    answered = set()                        # provider ids that gave a definite answer
    unresolved = list(pending)
    for pi, prov in enumerate(provs):
        if not unresolved:
            break
        if not prov.supports_resolve:
            continue                        # can't pre-resolve; appears only as a lazy fallback
        wanted = [i for i in unresolved if admits(prov, queries[i])]
        res = dict(zip(wanted, _parallel_resolve(prov, pool, wanted)))
        # Two providers on one API are one voice for the notices channel: a
        # silence is one notice, and either answering clears it.
        voice = streaming_service.health_key(prov)
        still = []
        for i in unresolved:
            if i not in res:
                still.append(i)             # not asked of this provider (demo spent, refused)
                continue
            r = res[i]
            if isinstance(r, ProviderUnavailable):
                unanswered.add(i)
                still.append(i)
                silence.setdefault(voice, str(r))
            elif r is not None:
                answered.add(voice)
                best[i], sids[i], durs[i], arts[i] = pi, r.source_id, r.duration, r.artwork_url
            else:
                answered.add(voice)
                still.append(i)
        unresolved = still

    if pending:
        # A provider silent for the WHOLE pass is a condition the user should
        # see (rows grey out with no reason otherwise); one that answered
        # anything is back. Transitions wake the notices channel.
        silent_all = {pid: reason for pid, reason in silence.items()
                      if pid not in answered}
        if streaming_service.note_provider_health(silent_all, answered):
            from db_pool import db_execute
            db_execute("NOTIFY sautium_notices")

    if pending:
        # One line per pass, so a catalog that quietly stops answering (quota,
        # a stale yt-dlp) or a gate that rejects everything is visible in the
        # log instead of inferred from what the queue ended up holding.
        got = ", ".join(
            f"{prov.manifest.id} {sum(1 for i in pending if best.get(i) == pi)}"
            for pi, prov in enumerate(provs) if prov.supports_resolve)
        logger.info("resolve pass: %d tracks — %s; nowhere %d; unanswered %d; demo spent %d%s",
                    len(pending), got, sum(1 for i in pending if i not in best),
                    len(unanswered),
                    sum(1 for i in pending if queries[i].track_id in spent),
                    "".join(f"; {k}: {v}" for k, v in silence.items()))

    for i in pending:
        q, d, art = queries[i], durs.get(i), arts.get(i)
        # Only MB-length-less tracks — the virtual duration is a FALLBACK, never an
        # override of a known MB length (that is the canonical display; the resolved
        # source may be a mismatch at a different length).
        if d and d > 0 and q.track_id and not q.duration:
            _resolved_durations[q.track_id] = float(d)
        if art and q.track_id:
            _resolved_artwork[q.track_id] = art

        if i in best:
            chain = [(provs[best[i]], sids[i])]
            chain += [(provs[pi], None) for pi in range(best[i] + 1, len(provs))
                      if admits(provs[pi], q)]
        else:
            chain = [(p, None) for p in provs
                     if not p.supports_resolve and admits(p, q)]
        chains[i] = chain
        if q.track_id and i not in unanswered:
            _chain_cache[(q.track_id, order_for(q))] = (now, chain)
    if unanswered_ids is not None:
        unanswered_ids.update(queries[i].track_id for i in unanswered if queries[i].track_id)
    return chains


def _excerpt_ids(items: list) -> list:
    """Track ids among ``(query, chain)`` items that will stream as a 30 s
    excerpt: every chain the track resolved on is headed by the excerpt
    provider (a demo listen spent, or nothing full-length has it)."""
    full, clipped = set(), set()
    for q, chain in items:
        if not q.track_id or not chain:
            continue
        (clipped if chain[0][0].manifest.excerpt else full).add(q.track_id)
    return sorted(clipped - full)


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
    provider resolve retries. Empty without the local MB dump (optional layer).

    The alternates steer which stream a phantom slot accepts, and that
    stream's analysis is then signed: on a node whose MB rows came from
    slices they are MusicBrainz's own answer for the one MBID the local
    artist of that name is bound to — never a slice's aliases — and nothing
    for a name no single binding covers."""
    import mb_backend as mb
    from canon import confirm
    if not name or not mb.LOCAL_DUMP:
        return ()
    if not confirm.required():
        return tuple(mb.artist_alt_names(name))
    return _answered_alts(name)


def _answered_alts(name: str, limit: int = 3) -> tuple:
    import mb_answers
    from uuid_utils import artist_uuid
    rows = _db_query("SELECT mbid::text AS mbid FROM artist_mbids WHERE artist_id = %(a)s::uuid",
                     {"a": str(artist_uuid(name))})
    if len(rows) != 1:
        return ()
    answer = mb_answers.answer("artist", rows[0]["mbid"])
    data = mb_answers.artist(answer.target) if answer and answer.known and answer.fresh else None
    if data is None:
        return ()
    own = name.strip().lower()
    out: list = []
    for cand in [data["name"]] + [a["name"] for a in data["aliases"]]:
        if cand and cand.lower() != own and cand not in out:
            out.append(cand)
    return tuple(out[:limit])


def _credit_alts(credit: str, album_artists: tuple = ()) -> tuple:
    """Names a catalog may file the credited recording under, beyond the MB
    aliases: the head of a collaboration credit — catalogs carry the guest in
    the title, so "Sarathy Korwar feat. Photay" is filed under Sarathy Korwar
    and a channel gate on the full credit matches nothing — then the album's
    own artist when the slot is credited to someone else. The head goes first
    among the additions: a provider's one retry searches under alts[0]."""
    from canon.split import detect_compound_type
    from streaming.base import norm_key
    alts = list(_artist_alts(credit))
    compound = detect_compound_type(credit)
    if compound:
        head = compound[2][0]
        alts.append(head)
        alts.extend(_artist_alts(head))
    alts.extend(a for a in album_artists if a)
    own = norm_key(credit)
    return tuple(dict.fromkeys(a for a in alts if a and norm_key(a) != own))


def _album_primary_artists(album_ids: list) -> dict:
    """The primary artists per album id."""
    if not album_ids:
        return {}
    rows = _db_query("""
        SELECT aa.album_id::text AS album_id, ar.name
        FROM album_artists aa JOIN artists ar ON ar.id = aa.artist_id
        WHERE aa.album_id = ANY(CAST(%(ids)s AS uuid[])) AND aa.role = 'primary'
        ORDER BY aa.album_id, ar.name
    """, {"ids": album_ids})
    out: dict = {}
    for r in rows:
        out.setdefault(r["album_id"], []).append(r["name"])
    return {k: tuple(v) for k, v in out.items()}


def _album_performers(album_ids: list) -> dict:
    """Performers per album id — the artists an MB credit names AFTER the
    composers, split on the ';' its join phrases carry ("Steve Reich;
    Ensemble Contrechamps & Eklekto", "Rihm, Currier; Anne-Sophie Mutter,
    New York Philharmonic, …").

    A classical release group IS one reading of a work, but our album is
    credited to whoever wrote it, and every other ensemble's reading of the
    same movements runs to nearly the same length: Music for 18 Musicians by
    Contrechamps/Eklekto matched Reich's own 1978 recording on two of
    fourteen sections (239 s against a catalogued 233 s — 2.6 %, well inside
    the tolerance) and would have streamed the wrong ensemble. The composer's
    name gates nothing here; the performers do. Empty for albums whose credit
    names only the artist — the ordinary case, where nothing changes."""
    import mb_backend as mb
    from canon import confirm
    if not album_ids or not mb.LOCAL_DUMP:
        return {}
    if confirm.required():
        # A node with slices reads the group's credit as MusicBrainz answered
        # it (the discography's browse fetched it), never a slice's.
        rows = _db_query("""
            SELECT a.id::text AS album_id, c.position, c.artist_name AS artist,
                   c.join_phrase
            FROM albums a
            JOIN mbws_credits c ON c.owner_kind = 'release_group' AND c.owner = a.musicbrainz_id
            WHERE a.id = ANY(CAST(%(ids)s AS uuid[]))
            ORDER BY a.id, c.position
        """, {"ids": album_ids})
    else:
        rows = _db_query("""
            SELECT a.id::text AS album_id, acn.position,
                   COALESCE(ma.name, acn.name) AS artist, acn.join_phrase
            FROM albums a
            JOIN mb_release_group rg ON rg.gid = a.musicbrainz_id
            JOIN mb_artist_credit_name acn ON acn.artist_credit = rg.artist_credit
            LEFT JOIN mb_artist ma ON ma.id = acn.artist
            WHERE a.id = ANY(CAST(%(ids)s AS uuid[]))
            ORDER BY a.id, acn.position
        """, {"ids": album_ids})
    members: dict = {}
    for r in rows:
        members.setdefault(r["album_id"], []).append(r)
    out: dict = {}
    for aid, ms in members.items():
        split = next((i for i, m in enumerate(ms) if ";" in (m["join_phrase"] or "")), None)
        if split is None:
            continue
        names = tuple(m["artist"] for m in ms[split + 1:] if m["artist"])
        if names:
            out[aid] = names
    return out


def _album_barcodes(album_ids: list) -> dict:
    """MB barcodes per album id — those of the release group's editions that
    carry exactly this many tracks, i.e. the tracklist the phantom shows. A
    catalog that answers to a barcode then names the
    edition outright, with no search to second-guess. Digital editions first:
    that is the one a streaming catalog stocks (a catalog's Protection is MB's
    Digital Media release, barcode for barcode). Empty without the local MB
    dump (optional layer), or for an album MB never barcoded."""
    import mb_backend as mb
    from canon import confirm
    if not album_ids or not mb.LOCAL_DUMP:
        return {}
    if confirm.required():
        # A node with slices names only editions MusicBrainz answered for (the
        # releases the tracklist mint asked about), never a slice's barcodes.
        rows = _db_query("""
            WITH ours AS (
                SELECT a.id AS album_id, a.musicbrainz_id AS rg,
                       (SELECT count(*) FROM album_tracks x WHERE x.album_id = a.id) AS n
                FROM albums a
                WHERE a.id = ANY(CAST(%(ids)s AS uuid[])) AND a.musicbrainz_id IS NOT NULL
            ), editions AS (
                SELECT o.album_id, o.n, r.mbid::text AS release_id, r.barcode,
                       bool_or(t.media_format = 'Digital Media') AS digital,
                       count(*) AS tracks
                FROM ours o
                JOIN mbws_releases r ON r.release_group = o.rg
                JOIN mbws_tracks t ON t.release = r.mbid
                WHERE r.barcode IS NOT NULL AND r.barcode <> ''
                GROUP BY o.album_id, o.n, r.mbid, r.barcode
            )
            SELECT album_id::text AS album_id, barcode,
                   bool_or(digital) AS digital, min(release_id) AS first_id
            FROM editions WHERE tracks = n
            GROUP BY album_id, barcode
            ORDER BY album_id, digital DESC, first_id
        """, {"ids": album_ids})
    else:
        rows = _db_query("""
            WITH ours AS (
                SELECT a.id AS album_id, rg.id AS rg_id,
                       (SELECT count(*) FROM album_tracks x WHERE x.album_id = a.id) AS n
                FROM albums a JOIN mb_release_group rg ON rg.gid = a.musicbrainz_id
                WHERE a.id = ANY(CAST(%(ids)s AS uuid[]))
            ), editions AS (
                SELECT o.album_id, o.n, r.id AS release_id, r.barcode,
                       bool_or(f.name = 'Digital Media') AS digital,
                       sum(m.track_count) AS tracks
                FROM ours o
                JOIN mb_release r ON r.release_group = o.rg_id
                JOIN mb_medium m ON m.release = r.id
                LEFT JOIN mb_medium_format f ON f.id = m.format
                WHERE r.barcode IS NOT NULL AND r.barcode <> ''
                GROUP BY o.album_id, o.n, r.id, r.barcode
            )
            SELECT album_id::text AS album_id, barcode,
                   bool_or(digital) AS digital, min(release_id) AS first_id
            FROM editions WHERE tracks = n
            GROUP BY album_id, barcode
            ORDER BY album_id, digital DESC, first_id
        """, {"ids": album_ids})
    out: dict = {}
    for r in rows:
        codes = out.setdefault(r["album_id"], [])
        if len(codes) < 3:
            codes.append(r["barcode"])
    return {k: tuple(v) for k, v in out.items()}


def _phantom_track_queries(track_ids: list[str]) -> dict:
    """TrackQuery per phantom track UUID, from album_tracks — keyed by id, ids
    with no tracklist row absent. Batched: a similar-tracks set is a dozen
    phantoms at once and one query per track is a dozen round-trips."""
    from streaming.base import TrackQuery
    if not track_ids:
        return {}
    # No album context here, so the query vouches for EVERY length the
    # catalog lists the track at (`lengths`): the track sits on each album
    # that names it, and editions name it at their own lengths — a studio cut
    # and its live takes share one uuid. The DISTINCT ON row is only the
    # display edition; holding the provider to its length alone rejected the
    # Kind of Blue "All Blues" for being longer than a compilation's.
    rows = _db_query("""
        SELECT DISTINCT ON (t.id)
               t.id::text AS track_id, t.title, atr.length_ms,
               al.title AS album, al.id::text AS album_id, al.cover_url,
               (SELECT array_agg(DISTINCT x.length_ms) FROM album_tracks x
                 WHERE x.track_id = t.id AND x.length_ms IS NOT NULL) AS lengths_ms,
               (SELECT ar.name FROM track_artists ta
                  JOIN artists ar ON ar.id = ta.artist_id
                WHERE ta.track_id = t.id
                ORDER BY (ta.role = 'primary') DESC LIMIT 1) AS artist
        FROM tracks t
        JOIN album_tracks atr ON atr.track_id = t.id
        JOIN albums al ON al.id = atr.album_id
        WHERE t.id = ANY(CAST(%(ids)s AS uuid[]))
        ORDER BY t.id, (al.cover_url IS NOT NULL) DESC,
                 (atr.length_ms IS NOT NULL) DESC, al.id
    """, {"ids": list(track_ids)})
    album_ids = list({r["album_id"] for r in rows})
    owners = _album_primary_artists(album_ids)
    codes = _album_barcodes(album_ids)
    players = _album_performers(album_ids)
    alts = {(r["artist"], r["album_id"]): _credit_alts(r["artist"], owners.get(r["album_id"], ()))
            for r in rows if r["artist"]}
    return {
        r["track_id"]: TrackQuery(
            artist=r["artist"] or "", title=r["title"], album=r["album"],
            artist_alts=alts.get((r["artist"], r["album_id"]), ()),
            duration=(float(r["length_ms"]) / 1000.0 if r["length_ms"] else None),
            lengths=tuple(float(ms) / 1000.0 for ms in (r["lengths_ms"] or [])),
            track_id=r["track_id"], cover_url=r["cover_url"],
            album_id=r["album_id"], album_artists=owners.get(r["album_id"], ()),
            barcodes=codes.get(r["album_id"], ()),
            performers=players.get(r["album_id"], ()))
        for r in rows if r["title"]
    }


def _phantom_track_query(track_id: str, album_id: Optional[str] = None):
    """TrackQuery for one phantom track, or None. `album_id` is the album the
    row was clicked on: the query is then that album's own — the one its
    Stream all builds — so the slot carries the same edition, length, art and
    album_id, and Now Playing never lands on a reissue the listener didn't
    press (a canonical track sits on every album that lists it). No context,
    or an album that no longer lists the track: the display edition."""
    if album_id:
        own = _phantom_album_queries(album_id, track_id=track_id)
        if own:
            return own[0]
    return _phantom_track_queries([track_id]).get(track_id)


def _phantom_album_queries(album_id: str, track_id: Optional[str] = None) -> list:
    """Ordered TrackQuery list for a phantom album's tracklist (album_tracks) —
    or, with `track_id`, just that track's row on this album: the query a
    single row's click shares with the album's Stream all (empty when the
    album no longer lists the track)."""
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
          AND (%(track_id)s::uuid IS NULL OR atr.track_id = %(track_id)s::uuid)
        ORDER BY atr.disc, atr.position
    """, {"album_id": album_id, "track_id": track_id})
    album_artists = _album_primary_artists([album_id]).get(album_id, ())
    barcodes = _album_barcodes([album_id]).get(album_id, ())
    performers = _album_performers([album_id]).get(album_id, ())
    alts = {n: _credit_alts(n, album_artists) for n in {r["artist"] for r in rows if r["artist"]}}
    return [
        TrackQuery(
            artist=r["artist"] or "", title=r["title"], album=r["album"],
            artist_alts=alts.get(r["artist"], ()),
            duration=(float(r["length_ms"]) / 1000.0 if r["length_ms"] else None),
            track_id=r["track_id"], cover_url=r["cover_url"], album_id=album_id,
            album_artists=album_artists, barcodes=barcodes, performers=performers)
        for r in rows if r["title"]
    ]


@router.get("/phantom-availability/{album_id}")
def phantom_availability(album_id: str) -> dict:
    """Track ids of a phantom album NO provider can stream, so the album page can
    dim + disable them up front (no need to stream first). Tries every provider
    (lossless-first); a track is unavailable only if none has it, and available if
    ANY of its tracklist rows resolves (the same title can appear at several
    durations — one match is enough). The resolve behind it is cached per track
    (_chain_cache), which is also what the Play that follows this page view
    reads — so the album resolves ONCE, off the path to the first sound."""
    from streaming import service as streaming_service
    if not streaming_service.is_enabled():
        return {"unavailable": []}
    if not streaming_service.providers_preferred():
        return {"unavailable": []}

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
    unavailable = all_ids - set(best_lossless)
    quality = _mix_quality(sum(best_lossless.values()),
                           sum(1 for v in best_lossless.values() if not v))
    # DISPLAY-only durations the resolve recovered for MB-length-less tracks
    # (not persisted — see _resolved_durations).
    durations = {t: _resolved_durations[t] for t in all_ids
                 if t in _resolved_durations}
    # Why each unavailable row does not stream here: what a provider said
    # when it refused it (_refusals), else no source has it
    reasons = {t: _refused_code(t) or "nowhere" for t in unavailable}
    first = next((q.track_id for q in queries if q.track_id), None)

    return {"unavailable": [{"track_id": t, "reason": reasons[t]} for t in unavailable],
            "quality": quality, "durations": durations,
            # Rows that will play as a 30 s excerpt (an excerpt head counts as
            # lossy in `quality` — it is).
            "excerpt": _excerpt_ids(list(zip(queries, chains))),
            # Nothing on the album streams here: said before Play, with the
            # reason of its first track
            "not_streamable": ({"reason": reasons.get(first, "nowhere")}
                               if all_ids and unavailable == all_ids else None)}


class PlayPhantomAlbumRequest(BaseModel):
    album_id: str             # UUID of a phantom (not-owned) album
    position: str = "end"     # queue endpoint only: 'next' | 'end'


def _output_refusal(e: Exception) -> tuple[int, str]:
    """What a play intent or transport command the output did not take
    answers, and why: 409 while the output is lent (the HQPlayer benchmark),
    while HQPlayer builds a setting or takes the connection and says nothing,
    and for a command its link no longer sends (a newer intent came first,
    the output was switched away) — busy, not broken: the UI must not send
    the owner to the Output picker — 503 when it cannot take it. A backend
    detached by the hold that an intent took just before refuses with a
    plain ConnectionError — the hold names it."""
    hold = manager.held
    if isinstance(e, OutputHeld):
        return 409, str(e)
    if hold is not None and isinstance(e, ConnectionError):
        return 409, str(OutputHeld(hold))
    if isinstance(e, (Building, NotAnswering, Dropped)):
        return 409, str(e)
    return 503, str(e)


def _output_error(e: Exception, refusal: Optional[tuple[int, str]] = None) -> HTTPException:
    """The refusal (_output_refusal, or the one the caller already read) as
    the route's answer, with what the person's words made meanwhile could
    not do (said_with: a track tapped while a new queue that did not commit
    loaded) — for the person, never for the log."""
    if isinstance(e, NotHere):
        return _not_here(e.why, e.album or None)
    status, why = refusal or _output_refusal(e)
    said = said_with(e)
    return HTTPException(status_code=status, detail=f"{why} — {said}" if said else why)


def _streaming_503(msg: str) -> HTTPException:
    """Streaming-subsystem failure. The structured detail (`reason`) lets the
    UI route it to a plain error dialog — the audio output is fine, so the
    output-picker dialog the other 503s get would misdirect the user."""
    return HTTPException(status_code=503,
                         detail={"message": msg, "reason": "streaming"})


def _ensure_streaming_ready() -> None:
    """Pre-flight for the phantom endpoints: the subsystem is up AND at least
    one provider is registered (a missing yt-dlp leaves the registry empty —
    one honest 503 beats every track greyed out as provider-missing)."""
    from streaming import service as streaming_service
    if not streaming_service.is_enabled():
        raise _streaming_503("Streaming preview is disabled on this node")
    if not streaming_service.providers_preferred():
        raise _streaming_503("No streaming provider is available right now — "
                             "see the backend log")


def _known_unstreamable(queries: list, whole: bool = False) -> dict:
    """track_id -> why, for the queries the node ALREADY knows nothing streams
    here: a resolve's empty chain still cached, or every provider having
    refused the track (_refusals). Never resolves — a tap that asked the
    providers before it committed would let a later tap commit first, the
    queue's order the resolve's timing (B-124). `whole` (a slot played whole
    or not at all): also what the node knows streams only as an excerpt —
    no full-length provider left in its order (its demo listen spent), or a
    cached chain headed by the excerpt."""
    from streaming import demo
    from streaming import service as streaming_service
    provs = streaming_service.providers_preferred()
    ids = [q.track_id for q in queries if q.track_id]
    spent = demo.consumed(ids)
    now = time.time()
    out = {}
    for tid in ids:
        refused = _refusals_of(tid)
        order = tuple(p.manifest.id for p in provs
                      if demo.admits(p, tid, spent) and p.manifest.id not in refused)
        hit = _chain_cache.get((tid, order))
        fresh = hit is not None and now - hit[0] < _CHAIN_TTL_S
        if (provs and not order) or (fresh and not hit[1]):
            out[tid] = _refused_code(tid) or "nowhere"
        elif whole and (
                (provs and all(p.manifest.excerpt for p in provs if p.manifest.id in order))
                or (fresh and hit[1][0][0].manifest.excerpt)):
            out[tid] = "excerpt_only"
    return out


def _streamed_items(queries: list, whole: bool = False) -> tuple[list, list]:
    """The slots a tap commits for streamed tracks (playback.queue.
    items_for_streamed), in order, and what it leaves out — only what the
    node already knows does not stream here, each with why."""
    known = _known_unstreamable(queries, whole=whole)
    left = [{"track_id": q.track_id, "title": q.title, "reason": known[q.track_id]}
            for q in queries if q.track_id in known]
    return queue_mod.items_for_streamed([q for q in queries if q.track_id not in known],
                                        whole=whole), left


def _tapped(queries: list, items: list, left: list, **extra) -> dict:
    """What a tap on streamed tracks answers, at once: how many slots it
    queued, and what it left out (`not_streamable`, each with its reason;
    `missing` the same, for the pages that read it). Whether each one
    streams is told by its row as the lane brings it."""
    return {"ok": True, "track_count": len(items), "requested": len(queries),
            "not_streamable": left, "missing": left, **extra}


@router.post("/play-phantom-album")
def play_phantom_album(req: PlayPhantomAlbumRequest):
    """Play a phantom (not-in-library) album: every track the node does not
    already know to be unstreamable takes its slot in a new queue at once, as
    the track itself — its stream is the fetch lane's, in the queue's order
    (playback.substitute), and the play starts on slot 1 when its stream
    lands. A first slot that fails is walked past; a Play whose first slots
    all fail is said at once (PlaybackManager._slot_failed)."""
    _ensure_streaming_ready()

    queries = _phantom_album_queries(req.album_id)
    if not queries:
        raise HTTPException(status_code=404, detail="Phantom album not found or has no tracklist")
    items, left = _streamed_items(queries)
    if not items:
        # Nothing on it streams here, as the node already knows: the queue
        # as it was; the page says so before Play
        return _tapped(queries, items, left, album=queries[0].album, artist=queries[0].artist)
    try:
        # The prior queue snapshotted into a session once the output took the
        # new one, then a phantom-album one opened — streamed albums land in
        # the Home shelf like owned albums (origin_album_id is the phantom
        # album's UUID; the seed is its first track).
        added, _gen = manager.replace_queue(items, play=True, archive=lambda: sessions.rotate_session(
            manager.queue, 'album', origin_album_id=str(req.album_id),
            seed_track_id=items[0].track_id))
        _exit_radio_mode()
    except Exception as e:
        raise _output_error(e)
    return _tapped(queries, items[:added], left, album=queries[0].album, artist=queries[0].artist)


class PlayPhantomTrackRequest(BaseModel):
    track_id: str             # UUID of a phantom (not-owned) track
    album_id: Optional[str] = None   # the album the row was clicked on = the slot's
                                     # album; None = no album context (a similar/search row)
    position: str = "end"     # queue endpoint only: 'next' | 'end'


@router.post("/play-phantom-track")
def play_phantom_track(req: PlayPhantomTrackRequest):
    """Play a single phantom track (replaces the queue), the streaming
    counterpart of clicking an owned track: its slot at once, its stream the
    lane's. `track_count` 0 with the track in `not_streamable` when the node
    already knows nothing streams it here."""
    _ensure_streaming_ready()

    q = _phantom_track_query(req.track_id, req.album_id)
    if q is None:
        raise HTTPException(status_code=404, detail="Phantom track not found")
    items, left = _streamed_items([q])
    if not items:
        return _tapped([q], items, left)
    try:
        # A single streamed track is a 'track'-origin session seeded by its
        # UUID; the album the row was clicked on is the session's origin
        # album, so the card wears that edition's cover.
        added, _gen = manager.replace_queue(items, play=True,
                                            archive=lambda: sessions.rotate_session(
                                                manager.queue, 'track', seed_track_id=req.track_id,
                                                origin_album_id=req.album_id))
        _exit_radio_mode()
    except Exception as e:
        raise _output_error(e)
    return _tapped([q], items[:added], left, title=q.title, artist=q.artist, album=q.album)


def _queue_streamed(queries: list, position: str) -> dict:
    """Streamed tracks into the queue in ONE mutation — at the end, or right
    after the playing slot ('next') — every slot in its place at the tap:
    whatever is tapped after it goes after it, whatever the fetches' timing
    (B-124). The canonical queue is the output's to follow, so this works
    with no output attached."""
    items, left = _streamed_items(queries)
    if items:
        try:
            manager.append(items, "next" if position == "next" else "end")
        except Exception as e:
            raise _output_error(e)
    return _tapped(queries, items, left)


@router.post("/queue-phantom-track")
def queue_phantom_track(req: PlayPhantomTrackRequest):
    """Add one phantom track to the queue (at the end, or next): its slot at
    once, its stream the lane's."""
    _ensure_streaming_ready()
    q = _phantom_track_query(req.track_id, req.album_id)
    if q is None:
        raise HTTPException(status_code=404, detail="Phantom track not found")
    return _queue_streamed([q], req.position)


@router.post("/queue-phantom-album")
def queue_phantom_album(req: PlayPhantomAlbumRequest):
    """Add a phantom album to the queue (at the end, or next): its slots at
    once, in its order, their streams the lane's."""
    _ensure_streaming_ready()
    queries = _phantom_album_queries(req.album_id)
    if not queries:
        raise HTTPException(status_code=404, detail="Phantom album not found or has no tracklist")
    return _queue_streamed(queries, req.position)


# -- Mixed (owned + phantom) queueing -----------------------------------------
#
# One ordered list of CANONICAL entities — tracks and albums, owned files and
# phantom tracklists freely mixed. This is what a caller that speaks the
# catalog rather than the filesystem needs: the AI assistant holds UUIDs and
# has no business knowing which of them happen to have a file on disk.
#
# Every slot is committed at the tap, owned and streamed alike, in the
# caller's order, in ONE mutation: a streamed one is the track itself, its
# stream resolved and fetched later by the lane in the queue's order
# (playback.substitute) — never inside the request, and never a slot
# appended as its fetch lands, which let a later tap land in between.


@router.get("/reach")
def reach():
    """Whether this node streams now: what the owner's music with no file
    here plays as — a stream, or nothing until its files are back — for the
    pages' words about a folder that does not answer."""
    return {"streams": substitute.streams()}


def _gone() -> list:
    """The stored-path prefixes of the folders that do not answer now — the
    `gone` every copy-picking query takes (sql_queries.HERE)."""
    from scanner import gone_prefixes
    return gone_prefixes()


# -- What opens here now ---------------------------------------------------------
#
# An owned track's file may not open here now — its copy away, or under a
# folder that does not answer (sql_queries.HERE). No route queues such a file:
# every one that takes owned music goes through ONE split (_reach_segments) —
# a copy that opens where there is one, else the track's stream, queued as
# the track itself, exactly as a not-owned track streams: the demo channel's
# one full listen, then the excerpt (no ledger exemption for a copy the node
# cannot read: it has rows, not bytes), a provider told nothing but title,
# artist and length. Where this node streams nothing the track is left out
# with why, and a request none of whose slots opens is refused before the
# queue or the output is touched (_not_here).

# Why a row of an owned track names no file that opens: its copies are away
# (a Rescan set them aside), or lie under a folder that does not answer.
_WHY = "CASE WHEN {f}.here THEN NULL WHEN {f}.absent_since IS NOT NULL THEN 'away' ELSE 'disk' END"

_NOT_HERE_UNTIL = {"disk": "its disk is connected", "away": "its files are back"}


def _not_here(why: str, album: Optional[str] = None) -> HTTPException:
    """The refusal of a play or a queue-add of which nothing opens here now,
    on a node that streams nothing: 409 with `reason` not_here, `why`
    ('disk' | 'away') and the sentence the page says as a toast. `album`:
    the one album every refused slot is of."""
    return HTTPException(status_code=409, detail={
        "reason": "not_here", "why": why,
        "message": f"{album or 'This music'} cannot play until {_NOT_HERE_UNTIL[why]}."})


def _album_rows(album_id: str) -> list[dict]:
    """The album's tracks in play order for the ACTIVE output: one row per
    track, its best copy — the copy at the HQPlayer that is the output first
    (it plays that natively), then the local rips; a second variant must
    neither duplicate the tracklist nor win over a better one. A copy at
    another HQPlayer is left out: this output cannot open it, and an album
    with nothing else streams like a phantom. A track none of whose copies
    opens here now has `id` None and `why` ('disk' | 'away'); the others
    carry `id` + `location` (queue.items_for_owned_rows). All carry the
    track UUID and the album's."""
    return _db_query(f"""
        SELECT CASE WHEN here THEN id END AS id, location, track_id, %(id)s AS album_id,
               title, track_number, artist, album, why
        FROM (
            SELECT DISTINCT ON (f.track_id)
                   f.id, f.location::text AS location, f.track_id::text AS track_id,
                   f.disc_number, f.track_number, t.title,
                   a.name AS artist, al.title AS album, f.here, {_WHY.format(f='f')} AS why
            FROM ({ALBUM_FILES}) f
            JOIN tracks t ON t.id = f.track_id
            JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
            JOIN artists a ON a.id = ta.artist_id
            JOIN albums al ON al.id = %(id)s::uuid
            WHERE f.location = 'local'
               OR f.hqp_endpoint_id = %(hqp_id)s
            ORDER BY f.track_id, {owned_rank('f')}, (f.absent_since IS NOT NULL),
                     {best_rip_order('f')}
        ) picked
        ORDER BY disc_number NULLS FIRST, track_number
    """, {"id": album_id, "hqp_id": active_hqp_endpoint(), "gone": _gone()})


# The media files %(ids)s names, in that order, as slots: the file itself
# where it opens here now (`id`, `location` 'local'), else the best copy of
# its track that does — one on the same album first — else `id` None with
# `why` ('disk' | 'away'). `asked` is the id named; every row carries its
# track's UUID, the file's album and the display fields.
_FILE_ROWS = f"""
        SELECT mf.id AS asked,
               CASE WHEN own.here THEN mf.id ELSE c.id END AS id,
               CASE WHEN own.here THEN 'local' ELSE c.location::text END AS location,
               mf.track_id::text AS track_id, av.album_id::text AS album_id,
               t.title, al.title AS album,
               (SELECT ar.name FROM track_artists ta JOIN artists ar ON ar.id = ta.artist_id
                 WHERE ta.track_id = t.id
                 ORDER BY (ta.role = 'primary') DESC LIMIT 1) AS artist,
               CASE WHEN own.here OR c.id IS NOT NULL THEN NULL
                    WHEN EXISTS (SELECT 1 FROM media_files m3
                                 JOIN album_variants v3 ON v3.id = m3.album_variant_id
                                 WHERE m3.track_id = mf.track_id AND v3.absent_since IS NULL)
                    THEN 'disk' ELSE 'away' END AS why
        FROM unnest(%(ids)s::int[]) WITH ORDINALITY AS s(id, ord)
        JOIN media_files mf ON mf.id = s.id
        JOIN tracks t ON t.id = mf.track_id
        JOIN album_variants av ON av.id = mf.album_variant_id
        JOIN albums al ON al.id = av.album_id
        CROSS JOIN LATERAL (SELECT {HERE} AS here) own
        LEFT JOIN LATERAL (
            SELECT f.id, f.location
            FROM (
                SELECT m2.id, v2.location, v2.hqp_endpoint_id, v2.album_id,
                       {here('m2', 'v2')} AS here,
                       m2.is_lossless, m2.sample_rate, m2.bit_depth
                FROM media_files m2 JOIN album_variants v2 ON v2.id = m2.album_variant_id
                WHERE m2.track_id = mf.track_id
                UNION ALL
                SELECT hf.id, v2.location, v2.hqp_endpoint_id, v2.album_id, true,
                       hf.is_lossless, hf.sample_rate, hf.bit_depth
                FROM hqp_library_files hf JOIN album_variants v2 ON v2.id = hf.album_variant_id
                WHERE hf.track_id = mf.track_id
            ) f
            WHERE f.here AND (f.location = 'local' OR f.hqp_endpoint_id = %(hqp_id)s)
            ORDER BY {owned_rank('f')}, (f.album_id = av.album_id) DESC, {best_rip_order('f')}
            LIMIT 1) c ON NOT own.here
        ORDER BY s.ord
"""


def _file_rows(ids: list) -> list[dict]:
    """The slots of the media files `ids` name (_FILE_ROWS), in that order."""
    return _db_query(_FILE_ROWS, {"ids": list(ids), "hqp_id": active_hqp_endpoint(),
                                  "gone": _gone()})


def _stream_queries(slots: list) -> dict:
    """TrackQuery per (track_id, album_id) slot that streams, built per
    album — the slot's album keeps the edition the listener pressed — and a
    slot whose album does not list the track (or that names none) gets the
    display edition. A slot no tracklist names is absent."""
    by_album: dict[Optional[str], list[str]] = {}
    for tid, album_id in slots:
        by_album.setdefault(album_id, []).append(tid)
    out: dict = {}
    loose: list = []
    for album_id, tids in by_album.items():
        own = ({q.track_id: q for q in _phantom_album_queries(album_id)}
               if album_id else {})
        for tid in tids:
            if tid in own:
                out[(tid, album_id)] = own[tid]
            else:
                loose.append((tid, album_id))
    if loose:
        found = _phantom_track_queries([tid for tid, _a in loose])
        for tid, album_id in loose:
            if tid in found:
                out[(tid, album_id)] = found[tid]
    return out


def _reach_segments(rows: list) -> tuple[list, list]:
    """THE split of slots by what opens here now, in the rows' order. A row
    with a file `id` (+ `location`) is an 'owned' segment's file row. One
    with none is its track's stream — a 'phantom' segment's TrackQuery, the
    row's own `query` or the one of its track on the album it was asked
    from (`track_id`, `album_id`): always for a track with no copy for this
    output at all (`why` None: the caller's _ensure_streaming_ready speaks
    for it), and for an owned track whose copy is not here now (`why`
    'disk' | 'away') only where this node streams (substitute.streams; no
    provider answering right now is the streaming refusal, as for any
    stream) — where it does not, the row is left out with reason `not_here`
    and its why, as is one no tracklist names (`nowhere`). Returns (segments, left); rows none of
    which makes a segment, every one left as not_here, are refused here
    (_not_here) — before the queue or the output is touched."""
    out = [r for r in rows if r.get("id") is None]
    streams = substitute.streams() if any(r.get("why") for r in out) else True
    queries = _stream_queries([(r["track_id"], r.get("album_id")) for r in out
                               if "query" not in r and (streams or not r.get("why"))])
    segments: list[tuple[str, list]] = []
    left: list = []
    his = False                 # a track of his own became its stream
    for r in rows:
        if r.get("id") is not None:
            _push_segment(segments, "owned", [r])
            continue
        away = bool(r.get("why")) and not streams
        q = None if away else (r["query"] if "query" in r
                               else queries.get((r["track_id"], r.get("album_id"))))
        if q is not None:
            _push_segment(segments, "phantom", [q])
            his = his or bool(r.get("why"))
        else:
            left.append({"track_id": r["track_id"], "title": r.get("title"),
                         "reason": "not_here" if away else "nowhere",
                         **({"why": r["why"], "album": r.get("album")} if away else {})})
    if not segments and left and all(t["reason"] == "not_here" for t in left):
        # named by the one album they are of, else — a single track — by its title
        albums = {t["album"] for t in left}
        name = albums.pop() if len(albums) == 1 else None
        raise _not_here(left[0]["why"], name or (left[0]["title"] if len(left) == 1 else None))
    if his:
        # as a not-owned track's tap: a node that streams, none of whose
        # providers answers right now, says that — in the words it has for it
        _ensure_streaming_ready()
    return segments, [{k: v for k, v in t.items() if k != "album"} for t in left]


def _push_segment(segments: list, kind: str, items: list) -> None:
    """Extend the run when the kind repeats — consecutive owned tracks belong in
    one append, consecutive phantoms in one resolve pass."""
    if not items:
        return
    if segments and segments[-1][0] == kind:
        segments[-1][1].extend(items)
    else:
        segments.append((kind, list(items)))


def _entity_segments(refs: list) -> tuple[list, list, list]:
    """Expand canonical refs into consecutive ('owned', [file rows]) /
    ('phantom', [TrackQuery]) segments, preserving the caller's order. An
    album is read per track: a file row where a copy opens here now, the
    track's stream where none does (_reach_segments). Returns (segments,
    ids that resolved to nothing playable, what of the owner's own was left
    out for not being here where nothing streams); refs none of which opens,
    all of them the owner's own, are refused (_not_here)."""
    hqp_id = active_hqp_endpoint()
    rows: list = []
    missing: list[str] = []
    for ref in refs:
        if ref.kind == "album":
            found = _album_rows(ref.id) or [{"query": q, "track_id": q.track_id}
                                            for q in _phantom_album_queries(ref.id)]
        else:
            # The track's best copy the ACTIVE output can open (local, or
            # held at the HQPlayer that is the output) where one opens now;
            # none here now, or none at all → a stream.
            row = _db_query_one(f"""
                SELECT CASE WHEN f.here THEN f.id END AS id, f.location::text AS location,
                       %(tid)s AS track_id, {_WHY.format(f='f')} AS why,
                       (SELECT t.title FROM tracks t WHERE t.id = %(tid)s::uuid) AS title
                FROM ({TRACK_FILES}) f
                WHERE f.location = 'local'
                   OR f.hqp_endpoint_id = %(hqp_id)s
                ORDER BY {owned_rank('f')}, (f.absent_since IS NOT NULL), {best_rip_order('f')}
                LIMIT 1
            """, {"tid": ref.id, "hqp_id": hqp_id, "gone": _gone()})
            if row:
                found = [row]
            else:
                q = _phantom_track_query(ref.id)
                found = [{"query": q, "track_id": ref.id}] if q else []
        if not found:
            missing.append(ref.id)
            continue
        rows.extend(found)
    segments, left = _reach_segments(rows)
    return segments, missing, left


def _segment_items(segments: list, left: Optional[list] = None, *,
                   fill: bool = False) -> tuple[list, list]:
    """One list of slots for ('owned', [file rows]) / ('phantom' | 'whole',
    [TrackQuery]) segments, in the caller's order — owned files as files,
    streamed tracks as the tracks themselves (their streams the lane's;
    'whole' ones never as an excerpt, radio's rule) — and
    what of the streamed ones the node already knows does not stream here,
    left out with why, after what the split already left (`left`).
    Committed in ONE mutation by the caller: nothing tapped meanwhile lands
    between two segments. `fill`: an automatic fill's slots (Play similar),
    the owned ones marked as such (_fills)."""
    items, left = [], list(left or [])
    for kind, payload in segments:
        if kind == "owned":
            owned = queue_mod.items_for_owned_rows(payload)
            items.extend(_fills(owned) if fill else owned)
        else:
            streamed, out = _streamed_items(payload, whole=kind == "whole")
            items.extend(streamed)
            left.extend(out)
    return items, left


def _play_segments(segments: list, *, archive: Callable[[], None], play: bool = True,
                   fill: bool = False) -> list:
    """Replace the queue with these segments, every slot at once (and start
    it, once the queue lock is let go). `archive`, the caller's session
    rotation, runs once the output has taken the queue, before the commit
    (the old queue is archived intact; a refusal leaves it be). Returns what
    it left out (_segment_items).

    `play=False` assembles the queue without starting it: "put together a
    playlist" is a curation act, not a play command, and the canonical queue is
    independent of the output, so it works with nothing attached. `fill`: an
    automatic fill's slots (_segment_items)."""
    items, left = _segment_items(segments, fill=fill)
    if not items:
        raise HTTPException(status_code=404, detail="Nothing in that list streams here")
    added, _gen = manager.replace_queue(items, play=play, archive=archive)
    if not added:
        raise HTTPException(
            status_code=503,
            detail="The playback output did not accept the track — try again.")
    _exit_radio_mode()
    return left


def _queue_segments(segments: list, position: str) -> list:
    """Append (or play next) these segments, every slot at once, in order.
    Returns what it left out (_segment_items)."""
    items, left = _segment_items(segments)
    if items:
        manager.append(items, "next" if position == "next" else "end")
    return left


@router.post("/queue-entities")
def queue_entities(req: QueueEntitiesRequest):
    """Append canonical entities — tracks and/or albums, owned or phantom — to
    the queue in the given order, every slot at once: a streamed one is the
    track itself, its stream brought by the fetch lane in the queue's order
    (playback.substitute)."""
    if not req.items:
        raise HTTPException(status_code=400, detail="No entities provided")
    bad = {i.kind for i in req.items} - {"track", "album"}
    if bad:
        raise HTTPException(status_code=400, detail=f"invalid kind: {sorted(bad)[0]}")

    segments, missing, gone = _entity_segments(req.items)
    if not segments:
        raise HTTPException(status_code=404, detail="Nothing playable in that list")
    owned = sum(len(p) for k, p in segments if k == "owned")
    phantom = sum(len(p) for k, p in segments if k == "phantom")
    if phantom:
        _ensure_streaming_ready()
    try:
        left = _queue_segments(segments, req.position)
    except HTTPException:
        raise
    except Exception as e:
        raise _output_error(e)
    return {"ok": True, "queued": owned, "streaming": phantom - len(left),
            "not_found": missing, "not_streamable": gone + left}


@router.post("/play-entities")
def play_entities(req: QueueEntitiesRequest):
    """Replace the queue with these entities, in order — the assistant's
    "Play all" against a list of tracks and/or albums.

    The old queue is archived to the session history first, exactly as the
    album screen's Play all does, so "make me a playlist of her albums" is a
    NEW queue rather than a tail on whatever was already there. With
    `autoplay=false` the queue is assembled and left waiting, which is what
    "put together a playlist" asks for — the same thing restoring a queue from
    the history does."""
    if not req.items:
        raise HTTPException(status_code=400, detail="No entities provided")
    bad = {i.kind for i in req.items} - {"track", "album"}
    if bad:
        raise HTTPException(status_code=400, detail=f"invalid kind: {sorted(bad)[0]}")

    segments, missing, gone = _entity_segments(req.items)
    if not segments:
        raise HTTPException(status_code=404, detail="Nothing playable in that list")
    owned = sum(len(p) for k, p in segments if k == "owned")
    phantom = sum(len(p) for k, p in segments if k == "phantom")
    if phantom:
        _ensure_streaming_ready()

    # One album is an album session (it gets the album's title in history);
    # anything else is a mix.
    album_ids = [i.id for i in req.items if i.kind == "album"]
    single_album = album_ids[0] if len(req.items) == 1 and album_ids else None
    head_kind, head_items = segments[0]

    def archive() -> None:
        sessions.rotate_session(
            manager.queue, "album" if single_album else "mix",
            origin_album_id=single_album,
            seed_media_file_id=(head_items[0]["id"] if head_kind == "owned"
                                and (head_items[0].get("location") or "local") == "local"
                                else None),
            seed_track_id=(None if head_kind == "owned" else head_items[0].track_id))

    try:
        left = _play_segments(segments, archive=archive, play=req.autoplay)
    except HTTPException:
        raise
    except Exception as e:
        raise _output_error(e)
    return {"ok": True, "playing": req.autoplay, "queued": owned,
            "streaming": phantom - len(left), "not_found": missing, "not_streamable": gone + left}


# -- Listening-history replay --------------------------------------------------
#
# A session snapshot is a list of queue SLOTS — (track_id, media_file_id,
# album_id) — not a list of catalog refs: an owned slot names the very file
# that played, a streamed slot the album it was queued from. Replaying rebuilds
# the queue from those slots here, so a snapshot that mixes owned files with
# streamed tracks comes back whole. (The client used to replay through
# play-tracks with the owned ids alone, which silently dropped every streamed
# slot and could not replay an all-streamed track session at all.)


def _session_segments(session_id: str) -> tuple[list, int]:
    """('owned', [media rows]) / ('phantom', [TrackQuery]) segments for a
    session snapshot, in slot order, plus the count of slots nothing can
    play. An owned slot plays the copy the ACTIVE output opens natively
    — held at the HQPlayer that is the output, else its own file, else the
    track's best rip (the file was removed, the track kept) — where one
    opens here now; a slot with no such copy streams like a phantom, or is
    left out where the owner's copy is not here and nothing streams
    (_reach_segments, which refuses a snapshot none of whose slots opens).
    Streamed queries are built per album — the slot's album_id keeps the
    edition the listener pressed — and a slot whose album no longer lists
    the track (or never had one) gets the display edition."""
    hqp_id = active_hqp_endpoint()
    rows = _db_query(f"""
        SELECT st.track_id::text AS track_id, st.album_id::text AS album_id,
               f.id, f.location::text AS location, t.title, al.title AS album,
               CASE WHEN f.id IS NOT NULL THEN NULL
                    WHEN EXISTS (SELECT 1 FROM media_files m3
                                 JOIN album_variants v3 ON v3.id = m3.album_variant_id
                                 WHERE m3.track_id = st.track_id AND v3.absent_since IS NULL)
                    THEN 'disk'
                    WHEN EXISTS (SELECT 1 FROM media_files m3 WHERE m3.track_id = st.track_id)
                    THEN 'away' END AS why
        FROM session_tracks st
        LEFT JOIN tracks t ON t.id = st.track_id
        LEFT JOIN albums al ON al.id = st.album_id
        LEFT JOIN LATERAL (
            SELECT f.id, f.location
            FROM (
                SELECT mf.id, av.location, av.hqp_endpoint_id, {HERE} AS here,
                       mf.is_lossless, mf.sample_rate, mf.bit_depth,
                       (mf.id = st.media_file_id) AS own
                FROM media_files mf JOIN album_variants av ON av.id = mf.album_variant_id
                WHERE mf.track_id = st.track_id
                UNION ALL
                SELECT hf.id, av.location, av.hqp_endpoint_id, true,
                       hf.is_lossless, hf.sample_rate, hf.bit_depth, false
                FROM hqp_library_files hf JOIN album_variants av ON av.id = hf.album_variant_id
                WHERE hf.track_id = st.track_id
            ) f
            WHERE f.here AND (f.location = 'local'
               OR f.hqp_endpoint_id = %(hqp_id)s)
            ORDER BY {owned_rank('f')}, f.own DESC, {best_rip_order('f')}
            LIMIT 1) f ON true
        WHERE st.session_id = %(id)s::uuid
        ORDER BY st.position
    """, {"id": session_id, "hqp_id": hqp_id, "gone": _gone()})
    segments, left = _reach_segments(rows)
    return segments, len(left)


@router.post("/play-session")
def play_session(req: PlaySessionRequest):
    """Replace the queue with a listening-history snapshot and play it — the
    session screen's Play. The listen keeps its archived origin (an album
    session replays as that album, a track session as that track), except
    radio: the snapshot is a static list now, not a station, so it is a mix."""
    header = _db_query_one("""
        SELECT origin, title, origin_album_id::text AS origin_album_id
        FROM listening_sessions
        WHERE id = %(id)s::uuid AND ended_at IS NOT NULL
    """, {"id": req.session_id})
    if header is None:
        raise HTTPException(status_code=404, detail="Session not found")

    segments, missing = _session_segments(req.session_id)
    if not segments:
        raise HTTPException(status_code=404, detail="Nothing in this session can be played any more")
    owned = sum(len(p) for k, p in segments if k == "owned")
    phantom = sum(len(p) for k, p in segments if k == "phantom")
    if phantom:
        _ensure_streaming_ready()

    head_kind, head_items = segments[0]
    origin = "mix" if header["origin"] == "radio" else header["origin"]

    def archive() -> None:
        sessions.rotate_session(
            manager.queue, origin,
            origin_album_id=header["origin_album_id"],
            seed_media_file_id=(head_items[0]["id"] if head_kind == "owned"
                                and (head_items[0].get("location") or "local") == "local"
                                else None),
            seed_track_id=(None if head_kind == "owned" else head_items[0].track_id),
            # A mix keeps the name it was archived under (a radio, replaying
            # as a static mix, keeps its seed's); album and track cards are
            # recomputed from their origin and need nothing carried.
            title=(header["title"] if origin == "mix" else None))

    try:
        left = _play_segments(segments, archive=archive)
    except HTTPException:
        raise
    except Exception as e:
        raise _output_error(e)
    return {"ok": True, "queued": owned, "streaming": phantom - len(left),
            "missing": missing + len(left)}


@router.post("/queue-session")
def queue_session(req: QueueSessionRequest):
    """Append a listening-history snapshot to the queue (at the end, or
    next), every slot at once — owned files and streamed tracks alike."""
    if req.position not in ("next", "end"):
        raise HTTPException(status_code=400, detail="position must be 'next' or 'end'")
    segments, missing = _session_segments(req.session_id)
    if not segments:
        raise HTTPException(status_code=404, detail="Nothing in this session can be played any more")
    owned = sum(len(p) for k, p in segments if k == "owned")
    phantom = sum(len(p) for k, p in segments if k == "phantom")
    if phantom:
        _ensure_streaming_ready()

    try:
        left = _queue_segments(segments, req.position)
    except HTTPException:
        raise
    except Exception as e:
        raise _output_error(e)
    return {"ok": True, "queued": owned, "streaming": phantom - len(left),
            "missing": missing + len(left)}


@router.post("/play-similar")
def play_similar(req: PlaySimilarRequest):
    """Replace the queue with tracks similar to the seed and play.

    The seed is source-agnostic: the two-tier scorer (the one radio and the
    Now Playing shelf drift on) takes a track UUID, so a streamed phantom
    seeds a station as well as an owned file does. The results are the
    owner's own files, mixed — while his switch "Radio plays tracks you don't
    own" is on (radio.streamed, since 2026-11-22) — with streamed tracks,
    each played whole, never as a 30 s excerpt (radio's rule)."""
    # a copy the ACTIVE output opens itself (a local file, or one held at the
    # HQPlayer that is the output) queues as owned; anything else streams —
    # radio's rule, its switch and never an excerpt: a static station. Owned
    # only, the scorer's top-N is of the owner's own (never what a mixed
    # top-N left of them)
    if not _db_query_one("SELECT 1 AS ok FROM embeddings WHERE track_id = %(t)s::uuid LIMIT 1",
                         {"t": req.track_id}):
        raise HTTPException(
            status_code=404,
            detail="No similar tracks found — the seed may not be analysed yet.")
    streams = _radio_streams()
    rows = track_similarity.similar_tracks(req.track_id, limit=req.limit,
                                           served=None if streams else True)
    native = [bool(r["is_owned"] and r["playable"]) for r in rows]
    queries = (_phantom_track_queries([r["track_id"] for r, n in zip(rows, native) if not n])
               if streams else {})
    segments: list[tuple[str, list]] = []
    for r, n in zip(rows, native):
        if n:
            _push_segment(segments, "owned", [{"id": r["file_id"], "location": r["location"]}])
        elif r["track_id"] in queries:
            _push_segment(segments, "whole", [queries[r["track_id"]]])
    if not segments:
        raise HTTPException(status_code=404, detail=(
            "No similar tracks found" if streams else _radio_owned_only_words("Play similar")))
    if any(k == "whole" for k, _p in segments):
        _ensure_streaming_ready()

    seed_mf = _db_query_one(f"""
        SELECT mf.id FROM media_files mf WHERE mf.track_id = %(tid)s::uuid
        ORDER BY {best_rip_order('mf')} LIMIT 1
    """, {"tid": req.track_id})
    try:
        left = _play_segments(segments, fill=True, archive=lambda: sessions.rotate_session(
            manager.queue, 'radio', seed_track_id=req.track_id,
            seed_media_file_id=(seed_mf or {}).get("id")))
    except HTTPException:
        raise
    except Exception as e:
        raise _output_error(e)

    # What was queued, and only that: a streamed row the switch leaves out,
    # or the node knows does not stream here, is no track of this station
    gone = {t["track_id"] for t in left}
    rows = [r for r, n in zip(rows, native)
            if n or (r["track_id"] in queries and r["track_id"] not in gone)]
    return {
        "ok": True,
        "count": len(rows),
        "not_streamable": left,
        "tracks": [
            {
                "track_id": r["track_id"],
                "id": r["media_file_id"],
                "title": r["title"],
                "artist": r["artist"],
                "album": r["album"],
                "is_owned": bool(r["is_owned"]),
                "similarity": float(r["similarity"]) if r["similarity"] is not None else None,
            }
            for r in rows
        ],
    }


@router.post("/play-tracks")
def play_tracks(req: PlayTracksRequest):
    """Play multiple tracks by file ids — each its file where that opens
    here now, else a copy that does, else its stream (_add_owned)."""
    if not req.track_ids:
        raise HTTPException(status_code=400, detail="No track IDs provided")
    if req.origin is not None and req.origin not in _SESSION_ORIGINS:
        raise HTTPException(status_code=400, detail=f"invalid origin: {req.origin}")

    rows = _file_rows(req.track_ids)
    if not rows:
        raise HTTPException(status_code=404, detail="No tracks found")

    try:
        left: list = []
        added = _add_owned(rows, clear_first=True, left=left, archive=lambda: sessions.rotate_session(
            manager.queue, req.origin or 'mix', origin_album_id=req.origin_album_id,
            seed_media_file_id=rows[0]["asked"]))
        wanted = len(rows) - len(left)
        if not wanted:
            raise HTTPException(status_code=404, detail="Nothing in that list streams here")
        _exit_radio_mode()
        if added < wanted:
            raise HTTPException(
                status_code=503,
                detail=f"HQPlayer added {added} of {wanted} tracks "
                       "(connection unstable). Try again.",
            )
        gone = {t["track_id"] for t in left}
        return {
            "ok": True,
            "count": wanted,
            "tracks": [
                {"id": r["asked"], "title": r["title"], "artist": r["artist"]}
                for r in rows if r["track_id"] not in gone
            ],
            "not_streamable": left,
        }
    except HTTPException:
        raise
    except NotHere as e:
        raise _output_error(e)          # an expected refusal, said in words: no error of the node's
    except Exception as e:
        logger.error(f"play-tracks failed: {e}")
        raise _output_error(e)


@router.post("/queue-tracks")
def queue_tracks(req: QueueTracksRequest):
    """Add the given tracks to the current queue, in order, without
    clearing — at the end, or right after the playing track ('next'). Used
    by the per-track "+" button and the "Queue album" action — these expect
    "add exactly these tracks", not the similarity-driven batches Radio Mode
    appends. The position is the server's to apply: the client appending and
    then reordering took three round trips and lost the block to any queue
    change in between. A file that does not open here now is queued as its
    track (_add_owned)."""
    if not req.track_ids:
        raise HTTPException(status_code=400, detail="No track IDs provided")
    if req.position not in ("next", "end"):
        raise HTTPException(status_code=400, detail="position must be 'next' or 'end'")

    rows = _file_rows(req.track_ids)
    if not rows:
        raise HTTPException(status_code=404, detail="No tracks found")

    try:
        left: list = []
        added = _add_owned(rows, clear_first=False, position=req.position, left=left)
        wanted = len(rows) - len(left)
        if added < wanted:
            raise HTTPException(
                status_code=503,
                detail=f"HQPlayer added {added} of {wanted} tracks "
                       "(connection unstable). Try again.",
            )
        gone = {t["track_id"] for t in left}
        return {
            "ok": True,
            "count": added,
            "tracks": [
                {"id": r["asked"], "title": r["title"], "artist": r["artist"], "album": r["album"]}
                for r in rows if r["track_id"] not in gone
            ],
            "not_streamable": left,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise _output_error(e)


# -- Radio mode (owned, and streamed when the owner's switch says so) --------
#
# Radio drifts: each batch is CLAP-similar to the CURRENTLY playing track. It
# is the owner's own tracks alone unless his switch "Radio plays tracks you
# don't own" (radio.streamed, off by default since 2026-11-22, B-121) is on:
# then a capped number of streamed tracks mixes in, each one played WHOLE — a
# streamed candidate qualifies only when its chain's head is full-length (a
# bring-your-own full provider, or the demo channel while the track's one
# listen is unspent: radio may spend it, past 90 % heard), and its slot never
# falls back to the 30 s excerpt (QueueItem.whole). Owned tracks take the
# places of candidates that do not qualify, so a batch keeps its length. The
# status observer refills near the end of the queue, so radio runs on; the
# fetch lane brings a batch's streams in the queue's order, LEAD slots ahead.

_RADIO_BATCH_SIZE = 10          # tracks per fill
_RADIO_MAX_PHANTOM = 4          # cap streamed tracks per batch — owned ones between
                                # them give the single-lane fetcher free buffer time,
                                # so a slow stream can't starve the playhead
_RADIO_PHANTOM_POOL = 12        # streamed candidates resolved per batch (of the
                                # 30): enough that four qualify where some stream
                                # only as an excerpt, one waterfall pass
_RADIO_PHANTOM_EVERY = 2        # place a phantom only after this many owned (spacing)
_RADIO_REFILL_AT = 3            # refill when <= this many tracks remain ahead
_RADIO_ARTIST_CAP = 2           # tracks per artist within one pool — raw KNN
                                # clusters hard (a Tangerine Dream seed put
                                # Edgar Froese in 10 of the top 30), and the
                                # artist-grain tag bonus lifts an artist's
                                # WHOLE catalog uniformly (45 Winehouse tracks
                                # would flood a Sade radio), so the cap is
                                # structural, not cosmetic
_RADIO_JITTER = 0.2             # ORDER BY score * (1 + JITTER*random()): reshuffles
                                # near-ties so the same seed never yields the same
                                # station twice, while distant pool members still
                                # rank behind close ones in sparse library corners
_radio_played: set = set()      # track UUIDs added this session — never repeat
_radio_refilling = False        # one fill thread at a time
_radio_last_artist: Optional[str] = None   # artist of the last queued track — the
                                # next batch avoids opening with the same artist
_radio_dry_seed: Optional[str] = None      # the seed whose fill gave nothing


def _fills(items: list) -> list:
    """`items`, the owner's own copies an automatic fill queues (a radio
    batch, Play similar), marked as nobody's tap and as his own copy
    (QueueItem.whole, own_fill): with
    its copy not here such a slot never streams behind his back — only
    while his switch lets radio stream, and then whole."""
    for it in items:
        it.whole = it.own_fill = True
    return items


def _radio_streams() -> bool:
    """Whether radio (and Play similar) mixes in tracks the owner does not
    own: his switch, while streaming is up — the streaming library on."""
    from routers.settings import _read as read_setting
    from streaming import service as streaming_service
    return streaming_service.is_enabled() and bool(read_setting("radio.streamed"))


def _radio_owned_only_words(what: str) -> str:
    """What radio's start (or Play similar) says when nothing of the owner's
    is like the seed and streamed tracks are not radio's (QUEUE-STREAM §7;
    drafts for the words sitting): it names the switch that would mix others
    in — the ones of the two that are off (Discover new, while the
    streaming library is off)."""
    from config import settings as app_settings
    from routers.settings import _read as read_setting
    from streaming import service as streaming_service
    head = f"{what} found nothing of yours like this track."
    if not getattr(app_settings, "streaming_preview_enabled", False):
        return head                 # the deployment streams nothing: no switch would help
    radio_off = not bool(read_setting("radio.streamed"))
    layer_off = not streaming_service.is_enabled() and not bool(
        read_setting("discovery.phantom_layer"))
    if layer_off and radio_off:
        return (f"{head} Turn on Discover new and “Radio plays tracks you don't own” "
                "in Streaming library to mix others in.")
    if layer_off:
        return f"{head} Turn on Discover new in Streaming library to mix others in."
    if radio_off and streaming_service.is_enabled():
        return (f"{head} Turn on “Radio plays tracks you don't own” in Streaming library "
                "to mix others in.")
    return head


def _plays_whole(chain: list) -> bool:
    """A streamed radio candidate's chain plays it whole: its head is a
    full-length link a provider RESOLVED — the demo channel's only while the
    track's one listen is unspent, which the waterfall already says by
    leaving it out. A lazy head (a provider that cannot pre-resolve, asked
    only at fetch) vouches for nothing: no place in a batch on a guess."""
    return bool(chain) and chain[0][1] is not None and not chain[0][0].manifest.excerpt


def _radio_similar(seed_uuid: str, exclude: set, limit: int, served: bool) -> list:
    """Audio-similar to the seed — the shared two-tier scorer
    (track_similarity.similar_tracks: mean-KNN recall pool, pure
    segment-chamfer rerank) with radio's artist cap and jitter — the rows the
    active output opens itself (`served`, the owner's own) or the ones that
    stream, each a top-N of its own. Rows carry what the batch builder
    needs: identity + file fields (owned) or the MB tracklist fields."""
    return track_similarity.similar_tracks(
        seed_uuid, exclude=exclude, limit=limit,
        artist_cap=_RADIO_ARTIST_CAP, jitter=_RADIO_JITTER, served=served)


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


def _radio_build_batch(seed_uuid: str, played: set,
                       prev_artist: Optional[str]) -> tuple[list, set]:
    """Pick a batch from the seed: the owner's own nearest tracks (a top 30
    of their own), and — with his switch on — the nearest streamed ones (a
    top _RADIO_PHANTOM_POOL of their own), resolved first (one waterfall
    pass — the lane's later resolve reads the cache), of which only those
    whose chain plays them whole qualify; up to _RADIO_MAX_PHANTOM of them
    are spaced out between owned tracks (which give the single fetch lane
    its time), owned ones filling the batch where few qualify. Returns the
    batch's slots in order — owned files as files, a streamed track as the
    track itself, its stream the lane's, never an excerpt — with the rows
    they came from; and the streamed candidates passed over, which the
    station does not ask about again. "Owned" means the ACTIVE output opens
    the copy itself (a local file, or one held at the HQPlayer that is the
    output); a track whose only copy sits at another HQPlayer streams."""
    owned = _radio_similar(seed_uuid, played, _RADIO_BATCH_SIZE * 3, served=True)
    phantom, queries, passed = [], {}, set()
    if _radio_streams():
        candidates = _radio_similar(seed_uuid, played, _RADIO_PHANTOM_POOL, served=False)
        queries = _phantom_track_queries([r["track_id"] for r in candidates])
        passed = {r["track_id"] for r in candidates if r["track_id"] not in queries}
        candidates = [r for r in candidates if r["track_id"] in queries]
        silent: set = set()
        chains = (_resolve_waterfall([queries[r["track_id"]] for r in candidates], silent)
                  if candidates else [])
        phantom = [r for r, ch in zip(candidates, chains) if _plays_whole(ch)]
        # passed over on a definite answer only: a provider silent this time
        # (a timeout, a quota) is asked again at the next batch
        passed |= {r["track_id"] for r in candidates} - {r["track_id"] for r in phantom} - silent
        phantom = phantom[:_RADIO_MAX_PHANTOM]
    for r in owned:
        r["is_owned"] = True
    for r in phantom:
        r["is_owned"] = False

    batch = []
    for r in _radio_interleave(owned, phantom, prev_artist):
        if r["is_owned"]:
            item = next(iter(_fills(queue_mod.items_for_owned_rows(
                [{"id": r["file_id"], "location": r["location"]}]))), None)
        else:
            item = queue_mod.items_for_streamed([queries[r["track_id"]]], whole=True)[0]
        if item is not None:
            batch.append((item, r))
    return batch, passed


def _radio_append_batch(batch: list, gen: int) -> None:
    """Append a batch WHOLE, in one mutation — no tap lands inside it — unless
    radio was turned off or another queue superseded this one (generation)."""
    global _radio_last_artist
    if not manager.radio_mode or not batch:
        return
    try:
        added = manager.append([item for item, _r in batch], generation=gen)
    except (Building, NotAnswering, Dropped) as e:
        # HQPlayer busy (a build, a silence, the output changing): the next
        # refill — the status tick near the queue's end — asks again
        logger.info("radio: a batch not added, the output is busy (%s)", e)
        return
    if not added:
        return
    for _item, r in batch:
        _radio_played.add(r["track_id"])
    _radio_last_artist = batch[-1][1]["artist"]


def _radio_fill(seed_uuid: str, gen: int, built: Optional[tuple] = None) -> None:
    """Pick + append one radio batch from the seed — or append the one radio's
    start built already. Background thread (the streamed candidates' resolve
    is slow); clears the one-at-a-time guard on exit. A seed that gives
    nothing is not asked again (_radio_dry_seed): the next track playing is
    the next seed — never a fill at every status tick."""
    global _radio_refilling, _radio_dry_seed
    try:
        if manager.radio_mode and manager.queue.generation == gen:
            batch, passed = built if built is not None else _radio_build_batch(
                seed_uuid, _radio_played, _radio_last_artist)
            # what this station learned, only while it is still the station
            # (a restart meanwhile has its own played set and no dry seed)
            if manager.queue.generation == gen:
                _radio_played.update(passed)
                if not batch:
                    _radio_dry_seed = seed_uuid
                    logger.info("radio: nothing more like %s — the next seed asks again",
                                seed_uuid)
            _radio_append_batch(batch, gen)
    except Exception:
        logger.exception("radio fill failed")
    finally:
        _radio_refilling = False


def _radio_refill_observer(new_data: dict, item) -> None:
    """Radio's refill: when the playhead nears the end of the radio queue,
    append another drifting batch (owned, and a few streamed ones when the
    owner's switch is on — read at each batch; seeded from the current
    track) so radio runs on. Registered as a
    manager status observer — runs on every status tick. Background —
    the phantom resolve+buffer is slow; one fill at a time. Not while
    another app holds the output (`source` external): nothing of the queue
    plays, the tick names at most the spot its takeover left."""
    global _radio_refilling
    if not manager.radio_mode or _radio_refilling or new_data.get("source") == "external":
        return
    idx = new_data.get("track_index")
    qlen = len(manager.queue)
    if isinstance(idx, int) and qlen > 0 and idx >= qlen - _RADIO_REFILL_AT:
        seed = item.track_id if item else None
        if seed and seed != _radio_dry_seed:
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


# One start at a time: a second start racing the first cleared the queue
# again and left two fill threads on one generation (double batch).
_radio_start_lock = threading.Lock()


@router.post("/radio/start")
def radio_start(req: RadioStartRequest):
    """Start drifting radio from a seed: keep the current track playing, clear what
    follows, and fill the queue in the background with a CLAP-similar batch —
    owned, and streamed ones played whole when the owner's switch is on. The
    poller refills near the end so it runs on. Async — the streamed
    candidates' resolve is slow; the toggle returns at once and the queue grows
    behind the seed a whole batch at a time, each streamed slot's stream the
    fetch lane's. Owned only, the first batch is built here (no provider is
    asked): nothing of the owner's like the seed is said at once, with the
    switch that would mix others in, before the queue is touched."""
    global _radio_played, _radio_refilling, _radio_last_artist, _radio_dry_seed

    if not _radio_start_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Radio is already starting.")
    try:
        return _radio_start(req)
    finally:
        _radio_start_lock.release()


def _radio_start(req: RadioStartRequest):
    global _radio_played, _radio_refilling, _radio_last_artist, _radio_dry_seed

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
        # "Play it a moment" is true only where a stream is analysed as it
        # plays: a node with no model waits for another node's analysis
        import hardware_profile
        raise HTTPException(
            status_code=409,
            detail=("This track isn't analysed yet — play it a moment, then start radio."
                    if hardware_profile.resolve().ml_available else
                    "This track has no sound analysis yet. This node analyses nothing itself: "
                    "radio starts once another Sautium node has shared its analysis."))

    first = None
    if not _radio_streams():
        first = _radio_build_batch(seed_uuid, {seed_uuid}, seed_row["artist"])
        if not first[0]:
            raise HTTPException(status_code=409, detail=_radio_owned_only_words("Radio"))

    # Clear everything around the reading slot — the seed plays on while the
    # batch flows in behind. New generation supersedes any prior filler. The
    # session rotates in the output's own clear, before it: one that cannot
    # be asked (HQPlayer building, the output lent or gone) refuses with
    # history intact. seed_uuid is resolved for BOTH an owned (req.track_id) and a
    # phantom (req.track_uuid) seed — the logical seed, so a radio started
    # from a streamed track gets a proper session card.
    try:
        gen = manager.clear_for_radio(
            lambda: sessions.rotate_session(manager.queue, 'radio', seed_track_id=seed_uuid,
                                            seed_media_file_id=req.track_id))
    except (BrokenPipeError, ConnectionError, OSError) as e:
        raise _output_error(e)

    _radio_played = {seed_uuid}
    _radio_last_artist = seed_row["artist"]
    _radio_dry_seed = None
    _radio_refilling = True                 # hold the refill until the first batch lands
    manager.set_radio_mode(True)            # flips the UI toggle via SSE at once

    threading.Thread(target=_radio_fill, args=(seed_uuid, gen, first),
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
