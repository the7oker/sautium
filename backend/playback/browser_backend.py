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
        self._current_ident: Optional[str] = None   # media identity of that slot
        self._state = "stopped"
        self._position = 0.0
        self._length = 0.0
        self._volume = 100.0
        # Bumped on every server-initiated track start; events stamped with
        # an older epoch are stale (they raced a newer load directive) and
        # must not resync the index or position.
        self._epoch = 0
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

    def resume_at(self, index: int) -> None:
        item = self._queue.item_at(index)
        if item is None:
            return
        self._index = index
        self._current_ident = self._item_ident(item)
        self._length = item.duration_seconds or 0.0
        self._position = 0.0
        self._emit_now()

    # -- renderer-tab channel (called from the SSE endpoint) -----------------

    def attach_tab(self, tab: str, channel: asyncio.Queue,
                   loop: asyncio.AbstractEventLoop) -> None:
        """A tab opened the command channel. Takeover rule: the newest tab
        wins; the previous one is told to release its <audio>. Priming is
        unconditional — the RENDERER is idempotent by queue_index, so a live
        reconnect (screen woke, network blip) updates bookkeeping without
        reloading the src it is already playing."""
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
        # (Re)prime the tab with the current slot, paused — the user
        # presses play (that tap is also the autoplay-unlock gesture).
        # The directive carries the last known position so a reloaded page
        # resumes where the music was, not from zero.
        item = self._queue.item_at(self._index)
        if item is not None:
            self._push(self._load_directive(self._index, item, play=False,
                                            position=self._position))
            if self._state == "playing":
                # A freshly primed element is paused by definition (a
                # reloaded page cannot resume without a user gesture).
                # Claiming "playing" here deadlocks the UI: the pause the
                # play/pause button then sends is a no-op on an already
                # paused element and no event ever corrects the state. If
                # this was a live SSE blip instead (audio still running),
                # the tab's next timeupdate flips the state right back.
                self._state = "paused"
                self._emit_now()

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
                        duration: Optional[float],
                        queue_index: Optional[int] = None,
                        epoch: Optional[int] = None) -> Optional[dict]:
        """Returns an optional payload for the POST response — the only
        downlink that provably works from a frozen background tab (its SSE
        channel is dead exactly when radio refills matter)."""
        if tab != self._tab:
            return None   # a displaced tab still flushing events
        if epoch is not None and epoch != self._epoch:
            return None   # stale event raced a newer load directive
        if queue_index is not None and int(queue_index) != self._index:
            # The renderer advanced locally (mobile background path — see
            # _queue_tail). Within an epoch its slot is the truth; every
            # event carries it, so a lost `advanced` POST self-heals here.
            self._index = int(queue_index)
            item = self._queue.item_at(self._index)
            self._current_ident = self._item_ident(item) if item else None
            self._length = (item.duration_seconds or 0.0) if item else 0.0
            self._position = 0.0
        if position is not None:
            self._position = float(position)
        if duration:
            self._length = float(duration)
        if event == "advanced":
            self._state = "playing"
            self._emit_now()
            # Fresh tail (new signed URLs + anything appended since the last
            # load — radio refills land here one advance after they trigger).
            return {"queue": self._queue_tail(self._index)}
        if event == "playing":
            self._state = "playing"
        elif event == "timeupdate":
            # The tab only posts timeupdate while its element is actually
            # playing — the authoritative signal that un-flips the paused
            # state a channel-reconnect prime assumed.
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
            return self._advance()
        self._emit_now()
        return None

    def _advance(self) -> Optional[dict]:
        """Tab-driven advance past its local tail. The load directive rides
        the POST response, not the SSE push — `ended` means the tab's list
        ran dry, and if it is frozen in the background the response is the
        only way to hand it the next track."""
        nxt_index = self._index + 1
        item = self._queue.item_at(nxt_index)
        if item is None:
            self._state = "stopped"
            self._emit_now()
            return None
        self._index = nxt_index
        self._current_ident = self._item_ident(item)
        self._position = 0.0
        self._length = item.duration_seconds or 0.0
        self._epoch += 1
        directive = self._load_directive(nxt_index, item, play=True)
        self._state = "playing"
        self._emit_now()
        return {"directive": directive}

    # -- directives ------------------------------------------------------------------

    @staticmethod
    def _media_url(item: QueueItem) -> Optional[str]:
        # Snapshot the global quality setting into each track's signed URL —
        # the media route transcodes to Opus for the bandwidth tiers. Read
        # fresh per track so a mid-session change applies from the next one.
        from routers.settings import _read
        q = _read("output.stream_quality")
        src = item.source
        if src["kind"] == "file":
            return media_urls.signed_media_url("file", str(item.media_file_id), q)
        if src["kind"] == "proxy":
            return media_urls.signed_media_url("preview", src["token"], q)
        return None

    @staticmethod
    def _item_meta(item: QueueItem) -> dict:
        return {
            "title": item.title,
            "artist": item.artist,
            "album": item.album,
            "cover_id": item.cover_id,
            "cover_url": item.cover_url,
        }

    @staticmethod
    def _item_ident(item: QueueItem) -> Optional[str]:
        """Stable media identity for the renderer's blob cache — queue
        indexes shift on replace/reorder, so slot numbers alone would let a
        stale blob impersonate a different track."""
        if item.media_file_id:
            return f"f{item.media_file_id}"
        src = item.source
        if src.get("kind") == "proxy":
            return f"p{src.get('token')}"
        return None

    # Mobile browsers freeze background tabs: the SSE channel is dead while
    # the screen is off, so track advancement must NOT round-trip through
    # the server. Every load ships the queue tail (signed URLs + metadata);
    # the tab advances locally inside its `ended` handler and notifies via
    # POST — the backend only resynchronizes. The limit keeps the deepest
    # tail entry comfortably inside the signed-URL TTL (40 tracks ≈ 3 h
    # vs 4 h), and every `advanced` refreshes the tail anyway.
    _QUEUE_TAIL_LIMIT = 40

    def _queue_tail(self, from_index: int) -> list:
        tail = []
        for offset in range(self._QUEUE_TAIL_LIMIT):
            index = from_index + offset
            item = self._queue.item_at(index)
            if item is None:
                break
            url = self._media_url(item)
            if url is None:
                continue
            tail.append({"queue_index": index, "url": url,
                         "media": self._item_ident(item),
                         "meta": self._item_meta(item)})
        return tail

    def _load_directive(self, index: int, item: QueueItem, *, play: bool,
                        position: float = 0.0) -> dict:
        return {
            "cmd": "load",
            "url": self._media_url(item),
            "play": play,
            "position": position,
            "queue_index": index,
            "media": self._item_ident(item),
            "epoch": self._epoch,
            "meta": self._item_meta(item),
            "queue": self._queue_tail(index),
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
        self._current_ident = self._item_ident(item)
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
        self._epoch += 1
        self._push(self._load_directive(index, item, play=play))
        if play:
            self._state = "playing"
        self._emit_now()
        return True

    # -- transport ---------------------------------------------------------------------

    def play(self) -> bool:
        if self._state in ("playing", "paused") and self._channel is not None:
            # Resume / ensure-playing — never reload the current slot. A jump
            # is select()+play(): reloading here would restart the track and
            # bump the epoch a second time, orphaning in-flight tab events.
            self._push({"cmd": "play"})
            return True
        # Without a renderer this parks the intent (see _start_at) instead
        # of failing — the claiming tab will start right here.
        return self._start_at(self._index if self._index >= 1 else 1, play=True)

    def pause(self) -> bool:
        self._push({"cmd": "pause"})
        if self._state == "playing":
            # Optimistic: pausing an already-paused element fires no event,
            # so waiting for one can leave a stale "playing" forever. The
            # tab's own events correct us if reality disagrees.
            self._state = "paused"
            self._emit_now()
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
        # Re-locate the current slot by MEDIA IDENTITY, not by number —
        # removing/reordering a track before the playing one shifts every
        # index (the DLNA backend does the same by URL). Closest match wins
        # so a duplicate of the playing track elsewhere in the queue can't
        # drag the slot to the wrong copy.
        snapshot = self._queue.snapshot()
        best = None
        if self._current_ident is not None:
            for i, it in enumerate(snapshot, start=1):
                if self._item_ident(it) == self._current_ident:
                    if best is None or abs(i - self._index) < abs(best - self._index):
                        best = i
        if best is not None:
            if best != self._index:
                self._index = best
                self._emit_now()
            # Keep the renderer's local tail in step with mutations (its
            # entries carry the new numbering; the tab re-locates itself
            # by media identity too).
            self._push({"cmd": "queue", "queue": self._queue_tail(self._index)})
            return
        # The playing item itself is gone from the queue.
        if self._state != "stopped":
            if snapshot:
                self._start_at(min(self._index, len(snapshot)),
                               play=self._state == "playing")
            else:
                self.stop()
            return
        self._push({"cmd": "queue", "queue": self._queue_tail(self._index)})

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
