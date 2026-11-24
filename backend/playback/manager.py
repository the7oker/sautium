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
first, then commit what it took to the canonical queue — nothing of a batch
HQPlayer refused whole; a slot it cannot open, or refused among others it
took, stays in the queue as a gap of that output (PlayerBackend.gap), named
in the playlist and never handed to it. `_mutate_lock` serialises
mutations so canonical and mirrored order can't interleave; a start a
mirror leaves (a replace that plays) goes once it is let go
(`PlayerBackend.queue_start`) — HQPlayer may build a start for minutes.
"""

import hashlib
import logging
import os
import threading
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import asdict
from typing import Any, Callable, Iterator, Optional

from config import settings, ui_build
from db_pool import Listener
from hqplayer_client import STATUS_READINGS, format_time

from playback import sessions, tracker
from playback.base import PlaybackStatus, PlayerBackend, ReorderPlan, Spot, new_id
from playback.queue import CanonicalQueue, QueueItem, gone_prefixes, resolved_artwork
from playback import substitute
from playback.substitute import LEAD, Lane

logger = logging.getLogger(__name__)

_PREVIEW_BEHIND = 1   # instant "previous"

# The states in which the output holds a slot open: its way in is its own
# until it leaves the slot (PlaybackManager._reread_ways_in) — unless all it
# does there is wait for a stream that has not come (_sounding)
_SOUNDING = ("playing", "paused", "loading", "unknown")

# How long the play path's one stat is waited for — the folder look's own
# patience (scanner.LOOK_PATIENCE_S): a dead network mount answers a stat
# never, and a Play's worker is not its to hold.
_STAT_PATIENCE_S = 3.0


# The stats in flight, by path: [done, answer]. One look of one path at a
# time — a stat already in flight for a path is the one every asker waits on.
_stats: dict = {}
_stats_lock = threading.Lock()


def _opens(path: str) -> bool:
    """Whether a file of this node opens now: one stat, on a thread of its
    own, waited for no longer than _STAT_PATIENCE_S. One that has not
    answered by then does not open now. A stat still in flight for the same
    path — an earlier asker's, on a mount that hangs — is the one this
    asker waits on, within the same patience: never a second thread for it.
    A stat that never returns leaves its one thread for good, as the
    folder's own look does (scanner._look_worker) — the kernel's call
    cannot be taken back; once the folder is recorded as not answering no
    path under it is asked for again."""
    with _stats_lock:
        flight = _stats.get(path)
        if flight is None:
            flight = _stats[path] = [threading.Event(), False]

            def ask() -> None:
                try:
                    os.stat(path)
                    flight[1] = True
                except OSError:
                    pass
                finally:
                    with _stats_lock:
                        _stats.pop(path, None)
                    flight[0].set()
            threading.Thread(target=ask, daemon=True, name="queue-look").start()
    return flight[0].wait(_STAT_PATIENCE_S) and flight[1]


class OutputHeld(ConnectionError):
    """The output is lent to a job that drives the device on its own
    connection (the HQPlayer benchmark): nothing else may play, switch or
    reconfigure it until the job ends or is cancelled."""

    def __init__(self, hold: dict):
        super().__init__(f"{hold['label']} — it gives the output back when it "
                         "finishes, or cancel it on the HQPlayer screen")


class Nowhere(ConnectionError):
    """The chosen renderer answers at none of the addresses it is known at:
    where it is now, only a scan can tell (PlaybackManager.ensure_active)."""


class QueueRefused(RuntimeError):
    """The output took none of a new queue: nothing commits, the queue as it
    was. Its words — and what the person's words made meanwhile could not do
    (said_with) — are the play route's answer."""


def said_with(e: BaseException) -> Optional[str]:
    """What the person's words made meanwhile could not do, carried by the
    refusal of the mutation they rode (PlaybackManager._output_mutation) for
    the routes to answer — never a note: a traceback prints notes, and this
    names a track."""
    return getattr(e, "_said", None)


class NotHere(RuntimeError):
    """A new queue — or a Play of the queue held — no slot of which opens
    here now, on a node that streams nothing: nothing commits, the queue as
    it was, the output never asked. `why` — 'disk': its folder does not
    answer — is the play route's answer (409 `not_here`)."""

    def __init__(self, why: str = "disk", album: Optional[str] = None):
        super().__init__("nothing of it opens here now, and this node streams nothing")
        self.why = why
        self.album = album          # the one album every refused slot is of


class QueueChanged(RuntimeError):
    """An edit planned on one order of the queue met another: refused
    whole, the queue as it is (409) — the planner reads it again."""


