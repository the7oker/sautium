"""
PlayerBackend contract (HARDWARE-TIERS §2.6).

A backend owns one playback output (HQPlayer / local device / DLNA /
browser): transport commands, `capabilities()`, and STATUS ACQUISITION —
each backend pushes `PlaybackStatus` events into the manager via the
`emit` callback it was constructed with. No active backend ⇒ no status
loop at all.

The canonical queue lives in the manager (`playback.queue`). Backends
whose player holds its own playlist (HQPlayer) mirror canonical-queue
mutations through the `queue_*` hooks; backends that render straight
from the canonical queue (local engine, browser) keep the no-op
defaults.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class PlaybackStatus:
    """One status tick from the active backend. `extra` carries
    backend-specific display fields (HQP: artist/album/song/genre from its
    own metadata, process_speed) that the manager folds into the SSE
    payload; backends without authoritative metadata leave it empty and
    the queue item supplies the fields."""
    state: str                          # stopped|paused|playing|stopping|unknown|disconnected
    position: float = 0.0
    length: float = 0.0
    queue_index: Optional[int] = None   # 1-based slot in the canonical queue
    volume: Optional[float] = None
    extra: dict = field(default_factory=dict)


@dataclass
class Capabilities:
    volume: bool = False
    volume_kind: str = "percent"        # "db" | "percent"
    seek: bool = False
    gapless: bool = False
    exclusive_mode: bool = False


@dataclass
class ReorderPlan:
    """A validated /reorder request, precomputed by the endpoint: the
    seamless path drops/keeps around the playing slot without touching it;
    the rebuild path clears and reloads (audible ~300 ms gap)."""
    seamless: bool
    status_idx: int                     # current playing slot (1-based)
    new_status_idx: int                 # where that slot lands in the new order
    old_before: list
    new_before: list
    old_after: list
    new_after: list
    order: list                         # full new order of media_file_ids
    items_by_id: dict                   # media_file_id -> QueueItem (URI derivation)
    position: int = 0                   # rebuild: resume seek position
    resume: bool = False                # rebuild: resume playback after select


class PlayerBackend(ABC):
    id: str = ""
    label: str = ""

    def __init__(self, emit: Callable[[PlaybackStatus], None]):
        self._emit = emit

    # -- lifecycle -------------------------------------------------------
    @abstractmethod
    def start(self) -> None:
        """Begin status acquisition (poller / engine / event subscription)."""

    @abstractmethod
    def shutdown(self) -> None:
        """Stop status acquisition and release the output."""

    def resume_at(self, index: int) -> None:
        """Seed the current queue slot right after attach, WITHOUT starting
        playback — an output switch keeps the queue AND the position in it:
        the play press on the new output continues from the track that was
        active on the old one, not from slot 1."""

    def healthy(self) -> bool:
        """Is the attached device still believed reachable? Backends whose
        devices come and go (a dozing DLNA DAP) override this; the manager's
        play-intent gate re-attaches an unhealthy output before starting a
        session. Default True — HQPlayer's poller and the browser channel
        self-heal on their own."""
        return True

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    # -- transport (False = command rejected) ------------------------------
    @abstractmethod
    def play(self) -> bool: ...

    @abstractmethod
    def pause(self) -> bool: ...

    @abstractmethod
    def stop(self) -> bool: ...

    @abstractmethod
    def next(self) -> bool: ...

    @abstractmethod
    def previous(self) -> bool: ...

    @abstractmethod
    def select(self, index: int) -> bool:
        """Move the playhead to queue slot `index` (1-based), without play."""

    def seek(self, seconds: int) -> bool:
        return False

    def set_volume(self, level: float) -> bool:
        return False

    def volume_up(self) -> bool:
        return False

    def volume_down(self) -> bool:
        return False

    # -- canonical-queue mirror hooks --------------------------------------
    # Two-step protocol. The queue_* hooks run BEFORE the canonical queue
    # mutates (mirror-first): the return value is how much the backend
    # actually applied (HQPlayer can short-add on a dropped control socket —
    # the manager commits only that prefix). `queue_changed` runs AFTER the
    # canonical commit — backends that render the canonical queue directly
    # (local engine) act here, when the queue is already in its new shape.

    def queue_changed(self, kind: str, *, play: bool = False) -> None:
        """Post-commit notification: kind ∈ replace|append|insert_next|
        remove|reorder|clear. Default: nothing to do (HQPlayer acted in the
        mirror hooks)."""

    def queue_replace(self, items: list, *, play: bool, probe_first: bool = False) -> int:
        return len(items)

    def queue_append(self, items: list) -> int:
        return len(items)

    def queue_insert_next(self, items: list, anchor_index: Optional[int]) -> int:
        return len(items)

    def queue_remove(self, index: int) -> bool:
        return True

    def queue_reorder(self, plan: ReorderPlan) -> dict:
        return {"removed": 0, "added": 0, "interrupted": False}

    def queue_clear_after_current(self) -> bool:
        return True
