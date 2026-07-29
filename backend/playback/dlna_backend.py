"""
DLNA output backend (HARDWARE-TIERS §2.6): the renderer plays one URI at a
time, so this backend walks the canonical queue — SetAVTransportURI + Play
per track, SetNextAVTransportURI where the renderer supports it.

Status acquisition per §2.6: GENA event subscription for transport-state
changes, plus a 1 Hz GetPositionInfo poll ONLY while playing — RelTime is
not evented, a documented boundary exception to the no-polling rule.

Runs its own asyncio loop on a daemon thread ("dlna-loop", the p2p_manager
pattern); the synchronous PlayerBackend methods bridge in with
run_coroutine_threadsafe. Media is served by the plain-http MediaProxy:
owned files via disk-backed /file/{token}, phantom streams via their
existing /preview/{token} buffers — both on a LAN-reachable advertised
host (renderers can't fetch from 127.0.0.1).
"""

import asyncio
import logging
import threading
import time
from datetime import timedelta
from typing import Optional
from urllib.parse import urlparse

from config import settings

from playback.base import Capabilities, PlaybackStatus, PlayerBackend
from playback.queue import CanonicalQueue, QueueItem

logger = logging.getLogger(__name__)

try:
    from async_upnp_client.aiohttp import AiohttpNotifyServer, AiohttpRequester
    from async_upnp_client.client_factory import UpnpFactory
    from async_upnp_client.profiles.dlna import DmrDevice, TransportState
    HAS_UPNP = True
except ImportError:
    HAS_UPNP = False
    logger.warning("async-upnp-client not installed — DLNA output disabled")

_CMD_TIMEOUT = 10.0
_TRACK_END_SLACK = 3.0      # STOPPED within this many seconds of the length = track finished


class DlnaAttachError(RuntimeError):
    """Attach failure whose message is already user-ready — start() re-raises
    it verbatim instead of wrapping it in the generic dozing-renderer hint."""

# The dlna-loop and the GENA notify server are PROCESS singletons: the
# notify port is a one-per-process resource, and rebinding it on every
# renderer switch proved fragile — any shutdown that failed to release it
# (a dozing phone holding half-open NOTIFY connections past the cleanup
# budget) bricked every later DLNA attach until a backend restart.
# Backends come and go; the loop and the server stay.
_shared_lock = threading.Lock()
_shared_loop: Optional[asyncio.AbstractEventLoop] = None
_shared_notify = None
_shared_notify_host: Optional[str] = None   # address its callback URL names


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _shared_loop
    with _shared_lock:
        if _shared_loop is None or _shared_loop.is_closed():
            loop = asyncio.new_event_loop()

            def _run():
                asyncio.set_event_loop(loop)
                loop.run_forever()

            threading.Thread(target=_run, daemon=True, name="dlna-loop").start()
            _shared_loop = loop
        return _shared_loop


async def _ensure_notify(requester, peer: Optional[str] = None):
    """The GENA callback server, rebuilt when the address it advertises stops
    being right. It is shared per process but its callback URL is not
    universal: a renderer reached over a tunnel must be told our tunnel
    address, and one on the LAN our LAN address, so switching between them
    means the server has to be re-announced."""
    global _shared_notify, _shared_notify_host
    host = media_host(peer)
    if _shared_notify is not None and _shared_notify_host != host:
        try:
            await _shared_notify.async_stop_server()
        except Exception as e:
            logger.debug("notify server stop failed: %s", e)
        _shared_notify = None
    if _shared_notify is None:
        port = settings.dlna_gena_port
        server = AiohttpNotifyServer(
            requester, source=("0.0.0.0", port),
            callback_url=f"http://{host}:{port}/notify")
        try:
            await server.async_start_server()
        except OSError as e:
            # UpnpServerOSError str()s to "None" (library bug) — translate
            # to the real story: the port is taken, most plausibly by another
            # Sautium node whose media ports weren't kept distinct.
            raise DlnaAttachError(
                f"DLNA event port {port} is already in use by another "
                "process on this host (another Sautium node?) — free it "
                "or change the port, then retry") from e
        _shared_notify = server
        _shared_notify_host = host
    return _shared_notify

_MIME_BY_FORMAT = {
    "FLAC": "audio/flac", "MP3": "audio/mpeg", "WAV": "audio/wav",
    "OGG": "audio/ogg", "M4A": "audio/mp4", "AIFF": "audio/aiff",
}


