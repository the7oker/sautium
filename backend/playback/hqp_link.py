"""
The HQPlayer link (2026-10-12): everything this node says to one
HQPlayer goes through its link — one per hqp_library.address_key, so this
machine's aliases are one link.

A link owns:
  * one connection to its own address (an HQPlayerClient whose requests all
    pass `request`, its gate), held by one owner at a time: a command
    sequence from its first request to its end, a question for its own. So
    at most one request from this node is outstanding at an HQPlayer — a
    command, the status poller, the HQPlayer screen, the play gate, the
    library, the assistants — and nothing reaches it while it builds but the
    request whose answer is held. HQPlayer's one control thread answers in
    order anyway: two connections only sent requests into each other's
    silences;
  * the command lock: one command sequence at a time, none while HQPlayer
    builds — a person's command is refused at once (Building, 409), never
    parked behind the build; background work waits the build out (through);
  * its build watch (BuildWatch): HQPlayer answers nothing while it builds a
    setting, and the late answer of the command that set the build off is
    the build's end;
  * the intent clock: what a person or this node asked of it, in order. An
    item — a start left for later, a resume, the rest of a DSP change, a
    Stop an output switch owed — is never sent once a newer intent of a kind
    that supersedes it was accepted (SUPERSEDED_BY), checked under the
    command lock, holding the connection, right before the bytes go;
  * the Stop an output switch owes it while it builds (the late Stop).

Life cycle: link_for creates a link on first use; the same HQPlayer keeps its
link; choosing another HQPlayer (choose) retires the old one — what its
detaching backend still sends and its late Stop go, then it closes, and
everything else it held gives up with why; the node stop closes every link
(close_all): sockets shut down and closed — shutdown() wakes a blocked read
on Linux and macOS, close() on Windows — and every waiter released.
"""

import logging
import select
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from config import settings
from hqplayer_client import HQPlayerClient, PlaybackState, redact_uri
from playback.hqp_benchmark import BUILD_LIMIT_S

logger = logging.getLogger(__name__)

# An answer later than this is HQPlayer building a setting. A PCM filter
# changed mid-play swaps without a silence, and a PCM start holds its answer
# for under half a second; an SDM start builds for a second and a half, a
# heavy SDM filter changed mid-play for ten (Desktop 6.2.4 on a laptop,
# 2026-10-11). A hot swap stays below it.
BUILDING_AFTER_S = 1.0
# A Stop is answered once the output is torn down: a second in PCM, up to
# three and a half in SDM through CoreAudio (Desktop 6.2.4, 2026-10-11), more
# on a slower output. A short silence, never a build.
STOP_ANSWER_S = 15.0
# A command's answer otherwise, and a question's.
COMMAND_WAIT_S = 5.0
QUESTION_WAIT_S = 4.0
# The play gate's GetInfo: a live HQPlayer answers it at once.
PROBE_WAIT_S = 2.0
# What may set off a build, or meet the silence of one set off a moment
# before (a Seek right after the SelectTrack that starts a stopped HQPlayer).
BUILD_COMMANDS = frozenset({"SetMode", "SetRate", "SetFilter", "SetShaping", "MatrixSetProfile",
                            "SetConvolution", "SetInvert", "Play", "SelectTrack", "Seek",
                            "Next", "Previous"})
# A failed connect fails the next ones fast for this long: a stalled control
# port would otherwise make every reconnect wait out the connect timeout.
CIRCUIT_COOLDOWN_S = 6.0

UNFINISHED = "HQPlayer did not finish building the filter — restart HQPlayer"
BUILDING = "HQPlayer is building the filter — it takes commands again once it is done"
LOST = "HQPlayer went away during the build"
NOT_ANSWERING = "HQPlayer is not answering"
_ENDS = {"timeout": UNFINISHED, "lost": LOST}