class PlaybackManager:
    def __init__(self):
        self.queue = CanonicalQueue()
        self._active: Optional[PlayerBackend] = None
        self._active_lock = threading.Lock()
        # Serializes ensure_active's check→detach→attach sequence so two
        # concurrent play intents don't double-attach (it nests activate,
        # which takes _active_lock — keep them separate).
        self._ensure_lock = threading.Lock()
        # Last known 1-based queue slot, independent of _latest_status —
        # the output-switch path hops through activate(None), whose
        # "disconnected" push wipes track_index from the status dict.
        self._last_slot = 0
        self._mutate_lock = threading.RLock()
        self.radio_mode = False
        # What the output is lent to (hold), with the job's progress; every
        # status payload carries it while it lasts.
        self._hold: Optional[dict] = None
        # The renderer the output is being attached to again (_attach_again),
        # or waited on where it is attached (_probe), by name: every status
        # payload says so while anyone holds it (_connecting_to).
        self._connecting: Optional[str] = None
        self._connecting_held = 0
        self._connecting_lock = threading.Lock()
        # Output changes so far, told as each is over: work one cut short — a
        # filler's add the link did not send — goes on through the output
        # there is then (wait_for_output_change)
        self._outputs = 0
        self._outputs_changed = threading.Condition()

        self._latest_status: dict = {"state": "disconnected"}
        self._status_version = 0
        self._sse_clients: list = []      # (asyncio.Event, asyncio.AbstractEventLoop)
        self._sse_clients_lock = threading.Lock()
        self._observers: list[Callable] = []
        self._persist_timer: Optional[threading.Timer] = None
        self._preview_window_key = None   # (slot, queue version) the window last ran for
        # The slot the output plays or stands on — what the fetch lane's
        # window counts from (playback.substitute.Lane): the item, so an edit
        # before it moves the window with it. None: slot 1, where Play starts.
        self._anchor_item: Optional[QueueItem] = None
        self._anchor_slot = 1
        # The streams the queue's slots need, in the queue's order, a lead
        # window ahead of the playhead; what each streamed row shows
        # (`slots`, told on the event stream apart from the status)
        self.lane = Lane(self.queue, self._slot_ready, self._window_anchor,
                         self._slots_changed, self._slot_failed, self._stream_arriving)
        self._slots: list = []
        self._slots_at = 0          # the queue version they were read at
        self._slots_version = 0
        self._slots_lock = threading.Lock()
        # A Play of streamed slots: whether its first ones failed with nothing
        # played — said once, then once more when all of them failed
        # (`stream_event` on the status, toasted once per page)
        self._play_run: Optional[dict] = None
        self._stream_event: Optional[dict] = None
        # The slot whose way in was not read again while the output sounded
        # it (_reread_ways_in): read when the output leaves it
        self._reach_owed: Optional[QueueItem] = None

    # -- backend lifecycle ---------------------------------------------------

    def backend(self) -> PlayerBackend:
        self._refuse_if_held()
        b = self._active
        if b is None:
            raise ConnectionError("No playback output configured")
        return b

    @property
    def held(self) -> Optional[dict]:
        """What the output is lent to, while it is (see hold)."""
        return self._hold

    def _refuse_if_held(self) -> None:
        hold = self._hold
        if hold is not None:
            raise OutputHeld(hold)

    @contextmanager
    def hold(self, by: str, label: str, spot: Optional[Spot] = None) -> Iterator[None]:
        """Lend the HQPlayer output to `by` (the benchmark), which drives
        HQPlayer on a connection of its own. The backend is detached — its
        poller ended, so no tick reaches the tracker and no mirror fights the
        job; the job stops HQPlayer itself — and while the hold lasts every
        play intent, transport command and output switch is refused
        (OutputHeld). Its link is lent the while (HqpLink.lend):
        nothing of this node's but the job's own connection reaches HQPlayer
        — no question, no probe, no Stop an earlier switch owed. The
        canonical queue stays the owner's: an edit lands in it as it does
        with no output attached. The release attaches HQPlayer again — still
        held, so no switch lands in between — and the attach mirrors the
        queue back into it and keeps its slot — or stands at `spot`, where a
        comparison began (playback.hqp_ab: its play press resumes there).

        The hold begins only once nothing else is driving HQPlayer: it takes
        the mutation lock (a queue replace still adding its tracks finishes
        first) and the ensure lock (a play intent attaching finishes first),
        and claims the output under the active lock, where activate() checks
        it. The link is lent before the detach: what the backend still sends
        is refused at its next request (HqpLink.lend), and the detach makes it
        refuse a sequence begun later (HqpBackend.shutdown)."""
        with self._mutate_lock, self._ensure_lock:
            with self._active_lock:
                self._refuse_if_held()
                b = self._active
                # Not its health: an output believed gone is detached by the
                # hold all the same, and the release attaches it afresh,
                # mirror and all. Whether HQPlayer answers is the job's own
                # check.
                if b is None or b.id != "hqplayer":
                    raise ConnectionError("HQPlayer is not the attached output")
                self._hold = {"by": by, "label": label, "progress": None}
            from playback import hqp_link
            link = hqp_link.chosen()
            # Lent first: what the backend still sends — a play press's hand-
            # over among it — is refused at its next request, so the detach
            # waits for no more than the request in flight
            link.lend()
            try:
                # No switch away: the job stops HQPlayer itself, and what was
                # made for the attachment it leaves — a run's owed put-back —
                # is the job's to take first
                self.activate(None, by_hold=True, stop_old=False)
                self._push_status({"state": "stopped"})
            except BaseException:
                link.reclaim()
                self._hold = None
                raise
        try:
            yield
        finally:
            with self._mutate_lock, self._ensure_lock:
                link.reclaim()
                try:
                    self.activate("hqplayer", by_hold=True, spot=spot)
                except Exception as e:      # activate rolled back and said "disconnected"
                    logger.warning("HQPlayer not re-attached after the %s (%s) — "
                                   "the next play attaches it", by, e)
                finally:
                    self._hold = None
                    self._push_status(dict(self._latest_status))

    def publish_status(self) -> None:
        """The latest status again, to every page: a job that never got the
        output (hold refused it) tells its sheet so."""
        self._push_status(dict(self._latest_status))

    def update_hold(self, **progress) -> None:
        """Publish the holding job's progress on the status channel."""
        hold = self._hold
        if hold is None:
            return
        self._hold = {**hold, "progress": progress}
        self._push_status(dict(self._latest_status))

    @contextmanager
    def output_change(self) -> Iterator[None]:
        """An output change from the picker or an address change — never
        while the output is lent: checked under the lock the hold takes, so
        a change that passed the check lands before a hold can begin, and
        none of its settings is written while one lasts. It takes the queue
        lock first, in the order the hold takes both: the attach mirrors the
        queue with no mutation in flight."""
        with self._mutate_lock, self._ensure_lock:
            self._refuse_if_held()
            yield

    def ensure_active(self) -> PlayerBackend:
        """Play-intent gate for a daemon whose outputs come and go (dozing
        DAPs, phones, HQPlayer starts/stops): the configured output is a
        preference, not a live connection. Called at the start of every
        playback session — a dead or never-attached output is (re)attached
        from the persisted settings right here; pause/stop/volume keep
        using backend() and never wake a device. An output believed healthy
        is only probed: no lock, so a play intent never waits for a queue
        edit or a play press's rebuild — unless the probe finds it moved (a
        renderer answering at another address now), which attaches it again. An attach takes the queue lock
        first, in the order the hold takes both: it mirrors the queue with
        no mutation in flight.

        A renderer that answers at none of the addresses it is known at is
        looked for once by the Output scan, which takes it where it answers
        now (routers.player._scan_run), and the press goes through the gate
        again: a renderer the scan reaches at a new address — a phone back
        on the LAN, or on its tunnel where the launcher's sweep asks the
        tailnet — plays from the first press; one it reaches nowhere is said
        offline after the scan, not after the probe alone. A wait the probe,
        the attach or the scan says ("connecting") is said through what
        follows it: one word, never taken back in between.

        Called with no lock held: the scan moves the output under the queue
        lock (renderer_reached). A press that mutates the queue passes the
        gate before it takes that lock (replace_queue)."""
        self._refuse_if_held()
        with ExitStack() as saying:
            try:
                return self._through_gate(saying)
            except Nowhere:
                if not self._found_by_scan(saying):
                    raise
            return self._through_gate(saying)

    def _gate_once(self) -> PlayerBackend:
        """One pass of the gate and no scan: the output a queue mutation
        picks under its lock, where a renderer found nowhere is offline."""
        with ExitStack() as saying:
            return self._through_gate(saying)

    def _through_gate(self, saying: ExitStack) -> PlayerBackend:
        """ensure_active's one pass: the output probed, or attached again
        under the output-change locks."""
        b = probed = self._active
        if b is not None and b.healthy():
            self._probe(b, saying)
            if b.healthy():
                return b
        else:
            probed = None
        with self._mutate_lock, self._ensure_lock:
            self._refuse_if_held()
            return self.answering(probed, saying)

    def _found_by_scan(self, saying: ExitStack) -> bool:
        """Did the Output scan reach the chosen renderer? Run for a press
        whose renderer answered nowhere it is known (ensure_active), no lock
        held: the scan moves the output itself (renderer_reached). A scan
        that fails leaves the press with the word it had."""
        from routers.player import scan_once, udn_key
        from routers.settings import _read
        renderer = _read("output.dlna_renderer") or {}
        saying.enter_context(self._connecting_to(renderer.get("name") or "DLNA renderer"))
        try:
            found = scan_once()
        except Exception as e:
            logger.warning("the scan for a renderer that answered nowhere failed (%s)", type(e).__name__)
            logger.debug("its failure: %s", e)
            return False
        return udn_key(renderer.get("udn")) in {udn_key(r.get("udn")) for r in found["renderers"]}

    def answering(self, probed: Optional[PlayerBackend] = None,
                  saying: Optional[ExitStack] = None) -> PlayerBackend:
        """The active output where its device answers, under the output-change
        locks (ensure_active; output_change for a renderer picked again): the
        instance as it is when it answers where it talks to its device —
        `probed` was asked already; one believed lost is asked again too, an
        HQPlayer that is off says so at every press — else the output
        attached again from the persisted settings, at the spot the instance
        left. A wait its probe says lasts through that attach, and through
        `saying` (the caller's) when given, as at a press (ensure_active)."""
        b = self._active
        with ExitStack() as own:
            saying = own if saying is None else saying
            if b is not None and b is not probed:
                self._probe(b, saying)
            if b is not None and b.healthy():
                return b
            from routers.settings import _read
            if _read("output.type") is None:
                return self.backend()   # legacy: honest ConnectionError
            self._attach_again(b, saying)
            return self.backend()

    def renderer_reached(self, udn: str, locations: list) -> None:
        """A scan reached the renderer `udn` at `locations`
        (routers.player._scan_run). When it is the active output and its
        instance talks to it elsewhere — at an address it was lost at, or one
        that does not answer a probe — the output moves to where the scan
        reached it, its spot kept, nothing started: the first Play then goes
        at once, with no probe of the address it left (a phone moved to a
        tunnel waited 9 s on its LAN address first, 2026-11-01). An instance
        whose own address still answers stays as it is."""
        def stays(b: Optional[PlayerBackend]) -> bool:
            return (b is None or b.id != "dlna" or not b.drives({"udn": udn})
                    or b.location in locations)
        b = self._active
        # Asked before the locks: a queue edit never waits on a dead
        # address. One that does not answer is lost there, its spot by the
        # clock (DlnaBackend.answers_here)
        if stays(b) or (b.healthy() and b.answers_here()):
            return
        with self._mutate_lock, self._ensure_lock:
            if self._active is not b or b.healthy():
                # Attached again meanwhile by a press or another scan — or
                # caught attaching by the look above (no device yet), and
                # attached since: no instance to move
                return
            logger.info("a scan reached %s elsewhere — the output moves where it answers", b.label)
            self._attach_again(b)

    def _attach_again(self, b: Optional[PlayerBackend], saying: Optional[ExitStack] = None) -> None:
        """The output attached again from the persisted settings, under the
        output-change locks, at the spot the instance `b` left (None: nothing
        was attached). A renderer is first looked for where it answers now —
        every address it is known at, at once: offline only when none does —
        and while it attaches every status says so (`connecting`: its name),
        never "disconnected": it is the same output, and a press waiting on
        it shows as one in flight — through `saying` (the caller's) when
        given. The instance let go sends no Stop; the attach resumes at the
        spot and starts nothing."""
        from routers.settings import _read
        otype = _read("output.type")
        renderer = None
        if otype == "dlna":
            renderer = _read("output.dlna_renderer")
            if not renderer:
                raise ConnectionError("No DLNA renderer configured")
        label = (renderer.get("name") or "DLNA renderer") if renderer is not None else None
        with ExitStack() as own:
            (own if saying is None else saying).enter_context(self._connecting_to(label))
            if renderer is not None:
                from playback.dlna_backend import renderer_online
                if not renderer_online(renderer):
                    raise Nowhere(
                        f"'{label}' is offline — turn it on, or pick another "
                        "output in Settings → Audio output")
            logger.info("%s '%s' output — attaching it again",
                        "unhealthy" if b is not None else "inactive", otype)
            # Where the lost instance left the queue — or where an attach that
            # failed was to resume (activate keeps it): the attach resumes
            # there, the queue and the second as they were
            spot = self.spot()
            self.activate(otype, spot=spot, stop_old=False, again=True,
                          device_id=_read("output.local_device"),
                          exclusive=bool(_read("output.local_exclusive")),
                          renderer=renderer)

    @contextmanager
    def _connecting_to(self, label: Optional[str]) -> Iterator[None]:
        """While the output is waited on or attached again — the renderer
        `label`: every status payload names it (`connecting`), said once the
        first holder takes it and taken back once the last lets it go (a
        press's probe and a scan's move may overlap), however each ends.
        None: nothing to say."""
        if label is None:
            yield
            return
        with self._connecting_lock:
            self._connecting_held += 1
            first = self._connecting_held == 1
            self._connecting = label
        try:
            # Within the hold: a first status that fails still lets it go,
            # never a word every later status carries
            if first:
                self._push_status(dict(self._latest_status))
            yield
        finally:
            with self._connecting_lock:
                self._connecting_held -= 1
                last = not self._connecting_held
                if last:
                    self._connecting = None
            if last:
                self._push_status(dict(self._latest_status))

    def _probe(self, b: PlayerBackend, saying: Optional[ExitStack] = None) -> None:
        """Fast offline-fail: a powered-off DLNA renderer must 503 within
        the probe window and one Output scan (ensure_active) with a clear
        message, not after stacked SOAP + re-attach timeouts (34s, live) —
        and the first attempt must not
        silently accept (fire-and-forget commands return 200 while the
        failure is async). A dozing-but-networked renderer passes the
        probe: its window is sized for a sleeping radio. A wait on a
        renderer already found elsewhere is said, as its attach is
        (_connecting_to) — held in `saying`, the caller's, when given, so
        it lasts through the attach that follows."""
        def waiting():
            if saying is None:
                return self._connecting_to(b.label)
            saying.enter_context(self._connecting_to(b.label))
            return nullcontext()
        if not b.reachable(waiting=waiting):
            b._gone = True
            hint = getattr(b, "offline_hint",
                           "turn it on, or pick another output in "
                           "Settings → Audio output")
            raise (Nowhere if b.id == "dlna" else ConnectionError)(f"'{b.label}' is offline — {hint}")

    @property
    def active(self) -> Optional[PlayerBackend]:
        return self._active

    def spot(self) -> Optional[Spot]:
        """Where the output stands in the queue, from its latest word: a
        track it plays, starts or holds paused, or the spot a stopped one's
        play press resumes (`resume`: an HQPlayer's, a renderer's that
        stopped answering, or the one an attach that failed was to resume
        at). None when it stands nowhere — stopped, or no output."""
        st = self._latest_status
        state, slot = st.get("state"), st.get("track_index")
        if state in ("playing", "loading", "paused") and isinstance(slot, int) and slot >= 1:
            return Spot(slot, float(st.get("position") or 0.0), state != "paused")
        resume = st.get("resume")
        if state in ("stopped", "disconnected") and resume and resume.get("index"):
            return Spot(resume["index"], float(resume.get("position") or 0.0), False)
        return None

    def activate(self, output_type: Optional[str], *, stop_old: bool = True,
                 by_hold: bool = False, spot: Optional[Spot] = None,
                 again: bool = False, **cfg) -> Optional[str]:
        """Switch the active output. The old backend is stopped (while it
        still believes its device reachable) and shut down; the canonical
        queue survives the switch; playback does NOT resume here — a
        person's switch plays on once its locks are let go, as their play
        press would (routers.settings.put_output_prefs). Returns what the
        person is told when the old output's stop is left for later
        (HQPlayer building, or not answering), else None.

        `stop_old=False` detaches without touching playback — app shutdown
        and clearing the HQPlayer host must leave an externally-running
        player alone (a backend restart mid-listening adopts the still-
        playing queue on the next start); the play gate attaching the same
        output again and a benchmark's hold use it too. Only a person's
        switch away supersedes what was made for the old attachment — a
        benchmark's owed put-back among it: a release (PlayerBackend.release),
        or another HQPlayer picked by address (HqpLink.retire); nothing else
        does.

        While the output is lent (hold) only the hold itself switches it
        (`by_hold`); anyone else gets OutputHeld.

        `spot` — where the output before it stood (spot(), read by the
        caller before the detach) — is where the new one resumes, the second
        in the track included; without it, the last slot from 0.

        `again` — the output of this type attached afresh, the instance
        before it let go (the play gate, a renderer a scan found elsewhere):
        no "disconnected" between them, as an output that stays chosen."""
        with self._active_lock, self._changing_output():
            if not by_hold:
                self._refuse_if_held()
            if (output_type is not None and self._active is not None
                    and self._active.id == output_type and not again):
                return None
            # The slot that was active on the OLD output — seeded into the
            # new backend after attach so play continues from the same track.
            prev_idx = self._last_slot
            old = self._active
            told = None
            if old is not None:
                self._active = None
                if stop_old:
                    try:
                        told = old.release()
                    except Exception as e:
                        logger.debug("stop on deactivate failed: %s", e)
                old.shutdown()
                logger.info("playback backend deactivated: %s", old.id)
            if output_type is None:
                # No output ⇒ no status source at all (§2.6).
                self._push_status({"state": "disconnected"})
                return told
            if output_type == "hqplayer":
                from playback.hqp_backend import HqpBackend
                backend = HqpBackend(emit=self._on_backend_status, queue=self.queue)
            elif output_type == "local":
                from playback.local.backend import LocalBackend
                backend = LocalBackend(emit=self._on_backend_status, queue=self.queue,
                                       device_id=cfg.get("device_id"),
                                       exclusive=bool(cfg.get("exclusive")))
            elif output_type == "dlna":
                from playback.dlna_backend import DlnaBackend
                backend = DlnaBackend(emit=self._on_backend_status, queue=self.queue,
                                      renderer=cfg["renderer"])
            elif output_type == "browser":
                from playback.browser_backend import BrowserBackend
                backend = BrowserBackend(emit=self._on_backend_status, queue=self.queue)
            else:
                raise ValueError(f"unknown output type: {output_type}")
            # Every slot is re-read for THIS output before it mirrors or
            # loads anything: the copy it opens itself, or a stream — the
            # one the lane brings a slot is every output's (playback.
            # substitute) — the queue survives the switch, the way in does
            # not.
            self._reresolve(backend)
            # The track and its second go on here: so does the listen —
            # before the new output's first word, its attach's among them. A
            # switch with nowhere to resume carries none, whatever an earlier
            # switch left carried
            tracker.carry_listen(spot is not None and spot.slot >= 1
                                 and self.queue.item_at(spot.slot) is not None)
            # Assigned before start() so early emits resolve their output
            # info — but a failed start MUST roll back: a half-constructed
            # backend left as "active" is a zombie that swallows every
            # later command (observed live: a dozing renderer failed the
            # boot attach, and Stream-all then died on the husk with
            # 'NoneType' errors and an empty Now Playing).
            self._active = backend
            try:
                # The status names the output attached from now, before its
                # start: the payload so far named none (the detach) or the
                # old one, and the new backend's own first word may be long
                # in coming — an HQPlayer that does not answer is said away
                # only past five misses, about half a minute. Inside the
                # rollback: its label may ask the database
                self._push_status({**self._latest_status, "output": self.output_info})
                backend.start()
            except Exception:
                self._active = None
                try:
                    backend.shutdown()
                except Exception as e:
                    logger.debug("rollback shutdown failed: %s", e)
                # The spot it was to resume at stays the output's: the next
                # play press attaches it again there, not at the slot's 0:00
                # (a renderer a scan found elsewhere, its attach failed)
                held = ({"resume": {"index": spot.slot, "position": spot.position}}
                        if spot is not None and spot.slot >= 1 else {})
                self._push_status({"state": "disconnected", **held})
                raise
            if spot is None:
                spot = Spot(prev_idx, 0.0, False)
            if spot.slot >= 1 and self.queue.item_at(spot.slot) is not None:
                prev_idx = spot.slot
                try:
                    backend.resume_at(spot.slot, spot.position)
                    logger.info("output switch keeps slot %d at %.0f s on %s",
                                spot.slot, spot.position, backend.id)
                except Exception as e:
                    logger.debug("resume_at(%d) failed: %s", spot.slot, e)
            # The streams for what this output cannot open natively, from
            # the slot it resumes at — the play press finds them buffering.
            if prev_idx >= 1:
                self._anchor_item, self._anchor_slot = self.queue.item_at(prev_idx), prev_idx
            self.lane.maintain()
            logger.info("playback backend activated: %s", backend.id)
            return told

    @contextmanager
    def _changing_output(self) -> Iterator[None]:
        """An output change (activate), told once it is over, however it
        ends."""
        try:
            yield
        finally:
            with self._outputs_changed:
                self._outputs += 1
                self._outputs_changed.notify_all()

    def output_changes(self) -> int:
        """How many output changes are over: what wait_for_output_change
        waits past."""
        with self._outputs_changed:
            return self._outputs

    def wait_for_output_change(self, seen: int, timeout: float) -> bool:
        """Until an output change after `seen` (output_changes) is over, at
        most `timeout` seconds. Returns whether one is."""
        with self._outputs_changed:
            return self._outputs_changed.wait_for(lambda: self._outputs != seen, timeout)

    def _reresolve(self, backend: PlayerBackend) -> None:
        """Recompute `QueueItem.play` for every slot against this output
        (playback.substitute.native_plays); the HQPlayer output names the
        endpoint whose library it holds so its own copies rank first."""
        endpoint_id = getattr(backend, "endpoint_id", None)
        self._reach_owed = None         # every slot is read here, for this output
        pending = self.queue.reresolve(
            lambda items: substitute.native_plays(items, backend.id, endpoint_id))
        if pending:
            logger.info("%d queued slot(s) need a stream on %s", pending, backend.id)

    def _slot_ready(self, item: QueueItem) -> None:
        """The lane settled `item`'s way in: the output hears it at the slot
        the item holds as it is told — read now, never before the lane's own
        work, so an edit meanwhile names the right one."""
        b = self._active
        index = self.queue.index_of(item)
        if b is not None and index is not None:
            b.slot_ready(index, item)

    def _stream_arriving(self, item: QueueItem) -> None:
        b = self._active
        if b is not None:
            b.stream_arriving(item)

    def _window_anchor(self) -> int:
        """The slot the fetch lane's window counts from: the one the output
        plays or stands on — where it stood when that item left the queue
        (a removal playing out) — else 1."""
        item = self._anchor_item
        at = self.queue.index_of(item) if item is not None else None
        if at is None:
            at = min(self._anchor_slot, len(self.queue))
        else:
            self._anchor_slot = at
        return at or 1

    # -- what the streamed rows show ------------------------------------------

    @property
    def slots_version(self) -> int:
        return self._slots_version

    def slots_message(self) -> dict:
        """The `slots` message of the event stream: every streamed slot's
        state, whole, with the queue version it was read at — a page applies
        it to the rows of that version, never to another's."""
        with self._slots_lock:
            return {"version": self._slots_at, "slots": list(self._slots)}

    def _slots_changed(self) -> None:
        """What a streamed row shows may have moved (a fetch began, passed a
        tenth, ended; a slot landed or failed; the queue changed): read again,
        and told when it did — never a queue version, which repaints the
        sheet."""
        version = self.queue.version
        slots = self.lane.slots()
        if self.queue.version != version:
            return              # an edit meanwhile: its own reading follows (_lane_follows)
        with self._slots_lock:
            if (version, slots) == (self._slots_at, self._slots):
                return
            self._slots, self._slots_at = slots, version
            self._slots_version += 1
        self.wake_sse()

    def _slot_failed(self, item: QueueItem, index: int, code: str) -> None:
        """A streamed slot failed (the lane): a Play whose first two slots
        failed with nothing played — or whose only one did — is said at once,
        with why; and once more when every one of its slots failed."""
        with self._slots_lock:
            run = self._play_run
            if run is None or run["generation"] != self.queue.generation or run["played"]:
                return
            run["failed"][id(item)] = code
            from playback.substitute import streamed
            # every slot whose way in is a stream, whatever it was queued as:
            # an owned track with no file here now streams like any other
            items = [it for it in self.queue.snapshot() if streamed(it)]
            failed = [it for it in items if id(it) in run["failed"]]
            firsts = items[:2]
            event = None
            if len(failed) == len(items) and not run["told_all"]:
                run["told_all"] = run["told_first"] = True
                event = {"all": True}
            elif (firsts and all(id(it) in run["failed"] for it in firsts)
                  and not run["told_first"]):
                run["told_first"] = True
                event = {"all": False}
            if event is None:
                return
            head = run["failed"].get(id(items[0])) if items else code
            self._stream_event = {"id": new_id(), "kind": "not_streamable",
                                  "album": run["album"], "reason": head or code, **event}
        self._push_status(dict(self._latest_status))

    def _play_started(self, items: list) -> None:
        """A new queue that plays: its run of streamed slots is watched from
        its start (_slot_failed), and what the last one said is over."""
        with self._slots_lock:
            albums = {it.album for it in items if it.album}
            self._play_run = {"generation": self.queue.generation, "played": False,
                              "failed": {}, "told_first": False, "told_all": False,
                              "album": albums.pop() if len(albums) == 1 else None}
            self._stream_event = None

    def stream_landed(self, token: str, reshaped: bool) -> None:
        """A stream's bytes landed in the media proxy (its track-ready hook,
        on the fetch's own thread): told to the active output, by the
        stream's token — the slots it fills may move meanwhile — a start
        parked on it goes, and an entry the output holds in another shape
        than the bytes came in is handed to it again first
        (PlayerBackend.stream_landed)."""
        b = self._active
        if b is not None:
            b.stream_landed(token, reshaped)

    def stream_failed(self, token: str) -> None:
        """A stream's fetch ended with no audio (the media proxy's
        track-failed hook): told to the active output (PlayerBackend.
        stream_failed)."""
        b = self._active
        if b is not None:
            b.stream_failed(token)

    def init_from_settings(self) -> None:
        """Boot-time output activation from persisted settings.

        Nothing chosen yet resolves to the browser: it is the only output that
        works with no configuration on either runtime — local needs PortAudio
        (absent in the image), DLNA needs a renderer, HQPlayer needs HQPlayer.
        A node therefore comes up able to play instead of coming up idle.

        An install older than the Output picker, where an endpoint being
        configured WAS the choice, had that choice saved once
        (db_migrate's hqp_choice_v1): nothing here reads "configured" as
        "chosen" any more.

        What opens here is this process's own observation, so the music
        folder is looked at first — before the persisted queue is restored
        and its slots' ways in read (scanner.gone_prefixes). The look is the
        notices' shared one, waited for no longer than its patience: what a
        slower folder answers is recorded when it does, and that record is
        the queue's event (reach_changed)."""
        from scanner import folder_unreachable_now
        folder_unreachable_now()
        from routers.settings import _read
        from routers.player import _hqp_configured
        otype = _read("output.type") or "browser"
        if otype == "local":
            self._restore_persisted_queue()
            try:
                self.activate("local", device_id=_read("output.local_device"),
                              exclusive=bool(_read("output.local_exclusive")))
            except Exception as e:
                logger.error("local output activation failed: %s", e)
        elif otype == "dlna":
            self._restore_persisted_queue()
            renderer = _read("output.dlna_renderer")
            if renderer:
                try:
                    self.activate("dlna", renderer=renderer)
                except Exception as e:
                    logger.error("DLNA output activation failed: %s", e)
            else:
                logger.info("output=dlna but no renderer persisted — idle")
        elif otype == "browser":
            self._restore_persisted_queue()
            self.activate("browser")
        elif otype == "hqplayer":
            if _hqp_configured():
                # Restore BEFORE activate: with a non-empty canonical queue
                # the attach mirrors it into an idle HQPlayer, while a
                # still-playing HQPlayer keeps its playlist (adopt/mirror
                # both step aside — see HqpBackend.start).
                self._restore_persisted_queue()
                try:
                    self.activate("hqplayer")
                except Exception as e:
                    logger.error("HQPlayer output activation failed: %s", e)
            else:
                logger.info("output=hqplayer but no endpoint configured — idle")
        else:
            logger.info("No playback output configured — idle")
        if self._active is None:
            # Nothing attached (unconfigured endpoint, absent renderer, failed
            # start): publish the selection anyway so the UI can name the
            # output it is waiting on instead of seeing no output at all.
            self._push_status({"state": "disconnected"})

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

    @property
    def output_info(self) -> dict:
        """The SELECTED output — a preference, not a live connection. A
        backend that never attached (HQPlayer closed, renderer asleep) leaves
        the user's choice standing, so the persisted type is the fallback and
        `attached` carries the liveness. Every surface that asks "which output
        is this node on?" — the status payload, the Output picker, the AI
        prompt — reads it from here, or they disagree the moment a backend
        drops."""
        backend = self._active
        if backend is None:
            from routers.settings import _read
            return {"type": _read("output.type"), "label": None,
                    "attached": False}
        info = {"type": backend.id, "label": backend.label, "attached": True}
        renderer_attached = getattr(backend, "renderer_attached", None)
        if renderer_attached is not None:
            # Browser output only: distinguishes "another device is playing"
            # (remote-control mode) from "nobody renders" in the UI.
            info["renderer_attached"] = renderer_attached
        return info

    def subscribe_status(self, fn: Callable) -> None:
        """Register `fn(status_dict, item)` — called on every successful
        status tick before the SSE fan-out (radio refill rides on this)."""
        self._observers.append(fn)

    def _push_status(self, new_data: dict) -> None:
        # Invariant: every payload names the selected output. The bare
        # "disconnected" pushes used to omit it, and the UI reads that field
        # as "which output is this node on?" — so a stalled HQPlayer poll or
        # a failed attach made the whole output vanish from the status while
        # the picker still showed it selected.
        if "output" not in new_data:
            new_data["output"] = self.output_info
        if self._hold is not None:
            new_data["hold"] = self._hold
        else:
            new_data.pop("hold", None)      # a re-published payload from the held stretch
        if self._connecting is not None:
            new_data["connecting"] = self._connecting
        else:
            new_data.pop("connecting", None)
        # A Play of streamed slots that does not stream here: said while that
        # run stands, toasted once per page (the event's id, as output_event)
        if self._stream_event is not None:
            new_data["stream_event"] = self._stream_event
        else:
            new_data.pop("stream_event", None)
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

    def _maintain_preview_window(self, idx, start=None) -> None:
        """Playback-position-keyed RAM bound for streamed/transcoded buffers —
        the long-standing eviction follow-up (the OOM path: a long phantom
        queue held every fetched FLAC in RAM forever). The window
        [idx-1 .. idx+LEAD] is untouchable — the fetch lane refetches what of
        it the budget dropped (playback.substitute.Lane.pick) — and so is
        `start`, the slot a start under way goes to while the status still
        names the one it leaves: its bytes, just in, stay until the output
        reads them; beyond it enforce_budget evicts most-distant-first
        (behind the playhead before far-ahead), and an evicted track
        refetches transparently on demand. Runs on the status thread; the
        (slot, queue-version, start) guard keeps it off the 1 Hz tick. Never
        while a mutation is in flight: the queue it would read is the one
        about to go, and a new queue's fetched tracks are in no slot of it —
        first out, before the output opens them; the commit's version brings
        the window back at the next tick."""
        if self.queue.view()[2]:
            return
        anchor = idx if isinstance(idx, int) and idx >= 1 else 1
        key = (anchor, self.queue.version, start)
        if key == self._preview_window_key:
            return
        self._preview_window_key = key
        from streaming import service as streaming_service
        proxy = streaming_service.get_proxy()
        if proxy is None:
            return

        def ident(it: QueueItem):
            tok = it.opener().get("token") or it.source.get("token")
            if tok:
                return tok
            if it.media_file_id is not None:
                return ("mf", it.media_file_id)
            return None

        lo = max(1, anchor - _PREVIEW_BEHIND)
        hi = anchor + LEAD
        keys = [ident(it) for it in self.queue.snapshot()]
        keep = {k for k in keys[lo - 1:hi] if k}
        if isinstance(start, int) and 1 <= start <= len(keys) and keys[start - 1]:
            keep.add(keys[start - 1])
        ranked = ([k for k in keys[:lo - 1] if k]             # behind: oldest first
                  + [k for k in reversed(keys[hi:]) if k])    # ahead: farthest first
        freed = proxy.enforce_budget(
            keep, ranked, settings.streaming_preview_ram_mb * 1048576)
        if freed:
            logger.info("preview budget: freed %.0f MiB beyond slots %d..%d",
                        freed / 1048576, lo, hi)

    def _played(self) -> None:
        """A slot of the queue sounds: a Play's run of failures is over,
        and so is what it said."""
        with self._slots_lock:
            run = self._play_run
            if run is None or run["played"]:
                return
            run["played"] = True
            told, self._stream_event = self._stream_event is not None, None
        if told:
            self._push_status(dict(self._latest_status))

    def _on_backend_status(self, sender: PlayerBackend, s: PlaybackStatus) -> None:
        """One status tick from the active backend → SSE payload (exact
        legacy shape + `output`), play tracking, end-of-queue archival,
        observers. Runs on the backend's status thread.

        Only the ACTIVE backend is heard. activate() empties the slot before
        it stops and shuts the old backend down, so whatever that one still
        says — its own stop tick, the error of a stop that failed, a poll or
        GENA event landing after unsubscribe — ends here rather than in the
        UI as a "Playback error" for the output the user just left (seen
        live: a phone renderer that had gone away refused the switch-away
        Stop, and the new output came up under that toast). Nothing is lost
        with its last tick: the listen it was in the middle of goes on on the
        new output, which plays the track on or holds it at its second
        (tracker.carry_listen) — a switch with nowhere to resume ends it at
        the new backend's first tick — and a switch is not an end of queue."""
        if sender is not self._active:
            return
        if s.state == "disconnected":
            self._push_status({"state": "disconnected"})
            self._reach_follows(None, s.state)
            return

        if s.item is not None:
            # Identified by the backend: its slot is wherever it sits now —
            # gone with a mutation that committed meanwhile, it is no slot;
            # a slot the queue let go that plays out (`out_after`) is no slot
            # either, and still the listen it was.
            idx = self.queue.index_of(s.item) or 0
            item = s.item if idx or s.out_after is not None else None
        else:
            idx = s.queue_index
            item = self.queue.item_at(idx)
        # A provider's stream — queued as one, or the way in of a slot this
        # output reaches no copy of (playback.substitute): the provider's own
        # metadata and badges either way.
        preview = bool(item and item.stream_token)
        # Phantom previews carry 'HTTP stream' in the player's own metadata —
        # surface the provider's real artist/title from the queue item. For
        # non-preview rows a backend with authoritative metadata (HQPlayer
        # file tags) supplies it via `extra`; engine-rendered backends leave
        # extra empty and the queue item is the source.
        if preview:
            artist, album, song = item.artist, (item.album or ""), item.title
        else:
            # A backend with authoritative metadata (HQPlayer's tags for a
            # file it opened) supplies it via `extra`; absent or empty, the
            # queue item is the source — HQPlayer reports nothing useful for
            # a slot it streams over http.
            artist = s.extra.get("artist") or (item.artist if item else "")
            album = s.extra.get("album") or ((item.album or "") if item else "")
            song = s.extra.get("song") or (item.title if item else "")

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
            "track_index": idx,   # 1-based; also mirrored into _last_slot below
            "media_file_id": item.media_file_id if item else None,
            # Universal track UUID (owned + phantom) — Discovery's
            # similar-to-now-playing seed reads it off currentStatus.
            "track_id": item.track_id if item else None,
            # The album this slot was queued FROM — Now Playing cannot re-derive
            # it, a canonical track belongs to every album that lists it.
            "album_id": item.album_id if item else None,
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
            "excerpt": bool(item.excerpt) if preview else False,
            "preview_track_id": item.track_id if preview else None,
            "output": self.output_info,
            # Stale-tab detection: tabs compare this against the build they
            # loaded with and reload themselves on mismatch (player.js).
            "ui_build": ui_build(),
        }
        # Engine failure surface (device open/start failed, unplayable
        # tracks) — additive; absent when everything is healthy.
        if s.extra.get("error"):
            new_data["error"] = s.extra["error"]
        # The output is playing something that is not ours (HQPlayer's
        # playlist edited in its own GUI, another source): additive, and
        # the slot above is 0 so nothing is tracked against a wrong track.
        if s.extra.get("source"):
            new_data["source"] = s.extra["source"]
        # A slot the queue let go plays out behind this many of its slots:
        # where a "play next" goes
        if s.out_after is not None:
            new_data["plays_out_after"] = s.out_after
        # Why the slot did not play (HQPlayer's playback trace): its own key —
        # `error` is a string the UI toasts as it stands.
        if s.extra.get("diagnosis"):
            new_data["diagnosis"] = s.extra["diagnosis"]
        # How often HQPlayer's limiter acted on its output (<Status clips>):
        # the meter's peak is read before upsampling, this is the after.
        if s.extra.get("limited") is not None:
            new_data["limited"] = s.extra["limited"]
        # The output mode HQPlayer runs now, by name ("PCM", "SDM (DSD)") —
        # the selected one may be [source]; the peak meter marks SDM's room.
        if s.extra.get("active_mode"):
            new_data["active_mode"] = s.extra["active_mode"]
        # What the published atlas says of the modulator and rate playing near
        # full scale, for that mark (hqp_atlas.near_full_scale)
        if s.extra.get("sdm_room"):
            new_data["sdm_room"] = s.extra["sdm_room"]
        # HQPlayer 6's further readings (the Apod counter, the output's bits
        # and channels, its delay…): additive, each while HQPlayer gives it.
        new_data.update({k: s.extra[k] for k in STATUS_READINGS if s.extra.get(k) is not None})
        # HQPlayer's active configuration profile by name (its own saved
        # setups), while it names one: shown on its screen, never switched
        if s.extra.get("configuration"):
            new_data["configuration"] = s.extra["configuration"]
        # The HQPlayer output's own words (playback.hqp_backend): the build it
        # is in, a silence no build of ours explains, the outcome of a DSP
        # change that went through a build, where the play press resumes a
        # stopped HQPlayer, a play press taking the queue to it again, and a
        # start under way — HQPlayer has yet to sound it.
        new_data.update({k: s.extra[k] for k in ("building", "not_answering", "dsp_change",
                                                 "resume", "rebuilding", "starting")
                         if s.extra.get(k) is not None})
        # A Seek of the node's that left a renderer elsewhere (playback.
        # dlna_backend): told once per id, while its load plays
        if s.extra.get("seek_missed"):
            new_data["seek_missed"] = s.extra["seek_missed"]
        # A renderer that paused our track by no word of the node's — its own
        # remote, a controller past UPnP: said as the renderer's pause
        if s.extra.get("paused_on_device"):
            new_data["paused_on_device"] = True
        # What another controller did to the output — took it, stopped it, had
        # its track refused, paused the renderer: one event with an id, said
        # while its state stands, told once by every page connected when it
        # was minted (ROON-COEX §12.6)
        if s.extra.get("output_event"):
            new_data["output_event"] = s.extra["output_event"]

        # Per-track listening history + scrobble (source-agnostic: owned and
        # streamed phantom items both carry the track UUID). Separate from the
        # session/Home-shelf archival below — that snapshots the whole queue,
        # this records each play.
        tracker.track_play_event(new_data["state"], s.position, s.length, item,
                                 external=(s.extra.get("source") == "external"
                                           or bool(s.extra.get("unslotted"))),
                                 holds=bool(s.extra.get("resume")),
                                 corrected=bool(s.extra.get("corrected")),
                                 taken=bool(s.extra.get("taken")))
        if item is not None and idx:
            self._anchor_item, self._anchor_slot = item, idx
        self._maintain_preview_window(idx, s.extra.get("start_slot"))
        self.lane.maintain()
        if new_data["state"] == "playing" and s.position > 0 and item is not None and idx:
            # A slot of the queue sounds. What plays at no slot of it — another
            # controller's entry (`source` external), a track of the queue
            # before (a renderer's load the queue names no more) — is no sound
            # of this Play's: its failures stay said
            self._played()

        # Natural end-of-queue: the player stopped on the last track. Archive
        # the active session so a fully-listened album/queue lands in history
        # without a follow-up play. Only when the previous tick was PLAYING
        # the last slot — a manual stop mid-queue is left alone so resume
        # doesn't fragment the session, and so is another app taking the
        # output there (stopped at the spot it cut, or under the listen with
        # the output's verdict still to come: Play takes it back).
        qlen = len(self.queue)
        if (new_data["state"] == "stopped" and new_data.get("source") != "external"
                and not s.extra.get("taken")
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

        if isinstance(idx, int) and idx >= 1:
            self._last_slot = idx
        self._push_status(new_data)
        self._reach_follows(item, new_data["state"], starting=bool(s.extra.get("starting")))

    # -- queue mutations -----------------------------------------------------------
    #
    # Mirror-first: the backend reports what it took and only that lands in
    # the canonical queue — on HQPlayer every slot of a batch it took any of,
    # a gap for what it did not open. A failed mirror leaves the canonical
    # queue untouched (matching the player's unchanged state).

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        """One queue mutation: serialised, and marked on the queue while the
        output holds it ahead of the commit (CanonicalQueue.mutation)."""
        with self._mutate_lock, self.queue.mutation():
            yield

    @contextmanager
    def _output_mutation(self, pick: Callable[[], Optional[PlayerBackend]]
                         ) -> Iterator[Optional[PlayerBackend]]:
        """One queue mutation the output follows (_mutation), the output
        `pick` names under the queue lock, its mutation held
        (PlayerBackend.mutation) — and, once the queue lock is let go, what
        the output owes started and the person's words made meanwhile
        carried out, the queue committed or not (queue_start). What of them
        could not go on a mutation that did not commit is said with its
        refusal (said_with, which the routes answer). Yields the output, None
        when there is none."""
        backend = None
        try:
            with self._mutation():
                backend = pick()
                with backend.mutation() if backend is not None else nullcontext():
                    yield backend
        except BaseException as e:
            said = backend.queue_start() if backend is not None else None
            if said:
                e._said = said
            raise
        finally:
            self._lane_follows()
        if backend is not None:
            backend.queue_start()

    def _lane_follows(self) -> None:
        """After a mutation: a slot that left cancels its fetch at the source,
        the window is the new queue's, and the rows' states are read again."""
        self.lane.prune()
        self.lane.maintain()
        self._slots_changed()

    @staticmethod
    def _read_ways_in(backend: Optional[PlayerBackend], items: list) -> None:
        """The way in of every new slot that is the track itself, for an output
        that reads none as it mirrors (every one but HQPlayer, whose own
        mirror reads each slot's): a copy here opens as a file — a streamed
        album may list a track owned on another album — else its stream.
        While a folder of the library does not answer (gone_prefixes), or
        the latest look did not find a file (substitute.unopened), a slot
        that is a file is read too: such a file is no way in. Otherwise a
        file slot is what the route that made it found here, and no copy is
        asked about."""
        if backend is None or backend.id == "hqplayer":
            return
        narrowed = gone_prefixes() or substitute.unopened()
        kinds = ("track", "file") if narrowed else ("track",)
        read = [it for it in items if it.source.get("kind") in kinds]
        if not read:
            return
        for it, play in zip(read, substitute.native_plays(read, backend.id, None)):
            it.play = play

    @staticmethod
    def _first_opens(backend: PlayerBackend, items: list, *, held: bool = False) -> bool:
        """The play path's own look, beside the events (reach_changed): a
        folder can stop answering with no event — a network share, a root
        that is itself a mount point — and a file can leave a folder that
        answers (deleted or moved outside Sautium, its row kept until a
        Rescan). Before an output is handed a start — a new queue played, a
        play intent on the queue the node holds (before_start) — the file
        that start would open is asked for once (_opens), where the slot's
        way in is a file of this node the node takes for reachable (a new
        slot that is the track itself is read for it: its copy here opens
        as a file; `held`: a slot of the queue, whose way in is read
        already). A miss is a producer's observation: the folder is looked
        at (scanner.check_folder, waited for no longer than the look's
        patience). Where the folder then answers, that file alone does not
        open now — this look's observation (substitute.observe), under
        which the slot is read again: the next copy that would open is
        asked the same way, until one opens or none is left (a stream, or
        nothing). What an earlier look found is asked again from nothing,
        so a file put back is its slot's way in again. Returns whether the
        observation is another one than before: the caller reads the ways
        in after it. Outside the queue lock: this is the one place the play
        path asks the file system."""
        if not items:
            return False
        it = items[0]
        missed: set = set()
        endpoint = getattr(backend, "endpoint_id", None)
        root = settings.library_db_root() + "/"
        # read against the copies only where that says more than the slot does:
        # a file slot nothing was observed of is its own file
        fresh = bool(substitute.unopened()) or (not held and it.source.get("kind") != "file")
        src = it.opener() if held else it.source
        # The look reads the slot under its OWN observation so far (this
        # thread's alone); every other reader keeps the one the last look
        # left until this one ends, and is then given this one's in one
        # step — never an empty one in between, under which a re-read on
        # another thread would hand a missing file back
        with substitute.looking(missed):
            while True:
                if fresh and substitute.by_copies(it):
                    src = substitute.native_plays([it], backend.id, endpoint)[0] or it.source
                src = src.get("file", src)      # a file HQPlayer is handed prepared is that file
                path = src.get("path") if src.get("kind") == "file" else None
                if (path is None or path in missed or not path.startswith(root)
                        or any(path.startswith(g) for g in gone_prefixes())):
                    break       # no file of the music folder the node takes for reachable
                if _opens(settings.translate_to_local_path(path)):
                    break
                if not missed:
                    from scanner import check_folder
                    check_folder(wait=True)
                    if any(path.startswith(g) for g in gone_prefixes()):
                        break   # its folder does not answer: the recorded fact says the rest
                missed.add(path)
                fresh = True
        return substitute.observe(missed)

    @staticmethod
    def _refuse_if_none_opens(backend: PlayerBackend, items: list) -> None:
        """Slots about to be started none of which opens here now, on a node
        that streams nothing, are refused before the output is asked
        (NotHere) — only while a folder of the library does not answer, or
        the latest look did not find a file: otherwise the routes' own split
        is the whole of it. A
        slot that does not open: a stream to come, or one the lane already
        said nothing brings (streaming off)."""
        if not items or not (gone_prefixes() or substitute.unopened()) or substitute.streams():
            return
        plays = ([it.play for it in items] if backend.id != "hqplayer" else
                 substitute.native_plays(items, backend.id, getattr(backend, "endpoint_id", None)))
        if all(p is not None and ((p.get("kind") == "pending" and not p.get("prepare"))
                                  or (p.get("kind") == "unplayable"
                                      and p.get("code") in ("off", "not_here")))
               for p in plays):
            # Its true why, by the first of them that is a file of the
            # owner's: under a folder that does not answer, or — its copy
            # away, the file not found by the look — not there. With no such
            # slot among them nothing here is "not here": what does not
            # stream is the output's to say, in streaming's own words
            gone = gone_prefixes()
            own = [it for it in items if it.source.get("kind") == "file"]
            if not own:
                return
            why = "disk" if any(own[0].source["path"].startswith(g) for g in gone) else "away"
            albums = {it.album for it in own}
            raise NotHere(why, albums.pop() if len(albums) == 1 else None)

    def _start_slot(self) -> Optional[int]:
        """The slot a Play would hand the output a start of, from its latest
        word: the spot it resumes, else the slot a stopped output rests on,
        else the first — also while another controller holds the output
        (`source` external: its entry is none of the queue's). None while
        the output holds a slot of the queue open (it plays on, or resumes
        what it has) or a start already waits for its stream."""
        st = self._latest_status
        state, external = st.get("state"), st.get("source") == "external"
        if state == "loading" or (state in _SOUNDING and not external):
            return None
        resume = st.get("resume")
        if resume and resume.get("index"):
            return resume["index"]
        slot = st.get("track_index")
        return slot if isinstance(slot, int) and slot >= 1 and not external else 1

    def before_start(self, backend: PlayerBackend, slot: Optional[int] = None) -> None:
        """A play intent on the queue the node holds is about to hand the
        output a start — the transport's Play (`slot` None: where the latest
        status says it starts, _start_slot), a tap on a row (`slot`): the
        look a new queue's start takes (_first_opens), at the file that
        start would open, before the output is asked. A folder that went
        with no event is recorded here, and the queue's ways in are read at
        once — the output is then asked for a start of what opens now: a
        stream to wait for, on an HQPlayer another controller holds too
        (nothing sent until it landed). A Play of which nothing from its
        slot on opens, on a node that streams nothing, is refused (NotHere)
        with the output never asked."""
        tapped = slot is not None
        if slot is None:
            slot = self._start_slot()
        items = self.queue.snapshot()[slot - 1:] if slot else []
        if not items:
            return
        was = gone_prefixes()
        moved = self._first_opens(backend, items, held=True)
        if moved or gone_prefixes() != was:
            # the look's own record follows on a thread: the start needs it
            # now — as it does a file the look alone did not find, which is
            # no way in of its slot from here on (another copy, its stream).
            # Behind a queue edit in flight a play intent never waits: the
            # queue's slots are read once the edit is done, on the thread
            # the record's change uses. The slot this start opens is not
            # left to that race — on an output that opens what the slot's
            # way in says as it starts (HQPlayer reads every slot's again at
            # each mirroring) it is read here, at once, under the queue's
            # own lock, which orders it with every re-read: the output is
            # asked for a start of what the look left, and hears of it with
            # the rest (`ahead`)
            if self._mutate_lock.acquire(blocking=False):
                try:
                    self.reach_changed()
                finally:
                    self._mutate_lock.release()
            else:
                start = items[0]
                ahead = backend.id != "hqplayer" and substitute.by_copies(start) and (
                    self.queue.set_play(start, substitute.native_plays([start], backend.id, None)[0]))
                threading.Thread(target=self.reach_followed, args=(ahead,), daemon=True,
                                 name="queue-reach").start()
        if not tapped:
            self._refuse_if_none_opens(backend, items)

    def before_step(self, backend: PlayerBackend, step: int) -> None:
        """Next (`step` 1) or Previous (-1) is about to hand the output a
        start of the slot beside the one it is on — beside the spot a Play
        would resume, where the status names one: the same one look at the
        file that start would open (before_start, as a tap on that row).
        Not while a queue mutation is in flight, as for a tap: the slots
        move under it, and the output carries the word out on the queue it
        is handed."""
        if self.queue.view()[2]:
            return
        status = self._latest_status
        at = (status.get("resume") or {}).get("index") or status.get("track_index")
        if isinstance(at, int) and 1 <= at + step <= len(self.queue):
            self.before_start(backend, at + step)

    def replace_queue(self, items: list[QueueItem], *, play: bool,
                      archive: Optional[Callable[[], None]] = None) -> tuple[int, int]:
        """Replace the queue (and start playback, once the queue lock is let
        go). `archive` — the session rotation a play route starts with — runs
        once the output has taken the queue, under the queue lock, before the
        commit: an output that refused it (busy, gone) leaves history as it
        was; a rotation that fails commits nothing, and the output abandons
        what it was given (PlayerBackend.queue_abandon). An output that took
        none of `items` raises QueueRefused; a queue to play whose first
        file is not there is read after a look at its folder (_first_opens),
        and one of which nothing opens here, on a node that streams nothing,
        raises NotHere before the output is asked. Returns (added, generation);
        generation is what background fillers must guard their appends
        with."""
        pick = self.backend
        if play:
            # The gate before the queue lock: its scan for a renderer found
            # nowhere moves the output under that lock (ensure_active). Under
            # it the output is taken as the gate left it, or passes it once
            # more, with no scan
            gated = self.ensure_active()
            pick = lambda: gated if self._active is gated and gated.healthy() else self._gate_once()  # noqa: E731
            self._first_opens(gated, items)
        with self._output_mutation(pick) as backend:
            self._read_ways_in(backend, items)
            self._refuse_if_none_opens(backend, items)
            accepted = backend.queue_replace(items, play=play)
            added = len(accepted)
            if items and not added:
                # Refused whole: raised under the mutation, so what the
                # person's words made meanwhile could not do goes with it
                raise QueueRefused(backend.refusal() or "the output took none of the new queue")
            if added and archive is not None:
                try:
                    archive()
                except Exception:
                    backend.queue_abandon()
                    raise
            if added:
                gen = self.queue.replace(accepted)
                self._anchor_item, self._anchor_slot = accepted[0], 1
                if play:
                    self._play_started(accepted)
                self._last_slot = 0   # fresh queue — no resume slot in it
                backend.queue_changed("replace", play=play)
                self._schedule_persist()
            else:
                gen = self.queue.generation
        return added, gen

    def append(self, items: list[QueueItem], position: str = "end",
               generation: Optional[int] = None,
               after: Optional[QueueItem] = None) -> Optional[int]:
        """Append/insert into the CANONICAL queue. Returns the count applied,
        or None when `generation` is stale (the queue was replaced — the
        filler must stop). 'next' inserts after the playing slot; with
        nothing playing it appends at the end.

        `after` is an item a previous call already inserted, and this call
        EXTENDS that run: a streamed 'play next' album lands track by track as
        each one buffers, and each must follow the one before it rather than
        jump ahead of it. The playhead still wins when it has already passed
        that item — inserting behind it would drop the tracks into the past.

        The queue is authoritative and independent of the output: building it
        needs no live device. A None/absent backend just skips the mirror —
        the user can queue tracks before any output is reachable and press
        play once it is (that press attaches/probes the output)."""
        with self._output_mutation(lambda: self._active) as backend:
            if generation is not None and self.queue.generation != generation:
                return None
            self._read_ways_in(backend, items)
            if position == "next":
                anchor = self._latest_status.get("track_index")
                anchor = anchor if (isinstance(anchor, int)
                                    and 1 <= anchor <= len(self.queue)) else None
                out = self._latest_status.get("plays_out_after")
                if anchor is None and isinstance(out, int) and 0 <= out <= len(self.queue):
                    anchor = out            # right behind a removed track playing out
                run = self.queue.index_of(after) if after is not None else None
                if run is not None:
                    anchor = run if anchor is None else max(anchor, run)
            else:
                anchor = None
            if position == "next" and anchor is not None:
                accepted, anchor = (backend.queue_insert_next(items, anchor) if backend
                                    else (list(items), anchor))
                if accepted:
                    self.queue.insert_after(anchor, accepted)
                    if backend:
                        backend.queue_changed("insert_next")
                    self._schedule_persist()
            else:
                accepted = backend.queue_append(items) if backend else list(items)
                if accepted:
                    self.queue.append(accepted)
                    if backend:
                        backend.queue_changed("append")
                    self._schedule_persist()
            added = len(accepted)
            if added and self._latest_status.get("state") not in (
                    "playing", "paused", "loading"):
                # Nothing is playing (fresh/stopped queue, or no backend) —
                # surface the queue so the mini-player appears with a Play
                # button. A stopped backend doesn't reliably emit on append.
                self._emit_idle_status()
            return added

    def remove(self, index: int, track_id: Optional[str] = None) -> bool:
        """`track_id`, when given, is a precondition: the slot must still
        hold that track, else nothing is removed and False comes back."""
        with self._output_mutation(lambda: self._active) as backend:
            item = self.queue.item_at(index)
            if item is None or (track_id and item.track_id != track_id):
                return False
            ok = backend.queue_remove(index) if backend else True
            if ok:
                self.queue.remove(index)
                if backend:
                    backend.queue_changed("remove")
                else:
                    self._emit_idle_status(slots_moved=True)
                self._schedule_persist()
            return ok

    def _emit_idle_status(self, *, slots_moved: bool = False) -> None:
        """Nothing playing but the canonical queue exists — emit a stopped
        status anchored on the track Play would start (the last slot, or 1
        for a never-played queue) so the mini-player shows with a Play button
        (pressing it runs ensure_active → attach/probe/play). `slots_moved`:
        the edit moved the queue's slots (a removal with no output attached):
        the spot the last status named by its index is not carried on."""
        slot = self._last_slot if self._last_slot >= 1 else 1
        first = self.queue.item_at(slot) or self.queue.item_at(1)
        if first is None:
            return   # empty queue — leave the current status untouched
        idle = {
            "state": "stopped",
            "artist": first.artist, "album": first.album or "", "song": first.title,
            "genre": "", "position": 0.0, "length": first.duration_seconds or 0.0,
            "volume": 0.0, "process_speed": 0.0,
            "track_index": self._last_slot if self._last_slot >= 1 else 0,
            "media_file_id": first.media_file_id, "track_id": first.track_id,
            "cover_id": first.cover_id, "cover_url": first.cover_url,
            "provider_cover_url": (resolved_artwork.get(first.track_id)
                                   if first.stream_token else None),
            "progress_percent": 0.0, "position_formatted": "0:00",
            "length_formatted": format_time(first.duration_seconds or 0),
            "playlist_version": self.queue.version,
            "radio_mode": self.radio_mode,
            "preview": bool(first.stream_token),
            "album_id": first.album_id,
            "provider": first.provider if first.stream_token else None,
            "excerpt": bool(first.excerpt) if first.stream_token else False,
            "output": self.output_info,
            "ui_build": ui_build(),
        }
        # A queue edit while stopped keeps the explanation of the last play
        # on offer, and a start under way (HQPlayer's own advance, a play
        # press); the backend's next tick decides whether they still hold.
        # Where Play resumes stands on too, unless the edit moved the slots
        # (an append moves none; with no output attached nothing follows to
        # correct a spot a removal moved — a failed attach's spot would name
        # another track at its second). While an output is attached, so do
        # its other words — what another app did to it, a build or a silence
        # HQPlayer is in, a queue it takes again: a page ends each one's
        # toast with the status that says it no more (an append under a
        # takeover's spot ended it, and a silent HQPlayer's next status
        # raised its toast again for the person who had closed it).
        carried = ["diagnosis"]
        if not slots_moved:
            carried.append("resume")
        if self._active is not None:
            carried += ["output_event", "building", "not_answering", "rebuilding"]
        for key in carried:
            if self._latest_status.get(key):
                idle[key] = self._latest_status[key]
        if self._latest_status.get("starting"):
            idle["starting"] = True
        self._push_status(idle)

    def apply_reorder(self, plan: ReorderPlan) -> dict:
        """Execute a validated reorder. The mirror runs first (it can raise
        mid-way — then the canonical queue is left untouched and the drift
        canary flags the divergence; a retry reconverges); on success the
        canonical queue adopts the new order. A plan made on another order of
        the queue than the one it meets (a filler's append landed first) is
        refused whole (QueueChanged): it would hand HQPlayer an order of a
        queue that is gone — and so is one made on another output, whose
        position and resume were that output's. A slot's way in read again
        moves no slot."""
        # queue reorder is output-independent
        with self._output_mutation(lambda: self._active) as backend:
            now = self.queue.snapshot()
            if plan.items is not None and (len(now) != len(plan.items) or any(
                    a is not b for a, b in zip(now, plan.items))):
                raise QueueChanged("the queue changed while its new order was made")
            if plan.output is not backend:
                raise QueueChanged("the output changed while its new order was made")
            if backend is None:
                self.queue.reorder(plan.order)
                self._schedule_persist()
                return {"removed": 0, "added": 0, "interrupted": False}
            result = backend.queue_reorder(plan)
            self.queue.reorder(plan.order)
            backend.queue_changed("reorder")
            self._schedule_persist()
        return result

    def rebind_files(self, retracked: tuple = ()) -> None:
        """Re-bind every slot whose file left the library: it takes a live
        copy of its track, or the track itself, and a slot whose way in named
        a gone file is re-read for the active output (playback.queue.
        rebind_origins). The media_files delete trigger wakes the files
        listener below — a rescan's prune, a superseded CUE image, a cascade,
        whichever process ran it — as do a Rescan that sets a copy aside as
        away and an import while copies are away (a slot on an away file
        moves to a copy here; with none it keeps its file and streams, and
        is that file again when the copy is back — every slot's way in is
        read again, _reread_ways_in); the restore of a persisted queue calls
        it once. A gone id left in a slot is written into session_tracks and
        listening_history against their foreign keys (every Play died on the
        archive of the old queue) and its path no longer opens. `retracked`:
        the canon's words that came with the wake — (old, new) track uuids
        it rewrote; every slot bound to a file that carries an old one takes
        the new, and the listen in progress follows its item in the same
        step (tracker.follow), as does a slot whose file belongs to another
        track now. A streamed slot keeps the uuid it was queued with: its
        outputs know its stream by it. The output
        converges through queue_changed("rebind"); the status is re-published
        with the playing slot's new binding so Now Playing refetches."""
        from playback.queue import rebind_origins
        with self._output_mutation(lambda: self._active) as backend:
            plays = None
            if backend is not None:
                endpoint_id = getattr(backend, "endpoint_id", None)
                plays = lambda items: substitute.native_plays(items, backend.id, endpoint_id)
            moved = self.queue.rebind(rebind_origins, plays, retracked, tracker.follow)
            reread = self._reread_ways_in(backend) if backend is not None else []
            if not moved and not reread:
                return
            if moved:
                logger.info("%d queued slot(s) re-bound: their file left the library",
                            len(moved))
                self._schedule_persist()
            self._rebound(backend)

    def reach_changed(self, ahead: bool = False) -> None:
        """What opens here changed: the music folder stopped or started
        answering (scanner's recorded answer — the mount table, the folder
        watcher's loss, a library file served or found missing, a scan's
        refusal, Library opened: events, never a timer), or the output left
        the slot it sounded while that happened (_reach_follows). Every
        slot's way in is read again for the active output, its origin
        untouched: a file that does not open here now is a stream to come,
        and the same slot is its file again — no question — when the folder
        is back. The output follows by queue_changed("rebind"), the status
        carries the new playlist version, the lane looks at its window; a
        start that waited for the stream of a slot whose file is back is
        told as a landing tells it (slot_ready). With no output attached
        nothing is read: the attach reads every slot's."""
        settled = None
        with self._output_mutation(lambda: self._active) as backend:
            if backend is None:
                return
            status = self._latest_status
            waited = (self.queue.item_at(status.get("track_index"))
                      if status.get("state") == "loading" else None)
            if waited is not None and waited.opener().get("kind") != "pending":
                waited = None
            changed = self._reread_ways_in(backend)
            if not changed and not ahead:
                return
            logger.info("%d queued slot(s) open another way: what is reachable here changed",
                        len(changed) + ahead)
            self._rebound(backend)
            if waited is not None and waited.opener().get("kind") != "pending":
                settled = waited
        if settled is not None:
            # the start that waited for this slot's stream goes on what the
            # slot opens now — told as a landing is, once the mutation is over
            self._slot_ready(settled)

    def _sounding(self) -> Optional[QueueItem]:
        """The slot the output plays, is paused on or is loading — but one
        it only waits on for a stream that has not come: nothing of that
        slot is open, and its file coming back is its way in at once."""
        status = self._latest_status
        if status.get("state") not in _SOUNDING:
            return None
        item = self.queue.item_at(status.get("track_index"))
        if item is not None and item.opener().get("kind") == "pending":
            return None
        return item

    def _reread_ways_in(self, backend: PlayerBackend) -> list:
        """Read again, for `backend`, the way in of every slot that is read
        against its track's copies (one query for the queue) — but the slot
        the output sounds: its end of the file, or of the stream, is the
        output's own, and a way in swapped under it would be a slot started
        over. It is owed its reading until the output leaves it
        (_reach_follows). Returns the slots whose way in changed. Caller
        holds the queue lock."""
        endpoint_id = getattr(backend, "endpoint_id", None)
        sounding = self._sounding()
        changed, owed = self.queue.reread(
            substitute.by_copies, lambda items: substitute.native_plays(items, backend.id, endpoint_id), hold=sounding)
        self._reach_owed = sounding if owed else None
        return changed

    def _reach_follows(self, item: Optional[QueueItem], state: str, *, starting: bool = False) -> None:
        """On the status thread, after the status that says so is published:
        the output left the slot whose way in was not read again while it
        sounded — read now, off this thread. A status that says a start
        under way (`starting`: HQPlayer between two entries as it moves on
        by itself, or rebuilt around the slot it plays) says nothing of that
        yet — a read then swapped the way in under a slot about to play on,
        and had HQPlayer take the queue afresh in the middle of its own
        advance: the status after the start says where the output is."""
        owed = self._reach_owed
        if owed is None or starting or (item is owed and state in _SOUNDING):
            return
        self._reach_owed = None
        threading.Thread(target=self.reach_followed, daemon=True, name="queue-reach").start()

    def reach_followed(self, ahead: bool = False) -> None:
        """reach_changed on a thread of its own — the scanner's record of a
        change, the output leaving the slot it sounded, a look that came
        back with a queue edit in flight (`ahead`: the slot its start opens
        was read already, and the output has yet to hear): what goes wrong
        there is said in the log, where no caller waits to hear it."""
        try:
            self.reach_changed(ahead)
        except Exception:
            logger.exception("the queue's ways in were not read again after what is reachable here changed")

    def _rebound(self, backend: Optional[PlayerBackend]) -> None:
        """Slots were re-bound, or their ways in read again, under the queue:
        the output converges, and the status is re-published with the new
        playlist version and the playing slot's binding."""
        if backend is not None:
            backend.queue_changed("rebind")
        status = self._latest_status
        if status.get("state") not in (None, "disconnected"):
            update = {"playlist_version": self.queue.version}
            item = self.queue.item_at(status.get("track_index"))
            if item is not None:
                update.update(media_file_id=item.media_file_id,
                              track_id=item.track_id)
            self._push_status({**status, **update})

    def playlist(self) -> dict:
        """The queue as /api/player/playlist serves it: each slot the active
        output does not hold named with why (PlayerBackend.gap)."""
        b = self._active
        return self.queue.payload(gap=b.gap if b is not None else None, states=self.lane.slots)

    def mirror_step(self, backend: PlayerBackend, step: Callable[[], None]) -> None:
        """An output's own mirror step — the reconverge, HQPlayer following a
        re-bind once the build that held it off is done — as a mutation the
        output follows (_output_mutation): under the queue lock, while
        `backend` is still the active output; the start it leaves, and the
        words made meanwhile, after."""
        with self._output_mutation(
                lambda: backend if self._active is backend else None) as active:
            if active is not None:
                step()

    def mirror_locked(self, backend: PlayerBackend, step: Callable[[], Any]) -> Any:
        """A part of an output's own work that reads or writes the queue as
        it stands — a play press's first and last steps: under the queue
        lock, while `backend` is still the active output. Returns what the
        step returned, None when it is not; any start it leaves is the
        caller's to send (PlayerBackend.queue_start)."""
        with self._mutation():
            if self._active is not backend:
                return None
            return step()

    def clear_for_radio(self, archive: Callable[[], None]) -> int:
        """Radio start: clear everything after the playing slot (the seed
        plays on). `archive` — the session rotation radio starts with — runs
        in the output's own clear, before it (queue_clear_after_current): an
        output that cannot be asked (busy, lent, gone) refuses before either,
        and a rotation that fails clears nothing. Returns the new generation
        for the radio filler — the current one when the output did not
        clear: mirror-first, the queue keeps the tail the player keeps, and
        the radio follows it, history rotated. A play intent made while the
        output clears is the output's to carry out once the queue stands
        (PlayerBackend.mutation, queue_start)."""
        with self._output_mutation(self.backend) as backend:
            cleared = backend.queue_clear_after_current(archive)
            gen = self.queue.generation
            if cleared:
                gen = self.queue.clear_for_radio(self._latest_status.get("track_index"))
                self._anchor_item, self._anchor_slot = self.queue.item_at(1), 1
                backend.queue_changed("clear")
                self._schedule_persist()
        return gen

    # -- queue persistence -------------------------------------------------------
    #
    # The HQPlayer output survives backend restarts through adopt-on-attach
    # (the external player still holds the playlist). Engine-rendered outputs
    # have no external keeper, so the canonical queue is snapshotted into
    # user_settings (debounced) and restored on boot — stopped, never
    # auto-playing. Proxy-backed items are skipped: their in-memory stream
    # sessions die with the process and would restore as dead tokens. A slot
    # that is the track itself (`track`: a streamed one, one whose file left
    # the library) is kept as its identity, its way in left behind — the
    # attach resolves it again, and the lane brings its stream anew. The
    # files named may leave the library while the queue sits in storage, so
    # the restore re-binds it.

    def _schedule_persist(self) -> None:
        if self._persist_timer is not None:
            self._persist_timer.cancel()
        t = threading.Timer(1.0, self._persist_queue)
        t.daemon = True
        self._persist_timer = t
        t.start()

    def _persist_queue(self) -> None:
        try:
            from routers.settings import _write
            items = [{**asdict(it), "play": None,
                      **({"provider": None, "excerpt": False}
                         if it.source.get("kind") == "track" else {})}
                     for it in self.queue.snapshot()
                     if it.source.get("kind") in ("file", "track")]
            _write("player.queue", {"items": items})
        except Exception as e:
            logger.warning("queue persist failed: %s", e)

    def _restore_persisted_queue(self) -> None:
        try:
            from routers.settings import _read
            data = _read("player.queue") or {}
            items = [QueueItem(**d) for d in (data.get("items") or [])]
        except Exception as e:
            logger.warning("queue restore failed: %s", e)
            return
        if items and len(self.queue) == 0:
            self.queue.replace(items)
            logger.info("restored %d queued tracks from the previous session",
                        len(items))
            self.rebind_files()


manager = PlaybackManager()

# Re-binds the queue on sautium_files_removed (the media_files delete
# trigger, a Rescan that sets a copy aside as away, an import while copies
# are away, the canon's rewrite of a track's uuid) and on every (re)connect. A burst — a CUE reconcile commits per
# image — folds into few passes. The manager is looked up at each call.
def _files_changed(notifies: list) -> None:
    """The files listener's wake: the queue re-binds, with the canon's words
    among the notifications — "track <old> <new>", a track's uuid rewritten
    (canon.identity._update_track_uuid) — in the order they were said."""
    words = [n.payload.split() for n in notifies if n.payload.startswith("track ")]
    manager.rebind_files(tuple((old, new) for _word, old, new in words))


_files_listener = Listener("queue-files-listener", ("sautium_files_removed",),
                           on_notify=_files_changed,
                           on_connect=lambda: manager.rebind_files())


def reach_changed() -> None:
    """scanner._record's follower, on a thread of its own: the recorded
    answer of the music folder changed. The pages' streams are woken — each
    says what is reachable now (reach_key: the `reach` message, which an
    open album or shelf reads its payload again on) — and the queue reads
    its slots' ways in again (PlaybackManager.reach_changed). The manager
    is looked up at the call."""
    substitute.observe(())      # the folder's answer moved: a look's word of one file is over
    manager.wake_sse()
    manager.reach_followed()


def reach_key() -> str:
    """What does not answer now, as a page compares it: empty while every
    folder answers, else a digest of the gone prefixes (no path leaves the
    node in it) — the recorded fact itself, the one the album payload and
    the play routes read, so a page that follows it never waits on the
    notice, whose readers may say "unreachable" before the look has."""
    gone = gone_prefixes()
    return hashlib.sha256("\n".join(gone).encode()).hexdigest()[:12] if gone else ""


def start_files_listener() -> None:
    _files_listener.start()


def stop_files_listener() -> None:
    _files_listener.stop()


def active_hqp_endpoint():
    """The hqp_endpoints id of the HQPlayer that IS the active output, else
    None (another output, or that HQPlayer's library was never imported).
    The copy of an album held at that HQPlayer is what it plays natively —
    sql_queries.owned_rank ranks it first; a copy at any other HQPlayer is
    left to the streams."""
    b = manager.active
    if b is not None and b.id == "hqplayer":
        return b.endpoint_id
    return None