def renderer_reachable(location: str, timeout: float = 2.0) -> bool:
    """Cheap TCP-connect liveness check on a renderer's description host:port.
    A powered-off renderer fails this in ~timeout s, so the play-intent gate
    can report it offline fast instead of eating stacked SOAP/attach
    timeouts. A dozing-but-networked renderer still accepts the connect and
    wakes on the SOAP that follows."""
    import socket
    try:
        u = urlparse(location)
        host, port = u.hostname, u.port or 80
        if not host:
            return False
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _same_network(a: str, b: str) -> bool:
    """Would a device at `b` reach a server at `a` without leaving its own
    network? CGNAT is treated whole because a tailnet hands out /32s from
    100.64/10 with no subnet structure — two peers there reach each other
    regardless of how far apart the addresses look."""
    import ipaddress
    try:
        ia, ib = ipaddress.ip_address(a), ipaddress.ip_address(b)
    except ValueError:
        return False
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    if ia in cgnat or ib in cgnat:
        return ia in cgnat and ib in cgnat
    return (ipaddress.ip_network(f"{a}/24", strict=False)
            == ipaddress.ip_network(f"{b}/24", strict=False))


def _source_address_toward(peer: str) -> Optional[str]:
    """Which of our addresses the kernel would speak from to reach `peer`.

    Costs nothing and sends nothing — connecting a UDP socket only resolves a
    route. This is the authoritative answer wherever the process runs on the
    real host: it needs no configuration and gets tunnels right for free,
    because the route to a tailnet peer leaves by the tailnet interface and
    the kernel says so. It is exactly wrong inside a container, where every
    route ends at the bridge; the caller checks for that."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((peer, 9))
            return s.getsockname()[0]
    except OSError:
        return None


def media_host(peer: Optional[str] = None) -> str:
    """The address to hand a renderer so it can pull media back from us.

    A host has several addresses and which one is correct depends entirely on
    who is asking: a renderer on the LAN must be told the LAN address, one
    joined over a tunnel must be told the tunnel address, and giving either
    the other's is a request that goes nowhere. So `peer` — the renderer's own
    address — picks among our candidates by which network it shares.

    Inside Docker none of this is discoverable: every socket reports the
    bridge address, and what the outside world sees is a NAT the container
    cannot look through. The candidates therefore have to be told to us, via
    MEDIA_PROXY_ADVERTISED_HOST and SAUTIUM_HOST_IPS."""
    import os
    from tls_gen import detect_private_host_ips
    configured = settings.media_proxy_advertised_host
    candidates = [ip for ip in
                  ([configured] if configured and configured != "127.0.0.1" else [])
                  + [s.strip() for s in os.getenv("SAUTIUM_HOST_IPS", "").split(",")]
                  if ip]
    if peer:
        match = next((ip for ip in candidates if _same_network(ip, peer)), None)
        if match:
            return match
        routed = _source_address_toward(peer)
        # Accept the kernel's answer only if it lands on the renderer's own
        # network. That is the whole requirement, and it doubles as the
        # container check for free: a bridge address shares a network with
        # nothing outside, so inside Docker this rejects itself and the answer
        # falls back to configuration, where it belongs.
        if routed and _same_network(routed, peer):
            return routed
        logger.warning(
            "no local address on %s's network — media URLs will point at %s "
            "and the renderer will not reach them; add its address to "
            "SAUTIUM_HOST_IPS", peer, candidates[0] if candidates else configured)
    if candidates:
        return candidates[0]
    ips = detect_private_host_ips()
    lan = [ip for ip in ips if not ip.startswith("172.")]
    if lan or ips:
        return (lan or ips)[0]
    return configured


class DlnaBackend(PlayerBackend):
    id = "dlna"

    def __init__(self, emit, queue: CanonicalQueue, renderer: dict):
        super().__init__(emit)
        if not HAS_UPNP:
            raise RuntimeError("async-upnp-client not installed")
        self._queue = queue
        self._renderer = renderer                # {udn, location, name}
        # The renderer's own address decides which of our addresses it can
        # reach us at — see media_host().
        self._peer_host = urlparse(renderer.get("location") or "").hostname
        self.label = renderer.get("name") or "DLNA renderer"
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # dlna-loop-owned state
        self._dmr: Optional["DmrDevice"] = None
        self._gone = False       # device believed off the network (see healthy)
        self._index = 0                          # 1-based canonical slot
        self._current_url: Optional[str] = None
        self._next_url: Optional[str] = None     # set via SetNextAVTransportURI
        self._position = 0.0
        # Wall-clock position estimate for renderers that don't report RelTime
        # (KANN on VBR Opus): position at the last baseline + elapsed since.
        self._wall_base = 0.0
        self._wall_t0 = 0.0
        # Server-side seek offset: the current stream may be a track
        # re-encoded from N seconds (Opus seek), so a renderer's RelTime is
        # 0-based on that stream — the real track position is offset + rel.
        self._pos_offset = 0.0
        self._length = 0.0
        self._poll_task: Optional[asyncio.Task] = None
        self._error: Optional[str] = None
        # Track-change SOAP sequence in flight → status reports `loading`
        # (the UI shows a transition spinner on the tapped track).
        self._loading = False
        self._load_lock = asyncio.Lock()
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._loop = _ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(self._async_start(), self._loop)
        try:
            fut.result(timeout=25)
        except DlnaAttachError as e:
            logger.error("DLNA attach failed: %s", e)
            raise
        except Exception as e:
            reason = str(e).strip() or type(e).__name__
            if len(reason) > 80:      # raw aiohttp reprs are debug noise
                reason = reason[:77] + "…"
            msg = (f"'{self.label}' did not respond ({reason}) — "
                   "wake the device (phone renderers doze) and try again")
            logger.error("DLNA attach failed: %s", msg)
            raise RuntimeError(msg)
        logger.info("DLNA backend attached to %s", self.label)

    def shutdown(self) -> None:
        self._closed = True   # stop emitting / polling before cleanup runs
        if self._loop is not None:
            # Unsubscribe only — the shared loop and notify server outlive
            # this backend by design (see the singleton note above).
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self._async_shutdown(), self._loop)
                fut.result(timeout=8)
            except Exception as e:
                logger.warning("DLNA shutdown cleanup incomplete: %s",
                               str(e).strip() or type(e).__name__)
        logger.info("DLNA backend detached from %s", self.label)

    def capabilities(self) -> Capabilities:
        return Capabilities(volume=True, volume_kind="percent", seek=True,
                            gapless=False)

    def healthy(self) -> bool:
        """Battery DAPs and phones drop off the network in deep sleep —
        once that's detected (poll misses, connection-level command
        failures) the next play intent must re-attach, not trust this
        instance."""
        return self._dmr is not None and not self._gone

    def reachable(self) -> bool:
        return renderer_reachable(self._renderer.get("location", ""))

    def resume_at(self, index: int) -> None:
        if self._queue.item_at(index) is None:
            return
        self._index = index
        self._current_url = None     # play() loads this slot fresh
        self._emit_now("stopped")

    async def _async_start(self) -> None:
        # 10s over the default 5s: phone renderers in Wi-Fi power-save can
        # stall the first request for seconds while the radio wakes up.
        requester = AiohttpRequester(timeout=10)
        notify_server = await _ensure_notify(requester, self._peer_host)
        factory = UpnpFactory(requester, non_strict=True)
        device = await factory.async_create_device(self._renderer["location"])

        self._dmr = DmrDevice(device, notify_server.event_handler)
        self._dmr.on_event = self._on_gena_event
        try:
            await self._dmr.async_subscribe_services(auto_resubscribe=True)
        except Exception as e:
            # Some renderers/firewalls reject eventing — degrade to the
            # position poll (it already carries transport state).
            logger.warning("GENA subscribe failed (%s) — poll-only mode", e)
        await self._dmr.async_update()
        self._emit_now("stopped")

    async def _async_shutdown(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        try:
            if self._dmr:
                # Unsubscribe is HTTP to the renderer — a powered-off or
                # dozing device would hang it far past the shutdown budget.
                await asyncio.wait_for(self._dmr.async_unsubscribe_services(), 3)
        except Exception as e:
            logger.debug("GENA unsubscribe: %s", e)

    # -- status ---------------------------------------------------------------

    def _state_name(self) -> str:
        st = self._dmr.transport_state if self._dmr else None
        if st in (TransportState.PLAYING, TransportState.TRANSITIONING):
            return "playing"
        if st == TransportState.PAUSED_PLAYBACK:
            return "paused"
        return "stopped"

    def _emit_now(self, state: Optional[str] = None) -> None:
        if self._closed:
            # A detached backend must never touch the manager's status —
            # its GENA subscription rides the SHARED notify server and can
            # deliver one more event after unsubscribe; without this guard
            # the zombie and the live backend flap the status (observed:
            # track_index oscillating between two slots, even paused).
            return
        if state is None and self._loading:
            # Poll/GENA ticks that land mid-track-change would report the
            # OLD track's transport state against the NEW index.
            state = "loading"
        extra = {}
        if self._error:
            extra["error"] = self._error
        vol = None
        if self._dmr is not None and self._dmr.volume_level is not None:
            vol = round(self._dmr.volume_level * 100.0, 1)
        self._emit(PlaybackStatus(
            state=state or self._state_name(),
            position=self._position,
            length=self._length,
            queue_index=self._index,
            volume=vol,
            extra=extra,
        ))

    def _on_gena_event(self, service, state_variables) -> None:
        """Transport-state changes arrive here (renderer's own remote
        included). Runs on the dlna-loop."""
        asyncio.ensure_future(self._handle_state_change(), loop=self._loop)

    async def _handle_state_change(self) -> None:
        if self._closed:
            return
        if self._loading:
            # A manual track change owns the transport: its own Stop and
            # URI flapping must not be read as track-end or auto-advance,
            # and its `loading` status must not be overwritten.
            return
        state = self._state_name()
        if state == "playing":
            self._ensure_poll()
            # Renderer may have auto-advanced to the SetNext URI on its own.
            cur = getattr(self._dmr, "current_track_uri", None)
            if cur and self._next_url and cur == self._next_url:
                self._advanced_to_next()
        elif state == "stopped":
            self._stop_poll()
            finished = (self._length > 0
                        and self._position >= self._length - _TRACK_END_SLACK)
            if finished:
                await self._advance()
                return
        self._emit_now(state)

    def _rebaseline(self, pos: float) -> None:
        """Anchor the wall-clock position estimate at `pos` now — called
        whenever the true position is known (track start, resume, seek, or a
        real RelTime reading)."""
        self._position = pos
        self._wall_base = pos
        self._wall_t0 = time.monotonic()

    def _ensure_poll(self) -> None:
        if self._closed:
            return
        # The track-end advance runs INSIDE the exiting poll task: a naive
        # aliveness check sees "the poll" still running (it is the caller,
        # one `return` from death) and skips the restart — every track that
        # started through that path played deaf (position frozen at 0, no
        # end-of-track backstop; radio died on the next boundary).
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if (self._poll_task is None or self._poll_task.done()
                or self._poll_task is current):
            self._poll_task = asyncio.ensure_future(self._poll_position(),
                                                    loop=self._loop)

    def _stop_poll(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()

    async def _poll_position(self) -> None:
        """1 Hz GetPositionInfo while playing — RelTime is not evented
        (documented boundary exception, §2.6). Also our track-end detector:
        some renderers under-report the final GENA STOPPED."""
        misses = 0
        first_pos_logged = False
        logger.info("position poll started (slot %d)", self._index)
        try:
            while True:
                await asyncio.sleep(1.0)
                if self._dmr is None or self._closed:
                    return
                if self._loading:
                    continue   # the load sequence owns the status right now
                try:
                    await self._dmr.async_update()
                    misses = 0
                except Exception as e:
                    logger.debug("position poll update failed: %s", e)
                    misses += 1
                    if misses >= 3:
                        # The renderer left the network (battery DAPs drop
                        # Wi-Fi in deep sleep while playing from buffer) —
                        # a frozen "playing" status would just gaslight the
                        # user. Report the loss and stop pretending.
                        self._gone = True
                        self._error = (f"'{self.label}' stopped responding — "
                                       "it may have left the network "
                                       "(deep sleep / Wi-Fi off)")
                        logger.warning("DLNA renderer unreachable after "
                                       "%d poll misses — reporting stopped",
                                       misses)
                        self._emit_now("stopped")
                        return
                    continue
                dur = self._dmr.media_duration
                if dur:
                    self._length = float(dur)
                state = self._state_name()
                # Position: trust the renderer's RelTime when it advances, but
                # some renderers (KANN on VBR Opus, live) play fine yet report
                # RelTime 00:00:00 forever. So keep a wall-clock estimate and
                # snap to RelTime whenever it gives a real value — FLAC and
                # software renderers self-correct every tick; the frozen ones
                # ride the clock instead of a stuck slider.
                rel = self._dmr.media_position
                now = time.monotonic()
                if rel is not None and rel > 0.5:
                    # RelTime is 0-based on the (possibly offset) stream.
                    self._rebaseline(self._pos_offset + float(rel))
                elif state == "playing":
                    est = self._wall_base + (now - self._wall_t0)
                    self._position = min(est, self._length) if self._length else est
                if state == "stopped":
                    finished = (self._length > 0 and
                                self._position >= self._length - _TRACK_END_SLACK)
                    logger.info("position poll exit: device stopped (pos=%.1f/%.1f finished=%s)",
                                self._position, self._length, finished)
                    if finished:
                        await self._advance()
                    else:
                        self._emit_now("stopped")
                    return
                cur = getattr(self._dmr, "current_track_uri", None)
                if cur and self._next_url and cur == self._next_url:
                    self._advanced_to_next()
                self._emit_now(state)
        except asyncio.CancelledError:
            logger.info("position poll exit: cancelled")

    # -- queue walking -----------------------------------------------------------

    def _url_for(self, item: QueueItem) -> Optional[str]:
        from streaming import service as streaming_service
        src = item.source
        host = media_host(self._peer_host)
        # Snapshot the global quality setting into the URL — the proxy
        # transcodes to Opus for the remote/bandwidth tiers. HQPlayer's own
        # _url_for equivalent never appends this, so its previews (same
        # /preview route) stay lossless.
        qs = self._quality_suffix()
        if src["kind"] == "file":
            proxy = streaming_service.ensure_proxy()
            path = settings.translate_to_local_path(src["path"])
            mime = _MIME_BY_FORMAT.get((src.get("format") or "").upper(),
                                       "application/octet-stream")
            tok = proxy.register_file(path, mime)
            return f"http://{host}:{proxy.port}/file/{tok}{qs}"
        if src["kind"] == "proxy":
            proxy = streaming_service.get_proxy()
            if proxy is None:
                return None
            return f"http://{host}:{proxy.port}/preview/{src['token']}{qs}"
        return None   # foreign URIs (file:// passthrough) aren't renderer-reachable

    @staticmethod
    def _quality_suffix() -> str:
        from routers.settings import _read
        q = _read("output.stream_quality")
        return f"?q={q}" if q and q != "lossless" else ""

    @staticmethod
    def _didl_meta(item: QueueItem) -> dict:
        """Structured DIDL-Lite fields — renderers show artist/album in their
        own UI (a bare title left them as "unknown artist/album")."""
        meta = {}
        if item.artist:
            # Both spellings: renderers disagree on which one feeds the
            # "artist" line (upnp:artist per spec, dc:creator in practice).
            meta["artist"] = item.artist
            meta["creator"] = item.artist
        if item.album:
            meta["album"] = item.album
        if item.track_number:
            meta["original_track_number"] = item.track_number
        return meta

    @staticmethod
    def _load_cover_jpeg(cover_id: str) -> Optional[bytes]:
        """Cover bytes from the covers table, converted webp→JPEG — hi-fi
        renderers rarely decode webp. Runs in a worker thread (blocking DB +
        PIL)."""
        from db_pool import db_query_one
        row = db_query_one("SELECT data FROM covers WHERE id = %(id)s::uuid",
                           {"id": cover_id})
        if not row or not row["data"]:
            return None
        try:
            import io
            from PIL import Image
            img = Image.open(io.BytesIO(bytes(row["data"]))).convert("RGB")
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=85)
            return out.getvalue()
        except Exception as e:
            logger.debug("cover convert failed for %s: %s", cover_id, e)
            return None

    async def _art_url(self, item: QueueItem) -> Optional[str]:
        """Renderer-fetchable cover URL: owned art is re-served as JPEG on
        the plain-http proxy (/art/{token}); phantom art passes through its
        external provider/CAA URL."""
        if item.cover_id:
            from streaming import service as streaming_service
            proxy = streaming_service.ensure_proxy()
            tok = proxy.blob_token_for(item.cover_id)
            if tok is None:
                data = await asyncio.to_thread(self._load_cover_jpeg, item.cover_id)
                if data is None:
                    return None
                tok = proxy.register_blob(item.cover_id, data, "image/jpeg")
            return f"http://{media_host(self._peer_host)}:{proxy.port}/art/{tok}"
        if item.preview:
            from playback.queue import resolved_artwork
            return resolved_artwork.get(item.track_id) or item.cover_url
        return None

    async def _transport_meta(self, url: str, item: QueueItem) -> str:
        """DIDL with the class FORCED to musicTrack: the library derives the
        class from the mime type and lands on plain audioItem, whose didl
        type silently drops artist/album/track-number on serialization
        (verified by reading CurrentURIMetaData back off the renderer).
        albumArtURI is injected manually — this didl_lite version has no such
        property on MusicTrack at all."""
        xml = await self._dmr.construct_play_media_metadata(
            url, item.title or "Sautium",
            override_upnp_class="object.item.audioItem.musicTrack",
            meta_data=self._didl_meta(item))
        # Declare the track duration on <res>. Lossless (FLAC/PCM) renderers
        # infer elapsed time from byte offset × a fixed rate, so they track
        # RelTime without it — but VBR Opus has no such mapping, and a
        # renderer with no res@duration then plays fine yet reports RelTime
        # 00:00:00 forever (KANN, live). Standard DLNA metadata anyway.
        if item.duration_seconds:
            total = int(item.duration_seconds)
            dur = f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}.000"
            xml = xml.replace("<res ", f'<res duration="{dur}" ', 1)
        art = await self._art_url(item)
        if art:
            from xml.sax.saxutils import escape
            xml = xml.replace(
                "</item>",
                f"<upnp:albumArtURI>{escape(art)}</upnp:albumArtURI></item>", 1)
        return xml

    async def _load_and_play(self, index: int, *, play: bool = True,
                             ss: float = 0.0,
                             url_override: Optional[str] = None) -> bool:
        # Serialized: fire-and-forget track changes must not interleave
        # their SOAP sequences. The lock lives OUTSIDE the recursion in
        # _load_seq (asyncio.Lock is not reentrant). `ss` (seconds) is a
        # server-side seek — the track re-encodes from the offset;
        # `url_override` reuses the currently-playing stream's exact URL
        # (a seek must keep the format the track was dispatched in).
        async with self._load_lock:
            self._loading = True
            try:
                return await self._load_seq(index, play=play, ss=ss,
                                            url_override=url_override)
            finally:
                self._loading = False

    async def _load_seq(self, index: int, *, play: bool = True, ss: float = 0.0,
                        url_override: Optional[str] = None) -> bool:
        item = self._queue.item_at(index)
        if item is None:
            self._emit_now("stopped")
            return False
        # url_override (a seek) carries the currently-playing stream's URL,
        # already including ?ss — keeping the format the track was dispatched
        # in even if the global quality has since changed.
        url = url_override or self._url_for(item)
        if url is None:
            logger.warning("DLNA: skipping unreachable item %s — %s",
                           item.artist, item.title)
            return await self._load_seq(index + 1, play=play)
        if ss > 0 and not url_override:
            # Server-side seek: re-encode the track from the offset (the
            # media route honors ?ss). Only Opus URLs (which carry ?q) take
            # this path, so the "&" join is always right.
            url += ("&" if "?" in url else "?") + f"ss={int(ss)}"
        # A track change is seconds of SOAP + renderer buffering: adopt the
        # new slot and report `loading` FIRST, so the UI shows the tapped
        # track with a transition spinner instead of either lying
        # ("playing") or looking dead while the old audio rides on.
        self._index = index
        self._pos_offset = ss   # 0 for a fresh track; the seek offset otherwise
        self._position = ss     # so the loading state shows the seek target
        self._length = item.duration_seconds or 0.0
        if play:
            self._emit_now("loading")
        # Disarm the gapless auto-advance detector: a manual jump to the
        # very track that SetNext armed makes CurrentURI == _next_url and
        # looked exactly like an auto-advance — the index bumped one PAST
        # the tapped track (observed live on BubbleUPnP).
        self._next_url = None
        if play and self._state_name() in ("playing", "paused"):
            # Renderers with gapless (BubbleUPnP) do NOT interrupt current
            # playback on SetAVTransportURI while PLAYING: the display
            # adopts the new track, the audio keeps the old one, and Play
            # is a no-op. An explicit Stop makes the switch real.
            try:
                await self._avt("Stop")
            except Exception as e:
                logger.debug("pre-load stop: %s", e)
        # Direct AVT actions, never the DmrDevice helpers: they gate on the
        # renderer's CurrentTransportActions report and silently NO-OP when
        # an action is missing from it (the Phase-2 Pause lesson). A gated
        # SetAVTransportURI/Play here meant next/prev committed the new
        # index while the renderer kept playing the old track.
        await self._avt("SetAVTransportURI", CurrentURI=url,
                        CurrentURIMetaData=await self._transport_meta(url, item))
        self._current_url = url
        if play:
            await self._avt("Play", Speed="1")
            self._rebaseline(ss)      # clock starts at the offset (0 for a fresh track)
            self._ensure_poll()
        await self._preload_next()
        # Explicit state: _loading is still set here, and the default
        # resolution would keep reporting "loading" after the work is done.
        self._emit_now("playing" if play else self._state_name())
        return True

    async def _preload_next(self) -> None:
        """SetNextAVTransportURI where supported — the renderer transitions
        gaplessly on its own; we detect the URI change and follow."""
        self._next_url = None
        nxt = self._queue.item_at(self._index + 1)
        url = self._url_for(nxt) if nxt is not None else None
        if url is None:
            # DISARM, don't just skip: the renderer still holds the last
            # armed NextURI — e.g. a track the user just REMOVED from the
            # queue (its proxy buffer outlives the removal) — and would
            # gapless-transition into it at track end, playing deleted
            # content while the indication stays frozen (our detector no
            # longer knows that URL). Observed live: a de-duplicated
            # streamed album "restarting from the top".
            try:
                await self._avt("SetNextAVTransportURI",
                                NextURI="", NextURIMetaData="")
            except Exception as e:
                logger.debug("SetNext disarm unsupported/failed: %s", e)
            return
        try:
            await self._avt("SetNextAVTransportURI", NextURI=url,
                            NextURIMetaData=await self._transport_meta(url, nxt))
            self._next_url = url
        except Exception as e:
            logger.debug("SetNextAVTransportURI unsupported/failed: %s", e)

    def _advanced_to_next(self) -> None:
        """Renderer moved to the preloaded next URI by itself."""
        self._index += 1
        self._current_url = self._next_url
        self._next_url = None
        item = self._queue.item_at(self._index)
        self._length = (item.duration_seconds or 0.0) if item else 0.0
        self._pos_offset = 0.0    # a gapless auto-advance is always a fresh track
        self._rebaseline(0.0)     # gapless auto-advance → fresh track clock
        # The poll exits on any STOPPED observation (including the brief
        # blip some renderers emit at a gapless boundary) — a track that
        # starts through THIS path must restart it, or the backend goes
        # deaf: position frozen at 0, and the poll-side track-end backstop
        # (which caught radio's queue walking when GENA went quiet) never
        # fires again. Observed live: radio died at the end of the first
        # auto-advanced track.
        self._ensure_poll()
        asyncio.ensure_future(self._preload_next(), loop=self._loop)
        self._emit_now("playing")

    async def _advance(self) -> None:
        """Track finished (STOPPED near the end) → play the next queue slot."""
        if self._queue.item_at(self._index + 1) is not None:
            await self._load_and_play(self._index + 1)
        else:
            self._emit_now("stopped")

    # -- transport (uvicorn threads → dlna-loop) ----------------------------------

    async def _avt(self, name: str, **kwargs):
        """Invoke an AVTransport action DIRECTLY. DmrDevice's transport
        helpers gate on the renderer's CurrentTransportActions report and
        silently no-op when an action is missing from it — the AK Connect
        renderer omits "Pause" there while executing the action perfectly,
        so the report can't be trusted."""
        action = self._dmr._action("AVT", name)
        if action is None:
            raise RuntimeError(f"renderer lacks AVTransport/{name}")
        return await action.async_call(InstanceID=0, **kwargs)

    def _cmd_failed(self, e: Exception) -> None:
        if isinstance(e, (TimeoutError, OSError)):
            # Connection-level failure (hang / refused / unreachable) —
            # the doze signature. Mark the instance so the next play
            # intent re-attaches instead of hammering a ghost.
            self._gone = True
        reason = str(e).strip() or type(e).__name__
        # Surface it: a silently swallowed command is how "next" looked
        # like it worked while the renderer kept playing the old track.
        self._error = (f"'{self.label}' command failed ({reason}) — "
                       "the renderer may be asleep")
        logger.warning("DLNA command failed: %s", reason)
        self._emit_now()

    def _call(self, coro) -> bool:
        if self._loop is None:
            return False
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            fut.result(timeout=_CMD_TIMEOUT)
            if self._error:
                self._error = None   # a command went through — device is back
            return True
        except Exception as e:
            self._cmd_failed(e)
            return False

    def _call_async(self, coro) -> bool:
        """Track changes: accept immediately and run on the loop — a DLNA
        load is seconds of SOAP + renderer buffering, and blocking the API
        request on it made every tap feel dead. Progress reaches the UI as
        the `loading` state; failures land in the status error field."""
        if self._loop is None:
            return False
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def _done(f):
            try:
                f.result()
                if self._error:
                    self._error = None
            except Exception as e:
                self._cmd_failed(e)
        fut.add_done_callback(_done)
        return True

    def play(self) -> bool:
        async def _p():
            if self._current_url is None:
                await self._load_and_play(self._index if self._index >= 1 else 1)
            else:
                await self._avt("Play", Speed="1")
                self._rebaseline(self._position)   # resume from where we are
                self._ensure_poll()
                self._emit_now("playing")
        return self._call_async(_p())

    def pause(self) -> bool:
        async def _p():
            await self._avt("Pause")
            self._emit_now("paused")
        return self._call(_p())

    def stop(self) -> bool:
        async def _s():
            self._stop_poll()
            await self._avt("Stop")
            self._emit_now("stopped")
        return self._call(_s())

    def next(self) -> bool:
        return self._call_async(self._load_and_play(self._index + 1))

    def previous(self) -> bool:
        return self._call_async(self._load_and_play(max(1, self._index - 1)))

    def select(self, index: int) -> bool:
        if self._queue.item_at(index) is None:
            return False
        return self._call_async(self._load_and_play(index))

    def seek(self, seconds: int) -> bool:
        # Decide by the CURRENTLY-PLAYING stream's format, not the global
        # quality setting: a mid-track quality switch leaves the current
        # track playing its ORIGINAL format (the change applies from the
        # next track), so its URL — not the setting — is the source of truth.
        cur = self._current_url or ""
        if "q=opus" in cur:
            # VBR Opus renderers (KANN) can't time-seek — reload the SAME
            # stream at the offset (reusing cur keeps its Opus format even
            # after the setting flipped to lossless). Fire-and-forget like a
            # track change; the UI holds the scrubbed position meanwhile.
            base = cur
            for sep in ("&ss=", "?ss="):
                if sep in base:
                    base = base.split(sep)[0]
            url = base + ("&" if "?" in base else "?") + f"ss={int(seconds)}"
            return self._call_async(
                self._load_and_play(self._index, ss=float(seconds), url_override=url))

        async def _s():
            total = int(seconds)
            target = f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
            await self._avt("Seek", Unit="REL_TIME", Target=target)
            self._rebaseline(float(total))   # clock continues from the seek target
            self._emit_now()
        return self._call(_s())

    def set_volume(self, level: float) -> bool:
        async def _v():
            await self._dmr.async_set_volume_level(
                max(0.0, min(1.0, float(level) / 100.0)))
            self._emit_now()
        return self._call(_v())

    def volume_up(self) -> bool:
        cur = (self._dmr.volume_level or 0.0) * 100.0 if self._dmr else 0.0
        return self.set_volume(min(100.0, cur + 5.0))

    def volume_down(self) -> bool:
        cur = (self._dmr.volume_level or 0.0) * 100.0 if self._dmr else 0.0
        return self.set_volume(max(0.0, cur - 5.0))

    # -- canonical-queue hooks -------------------------------------------------------

    def queue_changed(self, kind: str, *, play: bool = False) -> None:
        if kind == "replace":
            if play and len(self._queue):
                self._call_async(self._load_and_play(1))
            else:
                self.stop()
            return

        async def _resync():
            # The queue mutated around the playing slot: re-locate the
            # current index by media URL (file tokens are idempotent per
            # path, preview tokens stable — URLs survive mutations), then
            # refresh the preloaded next. A removed current item keeps
            # playing on the renderer until its natural end.
            if self._current_url is not None:
                for i, it in enumerate(self._queue.snapshot(), start=1):
                    if self._url_for(it) == self._current_url:
                        self._index = i
                        break
            await self._preload_next()
            self._emit_now()
        self._call(_resync())