def _command_waits() -> Dict[str, float]:
    """How long a link's client waits for each answer, read when the link is
    made: a Stop's output teardown, and the build limit for what may set off
    a build — HQPlayer carries out a command given up on all the same, after
    the build, out of order."""
    return {"Stop": STOP_ANSWER_S, **{c: BUILD_LIMIT_S for c in BUILD_COMMANDS}}


class Building(ConnectionError):
    """Refused while HQPlayer builds a setting (409): it answers nothing
    until the build is done, and a command sent into the silence would be
    carried out after it, out of order. Past the build limit the build is
    `unfinished`, and the person is told to restart HQPlayer."""

    def __init__(self, link: "HqpLink", state: dict):
        super().__init__(UNFINISHED if state.get("unfinished") else BUILDING)
        self.link = link
        self.state = state


class GaveUp(ConnectionError):
    """Background work that waited for a build and does not go on: HQPlayer
    did not finish the build or went away during it (`failed`), or the work
    was no longer wanted — its link was let go (another HQPlayer, the node
    stopping), the queue moved on, the output changed."""

    def __init__(self, why: str, *, failed: bool):
        super().__init__(why)
        self.failed = failed


class Dropped(ConnectionError):
    """A request that does not go: a newer intent superseded the item it
    serves (SUPERSEDED_BY), or its link was let go or lent."""


class NotAnswering(ConnectionError):
    """HQPlayer takes the connection and answers nothing, with no build of
    this node's to explain it: busy, not offline (409)."""

    def __init__(self) -> None:
        super().__init__(NOT_ANSWERING)


# Which newer intents supersede an item of each kind.
# The kinds: transport — play, pause, stop, next, previous, jump, seek; queue
# — a new queue (replace, radio clear); rebuild — HQPlayer's playlist taken
# afresh (a reorder, a re-bind); start — a start-like item made (a start or a
# resume); dsp — a DSP change; switch — the output switched away from this
# link; attach — this link attached again.
SUPERSEDED_BY = {
    "start": frozenset({"transport", "queue", "rebuild", "start", "switch"}),
    "resume": frozenset({"transport", "queue", "rebuild", "start", "dsp", "switch"}),
    "late_stop": frozenset({"attach"}),
    "dsp_knob": frozenset({"dsp", "switch"}),
}
_WHY = {"transport": "a newer request came first", "queue": "another queue plays",
        "rebuild": "the queue was taken afresh", "start": "a newer start came first",
        "dsp": "a newer DSP change came first", "switch": "the output was switched away",
        "attach": "HQPlayer is the output again"}


@dataclass
class Item:
    """What a command sequence serves: its kind, the number of the intent it
    came from, and — for a resume — the queue generation it was made for."""
    kind: str
    n: int
    queue: Any = None
    generation: Optional[int] = None


