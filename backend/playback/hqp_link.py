"""
The HQPlayer link (2026-10-12): everything this node says to one
HQPlayer goes through its link — one per hqp_library.address_key, so this
machine's aliases are one link.

A link owns:
  * one connection to its own address (an HQPlayerClient whose requests all
    pass `request`, its gate), taken by each request for itself, from its
    send to its answer, in the order they came: a question goes between the
    requests of a command sequence. So at most one request from this node is
    outstanding at an HQPlayer — a command, the status poller, the HQPlayer
    screen, the play gate, the library, the assistants — and nothing reaches
    it while it builds but the request whose answer is held. HQPlayer's one
    control thread answers in order anyway: two connections only sent
    requests into each other's silences. After a command whose silence may
    follow it (HANDOVER_COMMANDS: what sets off a build, a Stop's teardown)
    the connection passes to the sequence's next request, or to a Status the
    link holds itself: that silence never falls on a question with a short
    wait. A request left unanswered ends its sequence or question block: the
    rest of it is refused (NotAnswering), never sent into the same silence;
  * the command lock: one command sequence at a time, in the order they
    came; a person's command is refused at once while HQPlayer builds
    (Building, 409) or answers nothing past a short silence's wait
    (NotAnswering), never parked behind it; background work waits a build
    out (through);
  * its build watch (BuildWatch): HQPlayer answers nothing while it builds a
    setting, and the late answer of the command that set the build off is
    the build's end;
  * the intent clock: what a person or this node asked of it, in order. The
    order of intents is the order of the asks — each is numbered as it is
    asked — and an intent supersedes only from the moment its command
    sequence holds the command lock: one refused as busy takes none and
    supersedes nothing. A new queue and a rebuild supersede from their ask
    (a play press's hand-over is cut short at its next add) and withdraw
    that if the lock refuses them. An item — a start left for later, a
    resume, the rest of a DSP change, a Stop an output switch owed — is
    never sent once a newer intent of a kind that supersedes it holds
    (SUPERSEDED_BY), checked right before the bytes go;
  * the Stop a person's switch away owes it (the late Stop): sent at once,
    or once HQPlayer answers through a build or a silence.

Life cycle: link_for creates a link on first use; the same HQPlayer keeps its
link; choosing another HQPlayer (choose) retires the old one — what its
detaching backend still sends and its late Stop go, then it closes, and
everything else it held gives up with why; the node stop closes every link
(close_all): every wait on HQPlayer — a read, a connect — is woken through its
client's wake channel and closes its own socket (a socket closed by a thread
that does not read it races the one that does: hqplayer_client.Waker), an
idle one is closed, and every waiter released.
"""

import logging
import socket
import threading
import time
import uuid
from collections import deque
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
# on a slower output. A short silence, never a build: more than four times
# the longest one measured, with room for outputs not measured (a Pi's ALSA,
# Windows). Past it, HQPlayer is not answering.
STOP_ANSWER_S = 15.0
# A command's answer otherwise, and a question's.
COMMAND_WAIT_S = 5.0
QUESTION_WAIT_S = 4.0
# The play gate's GetInfo: a live HQPlayer answers it at once.
PROBE_WAIT_S = 2.0
# What a silence may follow: the connection passes from one of them to the
# sequence's next request, or to a Status the link holds itself — never to
# a question with a short wait.
HANDOVER_COMMANDS = frozenset({"SetMode", "SetRate", "SetFilter", "SetShaping", "MatrixSetProfile",
                               "SetConvolution", "Play", "SelectTrack", "Next", "Previous",
                               "Stop", "PlaylistClear"})
# Of them, the silences that are no build: the output torn down, the playlist
# dropped around the entry read — waited out up to STOP_ANSWER_S.
SHORT_SILENCE_COMMANDS = frozenset({"Stop", "PlaylistClear"})
# What may set off a build, or meet the silence of one set off a moment
# before (a Seek right after the SelectTrack that starts a stopped HQPlayer):
# a late answer to one of them is a build.
BUILD_COMMANDS = (HANDOVER_COMMANDS - SHORT_SILENCE_COMMANDS) | {"Seek"}
# A failed connect fails the next ones fast for this long: a stalled control
# port would otherwise make every reconnect wait out the connect timeout.
CIRCUIT_COOLDOWN_S = 6.0

UNFINISHED = "HQPlayer did not finish building the filter — restart HQPlayer"
BUILDING = "HQPlayer is building the filter — it takes commands again once it is done"
LOST = "HQPlayer went away during the build"
NOT_ANSWERING = "HQPlayer is not answering"
_ENDS = {"timeout": UNFINISHED, "lost": LOST}
# What a switch away tells the person when its Stop stays owed
STOP_OWED_BUILDING = ("HQPlayer is building the filter; if it still plays our track once the "
                      "build is done, Sautium stops it then")
