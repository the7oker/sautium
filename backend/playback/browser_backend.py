"""
Browser output ("this device"): the web-UI tab that selected it becomes
the renderer (HARDWARE-TIERS §2.6).

Fully event-driven in both directions. Down: transport directives ride a
per-tab SSE command channel (GET /api/player/browser/channel — opened by
the renderer tab; a NEW subscriber displaces the old one, which receives
a `released` directive). Up: the tab POSTs its <audio> element events
(playing/paused/ended/timeupdate/error — element callbacks, not polling)
to /api/player/browser/event, and those become the PlaybackStatus feed.

Media rides the SAME HTTPS origin as the app via short-lived signed URLs
(media_urls) — audio elements can't set HMAC headers, and an https page
can't fetch the plain-http LAN proxy (mixed content).

Tab lifetime: a closed tab drops its SSE stream; after a grace period
with no reconnect the backend reports `stopped` — the queue survives, any
tab (or output) can pick it up.
"""

import asyncio
import logging
import threading
from typing import Optional

import media_urls

from playback.base import Capabilities, PlaybackStatus, PlayerBackend
from playback.queue import CanonicalQueue, QueueItem

logger = logging.getLogger(__name__)

_DISCONNECT_GRACE_S = 5.0


class BrowserBackend(PlayerBackend):
    id = "browser"
    label = "This device (browser)"

    def __init__(self, emit, queue: CanonicalQueue):
        super().__init__(emit)
        self._queue = queue
        self._lock = threading.Lock()
        # The single active renderer tab: its id and its SSE feed
        # (asyncio.Queue + the loop it lives on, for thread-safe pushes).
        self._tab: Optional[str] = None
        self._channel: Optional[asyncio.Queue] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._grace_timer: Optional[threading.Timer] = None

        self._index = 0                # 1-based canonical slot
        self._state = "stopped"
        self._position = 0.0
        self._length = 0.0
        self._volume = 100.0
        # A play intent that arrived while NO renderer tab was attached
        # (e.g. the previous renderer closed): honored the moment a tab
        # claims the output, instead of silently playing into the void.
        self._pending_play_index: Optional[int] = None

    @property
    def renderer_attached(self) -> bool:
        """A renderer tab currently holds the command channel — surfaced in
        the status payload so control surfaces can tell 'remote control'
        from 'nobody is playing'."""
        return self._channel is not None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._emit_now()
        logger.info("browser output active — waiting for a renderer tab")

    def shutdown(self) -> None:
        self._push({"cmd": "released"})
        self._cancel_grace()
        logger.info("browser output detached")

    def capabilities(self) -> Capabilities:
        return Capabilities(volume=True, volume_kind="percent", seek=True,
                            gapless=False)

    # -- renderer-tab channel (called from the SSE endpoint) -----------------

    def attach_tab(self, tab: str, channel: asyncio.Queue,
                   loop: asyncio.AbstractEventLoop) -> None:
        """A tab opened the command channel. Takeover rule: the newest tab
        wins; the previous one is told to release its <audio>."""
        with self._lock:
            if self._channel is not None and self._tab != tab:
                self._push_locked({"cmd": "released"})
                logger.info("browser renderer takeover: %s → %s", self._tab, tab)
            self._tab = tab
            self._channel = channel
            self._loop = loop
        self._cancel_grace()
        if self._pending_play_index is not None:
            # A play intent queued up while no renderer existed — honor it
            # now on the tab that just claimed the output.
            index = self._pending_play_index
            self._pending_play_index = None
            self._start_at(index, play=True)
            return
        # (Re)prime the new tab with the current slot, paused — the user
        # presses play (that tap is also the autoplay-unlock gesture).
        item = self._queue.item_at(self._index)
        if item is not None:
            self._push(self._load_directive(self._index, item, play=False))

    def detach_tab(self, tab: str) -> None:
        """The tab's SSE stream ended (closed tab / navigation). After a
        short grace (reloads reconnect quickly) the output reports stopped."""
        with self._lock:
            if self._tab != tab:
                return
            self._channel = None
            self._loop = None
        self._cancel_grace()
        timer = threading.Timer(_DISCONNECT_GRACE_S, self._grace_expired)
        timer.daemon = True
        self._grace_timer = timer
        timer.start()

    def _grace_expired(self) -> None:
        with self._lock:
            if self._channel is not None:
                return   # reconnected in time
        if self._state != "stopped":
            self._state = "stopped"
            self._emit_now()
        logger.info("browser renderer tab gone — output stopped")

    def _cancel_grace(self) -> None:
        if self._grace_timer is not None:
            self._grace_timer.cancel()
            self._grace_timer = None

    def _push(self, directive: dict) -> None:
        with self._lock:
            self._push_locked(directive)

    def _push_locked(self, directive: dict) -> None:
        if self._channel is None or self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._channel.put_nowait, directive)

    # -- events from the tab (called from the POST endpoint) --------------------

    def on_client_event(self, tab: str, event: str, position: Optional[float],
                        duration: Optional[float]) -> None:
        if tab != self._tab:
            return   # a displaced tab still flushing events
        if position is not None:
            self._position = float(position)
        if duration:
            self._length = float(duration)
        if event == "playing":
            self._state = "playing"
        elif event == "paused":
            self._state = "paused"
        elif event == "error":
            item = self._queue.item_at(self._index)
            logger.warning("browser renderer error on %s — %s",
                           item.artist if item else "?",
                           item.title if item else "?")
            self._state = "stopped"
        elif event == "ended":
            self._advance()
            return
        self._emit_now()

    def _advance(self) -> None:
        nxt_index = self._index + 1
        item = self._queue.item_at(nxt_index)
        if item is None:
            self._state = "stopped"
            self._emit_now()
            return
        self._index = nxt_index
        self._position = 0.0
        self._length = item.duration_seconds or 0.0
        self._push(self._load_directive(nxt_index, item, play=True))
        self._state = "playing"
        self._emit_now()

    # -- directives ------------------------------------------------------------------

    @staticmethod
    def _media_url(item: QueueItem) -> Optional[str]:
        src = item.source
        if src["kind"] == "file":
            return media_urls.signed_media_url("file", str(item.media_file_id))
        if src["kind"] == "proxy":
            return media_urls.signed_media_url("preview", src["token"])
        return None

    def _load_directive(self, index: int, item: QueueItem, *, play: bool) -> dict:
        return {
            "cmd": "load",
            "url": self._media_url(item),
            "play": play,
            "meta": {
                "title": item.title,
                "artist": item.artist,
                "album": item.album,
                "cover_id": item.cover_id,
                "cover_url": item.cover_url,
            },
        }

    def _start_at(self, index: int, *, play: bool) -> bool:
        item = self._queue.item_at(index)
        if item is None:
            return False
        url = self._media_url(item)
        if url is None:
            logger.warning("browser output: skipping unreachable item %s — %s",
                           item.artist, item.title)
            return self._start_at(index + 1, play=play)
        self._index = index
        self._position = 0.0
        self._length = item.duration_seconds or 0.0
        if self._channel is None:
            # No renderer tab: don't pretend to play — park the intent (the
            # next tab to claim the output starts here) and report honestly.
            if play:
                self._pending_play_index = index
            self._state = "stopped"
            self._emit_now()
            return True
        self._push(self._load_directive(index, item, play=play))
        if play:
            self._state = "playing"
        self._emit_now()
        return True

    # -- transport ---------------------------------------------------------------------

    def play(self) -> bool:
        if self._state == "paused" and self._channel is not None:
            self._push({"cmd": "play"})
            return True
        # Without a renderer this parks the intent (see _start_at) instead
        # of failing — the claiming tab will start right here.
        return self._start_at(self._index if self._index >= 1 else 1, play=True)

    def pause(self) -> bool:
        self._push({"cmd": "pause"})
        return True

    def stop(self) -> bool:
        self._push({"cmd": "stop"})
        self._state = "stopped"
        self._position = 0.0
        self._emit_now()
        return True

    def next(self) -> bool:
        return self._start_at(self._index + 1, play=True)

    def previous(self) -> bool:
        return self._start_at(max(1, self._index - 1), play=True)

    def select(self, index: int) -> bool:
        if self._queue.item_at(index) is None:
            return False
        return self._start_at(index, play=True)

    def seek(self, seconds: int) -> bool:
        self._push({"cmd": "seek", "position": int(seconds)})
        self._position = float(seconds)
        return True

    def set_volume(self, level: float) -> bool:
        self._volume = max(0.0, min(100.0, float(level)))
        self._push({"cmd": "volume", "level": self._volume})
        self._emit_now()
        return True

    def volume_up(self) -> bool:
        return self.set_volume(self._volume + 5.0)

    def volume_down(self) -> bool:
        return self.set_volume(self._volume - 5.0)

    # -- canonical-queue hooks -------------------------------------------------------------

    def queue_changed(self, kind: str, *, play: bool = False) -> None:
        if kind == "replace":
            if play and len(self._queue):
                self._start_at(1, play=True)
            else:
                self.stop()
            return
        # Re-locate the current slot by item identity after mutations.
        current = self._queue.item_at(self._index)
        if current is None and self._state != "stopped":
            snapshot_len = len(self._queue)
            if snapshot_len:
                self._start_at(min(self._index, snapshot_len), play=self._state == "playing")
            else:
                self.stop()

    # -- status --------------------------------------------------------------------------

    def _emit_now(self) -> None:
        extra = {}
        if self._channel is None and (self._state != "stopped"
                                      or self._pending_play_index is not None):
            extra["error"] = ("no playback device — open Sautium on the "
                              "device that should play and press play")
        self._emit(PlaybackStatus(
            state=self._state,
            position=self._position,
            length=self._length,
            queue_index=self._index if self._index >= 1 else 0,
            volume=self._volume,
            extra=extra,
        ))