class BuildWatch:
    """A link's build watch. HQPlayer answers nothing on its control port
    while it builds a setting — a filter, a modulator, a rate, every start —
    and carries out what was sent meanwhile once it is done. The link's
    client meets a build as a late answer (BUILDING_AFTER_S) to one of
    BUILD_COMMANDS or to the <Status/> held after a Set* (change_dsp), and
    waits it out up to BUILD_LIMIT_S; the build ends where that answer comes.
    A new connection says nothing — HQPlayer accepts and starts them all
    through a build — and the meter's first frame comes after the answer.
    While it lasts, `state` says since when and after which command: the
    link refuses commands and questions (Building), background work waits for
    its end (through), and the player status carries it. Past BUILD_LIMIT_S
    it is `unfinished` until HQPlayer answers the poller again. A held answer
    whose connection breaks (EOF, a reset, keepalive or TCP_USER_TIMEOUT)
    ends no build: HQPlayer went away or restarted, and the poller asks
    again and reads its return as any other. An Embedded box answers all
    through its builds, saying it plays with nothing out yet
    (hqp_benchmark._building): the poller reports that as a build too, with
    nothing to refuse."""

    after = BUILDING_AFTER_S
    commands = BUILD_COMMANDS

    def __init__(self, link: "HqpLink") -> None:
        self._link = link
        self._lock = threading.Lock()
        # Every change wakes the background work waiting a build out (wait_out)
        self._changed = threading.Condition(self._lock)
        self.state: Optional[dict] = None
        # The latest DSP change that went through a build — its outcome on the
        # player status, for a refusal its caller was not there to hear: an
        # id no restart repeats, and the attachment it was made in
        self.last_change: Optional[dict] = None
        # The builds begun so far, when the latest began, and how the latest
        # to end ended: (its number, how)
        self._builds = 0
        self._began = 0.0
        self._ended: tuple = (0, None)
        # What waits to hear a build begin (waking)
        self._wake_on_build: set = set()

    def begin(self, command: str, attributes: Dict[str, str]) -> Callable[[str], None]:
        """A command's answer is late: HQPlayer builds. Told by the client on
        the thread that waits for the answer, holding the connection."""
        with self._lock:
            self._builds += 1
            n, self._began = self._builds, time.monotonic()
            self.state = {"since": time.time(), "command": command, "silent": True}
            wake = list(self._wake_on_build)
            self._changed.notify_all()
        logger.info("HQPlayer is building (after %s) — commands wait for it", command)
        for event in wake:
            event.set()
        self._link._build_began()
        return lambda how: self._end(n, how)

    def _end(self, n: int, how: str) -> None:
        with self._lock:
            if self.state is None or self._link.state == "closed":
                return
            since = self.state["since"]
            self.state = {**self.state, "unfinished": True} if how == "timeout" else None
            self._ended = (n, how)
            self._changed.notify_all()
        if how == "answered":
            logger.info("HQPlayer answered again after building %.1f s", time.time() - since)
        elif how == "timeout":
            logger.warning("HQPlayer did not finish building the filter")
        else:
            logger.warning("HQPlayer's connection broke during the build — it went away "
                           "or restarted, and is asked again")
        self._link._build_ended(how)

    def answering(self, building: bool) -> None:
        """The poller's reading of an Embedded box: building while it says it
        plays with nothing out yet, over as soon as it does not."""
        with self._lock:
            if building and self.state is None:
                self.state = {"since": time.time(), "command": None, "silent": False}
            elif not building and self.state is not None and not self.state["silent"]:
                self.state = None

    def answered(self) -> None:
        """HQPlayer answered the poller: a build it did not finish is over."""
        with self._lock:
            if self.state is not None and self.state.get("unfinished"):
                self.state = None

    def silent(self) -> Optional[dict]:
        """The build HQPlayer is silent in — one it did not finish included
        — if one is on; not an Embedded box's answering build."""
        state = self.state
        return state if state is not None and state["silent"] else None

    def building(self) -> Optional[dict]:
        """The silent build in progress, not one past the limit."""
        state = self.silent()
        return state if state is not None and not state.get("unfinished") else None

    def refuse(self) -> None:
        state = self.silent()
        if state is not None:
            raise Building(self._link, state)

    def wake_waiters(self) -> None:
        """What background work waits on beside the build changed — the
        output was detached, the link let go: each wait looks again."""
        with self._lock:
            self._changed.notify_all()

    def wait_out(self, until: Callable[[], bool] = lambda: False) -> Optional[GaveUp]:
        """Where background work refused by a build (Building) waits for its
        end: the watch's event, never a poll, bounded by the build's own
        limit. None once the wait is over — HQPlayer answered again, or
        `until` says the work need not wait (it goes elsewhere now, or was
        called off) — else why the work gives up: HQPlayer did not finish
        building or went away during the build (failed), or the link was let
        go. `until` is asked under the watch's lock: cheap and lock-free."""
        with self._lock:
            n = self._builds
            deadline = self._began + BUILD_LIMIT_S + self.after
            while self._ended[0] < n and not until() and self._link._waitable():
                left = deadline - time.monotonic()
                if left <= 0:
                    return GaveUp(UNFINISHED, failed=True)
                self._changed.wait(left)
            if not self._link._waitable():
                return GaveUp(self._link.why or "HQPlayer was let go", failed=False)
            if self._ended[0] < n:
                return None
            why = _ENDS.get(self._ended[1])
            return GaveUp(why, failed=True) if why else None

    @contextmanager
    def waking(self, event: threading.Event):
        """`event` is set when a build begins while the block runs — the DSP
        change answering its caller at once."""
        with self._lock:
            self._wake_on_build.add(event)
        try:
            yield
        finally:
            with self._lock:
                self._wake_on_build.discard(event)

    def report(self, applied: dict, failed: dict, attachment: Optional[int]) -> None:
        """A DSP change that went through a build: its outcome rides the
        player status (`dsp_change`) of the attachment it was made in."""
        with self._lock:
            self.last_change = {"id": uuid.uuid4().hex, "attachment": attachment,
                                "applied": applied, "failed": failed}
        self._link._poke()