STOP_OWED_SILENT = ("HQPlayer is not answering; if it still plays our track, Sautium stops it "
                    "as soon as it answers")


def _command_waits() -> Dict[str, float]:
    """How long a link's client waits for each answer, read when the link is
    made: a short silence's, and the build limit for what may set off a
    build — HQPlayer carries out a command given up on all the same, after
    the build, out of order."""
    return {**{c: STOP_ANSWER_S for c in SHORT_SILENCE_COMMANDS},
            **{c: BUILD_LIMIT_S for c in BUILD_COMMANDS}}


class Building(ConnectionError):
    """Refused while HQPlayer builds a setting (409): it answers nothing
    until the build is done, and a command sent into the silence would be
    carried out after it, out of order. Past the build limit the build is
    `unfinished`, and the person is told to restart HQPlayer. While the
    Status a switch away holds goes unanswered (the late Stop's quiet watch)
    it is no build of ours: the words are NOT_ANSWERING."""

    def __init__(self, link: "HqpLink", state: dict):
        super().__init__(NOT_ANSWERING if state.get("not_answering")
                         else UNFINISHED if state.get("unfinished") else BUILDING)
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
# link; attach — this link attached again. A play press's own rebuild
# ("press") goes whatever the person says meanwhile — their words decide
# only which start follows it — and only a new queue or a switch away drops
# it.
SUPERSEDED_BY = {
    "start": frozenset({"transport", "queue", "rebuild", "start", "switch"}),
    "resume": frozenset({"transport", "queue", "rebuild", "start", "dsp", "switch"}),
    "press": frozenset({"queue", "switch"}),
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
    it is `unfinished` until HQPlayer answers a request again. A held answer
    whose connection breaks (EOF, a reset, keepalive or TCP_USER_TIMEOUT)
    ends no build: HQPlayer went away or restarted, and the poller asks
    again and reads its return as any other. It watches the Status the late
    Stop holds the same way, quietly: a silence there is no build of ours —
    said as not answering, never building — and past the build limit
    HQPlayer is not answering. An Embedded box answers all
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
        self._ended: tuple = (0, None, False)
        # What waits to hear a build begin (waking)
        self._wake_on_build: set = set()

    def begin(self, command: str, attributes: Dict[str, str]) -> Callable[[str], None]:
        """A command's answer is late: HQPlayer builds. Told by the client on
        the thread that waits for the answer, holding the connection."""
        # The Status a switch away holds (the late Stop): a silence no build
        # of ours explains — HQPlayer not answering, never "building"
        quiet = getattr(self._link._local, "late", False)
        with self._lock:
            self._builds += 1
            n, self._began = self._builds, time.monotonic()
            self.state = {"since": time.time(), "command": command, "silent": True}
            if quiet:
                self.state["not_answering"] = True
            wake = list(self._wake_on_build)
            self._changed.notify_all()
        if quiet:
            logger.info("HQPlayer is not answering the Status a switch away holds — commands "
                        "wait for its answer")
        else:
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
            quiet = bool(self.state.get("not_answering"))
            self.state = ({**self.state, "unfinished": True}
                          if how == "timeout" and not quiet else None)
            self._ended = (n, how, quiet)
            self._changed.notify_all()
        if quiet:
            if how == "timeout":
                self._link._not_answering("the Status a switch away held", BUILD_LIMIT_S)
            self._link._build_ended(how)
            return
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
        """HQPlayer answered a request on the link — anyone's: a build it did
        not finish is over."""
        with self._lock:
            if self.state is not None and self.state.get("unfinished"):
                self.state = None

    @property
    def count(self) -> int:
        """The builds begun so far: one begun during a request means its
        silence was a build's, watched — never a silence of its own."""
        return self._builds

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
            _, how, quiet = self._ended
            why = NOT_ANSWERING if quiet and how == "timeout" else _ENDS.get(how)
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
    """A link's command lock: one command sequence at a time, in the order
    they came — and none while HQPlayer builds, or answers nothing past a
    short silence. A caller
    is refused at once (Building, NotAnswering), never parked for minutes
    behind the command that met it; one already parked is woken and refused
    when it begins. A command behind a short silence (a Stop's teardown)
    waits for it. A detach goes on without the lock while a build, a
    teardown or the Stop a switch away owes holds it (detaching)."""

    def __init__(self, link: "HqpLink") -> None:
        self._link = link
        self._cond = threading.Condition()
        self._held = False
        self.owner: Optional[int] = None
        # Held by the Stop a switch away owes (_LateStop): the link's own
        # work, which outlives the backend — a detach never waits for it
        self._late = False
        # Who waits for it, in the order they came: a volume change pressed
        # while a new queue is handed over goes once the hand-over is done,
        # before anything asked after it
        self._waiting: deque = deque()

    def __enter__(self) -> "_CommandLock":
        me = threading.get_ident()
        with self._cond:
            self._waiting.append(me)
            try:
                while True:
                    self._link._check_state()
                    self._link.refuse()
                    if not self._held and self._waiting[0] == me:
                        self._held = True
                        self.owner = me
                        self._late = getattr(self._link._local, "late", False)
                        return self
                    self._cond.wait()
            finally:
                self._waiting.remove(me)
                self._cond.notify_all()         # the next in line looks again

    def __exit__(self, *exc) -> bool:
        with self._cond:
            self._held = False
            self.owner = None
            self._late = False
            self._cond.notify_all()
        return False

    def wake(self) -> None:
        with self._cond:
            self._cond.notify_all()

    @contextmanager
    def detaching(self):
        """For a backend's detach — an output switch, the benchmark's hold,
        the app stopping: the lock once the command on its way is answered,
        never a build's wait, nor a teardown's (a Stop or a PlaylistClear of
        ours HQPlayer answers once its output is down — sent, or waiting its
        turn on the connection), nor the Stop a switch away owes. With a
        build on, or one that begins while this waits, such a teardown, or
        that Stop holding the lock — its Status and its answer — the detach
        goes on without the lock: the build stays watched, the Stop is the
        link's own work, and what the sequence waiting it out serves is
        superseded by a person's switch, or refused once the link is lent or
        closed."""
        with self._cond:
            while (self._held and not self._late and self._link.teardown is None
                   and self._link.teardown_due is None
                   and self._link.build.silent() is None
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
    """The Stop an output switch owes HQPlayer — the one a switch away
    sends, on its own thread; the switch waits for its first outcome (the
    Stop sent, or not needed) at most BUILDING_AFTER_S, never more. HQPlayer
    — undriven beside the new output — stops if it plays or pauses an entry
    of what it was given before the switch (`ours`), once it answers: at
    once, after a build it is in, or after a silence no build explains — its
    Status held up to the build limit, as a build's answer is, and nothing
    else of this node sent meanwhile. No answer by then: no Stop. It
    outlives the backend that owed it and a retiring link; a newer attach
    of the link supersedes it, a closed link drops it — a stopping node
    leaves HQPlayer as it is — and a connection that broke drops it, with no
    reconnect for it."""

    def __init__(self, link: "HqpLink", item: Item, ours: Callable[[Any], bool]):
        self.link = link
        self.item = item
        self.ours = ours
        self.decided = threading.Event()

    def _send(self) -> bool:
        with self.link.command(item=self.item):
            return self.link._stop_if_ours(self.ours, self.decided)

    def run(self) -> None:
        link = self.link
        link._local.late = True
        try:
            if background("HQPlayer not stopped after the output switch", self._send,
                          needed=lambda: link.superseded(self.item),
                          until=lambda: link.superseded(self.item) is not None):
                logger.info("HQPlayer stopped — the output had been switched away")
        finally:
            self.decided.set()
            link._local.late = False
            with link._state_lock:
                if link._late_stop is self:
                    link._late_stop = None
            link._close_if_retired()


def _closed(sock: socket.socket) -> bool:
    """Has this idle connection ended — the peer closed it, or it was shut
    under us? A peek that waits for nothing, with no select: no descriptor
    is too large to look at, and a closed socket is an answer, never a
    ValueError."""
    try:
        timeout = sock.gettimeout()
        sock.setblocking(False)
        try:
            return sock.recv(1, socket.MSG_PEEK) == b""
        finally:
            sock.settimeout(timeout)
    except BlockingIOError:
        return False            # nothing to read: alive
    except OSError:
        return True


class _Scope:
    """A command sequence's or a question block's hold on the link
    (HqpLink.command, HqpLink.asking): once a request of it went unanswered
    with no build of ours behind the silence, the rest of it is refused."""

    __slots__ = ("silent",)

    def __init__(self) -> None:
        self.silent = False


# The slot passed on to the Status a link holds itself, before its thread
# takes it (HqpLink._end_scope)
_HELD = object()


class HqpLink:
    """Everything this node says to one HQPlayer (see the module docstring).
    Its identity is fixed (`key`, this machine's aliases one); the address
    it dials is the alias chosen last (readdress)."""

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
        # The files this HQPlayer refused while it took the rest of their
        # batch, as (queue slot, the way in it refused): gaps of this output
        # until its next full mirror — kept by the link, which every backend
        # attached to this HQPlayer shares, never by one of them
        self.refusals: list = []
        self._state_lock = threading.Lock()
        self._late_stop: Optional[_LateStop] = None
        # The connection: the thread whose request holds it, the threads
        # waiting for it in the order they came, and the one a hand-over
        # left it to (a thread id, or _HELD) with the command that did
        self._conn = threading.Condition()
        self._owner: Optional[int] = None
        self._waiting: deque = deque()
        self._reserved: Any = None
        self._after: Optional[str] = None
        # Since when HQPlayer has answered nothing past a short silence's
        # wait: people's commands are refused, questions go on, and its next
        # answer ends it
        self.silent_since: Optional[float] = None
        # The command whose short silence the request on the connection may
        # be in (a Stop's teardown — SHORT_SILENCE_COMMANDS): no detach waits
        # for it — nor for one waiting its turn on the connection (teardown_due,
        # set and cleared by that request alone)
        self.teardown: Optional[str] = None
        self.teardown_due: Optional[str] = None
        self._local = threading.local()
        self._clock = 0
        # The newest number of each kind that holds, and the early ones still
        # asking for the command lock: number → kind
        self._latest: Dict[str, int] = {}
        self._pending: Dict[int, str] = {}
        self._unreachable_until = 0.0

    def __repr__(self) -> str:
        return f"HqpLink({self.host}:{self.port}, {self.state})"

    # -- the intent clock -------------------------------------------------------

    def accept(self, kind: str) -> int:
        """An intent this link takes at once — a word a play press or a
        queue mutation holds, the press itself, a switch, an attach — takes
        the next number of its clock and holds from now (publish)."""
        n = self.reserve()
        self.publish(kind, n)
        return n

    def reserve(self) -> int:
        """The clock's next number, taken as a command sequence is asked —
        its place in the order of the asks. It supersedes nothing until it
        holds (publish)."""
        with self._state_lock:
            self._clock += 1
            return self._clock

    def publish(self, kind: str, n: int) -> None:
        """The intent numbered `n` holds from now; background work waiting
        a build out looks again whether it is still wanted."""
        with self._state_lock:
            self._pending.pop(n, None)
            self._latest[kind] = max(self._latest.get(kind, 0), n)
        self.build.wake_waiters()

    def ask_early(self, kind: str) -> int:
        """A new queue's or a rebuild's number, as it is asked — before it
        waits for the command lock: it supersedes from now (a play press's
        hand-over is dropped at its next add instead of being waited out),
        published once its sequence holds the lock, withdrawn if the lock
        refuses it (command)."""
        with self._state_lock:
            self._clock += 1
            n = self._clock
            self._pending[n] = kind
        self.build.wake_waiters()
        return n

    def withdraw(self, n: int) -> None:
        """An early number whose sequence the command lock refused: it
        supersedes nothing from now."""
        with self._state_lock:
            self._pending.pop(n, None)

    def newest(self, *kinds: str) -> int:
        """The newest number that holds among intents of `kinds` (0: none)."""
        with self._state_lock:
            return max((self._latest.get(kind, 0) for kind in kinds), default=0)

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
            return self._superseded(item)

    def _superseded(self, item: Item) -> Optional[str]:
        """superseded, under the state lock."""
        by = SUPERSEDED_BY[item.kind]
        for kind in by:
            if self._latest.get(kind, 0) > item.n:
                return _WHY[kind]
        for n, kind in self._pending.items():
            if n > item.n and kind in by:
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

    def refuse(self) -> None:
        """A person's command to this HQPlayer is refused now — while it
        builds a setting or did not finish one (Building: restart HQPlayer),
        or answers nothing past a short silence (NotAnswering) — never parked
        behind it. The one rule every gate keeps: the command lock, the
        assistants' tools, the routes. Lent to the benchmark is the
        manager's to name (OutputHeld names the job)."""
        self.build.refuse()
        self._refuse_if_silent()

    def busy(self) -> Optional[str]:
        """What refuse() would say, in the person's words, or None."""
        try:
            self.refuse()
        except (Building, NotAnswering) as e:
            return str(e)
        return None

    def _refuse_if_silent(self) -> None:
        """A command is refused while HQPlayer answers nothing past a short
        silence's wait (silent_since): never parked behind it. The Stop a
        switch away owes goes all the same — its Status waits for HQPlayer
        (_LateStop)."""
        if self.silent_since is not None and not getattr(self._local, "late", False):
            raise NotAnswering()

    def _take(self) -> Optional[str]:
        """Hold the connection for one request: the caller is its only user
        until _give. In turn behind the requests that came before it — first,
        when a hand-over left it to this thread: then the command that handed
        it over is returned, whose silence may fall on this request. Refused
        once the wait turns into a build (past the build limit questions go
        again: HQPlayer may be back), or its question block is no longer
        wanted (asking's `until`)."""
        me = threading.get_ident()
        with self._conn:
            self._waiting.append(me)
            try:
                while True:
                    self._check_state()
                    until = getattr(self._local, "until", None)
                    if until is not None and until():
                        raise Dropped("its question is no longer asked")
                    state = self.build.building()
                    if state is not None:
                        raise Building(self, state)
                    if self._owner is None and (self._reserved == me or (
                            self._reserved is None and self._waiting[0] == me)):
                        after = self._after if self._reserved == me else None
                        self._owner, self._reserved = me, None
                        return after
                    self._conn.wait()
            except BaseException:
                if self._reserved == me:
                    # What the hand-over kept it for does not go: the others may
                    self._reserved = None
                # Whoever is behind it in the line may be first now
                self._conn.notify_all()
                raise
            finally:
                self._waiting.remove(me)

    def _give(self, hand_over: Optional[str] = None) -> None:
        """Let the connection go — or, after `hand_over`, a command whose
        silence may follow it, keep it for this thread's next request. A link
        closed while this request held it outside a wait — nothing to wake —
        has its socket closed here, by the thread that held it — read while
        it still held it: a close after the next request took the socket
        is that request's to meet."""
        with self._conn:
            closed = self.state == "closed"
            self._owner = None
            if hand_over is not None:
                self._reserved, self._after = threading.get_ident(), hand_over
            self._conn.notify_all()
        if closed:
            self.client.disconnect()

    @contextmanager
    def _scope(self):
        """This thread's command sequence or question block: its requests
        share one fate (_Scope), and when it ends a connection a hand-over
        kept for it goes to a Status the link holds itself (_end_scope)."""
        if getattr(self._local, "scope", None) is not None:
            yield
            return
        self._local.scope = _Scope()
        try:
            yield
        finally:
            self._local.scope = None
            self._end_scope()

    def _end_scope(self) -> None:
        """The sequence sent its last request. A connection a hand-over kept
        for it — a silence may follow its last command — goes to a Status the
        link holds itself, in the background (_hold): the silence falls on
        that, never on a question with a short wait. A link nothing drives
        keeps no socket (a probe's, a library's)."""
        me = threading.get_ident()
        after = None
        with self._conn:
            if self._reserved == me:
                self._reserved, after = _HELD, self._after
            elif (self._owner is None and self._reserved is None and self.attached is None
                  and self._late_stop is None and not self._selected()):
                self.client.disconnect()
        if after is not None:
            threading.Thread(target=self._hold, args=(after,), daemon=True,
                             name="hqp-held-status").start()

    def _hold(self, after: str) -> None:
        """The Status the link holds after `after` when its sequence sent
        nothing more. After a build command it is watched up to the build
        limit: the watch says building, and its answer ends the build. After
        a short silence (a Stop's teardown, a PlaylistClear) it waits up to
        STOP_ANSWER_S, past which HQPlayer is not answering."""
        me = threading.get_ident()
        with self._conn:
            if self._reserved is not _HELD:
                return              # a close or a lend let the connection go meanwhile
            self._reserved = me
        short = after in SHORT_SILENCE_COMMANDS
        try:
            with self._scope():
                # Past a short silence's wait HQPlayer is not answering — said
                # by the request before the connection goes on
                self.client.get_status(wait=STOP_ANSWER_S if short else BUILD_LIMIT_S,
                                       watched=not short)
        except (BrokenPipeError, ConnectionError, OSError) as e:
            # Let go, lent, or another request's silence already: nothing held
            logger.debug("HQPlayer's Status after %s not held: %s", after, e)

    def _answered(self) -> None:
        """HQPlayer answered a request: a build it did not finish is over,
        and so is a silence past a short silence's wait."""
        self.build.answered()
        if self.silent_since is not None:
            self.silent_since = None
            logger.info("HQPlayer at %s:%d answers again", self.host, self.port)
            self._poke()

    def _not_answering(self, command: str, waited: float) -> None:
        """HQPlayer answered nothing to `command` within the `waited` seconds
        it was given (a short silence's wait, or the build limit of the
        Status a switch away held): not answering — never a build. People's
        commands are refused with that line, the ones waiting for it woken;
        questions go on, and the next answer ends it."""
        if self.silent_since is None:
            self.silent_since = time.time()
            logger.warning("HQPlayer did not answer within %g s of %s — it is not answering",
                           waited, command)
        self.lock.wake()
        with self._conn:
            self._conn.notify_all()
        self._poke()

    @contextmanager
    def asking(self, until: Optional[Callable[[], bool]] = None):
        """A block of this thread's questions — the poller's tick, the
        HQPlayer screen's reads, a probe: each takes the connection for
        itself, and one HQPlayer leaves unanswered ends the block. `until`:
        a question still waiting for the connection once it says so is not
        asked (Dropped) — a detached backend's poller, woken by wake_askers,
        never waits behind the Stop a switch away owes."""
        outer = getattr(self._local, "until", None)
        self._local.until = until
        try:
            with self._scope():
                yield self.client
        finally:
            self._local.until = outer

    def wake_askers(self) -> None:
        """Every request waiting for the connection looks again whether it
        is still wanted (asking's `until`)."""
        with self._conn:
            self._conn.notify_all()

    @contextmanager
    def request(self, command: str):
        """The client's gate, around every request it sends. Refused when an
        earlier request of its sequence or block went unanswered, and — in a
        command sequence — while HQPlayer is in a build it did not finish or
        answers nothing past a short silence. The connection is taken for
        the request alone (_take), a dropped socket reconnected; its bytes
        pass `sending`. One that takes it from a hand-over waits as the
        command that handed it over may make it: watched up to the build
        limit after what may set off a build, up to STOP_ANSWER_S after a
        short silence's command. After it: an answer ends what HQPlayer was
        silent in; a silence no build of ours explains ends the sequence, and
        past a short silence's wait HQPlayer is not answering — said before
        the connection goes to anyone else; after a command a silence may
        follow, the connection passes on (_give, _end_scope)."""
        me = threading.get_ident()
        scope = getattr(self._local, "scope", None)
        if scope is not None and scope.silent:
            raise NotAnswering()
        item = getattr(self._local, "item", None)
        if item is not None:
            why = self.superseded(item)
            if why is not None:
                raise Dropped(why)      # not even dialled for; `sending` has the last word
        if self.lock.owner == me:
            # A sequence whose own build HQPlayer did not finish sends
            # nothing more into its silence
            self.refuse()
        due = command if command in SHORT_SILENCE_COMMANDS and self.lock.owner == me else None
        if due is not None:
            # A Stop or a clear of a sequence, waiting its turn on the
            # connection (behind a question in a silence): a teardown to come
            self.teardown_due = due
            self.lock.wake()
        try:
            after = self._take()
        finally:
            if due is not None:
                self.teardown_due = None
        builds = self.build.count
        hand_over = None
        try:
            if self.lock.owner == me:
                self._refuse_if_silent()        # it began while this one waited its turn
            self._connect()
            c = self.client
            c.timed_out = False
            short = command if command in SHORT_SILENCE_COMMANDS else (
                after if after in SHORT_SILENCE_COMMANDS else None)
            if short is not None:
                self.teardown = short
                self.lock.wake()            # a detach waiting for the lock goes on
            if after is not None:
                c.inherit(STOP_ANSWER_S if after in SHORT_SILENCE_COMMANDS else BUILD_LIMIT_S,
                          watched=after not in SHORT_SILENCE_COMMANDS)
            yield
            if c.answered:
                self._answered()
                if command in HANDOVER_COMMANDS:
                    hand_over = command
            elif c.timed_out and self.build.count == builds:
                if scope is not None:
                    scope.silent = True
                if short is not None:
                    self._not_answering(short, STOP_ANSWER_S)
        finally:
            self.teardown = None
            self._give(hand_over)
        if hand_over is not None and scope is None:
            self._end_scope()

    @contextmanager
    def sending(self):
        """Around a request's bytes: the link's state and the item this
        thread serves checked under the lock an intent is accepted and a
        close is made under — an intent accepted after the check is newer
        than the bytes that went, and nothing goes once the link closed."""
        with self._state_lock:
            self._check_state()
            item = getattr(self._local, "item", None)
            if item is not None:
                why = self._superseded(item)
                if why is not None:
                    raise Dropped(why)
            yield
        sent = getattr(self._local, "sent", None)
        if sent is not None:
            sent.set()          # the switch away goes on once its Stop went

    def _connect(self) -> None:
        """A live socket for the holder: one the peer closed while idle is
        found before the send (_closed), a dropped one reconnected — never
        once the link closed; a failed connect opens the breaker for
        CIRCUIT_COOLDOWN_S."""
        c = self.client
        sock = c.socket
        if sock is not None and _closed(sock):
            logger.info("HQPlayer at %s:%d closed the connection — reconnecting",
                        self.host, self.port)
            c.losses += 1
            c.disconnect()
        if c.socket is not None:
            return
        if time.monotonic() < self._unreachable_until:
            raise ConnectionError(f"HQPlayer at {self.host}:{self.port} unreachable (circuit open)")
        sock = c.dial()
        if sock is None:
            with self._state_lock:
                if self.state == "closed":
                    # let go while it connected (the close woke the connect)
                    raise Dropped(self.why or "HQPlayer was let go")
            self._unreachable_until = time.monotonic() + CIRCUIT_COOLDOWN_S
            raise ConnectionError(f"Cannot connect to HQPlayer at {self.host}:{self.port}")
        self._unreachable_until = 0.0
        with self._state_lock:
            if self.state == "closed":
                sock.close()
                raise Dropped(self.why or "HQPlayer was let go")
            c.use(sock)

    @contextmanager
    def command(self, intent: Optional[str] = None, *, item: Any = None,
                number: Optional[int] = None):
        """A command sequence: the command lock (refused while HQPlayer
        builds — Building — or answers nothing past a short silence —
        NotAnswering), each of its requests taking the connection for
        itself. `intent`, a person's or this node's, takes the clock's next
        number as it is asked, before it waits for the lock, and holds once
        the lock is ours (yielded): its place is the order of the asks, and
        a sequence the lock refuses supersedes nothing. `number`: one taken
        earlier for it — a new queue's or a rebuild's (ask_early), which
        supersedes from its ask and is withdrawn when the lock refuses it.
        `item` is what the sequence serves — an Item made earlier (a start
        left for later, a resume, an owed Stop), or a kind, made here for
        `intent`: each of its requests is dropped once a newer intent
        supersedes it (Dropped)."""
        outer = getattr(self._local, "item", None)
        if isinstance(item, Item):
            self._local.item = item
        n = number if number is not None else self.reserve() if intent is not None else None
        held = False
        try:
            with self.lock, self._scope():
                held = True
                if intent is not None:
                    self.publish(intent, n)
                else:
                    n = getattr(item, "n", None)
                if isinstance(item, str):
                    self._local.item = self.item(item, n)
                yield n
        finally:
            self._local.item = outer
            if not held and number is not None:
                self.withdraw(number)

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
        """`backend` drives this HQPlayer no more — an output switch, the
        play gate attaching it again, a benchmark's hold, the node stopping.
        Only a person's switch away supersedes what was made for its
        attachment, and it says so in release — or in retire, another
        HQPlayer picked by address: a re-attach and a hold supersede nothing
        (a run's owed put-back stays owed)."""
        if self.attached is backend:
            self.attached = self.attachment = None
            self.build.last_change = None    # the outcome of a change was that attachment's
        self._close_if_retired()

    def release(self, ours: Optional[Callable[[Any], bool]]) -> Optional[str]:
        """A person switched the output away from this HQPlayer (switch): what
        was made for its attachment is superseded — a start or a resume left
        for later, the rest of a DSP change, a run's owed put-back — and it
        stops if it plays or pauses an entry of what it was given (`ours`;
        None: believed off the network, no Stop) — the one late Stop
        (_LateStop), on its own thread. The switch waits for its first
        outcome — the Stop sent, or not needed — at most BUILDING_AFTER_S,
        and returns at once while HQPlayer is known to build: the Stop then
        stays owed, and what the person is told comes back. Past the build
        limit HQPlayer is left as it is."""
        n = self.accept("switch")
        if ours is None:
            return None
        stop = self.item("late_stop", n)
        state = self.build.silent()
        if state is not None and state.get("unfinished"):
            logger.warning("HQPlayer not stopped at the output switch: %s", UNFINISHED)
            return None
        late = _LateStop(self, stop, ours)
        with self._state_lock:
            # One owed by an earlier switch was superseded by the attach since
            self._late_stop = late
        threading.Thread(target=late.run, daemon=True, name="hqp-late-stop").start()
        if state is None and late.decided.wait(BUILDING_AFTER_S):
            return None
        state = self.build.silent() if state is None else state
        return (STOP_OWED_BUILDING if state is not None and not state.get("not_answering")
                else STOP_OWED_SILENT)

    def _stop_if_ours(self, ours: Callable[[Any], bool], decided: threading.Event) -> bool:
        """Stop, if HQPlayer plays or pauses an entry of ours — judged on its
        Status, held up to the build limit (a silence it is in is waited
        out; nothing else goes meanwhile). `decided` is set the moment the
        Stop's bytes go, or once it is not needed."""
        c = self.client
        status = c.get_status(wait=BUILD_LIMIT_S, watched=True)
        if status is None:
            logger.warning("HQPlayer did not answer the Status a switch away held%s — not "
                           "stopped", " within the build limit" if c.timed_out else "")
            return False
        if status.state not in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            decided.set()
            return False
        if not ours(status):
            logger.info("HQPlayer plays something that is not ours — left playing")
            decided.set()
            return False
        self._local.sent = decided
        try:
            stopped = c.stop()
        finally:
            self._local.sent = None
        if stopped:
            return True
        logger.warning("HQPlayer did not take the Stop: %s", redact_uri(c.refusal()))
        return False

    def lend(self) -> None:
        """Lent to the benchmark, which drives HQPlayer on its own connection:
        nothing of this link goes out meanwhile. A request already sent is
        answered first — the run's connection is then the address's only
        sender; a connection kept for a next request or a held Status is let
        go."""
        with self._state_lock:
            self.lent = True
        with self._conn:
            self._reserved = None
            self._conn.notify_all()
            while self._owner is not None:
                self._conn.wait()
        self.build.wake_waiters()

    def reclaim(self) -> None:
        with self._state_lock:
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
        """Cancel everything: waiters and owed items give up with `why`. The
        request that holds the connection is woken (HQPlayerClient.close) and
        its own thread closes the socket — a socket closed by a thread that
        does not read it races the one that does; one no request holds is
        closed here, nothing able to take it any more (the state is closed
        first, under the lock a connect takes to use a socket)."""
        with self._state_lock:
            if self.state == "closed":
                return
            self.state, self.why = "closed", why
            self._late_stop = None
        with self._conn:
            self._reserved = None
            idle = self._owner is None
            self._conn.notify_all()
        self.lock.wake()
        self.build.wake_waiters()
        if idle:
            self.client.disconnect()
        self.client.close()

    def _selected(self) -> bool:
        return self.key == _key(settings.hqplayer_host, settings.hqplayer_port)

    def readdress(self, host: str, port: int) -> None:
        """Dial the alias chosen last (the same HQPlayer, `key`): on a Docker
        node the host's address replaces a container loopback that reaches
        nothing. A live connection stays until it ends; the attached
        backend's meter follows the alias at once."""
        if (host, port) == (self.host, self.port):
            return
        self.host, self.port = host, port
        self.client.host, self.client.port = host, port
        self._unreachable_until = 0.0
        logger.info("HQPlayer's link dials %s:%d from now on", host, port)
        attached = self.attached
        if attached is not None:
            attached.readdressed()          # its meter follows the alias

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


def background(what: str, action: Callable[[], Any], *,
               needed: Callable[[], Optional[str]] = lambda: None,
               until: Callable[[], bool] = lambda: False) -> Any:
    """`action` through a build (through), as background work no caller
    waits for: what ends it short is logged as `what` — a build HQPlayer did
    not finish or a connection that broke as a warning, work no longer
    wanted or a link that did not send it as news. Returns what `action`
    returned, or None when it did not go."""
    try:
        return through(action, needed=needed, until=until)
    except GaveUp as e:
        (logger.warning if e.failed else logger.info)("%s: %s", what, e)
    except Dropped as e:
        logger.info("%s: %s", what, e)
    except (BrokenPipeError, ConnectionError, OSError) as e:
        logger.warning("%s: %s", what, e)
    return None


# -- the registry --------------------------------------------------------------------

_links: Dict[tuple, HqpLink] = {}
_registry_lock = threading.Lock()
# Why the node let every link go (close_all): a link asked for after it is
# closed from the start — a hold's release re-attaching the output while the
# node stops sends nothing.
_closed_why: Optional[str] = None
# The key of the HQPlayer this node drives: the link that retires when the
# owner chooses another (choose)
_chosen: Optional[tuple] = None


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
    global _chosen
    link = link_for(settings.hqplayer_host, settings.hqplayer_port)
    with _registry_lock:
        if _chosen is None:
            _chosen = link.key
    return link


def choose(host: str, port: int) -> HqpLink:
    """The owner chose the HQPlayer at this address: its link — the same one,
    if it was chosen before and has not closed yet, dialling the alias
    chosen now — and the link of the HQPlayer chosen before retires. Every
    other link goes on: a library sync of another box, the picker's
    probes."""
    global _chosen
    link = link_for(host, port)
    link.readdress(host, port)
    link.unretire()
    with _registry_lock:
        before, _chosen = _chosen, link.key
        old = _links.get(before) if before is not None and before != link.key else None
    if old is not None:
        old.retire("another HQPlayer was chosen")
    return link


def stopping() -> Optional[str]:
    """Why the node let every link go (close_all), once it is stopping."""
    with _registry_lock:
        return _closed_why


def close_all(why: str) -> None:
    """The node is stopping: every link closes — its waiters and owed items
    give up, a request it holds is woken and closes its own socket — and so
    does the meter's session."""
    global _closed_why
    with _registry_lock:
        _closed_why = why
        links = list(_links.values())
    for link in links:
        link.close(why)
    from playback import hqp_meter
    hqp_meter.meter.detach()
