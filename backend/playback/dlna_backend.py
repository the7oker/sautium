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
from datetime import timedelta
from typing import Optional

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

GENA_PORT = 8831            # LAN listener for renderer event callbacks
_CMD_TIMEOUT = 10.0
_TRACK_END_SLACK = 3.0      # STOPPED within this many seconds of the length = track finished

_MIME_BY_FORMAT = {
    "FLAC": "audio/flac", "MP3": "audio/mpeg", "WAV": "audio/wav",
    "OGG": "audio/ogg", "M4A": "audio/mp4", "AIFF": "audio/aiff",
}


def media_host() -> str:
    """LAN-reachable host for renderer-pulled media URLs. An explicit
    MEDIA_PROXY_ADVERTISED_HOST wins; the localhost default (fine for
    HQPlayer on the same machine) is replaced by a detected private IP —
    inside Docker that detection only sees the bridge, so DLNA-from-Docker
    requires the explicit setting (documented in docker-compose)."""
    configured = settings.media_proxy_advertised_host
    if configured and configured != "127.0.0.1":
        return configured
    from tls_gen import detect_private_host_ips
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
        self.label = renderer.get("name") or "DLNA renderer"
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

        # dlna-loop-owned state
        self._dmr: Optional["DmrDevice"] = None
        self._notify_server = None
        self._index = 0                          # 1-based canonical slot
        self._current_url: Optional[str] = None
        self._next_url: Optional[str] = None     # set via SetNextAVTransportURI
        self._position = 0.0
        self._length = 0.0
        self._poll_task: Optional[asyncio.Task] = None
        self._error: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="dlna-loop")
        self._thread.start()
        if not self._ready.wait(timeout=20):
            raise RuntimeError("DLNA renderer connection timed out")
        if self._error:
            raise RuntimeError(self._error)
        logger.info("DLNA backend attached to %s", self.label)

    def shutdown(self) -> None:
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._async_shutdown(), self._loop)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("DLNA backend detached from %s", self.label)

    def capabilities(self) -> Capabilities:
        return Capabilities(volume=True, volume_kind="percent", seek=True,
                            gapless=False)

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_start())
        except Exception as e:
            self._error = f"DLNA attach failed: {e}"
            logger.error(self._error)
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()
        # loop stopped → drain pending shutdown tasks
        pending = asyncio.all_tasks(self._loop)
        for t in pending:
            t.cancel()
        self._loop.run_until_complete(
            asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    async def _async_start(self) -> None:
        requester = AiohttpRequester()
        factory = UpnpFactory(requester, non_strict=True)
        device = await factory.async_create_device(self._renderer["location"])

        host = media_host()
        self._notify_server = AiohttpNotifyServer(
            requester, source=("0.0.0.0", GENA_PORT),
            callback_url=f"http://{host}:{GENA_PORT}/notify")
        await self._notify_server.async_start_server()

        self._dmr = DmrDevice(device, self._notify_server.event_handler)
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
                await self._dmr.async_unsubscribe_services()
        except Exception as e:
            logger.debug("GENA unsubscribe: %s", e)
        try:
            if self._notify_server:
                await self._notify_server.async_stop_server()
        except Exception as e:
            logger.debug("notify server stop: %s", e)

    # -- status ---------------------------------------------------------------

    def _state_name(self) -> str:
        st = self._dmr.transport_state if self._dmr else None
        if st in (TransportState.PLAYING, TransportState.TRANSITIONING):
            return "playing"
        if st == TransportState.PAUSED_PLAYBACK:
            return "paused"
        return "stopped"

    def _emit_now(self, state: Optional[str] = None) -> None:
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

    def _ensure_poll(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.ensure_future(self._poll_position(),
                                                    loop=self._loop)

    def _stop_poll(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()

    async def _poll_position(self) -> None:
        """1 Hz GetPositionInfo while playing — RelTime is not evented
        (documented boundary exception, §2.6). Also our track-end detector:
        some renderers under-report the final GENA STOPPED."""
        try:
            while True:
                await asyncio.sleep(1.0)
                if self._dmr is None:
                    return
                try:
                    await self._dmr.async_update()
                except Exception as e:
                    logger.debug("position poll update failed: %s", e)
                    continue
                pos = self._dmr.media_position
                if pos is not None:
                    self._position = float(pos)
                dur = self._dmr.media_duration
                if dur:
                    self._length = float(dur)
                state = self._state_name()
                if state == "stopped":
                    finished = (self._length > 0 and
                                self._position >= self._length - _TRACK_END_SLACK)
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
            pass

    # -- queue walking -----------------------------------------------------------

    def _url_for(self, item: QueueItem) -> Optional[str]:
        from streaming import service as streaming_service
        src = item.source
        host = media_host()
        if src["kind"] == "file":
            proxy = streaming_service.ensure_proxy()
            path = settings.translate_to_local_path(src["path"])
            mime = _MIME_BY_FORMAT.get((src.get("format") or "").upper(),
                                       "application/octet-stream")
            tok = proxy.register_file(path, mime)
            return f"http://{host}:{proxy.port}/file/{tok}"
        if src["kind"] == "proxy":
            proxy = streaming_service.get_proxy()
            if proxy is None:
                return None
            return f"http://{host}:{proxy.port}/preview/{src['token']}"
        return None   # foreign URIs (file:// passthrough) aren't renderer-reachable

    @staticmethod
    def _didl_meta(item: QueueItem) -> dict:
        """Structured DIDL-Lite fields — renderers show artist/album in their
        own UI (a bare title left them as "unknown artist/album")."""
        meta = {}
        if item.artist:
            meta["artist"] = item.artist
        if item.album:
            meta["album"] = item.album
        if item.track_number:
            meta["original_track_number"] = item.track_number
        return meta

    async def _load_and_play(self, index: int, *, play: bool = True) -> bool:
        item = self._queue.item_at(index)
        if item is None:
            self._emit_now("stopped")
            return False
        url = self._url_for(item)
        if url is None:
            logger.warning("DLNA: skipping unreachable item %s — %s",
                           item.artist, item.title)
            return await self._load_and_play(index + 1, play=play)
        await self._dmr.async_set_transport_uri(
            url, item.title or "Sautium", meta_data=self._didl_meta(item))
        self._index = index
        self._current_url = url
        self._position = 0.0
        self._length = item.duration_seconds or 0.0
        if play:
            await self._dmr.async_play()
            self._ensure_poll()
        await self._preload_next()
        self._emit_now()
        return True

    async def _preload_next(self) -> None:
        """SetNextAVTransportURI where supported — the renderer transitions
        gaplessly on its own; we detect the URI change and follow."""
        self._next_url = None
        nxt = self._queue.item_at(self._index + 1)
        if nxt is None or not hasattr(self._dmr, "async_set_next_transport_uri"):
            return
        url = self._url_for(nxt)
        if url is None:
            return
        try:
            await self._dmr.async_set_next_transport_uri(
                url, nxt.title or "Sautium", meta_data=self._didl_meta(nxt))
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
        self._position = 0.0
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

    def _call(self, coro) -> bool:
        if self._loop is None:
            return False
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            fut.result(timeout=_CMD_TIMEOUT)
            return True
        except Exception as e:
            logger.warning("DLNA command failed: %s", e)
            return False

    def play(self) -> bool:
        async def _p():
            if self._current_url is None:
                await self._load_and_play(self._index if self._index >= 1 else 1)
            else:
                await self._avt("Play", Speed="1")
                self._ensure_poll()
                self._emit_now("playing")
        return self._call(_p())

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
        return self._call(self._load_and_play(self._index + 1))

    def previous(self) -> bool:
        return self._call(self._load_and_play(max(1, self._index - 1)))

    def select(self, index: int) -> bool:
        if self._queue.item_at(index) is None:
            return False
        return self._call(self._load_and_play(index))

    def seek(self, seconds: int) -> bool:
        async def _s():
            total = int(seconds)
            target = f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
            await self._avt("Seek", Unit="REL_TIME", Target=target)
            self._position = float(total)
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
                self._call(self._load_and_play(1))
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