class _CommandLock:
    """A link's command lock: one command sequence at a time — and none
    while HQPlayer builds. A caller is refused at once (Building), never
    parked for minutes behind the command that met the build; one already
    parked is woken and refused when the build begins. A detach goes on
    without it while a build holds it (detaching)."""

    def __init__(self, link: "HqpLink") -> None:
        self._link = link
        self._cond = threading.Condition()
        self._held = False
        self.owner: Optional[int] = None

    def __enter__(self) -> "_CommandLock":
        with self._cond:
            while True:
                self._link._check_state()
                self._link.build.refuse()
                if not self._held:
                    self._held = True
                    self.owner = threading.get_ident()
                    return self
                self._cond.wait()

    def __exit__(self, *exc) -> bool:
        with self._cond:
            self._held = False
            self.owner = None
            self._cond.notify_all()
        return False

    def wake(self) -> None:
        with self._cond:
            self._cond.notify_all()

    @contextmanager
    def detaching(self):
        """For a backend's detach — an output switch, the benchmark's hold,
        the app stopping: the lock once the command on its way is answered,
        never a build's wait. With a build on, or one that begins while this
        waits, the detach goes on without the lock: the build stays watched,
        and the items the sequence waiting it out serves are superseded by
        the switch (SUPERSEDED_BY)."""
        with self._cond:
            while (self._held and self._link.build.silent() is None
                   and self._link.state != "closed"):
                self._cond.wait()
            took = not self._held
            if took:
                self._held = True
                self.owner = threading.get_ident()
        try:
            yield
        finally:
            if took:
                self.__exit__()


class _LateStop:
    """The Stop an output switch owes HQPlayer while it builds and answers
    nothing: the switch goes on at once, and HQPlayer — undriven beside the
    new output — stops once the build is done, if it plays or pauses an
    entry of ours then. It outlives the backend that owed it and a retiring
    link; a newer attach of the link supersedes it, and a closed link drops
    it — a stopping node leaves HQPlayer as it is."""

    def __init__(self, link: "HqpLink", item: Item, ours: Callable[[Any], bool]):
        self.link = link
        self.item = item
        self.ours = ours

    def _send(self) -> bool:
        with self.link.command(item=self.item):
            return self.link._stop_if_ours(self.ours)

    def run(self) -> None:
        link = self.link
        link._local.late = True
        try:
            if through(self._send, needed=lambda: link.superseded(self.item),
                       until=lambda: link.superseded(self.item) is not None):
                logger.info("HQPlayer stopped once its build was done — the output had been "
                            "switched away")
        except GaveUp as e:
            (logger.warning if e.failed else logger.info)(
                "HQPlayer not stopped after the output switch: %s", e)
        except Dropped as e:
            logger.info("HQPlayer not stopped after the output switch: %s", e)
        except (BrokenPipeError, ConnectionError, OSError) as e:
            logger.warning("HQPlayer not stopped after the output switch: %s", e)
        finally:
            link._local.late = False
            with link._state_lock:
                if link._late_stop is self:
                    link._late_stop = None
            link._close_if_retired()


