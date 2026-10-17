"""
PlayerBackend contract (HARDWARE-TIERS §2.6).

A backend owns one playback output (HQPlayer / local device / DLNA /
browser): transport commands, `capabilities()`, and STATUS ACQUISITION —
each backend pushes `PlaybackStatus` events into the manager via the
`emit` callback it was constructed with (every tick names its backend —
the manager hears the active one only). No active backend ⇒ no status
loop at all.

The canonical queue lives in the manager (`playback.queue`). Backends
whose player holds its own playlist (HQPlayer) mirror canonical-queue
mutations through the `queue_*` hooks; backends that render straight
from the canonical queue (local engine, browser) keep the no-op
defaults.
"""

import logging
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from playback.queue import QueueItem

logger = logging.getLogger(__name__)


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
    # The queue item a backend identified as playing (HQPlayer names the
    # entry it reads): the manager takes it as it is and reads its slot off
    # it — a mutation may have committed between the backend's look and the
    # manager's, and an index looked up again would name another item.
    item: Optional["QueueItem"] = None


@dataclass
class Capabilities:
    volume: bool = False
    volume_kind: str = "percent"        # "db" | "percent"
    seek: bool = False
    gapless: bool = False
    exclusive_mode: bool = False


class OutputRefused(RuntimeError):
    """The output refused a step of what it was asked, in its own words —
    its answer, never a fault of the node's."""


@dataclass
class ReorderPlan:
    """A validated /reorder request, precomputed by the endpoint: the
    seamless path drops/keeps around the playing slot without touching it;
    the rebuild path clears and reloads (audible ~300 ms gap). Its lists
    are the queue's items themselves — a slot by identity, never by track
    id (a track queued twice is two slots; a re-bind may clear an id)."""
    seamless: bool
    status_idx: int                     # current playing slot (1-based)
    new_status_idx: int                 # where that slot lands in the new order
    old_before: list
    new_before: list
    old_after: list
    new_after: list
    order: list                         # the queue's items in the new order
    position: int = 0                   # rebuild: the second it resumes at
    resume: bool = False                # rebuild: play on (it was playing), else the play press resumes
    # The queue's items, in order, it was planned on: applied to no other
    # order — a re-read of a slot's way in moves no slot
    items: Optional[list] = None
    # The output it was planned on — its position and `resume` are that
    # output's: applied on no other
    output: Optional["PlayerBackend"] = None


