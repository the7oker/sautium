"""
PlaybackManager — the fan-out point above the output-backend abstraction
(HARDWARE-TIERS §2.6).

Owns the canonical queue, the active `PlayerBackend`, the SSE status
plumbing, and the single play-tracking point: every backend pushes
`PlaybackStatus` events into `_on_backend_status`, which builds the SSE
payload (same shape the UI has always consumed, plus `output`), feeds the
tracker, archives natural end-of-queue sessions, and notifies observers
(radio refill).

Queue mutations go through the manager: mirror into the active backend
first (it can short-apply on a dropped control socket), then commit the
actually-applied prefix to the canonical queue — so the canonical queue
never claims tracks the player doesn't have. `_mutate_lock` serialises
mutations so canonical and mirrored order can't interleave.
"""

import logging
import threading
from typing import Callable, Optional

from hqplayer_client import format_time

from playback import sessions, tracker
from playback.base import PlaybackStatus, PlayerBackend, ReorderPlan
from playback.queue import CanonicalQueue, QueueItem, resolved_artwork

logger = logging.getLogger(__name__)


class PlaybackManager:
    def __init__(self):
        self.queue = CanonicalQueue()
        self._active: Optional[PlayerBackend] = None
        self._active_lock = threading.Lock()
        self._mutate_lock = threading.RLock()
        self.radio_mode = False

        self._latest_status: dict = {"state": "disconnected"}
        self._status_version = 0
        self._sse_clients: list = []      # (asyncio.Event, asyncio.AbstractEventLoop)
        self._sse_clients_lock = threading.Lock()
        self._observers: list[Callable] = []

    # -- backend lifecycle ---------------------------------------------------

    def backend(self) -> PlayerBackend:
        b = self._active
        if b is None:
            raise ConnectionError("No playback output configured")
        return b

    @property
    def active(self) -> Optional[PlayerBackend]:
        return self._active

    def activate(self, output_type: Optional[str], *, stop_old: bool = True) -> None:
        """Switch the active output. The old backend is stopped and shut
        down; the canonical queue survives the switch; playback does NOT
        auto-resume — the user presses play on the new output.

        `stop_old=False` detaches without touching playback — app shutdown
        and clearing the HQPlayer host must leave an externally-running
        player alone (a backend restart mid-listening adopts the still-
        playing queue on the next start)."""
        with self._active_lock:
            if (output_type is not None and self._active is not None
                    and self._active.id == output_type):
                return
            old = self._active
            if old is not None:
                self._active = None
                if stop_old:
                    try:
                        old.stop()
                    except Exception as e:
                        logger.debug("stop on deactivate failed: %s", e)
                old.shutdown()
                logger.info("playback backend deactivated: %s", old.id)
            if output_type is None:
                # No output ⇒ no status source at all (§2.6).
                self._push_status({"state": "disconnected"})
                return
            if output_type == "hqplayer":
                from playback.hqp_backend import HqpBackend
                backend = HqpBackend(emit=self._on_backend_status, queue=self.queue)
            else:
                raise ValueError(f"unknown output type: {output_type}")
            self._active = backend
            backend.start()
            logger.info("playback backend activated: %s", backend.id)

    # -- SSE plumbing ----------------------------------------------------------

    @property
    def latest_status(self) -> dict:
        return self._latest_status

    @property
    def status_version(self) -> int:
        return self._status_version

    def sse_register(self, evt, loop) -> None:
        with self._sse_clients_lock:
            self._sse_clients.append((evt, loop))

    def sse_unregister(self, evt) -> None:
        with self._sse_clients_lock:
            self._sse_clients[:] = [(e, l) for e, l in self._sse_clients if e is not evt]

    def wake_sse(self) -> None:
        """Thread-safe: signal all SSE async generators to send new data."""
        with self._sse_clients_lock:
            for evt, loop in self._sse_clients:
                loop.call_soon_threadsafe(evt.set)

    def subscribe_status(self, fn: Callable) -> None:
        """Register `fn(status_dict, item)` — called on every successful
        status tick before the SSE fan-out (radio refill rides on this)."""
        self._observers.append(fn)

    def _push_status(self, new_data: dict) -> None:
        if new_data != self._latest_status:
            self._latest_status = new_data
            self._status_version += 1
            self.wake_sse()

    def set_radio_mode(self, value: bool) -> None:
        """Flip the radio flag and re-publish the current status payload so
        the Now Playing toggle snaps immediately, without waiting a tick."""
        if self.radio_mode == value:
            return
        self.radio_mode = value
        if self._latest_status.get("state") not in (None, "disconnected"):
            self._push_status({**self._latest_status, "radio_mode": value})
        else:
            self._status_version += 1
            self.wake_sse()

    # -- status pipeline ---------------------------------------------------------

    def _on_backend_status(self, s: PlaybackStatus) -> None:
        """One status tick from the active backend → SSE payload (exact
        legacy shape + `output`), play tracking, end-of-queue archival,
        observers. Runs on the backend's status thread."""
        backend = self._active
        if s.state == "disconnected":
            self._push_status({"state": "disconnected"})
            return

        idx = s.queue_index
        item = self.queue.item_at(idx)
        preview = bool(item and item.preview)
        # Phantom previews carry 'HTTP stream' in the player's own metadata —
        # surface the provider's real artist/title from the queue item. For
        # non-preview rows a backend with authoritative metadata (HQPlayer
        # file tags) supplies it via `extra`; engine-rendered backends leave
        # extra empty and the queue item is the source.
        if preview:
            artist, album, song = item.artist, (item.album or ""), item.title
        else:
            artist = s.extra.get("artist", item.artist if item else "")
            album = s.extra.get("album", (item.album or "") if item else "")
            song = s.extra.get("song", item.title if item else "")

        new_data = {
            "state": s.state,
            "artist": artist,
            "album": album,
            "song": song,
            "genre": s.extra.get("genre", ""),
            "position": s.position,
            "length": s.length,
            "volume": s.volume,
            "process_speed": s.extra.get("process_speed", 0.0),
            "track_index": idx,
            "media_file_id": item.media_file_id if item else None,
            # Universal track UUID (owned + phantom) — Discovery's
            # similar-to-now-playing seed reads it off currentStatus.
            "track_id": item.track_id if item else None,
            "cover_id": item.cover_id if item else None,
            "cover_url": item.cover_url if item else None,
            "provider_cover_url": (resolved_artwork.get(item.track_id)
                                   if preview else None),
            "progress_percent": round((s.position / s.length * 100)
                                      if s.length > 0 else 0.0, 1),
            "position_formatted": format_time(s.position),
            "length_formatted": format_time(s.length),
            "playlist_version": self.queue.version,
            "radio_mode": self.radio_mode,
            "preview": preview,
            "provider": item.provider if preview else None,
            "preview_track_id": item.track_id if preview else None,
            "output": {"type": backend.id if backend else None,
                       "label": backend.label if backend else None},
        }

        # Per-track listening history + scrobble (source-agnostic: owned and
        # streamed phantom items both carry the track UUID). Separate from the
        # session/Home-shelf archival below — that snapshots the whole queue,
        # this records each play.
        tracker.track_play_event(new_data["state"], s.position, s.length, idx, item)

        # Natural end-of-queue: the player stopped on the last track. Archive
        # the active session so a fully-listened album/queue lands in history
        # without a follow-up play. Only when the previous tick was PLAYING
        # the last slot — a manual stop mid-queue is left alone so resume
        # doesn't fragment the session.
        qlen = len(self.queue)
        if (new_data["state"] == "stopped"
                and self._latest_status.get("state") == "playing"
                and qlen > 0
                and self._latest_status.get("track_index") == qlen):
            try:
                sessions.close_active_session(self.queue)
            except Exception as e:
                logger.warning(f"end-of-queue archive failed: {e}")

        for fn in self._observers:
            try:
                fn(new_data, item)
            except Exception:
                logger.exception("status observer failed")

        self._push_status(new_data)

    # -- queue mutations -----------------------------------------------------------
    #
    # Mirror-first: the backend reports how much it actually applied and only
    # that prefix lands in the canonical queue. A failed mirror leaves the
    # canonical queue untouched (matching the player's unchanged state).

    def replace_queue(self, items: list[QueueItem], *, play: bool,
                      probe_first: bool = False) -> tuple[int, int]:
        """Replace the queue (and start playback). Returns (added, generation);
        generation is what background fillers must guard their appends with."""
        with self._mutate_lock:
            added = self.backend().queue_replace(items, play=play,
                                                 probe_first=probe_first)
            if added:
                gen = self.queue.replace(items[:added])
            else:
                gen = self.queue.generation
            return added, gen

    def append(self, items: list[QueueItem], position: str = "end",
               generation: Optional[int] = None) -> Optional[int]:
        """Append/insert. Returns the count applied, or None when
        `generation` is stale (the queue was replaced — the filler must
        stop). 'next' inserts after the playing slot; with nothing playing
        it appends at the end (legacy behavior)."""
        with self._mutate_lock:
            if generation is not None and self.queue.generation != generation:
                return None
            backend = self.backend()
            if position == "next":
                anchor = self._latest_status.get("track_index")
                anchor = anchor if (isinstance(anchor, int)
                                    and 1 <= anchor <= len(self.queue)) else None
            else:
                anchor = None
            if position == "next" and anchor is not None:
                added = backend.queue_insert_next(items, anchor)
                if added:
                    self.queue.insert_after(anchor, items[:added])
            else:
                added = backend.queue_append(items)
                if added:
                    self.queue.append(items[:added])
            return added

    def remove(self, index: int) -> bool:
        with self._mutate_lock:
            if self.queue.item_at(index) is None:
                return False
            ok = self.backend().queue_remove(index)
            if ok:
                self.queue.remove(index)
            return ok

    def apply_reorder(self, plan: ReorderPlan) -> dict:
        """Execute a validated reorder. The mirror runs first (it can raise
        mid-way — then the canonical queue is left untouched and the drift
        canary flags the divergence; a retry reconverges); on success the
        canonical queue adopts the new order."""
        with self._mutate_lock:
            result = self.backend().queue_reorder(plan)
            self.queue.reorder_by_media_ids(plan.order)
            return result

    def jump(self, index: int) -> bool:
        backend = self.backend()
        ok = backend.select(index)
        if ok:
            backend.play()
        return ok

    def clear_for_radio(self) -> int:
        """Radio start: clear everything after the playing slot (the seed
        plays on). Returns the new generation for the radio filler."""
        with self._mutate_lock:
            self.backend().queue_clear_after_current()
            return self.queue.clear_for_radio(self._latest_status.get("track_index"))


manager = PlaybackManager()