class HqpLink:
    """Everything this node says to one HQPlayer (see the module docstring).
    Its address is fixed: another address is another link."""

    def __init__(self, key: tuple, host: str, port: int):
        self.key = key
        self.host = host
        self.port = port
        self.build = BuildWatch(self)
        self.lock = _CommandLock(self)
        self.client = HQPlayerClient(host=host, port=port, timeout=COMMAND_WAIT_S,
                                     waits=_command_waits(), late=self.build, gate=self)
        self.client.on_outcome = self._route_outcome
        # open → retiring (another HQPlayer was chosen) → closed
        self.state = "open"
        self.why: Optional[str] = None
        self.lent = False
        # The backend driving this HQPlayer, while one is attached: hears the
        # answers (its playback trace), the build's changes (its poller) —
        # and the number its attach took
        self.attached = None
        self.attachment: Optional[int] = None
        self._state_lock = threading.Lock()
        self._late_stop: Optional[_LateStop] = None
        # The connection's owner (a thread id) and how deep it holds it
        self._conn = threading.Condition()
        self._owner: Optional[int] = None
        self._depth = 0
        self._local = threading.local()
        self._clock = 0
        self._latest: Dict[str, int] = {}
        self._unreachable_until = 0.0

    def __repr__(self) -> str:
        return f"HqpLink({self.host}:{self.port}, {self.state})"

    # -- the intent clock -------------------------------------------------------

    def accept(self, kind: str) -> int:
        """An intent this link accepts takes the next number of its clock;
        background work waiting a build out looks again whether it is
        still wanted."""
        with self._state_lock:
            self._clock += 1
            self._latest[kind] = n = self._clock
        self.build.wake_waiters()
        return n

    def item(self, kind: str, n: int, *, queue: Any = None,
             generation: Optional[int] = None) -> Item:
        """What a command sequence serves, made for the intent numbered `n`.
        A start-like item is the newest start from the moment it is made: an
        older one still waiting is superseded by it."""
        if kind in ("start", "resume"):
            self._mark("start", n)
        return Item(kind, n, queue, generation)

    def _mark(self, kind: str, n: int) -> None:
        with self._state_lock:
            self._latest[kind] = max(self._latest.get(kind, 0), n)

    def superseded(self, item: Item) -> Optional[str]:
        """Why `item` must not be sent any more, or None."""
        with self._state_lock:
            for kind in SUPERSEDED_BY[item.kind]:
                if self._latest.get(kind, 0) > item.n:
                    return _WHY[kind]
        if item.generation is not None and item.queue.generation != item.generation:
            return _WHY["queue"]
        return None

    # -- the connection -----------------------------------------------------------

    def _waitable(self) -> bool:
        """May work wait on this link? Not once it closed; a retiring link
        waits only for its late Stop."""
        return self.state == "open" or (self.state == "retiring"
                                        and getattr(self._local, "late", False))

    def _check_state(self) -> None:
        """Nothing goes out on a closed or lent link, and on a retiring one
        only the Stop it owes (a late_stop item): the new HQPlayer's link
        mirrors the whole queue at its attach."""
        if self.state == "closed":
            raise Dropped(self.why or "HQPlayer was let go")
        if self.lent:
            raise Dropped("HQPlayer is lent to the benchmark")
        if self.state == "retiring":
            item = getattr(self._local, "item", None)
            if item is None or item.kind != "late_stop":
                raise Dropped(self.why or "another HQPlayer was chosen")

    def _check_item(self) -> None:
        item = getattr(self._local, "item", None)
        if item is not None:
            why = self.superseded(item)
            if why is not None:
                raise Dropped(why)

    def _take(self) -> None:
        """Hold the connection: the caller is its only user until _give.
        Waits while another holds it, refused once that turns into a build
        (past the build limit questions go again: HQPlayer may be back)."""
        me = threading.get_ident()
        with self._conn:
            while True:
                self._check_state()
                if self._owner == me:
                    self._depth += 1
                    return
                state = self.build.building()
                if state is not None:
                    raise Building(self, state)
                if self._owner is None:
                    self._owner, self._depth = me, 1
                    return
                self._conn.wait()

    def _give(self) -> None:
        with self._conn:
            self._depth -= 1
            if self._depth:
                return
            if self.attached is None and self._late_stop is None and not self._selected():
                self.client.disconnect()         # a probe's link keeps no socket
            self._owner = None
            self._conn.notify_all()

    @contextmanager
    def asking(self):
        """The connection for this thread's questions — the poller's tick,
        the HQPlayer screen's reads, a probe: nothing else goes on it
        meanwhile, so what the client says of a failure is this caller's."""
        self._take()
        try:
            yield self.client
        finally:
            self._give()

    @contextmanager
    def request(self, command: str):
        """The client's gate, around every request it sends: the connection
        (already held by this thread's command sequence or question, else
        held for this one request), the link's state and the item this thread
        serves checked right before the bytes go, a dropped socket
        reconnected."""
        self._take()
        try:
            self._check_state()
            if self.lock.owner == threading.get_ident():
                # A sequence whose own build HQPlayer did not finish sends
                # nothing more into its silence
                self.build.refuse()
            self._check_item()
            self._connect()
            yield
        finally:
            self._give()

    def _connect(self) -> None:
        """A live socket for the holder: one the peer closed while idle is
        found before the send (a peek), a dropped one reconnected; a failed
        connect opens the breaker for CIRCUIT_COOLDOWN_S."""
        c = self.client
        sock = c.socket
        if sock is not None:
            try:
                if select.select([sock], [], [], 0)[0] and not sock.recv(1, socket.MSG_PEEK):
                    logger.info("HQPlayer at %s:%d closed the connection — reconnecting",
                                self.host, self.port)
                    c.disconnect()
            except OSError:
                c.disconnect()
        if c.socket is not None:
            return
        if time.monotonic() < self._unreachable_until:
            raise ConnectionError(f"HQPlayer at {self.host}:{self.port} unreachable (circuit open)")
        if not c.connect():
            self._unreachable_until = time.monotonic() + CIRCUIT_COOLDOWN_S
            raise ConnectionError(f"Cannot connect to HQPlayer at {self.host}:{self.port}")
        self._unreachable_until = 0.0

    def reset(self) -> None:
        """A fresh socket for the next request (a retry after a broken one)."""
        self._take()
        try:
            self.client.disconnect()
        finally:
            self._give()

    @contextmanager
    def command(self, intent: Optional[str] = None, *, item: Any = None):
        """A command sequence: the command lock (refused while HQPlayer
        builds — Building), then the connection held to its end. `intent`, a
        person's or this node's, is accepted once the lock is ours and takes
        the clock's next number (yielded). `item` is what the sequence
        serves — an Item made earlier (a start left for later, a resume, an
        owed Stop), or a kind, made here for `intent`: each of its requests
        is dropped once a newer intent supersedes it (Dropped)."""
        outer = getattr(self._local, "item", None)
        if isinstance(item, Item):
            self._local.item = item
        try:
            with self.lock:
                self._take()
                try:
                    n = self.accept(intent) if intent is not None else getattr(item, "n", None)
                    if isinstance(item, str):
                        self._local.item = self.item(item, n)
                    yield n
                finally:
                    self._give()
        finally:
            self._local.item = outer

    def ask_raw(self, xml: str, wait: float) -> bytes:
        """One raw request — the library's LibraryGet, its hash, GetInfo for a
        registration — answered as the line of bytes HQPlayer sent."""
        with self.asking() as c:
            return c.raw(xml, wait)

    # -- the build's moments --------------------------------------------------------

    def _build_began(self) -> None:
        self.lock.wake()
        with self._conn:
            self._conn.notify_all()
        self._poke()

    def _build_ended(self, how: str) -> None:
        b = self.attached
        if how == "lost" and b is not None:
            b._lost_sight()
        self._poke()

    def _poke(self) -> None:
        b = self.attached
        if b is not None:
            b.poke()

    def _route_outcome(self, outcome) -> None:
        b = self.attached
        if b is not None:
            b._on_outcome(outcome)

    # -- life cycle ------------------------------------------------------------------

    def attach(self, backend) -> int:
        """`backend` drives this HQPlayer now: an attach intent, which
        supersedes the Stop a switch away owed it. Returns its number — the
        attachment."""
        self.attached = backend
        self.attachment = self.accept("attach")
        return self.attachment

    def detach(self, backend) -> None:
        """`backend` drives this HQPlayer no more: a switch away, which
        supersedes what was made for its attachment — a start or a resume
        left for later, the rest of a DSP change."""
        if self.attached is backend:
            self.attached = self.attachment = None
            self.build.last_change = None    # the outcome of a change was that attachment's
            self.accept("switch")
        self._close_if_retired()

    def release(self, ours: Callable[[Any], bool]) -> None:
        """The output is switched away from this HQPlayer: it stops if it
        plays or pauses an entry of ours — at once, or, while it builds, once
        the build is done (the late Stop): the switch does not wait for it.
        Past the build limit it is left as it is."""
        stop = self.item("late_stop", self.accept("switch"))
        try:
            with self.command(item=stop):
                self._stop_if_ours(ours)
        except Building as e:
            if e.state.get("unfinished"):
                logger.warning("HQPlayer not stopped at the output switch: %s", e)
                return
            with self._state_lock:
                # One owed by an earlier switch was superseded by the attach since
                late = self._late_stop = _LateStop(self, stop, ours)
            threading.Thread(target=late.run, daemon=True, name="hqp-late-stop").start()

    def _stop_if_ours(self, ours: Callable[[Any], bool]) -> bool:
        """Stop, if HQPlayer plays or pauses an entry of ours; on a held
        connection."""
        c = self.client
        status = c.get_status(wait=QUESTION_WAIT_S)
        if status is None or status.state not in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            return False
        if not ours(status):
            logger.info("HQPlayer plays something that is not ours — left playing")
            return False
        if c.stop():
            return True
        logger.warning("HQPlayer did not take the Stop: %s", redact_uri(c.refusal()))
        return False

    def lend(self) -> None:
        """Lent to the benchmark, which drives HQPlayer on its own connection:
        nothing of this link goes out meanwhile."""
        self.lent = True
        with self._conn:
            self._conn.notify_all()
        self.build.wake_waiters()

    def reclaim(self) -> None:
        self.lent = False

    def retire(self, why: str) -> None:
        """Another HQPlayer was chosen: the backend still attached here sends
        what its detach needs, a late Stop owed here runs, and then the link
        closes; everything else gives up with `why`."""
        with self._state_lock:
            if self.state != "open":
                return
            self.state, self.why = "retiring", why
        with self._conn:
            self._conn.notify_all()
        self.lock.wake()
        self.accept("switch")
        self._close_if_retired()

    def unretire(self) -> None:
        """Chosen again before it closed: the same HQPlayer, the same link."""
        with self._state_lock:
            if self.state == "retiring":
                self.state, self.why = "open", None

    def _close_if_retired(self) -> None:
        if self.state == "retiring" and self.attached is None and self._late_stop is None:
            self.close(self.why or "another HQPlayer was chosen")

    def close(self, why: str) -> None:
        """Cancel everything: waiters and owed items give up with `why`, and
        the socket is shut down and closed — a read blocked on it ends on
        every platform."""
        with self._state_lock:
            if self.state == "closed":
                return
            self.state, self.why = "closed", why
            self._late_stop = None
        with self._conn:
            self._conn.notify_all()
        self.lock.wake()
        self.build.wake_waiters()
        sock = self.client.socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass        # the peer went first: the read has ended on its own
            sock.close()

    def _selected(self) -> bool:
        return self.key == _key(settings.hqplayer_host, settings.hqplayer_port)

    # -- the play gate ----------------------------------------------------------------

    def probe(self) -> Optional[str]:
        """The play gate: protocol-level liveness at play-intent time — None
        once a GetInfo is answered within PROBE_WAIT_S naming a product, else
        why HQPlayer is offline (refused, reset, closed, an answer naming no
        product). A bare TCP connect proves nothing — HQPlayer Embedded's
        trial stop keeps the port open and closes every connection at once
        (seen live), and HQPlayer Desktop listens only once its audio engine
        is up. A build — one in progress or past the limit — is busy
        (Building), and so is a connection taken and left unanswered
        (NotAnswering)."""
        self.build.refuse()
        try:
            with self.asking() as c:
                info = c.get_info(wait=PROBE_WAIT_S)
                silent = info is None and c.timed_out
                why = c.refusal()
        except (Building, Dropped):
            raise
        except (BrokenPipeError, ConnectionError, OSError) as e:
            return str(e)
        if info is not None and info.get("product"):
            return None
        if silent:
            raise NotAnswering()
        return why if info is None else "answered without naming a product"