class PlayerBackend(ABC):
    id: str = ""
    label: str = ""
    # Holds a person's play intents while a queue mutation hands it its
    # playlist, and finds a tap's track among what it is handed (HQPlayer):
    # the jump route hands it the row's index as the sheet sent it then
    holds_words: bool = False

    def __init__(self, emit: Callable[["PlayerBackend", PlaybackStatus], None]):
        # Every tick names the backend it came from: the manager listens to
        # the ACTIVE backend only, so one being detached — its stop during
        # an output switch, a poll or GENA tick landing after unsubscribe —
        # cannot write over the output that replaced it.
        self._emit = lambda s: emit(self, s)

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

    def reachable(self) -> bool:
        """Fast, synchronous liveness probe at play-intent time. Overridden
        by outputs that can be physically off (DLNA renderers) so a powered-
        off device fails in ~2s with a clear message instead of stacked
        command/attach timeouts. Default True — no cheap probe, trust it."""
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

    def release(self) -> Optional[str]:
        """The output is switched away from — a person's switch to another
        output: stop what it plays. Returns what the person is told when the
        stop is left for later — HQPlayer, building a setting or answering
        nothing, stops once it answers; the switch does not wait for it. A
        backend that believes its device off the network sends no stop: it
        would fail exactly as the last command did and burn the command
        timeout on a ghost (the switch away from a vanished phone renderer
        waited out its whole Stop + unsubscribe budget)."""
        if not self.healthy():
            logger.info("%s believed off the network — switched away without a stop", self.id)
            return None
        self.stop()
        return None

    def refusal(self) -> str:
        """Why the last command this thread sent was not taken, in the
        device's own words when it gave any — what the assistant quotes."""
        return ""

    def pressing(self) -> Optional[dict]:
        """While a play intent goes on behind its answer — HQPlayer taking
        the queue again, or a queue mutation it is held for — the track it
        will start ({"index", "track_id", "position"}), or {} when it will
        start none; None otherwise. Default: none — every other output starts
        before it answers."""
        return None

    @abstractmethod
    def next(self) -> bool: ...

    @abstractmethod
    def previous(self) -> bool: ...

    @abstractmethod
    def select(self, index: int, track_id: Optional[str] = None) -> bool:
        """Play queue slot `index` (1-based): load it and start it in ONE
        step. Every backend owns that atomically — a caller-side play()
        after select() raced the DLNA load and resumed the previous track
        while the new one buffered. `index` holds the track the person saw
        (the jump route finds it, CanonicalQueue.slot_of); `track_id` is
        that track — an output that holds play intents while a queue
        mutation is in flight (mutation) plays it wherever the mutation puts
        it."""

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
    # Both run under the queue lock; `queue_start` runs once it is let go:
    # a start the mirror left (HQPlayer may build it for minutes).

    def queue_changed(self, kind: str, *, play: bool = False) -> None:
        """Post-commit notification: kind ∈ replace|append|insert_next|
        remove|reorder|clear. Default: nothing to do (HQPlayer acted in the
        mirror hooks)."""

    def queue_start(self) -> Optional[str]:
        """After a mutation, outside the queue lock: the start its mirror
        left on this thread — a replace that plays, a rebuild that plays on —
        and the play intents held meanwhile, carried out whether the queue
        committed or not (mutation). Returns what of them could not go, in
        words — said with the refusal of a mutation that did not commit —
        or None. Default: nothing (a renderer starts in queue_changed)."""
        return None

    @contextmanager
    def mutation(self):
        """A queue mutation this output follows — a replace, a reorder, a
        re-bind, a radio start — on this thread, until the queue commits it
        or gives it up: a play intent made while the output takes it is the
        output's to hold and carry out once the queue lock is let go, on the
        queue as it then stands (queue_start). Default: nothing to hold."""
        yield

    def queue_abandon(self) -> None:
        """A replace this output mirrored was not committed (the session
        rotation before the commit failed): what its mirror left on this
        thread is not carried out. Default: nothing (a renderer had not
        started anything)."""

    def slot_ready(self, index: int) -> None:
        """A slot this output could not open when it was reached now can —
        a stream substitute landed on it (playback.substitute). Default:
        nothing; an output that parked a play there starts it."""

    def gap(self, item) -> Optional[str]:
        """Why this output does not hold the queue's slot `item` — it stays
        in the queue, named with this — or None. Default: every slot is
        held; HQPlayer's playlist leaves out what it cannot open."""
        return None

    # The three take what the output took of `items` into the queue — all of
    # them here and on HQPlayer, which has a gap for what it did not open;
    # nothing when HQPlayer refused them whole (mirror-first). A play next
    # also names the slot it went behind — HQPlayer's, read as the insert
    # goes, when it moved on past the anchor — and the queue puts the tracks
    # there too.

    def queue_replace(self, items: list, *, play: bool) -> list:
        return list(items)

    def queue_append(self, items: list) -> list:
        return list(items)

    def queue_insert_next(self, items: list, anchor_index: Optional[int]) -> tuple:
        return list(items), anchor_index

    def lose_mirror(self, why: str) -> None:
        """Background work could not hand the output what the queue took (a
        filler whose wait gave up). Default: nothing — only HQPlayer keeps a
        mirror of the queue."""

    def queue_remove(self, index: int) -> bool:
        return True

    def queue_reorder(self, plan: ReorderPlan) -> dict:
        return {"removed": 0, "added": 0, "interrupted": False}

    def queue_clear_after_current(self, archive: Callable[[], None]) -> bool:
        """Radio start: everything around the playing slot goes. `archive`
        (the session rotation) runs first — a rotation that fails clears
        nothing."""
        archive()
        return True