# -- background work through a build ------------------------------------------------

def through(action: Callable[[], Any], *, needed: Callable[[], Optional[str]] = lambda: None,
            until: Callable[[], bool] = lambda: False) -> Any:
    """Background work's one way through a build — a filler's add, the Stop
    an output switch left, HQPlayer following a re-bind, the resume after a
    DSP change, a start left behind a build: `action` refused because
    HQPlayer builds (Building) waits for the end of that link's build
    (BuildWatch.wait_out, cut short when `until` says so) holding no lock,
    and goes again once `needed` finds nothing against it — the queue moved
    on, the output changed. A build HQPlayer did not finish, or went away
    during, gives the work up (GaveUp, `failed`, with why); so does a link
    let go, or `needed`'s reason (not failed). A play intent is refused at
    once instead (409): someone is waiting for it."""
    while True:
        why = needed()
        if why is not None:
            raise GaveUp(why, failed=False)
        try:
            return action()
        except Building as e:
            gave_up = e.link.build.wait_out(until)
            if gave_up is not None:
                raise gave_up


# -- the registry --------------------------------------------------------------------

_links: Dict[tuple, HqpLink] = {}
_registry_lock = threading.Lock()
# Why the node let every link go (close_all): a link asked for after it is
# closed from the start — a hold's release re-attaching the output while the
# node stops sends nothing.
_closed_why: Optional[str] = None


def _key(host: str, port: int) -> tuple:
    import hqp_library
    return hqp_library.address_key(host, port)


def link_for(host: str, port: int) -> HqpLink:
    """The link of the HQPlayer at this address — this machine's aliases are
    one — made on first use; a closed one is made afresh."""
    key = _key(host, port)
    with _registry_lock:
        link = _links.get(key)
        if link is None or link.state == "closed":
            link = _links[key] = HqpLink(key, host, port)
            if _closed_why is not None:
                link.close(_closed_why)
        return link


def selected() -> HqpLink:
    """The link of the HQPlayer the owner chose (settings' address)."""
    return link_for(settings.hqplayer_host, settings.hqplayer_port)


def choose(host: str, port: int) -> HqpLink:
    """The owner chose the HQPlayer at this address: its link — the same one,
    if it was chosen before and has not closed yet — and every other open
    link retires."""
    link = link_for(host, port)
    link.unretire()
    with _registry_lock:
        others = [ln for ln in _links.values() if ln is not link and ln.state == "open"]
    for other in others:
        other.retire("another HQPlayer was chosen")
    return link


def close_all(why: str) -> None:
    """The node is stopping: every link closes — its waiters and owed items
    give up, its socket is shut and closed — and so does the meter's."""
    global _closed_why
    with _registry_lock:
        _closed_why = why
        links = list(_links.values())
    for link in links:
        link.close(why)
    from playback import hqp_meter
    hqp_meter.meter.detach()
