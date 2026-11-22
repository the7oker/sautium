"""
Comparing HQPlayer's filters by ear (AB-1; sautium-private AB-1-DESIGN,
answered in its §14–§16): a sitting plays the same passage of the track
playing through two filters with nothing to tell them apart but the filter,
and keeps the person's answers.

**The engine: "play this fragment with filter X".**

- The fragment is a FLAC cut of the track's own file
  (transcode.flac_fragment_path_for_file): the passage, faded in and out
  over 20 ms, after `lead` seconds of digital silence. Both sides play the
  very same samples, to the file's end.
- The sitting holds the output (PlaybackManager.hold, as the benchmark
  does): nothing else of Sautium's drives HQPlayer, no listen is recorded,
  and every surface that names the filter shows the hold's label instead.
  It drives HQPlayer on a connection of its own (the benchmark's _Link) and
  taps HQPlayer's meter stream (hqp_meter.tap), attached to the HQPlayer
  it drives for its length.
- **A start by a resume** (§15.1, §16 Q17): HQPlayer's own start after a
  Play is not stable — Play to the sound at the output moved by up to
  0.94 s in PCM and 1.4 s in SDM on Legion's HQPlayer, its median with the
  filter (the lab run of 2026-11-17, AB-1-PROPOSAL) — and a resume from a
  pause is: 69 ms of spread in PCM, 154 ms in SDM. So a fragment is played
  into its lead, paused at the meter's first frame of it (every frame of
  the lead is all-zero: nothing is heard; the build and the unstable start
  fall here), and resumed at the moment that gives the silence its length.
- **The silence P** is the same before every fragment of a sitting. The
  warm-up (muted: HQPlayer's volume at the bottom of its range, read back)
  plays each filter the sitting may play once and measures (search.Timing)
  its output delay `d`, from the end of the fragment before to its first
  frame `start`, the meter's position when its pause landed `p`, from its
  resume to its sound at the output beyond the lead that pause left `r`
  (each fragment's resume is timed from its own `p`: a pause landing late
  moves the resume, never the sound), and how long after its last sound
  left the output its end showed (`tail`). The lead is the latest pause
  seen plus 0.3 s, never under 1 s (§16): no frame of music passes the
  meter before the resume. P is the slowest end, start and resume plus
  0.5 s (search.silence) — measured from where the sound before left the
  output, its meter's last sounding frame plus its `d`, or from the
  person's tap when that came later.
- **What wakes each step** is an event: the end of the fragment before
  (Status stopped and the meter quiet — HQPlayer sends all-zero frames
  through its output delay after the music's end, then none; the lab saw
  sinc-M's run 2 s past its delay in PCM), the meter's first frame after a
  Play, its first sounding frame after the resume, an answer. The one
  timed wait is the silence itself, ended by the resume. HQPlayer has no
  push for its Status, so the sitting reads it on its connection.
- **Every silence played is checked** from the same events: the realised
  silence (the sound's onset at the output less the anchor) within 0.15 s
  in PCM and 0.3 s in SDM (Q10, Q18: the meter reads an SDM start to
  ~0.1 s at best), the pause landed before its resume and inside the lead.
  A step that missed is played again, P grown to cover what was seen, the
  slip counted on the sitting and never stored as a duel.

**Who is compared, the order and the stops** are hqp_ab_search's. A swap
writes the context's slot only (§14.5: HQPlayer holds one filter per slot,
and a test on a 96 kHz track must not wipe the 44.1k winner).

**Put back** at the end — done, Stop, a failed focus check, a failure, the
app's shutdown — is the champion (the chain) or the filter the person had
(the pair), never a candidate, and the volume they had; the hold's release mirrors
the queue back. A sitting cut short before that (the process died) is put
back the next time HQPlayer answers (recover), from its row. A DSP change
in HQPlayer's own window, or another controller playing, ends a sitting as
left: nothing more is put back.

**What is kept** — hqp_ab_sittings and hqp_ab_duels — is the person's own
record on this node: in the node backup (life data, life_merge), never in
the sync, carry, the share export, the seed or a support bundle. No log
line at info names a filter heard or a pick.
"""

import logging
import random
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import psycopg2

from db_pool import db_execute, db_query, db_query_one
from hqp_rate_check import BASE_LIMIT, Filter as RateFilter, mode_kind, refusal, same_family

from playback import hqp_ab_search as search
from playback import hqp_meter

logger = logging.getLogger(__name__)

LABEL = "Comparing filters"               # W19
LENGTH_DEFAULT_S = 10.0
LENGTH_MIN_S, LENGTH_MAX_S = 3.0, 30.0
FADE_S = 0.02                             # Q3
# Of the passage, what a warm-up play plays: it measures the starts and
# the delays, never the content
WARM_S = 1.0
# The meter's quiet that ends a fragment (with Status stopped): the meter's
# own gap between runs of sound
QUIET_S = hqp_meter.RUN_GAP_S
# Deadlines of an event that does not come: HQPlayer building (Play to its
# first frame), a sound after the resume, a tail, Status naming a delay
FIRST_FRAME_S = 90.0
SOUND_S = 15.0
END_S = 90.0
DELAY_S = 15.0
STATUS_TICK_S = 0.25
# How long the sitting's own link waits on one answer before it asks again on
# a new connection: its builds are paid in the paused lead, bounded by
# FIRST_FRAME_S, never by the benchmark's one-minute test
ANSWER_S = 45.0
# A step whose silence missed this often in a row is not played again: the
# sitting ends (what grows P after a miss converges in a step or two)
SLIPS_A_STEP = 3
# A warm-up play whose pause landed in the music is tried again on a longer
# lead this often
WARM_TRIES = 3
# A sitting waiting this long for the person (a tab closed, a phone asleep)
# gives the output back: it never holds listening hostage
IDLE_S = 20 * 60
_DB_EPS = 0.05

WORDS = {
    "late": "HQPlayer starts it too late here to compare it",
    "not_measured_here": "Speed not tested on this computer · The Benchmark tests them",   # W16
    "too_slow": "Too slow here: {speed}×",
    "too_slow_implied": "Too slow here: slower than at {from_rate}",
    "rate": "It would make HQPlayer stop on this track: it does not convert {src} to {out}",
    "not_in_atlas": "Not in Sautium's measurements for this context",
    "slow_start": "Over a minute to start here",
    "refused": "HQPlayer refuses it at this setting",
    "unstarted": "HQPlayer never got it going on this computer",
    "identical": "The same output as {other}, so one of them is offered.",
    "other_rate": "Left out: it plays at {rate} here, and your DAC's display would show it.",   # W18
    "listed": "HQPlayer does not list it now",
}

# Why no filter is left to compare yours with, by the reason that left out
# the most (Screened.dominant); `way`: what the sheet offers next to it
EMPTY = {
    "not_measured_here": ("No filter is speed-tested on this computer for {mode} on HQPlayer "
                          "{engine} yet: run the Benchmark, then compare."),
    "too_slow": "Every other filter is too slow here at {out}.",
    "rate": "Every other filter would make HQPlayer stop on this track: none converts {src} to {out}.",
    "not_in_atlas": "No other filter is in Sautium's measurements for this context.",
    "slow_start": "Every other filter took over a minute to start here.",
    "refused": "HQPlayer refuses every other filter at this setting.",
    "unstarted": "HQPlayer never got the other filters going on this computer.",
    "identical": "Every other filter gives the same output as yours here.",
    "other_rate": "Every other filter plays at another rate here, and your DAC's display would show it.",
    "late": "HQPlayer starts every other filter too late here to compare them.",
    "listed": "HQPlayer lists no other filter now.",
}
EMPTY_WAY = {"not_measured_here": "benchmark"}


class Refused(RuntimeError):
    """Why a sitting cannot start, in words for the person; `code` for the
    sheet where it offers something (fixed_volume: Go on)."""

    def __init__(self, words: str, code: Optional[str] = None, way: Optional[str] = None):
        super().__init__(words)
        self.code = code
        self.way = way


class _Stopped(Exception):
    """The person stopped the sitting, or the app is stopping."""


class _Left(Exception):
    """HQPlayer was changed or taken by someone else: nothing more is put back."""


class _Slip(Exception):
    """A fragment missed its silence (§2.5 step 5)."""


class _WarmLate(Exception):
    """A warm-up play's pause landed with the music near or past (§16):
    played again on a longer lead."""

    def __init__(self, p: float):
        super().__init__(p)
        self.p = p


@dataclass
class Played:
    out_onset: float            # its sound at the output (time.time())
    end_out: float              # its last sound at the output
    ended: float                # its end showed: Status stopped, the meter quiet
    timing: Optional[search.Timing] = None


_lock = threading.Lock()
_sitting: Optional["Sitting"] = None
# A put-back HQPlayer answered and did not take, by sitting row: what it
# refused last, (the filter, the volume) — the same refusal twice closes the
# row (_refused_again)
_refusals: Dict[int, Tuple[Optional[str], Optional[float]]] = {}
# Sitting rows a put-back is under way for (_recover), as the benchmark's:
# a switch away meanwhile leaves the row to that put-back's end
_putting: set = set()


def current() -> Optional["Sitting"]:
    return _sitting


def running() -> bool:
    """A sitting going: over once it has ended — before its hold's release,
    as the benchmark's job is closed before its output goes back, so the
    attach that takes the output back puts back a row the sitting still
    owes (recover)."""
    s = _sitting
    return s is not None and s.thread is not None and s.thread.is_alive() and s.ended is None


# -- the context of the track playing ------------------------------------------------

def context_of(kind: str, nx: int, x1: int, src_rate: int, rate_out: int) -> Dict[str, Any]:
    """The three facts a sitting is kept under (§14.6): the mode, the slot
    the track runs (running_slot: the 1x one under 50 kHz when it is set)
    and whether the conversion leaves the source's rate family."""
    slot = "1x" if x1 is not None and x1 >= 0 and src_rate < BASE_LIMIT else "nx"
    return {"mode": kind, "slot": slot, "fractional": not same_family(rate_out, src_rate),
            "src_rate": src_rate, "rate_out": rate_out}


def atlas_key(slot: str, src_rate: int) -> Tuple[str, int]:
    """The atlas's context for a slot and a source family, and its source
    rate: the app subset measures PCM at 44.1k and 48k for 1x, 88.2k and
    96k for Nx — the axes are read there for every source of the family."""
    fam44 = src_rate % 11025 == 0
    if slot == "1x":
        return ("1x-44.1k-705.6k", 44100) if fam44 else ("1x-48k-768k", 48000)
    return ("nx-88.2k-705.6k", 88200) if fam44 else ("nx-96k-768k", 96000)


def context_words(ctx: Dict[str, Any]) -> str:
    from hqp_atlas import rate_name
    return (f"{ctx['mode'].upper()} {ctx['slot']} · {rate_name(ctx['src_rate'])} → "
            f"{rate_name(ctx['rate_out'])}" + (" · converted across rate families"
                                               if ctx["fractional"] else ""))


def picks(endpoint_id: int, engine: str, ctx: Dict[str, Any]) -> Dict[str, int]:
    """"your pick · N" (§14.5): in how many sittings of this context each
    filter ended as their champion or won the pair — their own record, never
    a ranking: no list is sorted by it."""
    rows = db_query("""
        SELECT picked, count(*)::int AS n FROM hqp_ab_sittings
         WHERE hqp_endpoint_id = %(e)s AND engine = %(g)s AND mode = %(m)s::hqp_ab_mode
           AND slot = %(s)s::hqp_ab_slot AND fractional = %(f)s
           AND ended IN ('done', 'stopped', 'left') AND picked IS NOT NULL
         GROUP BY picked
    """, {"e": endpoint_id, "g": engine, "m": ctx["mode"], "s": ctx["slot"], "f": ctx["fractional"]})
    return {r["picked"]: r["n"] for r in rows}


# -- the door ------------------------------------------------------------------------

def open_sitting(form: str, second: Optional[str] = None, *,
                 fixed_volume_ok: bool = False) -> Dict[str, Any]:
    """The door's tap: read the track and HQPlayer, screen the filters and
    start the sitting's thread — the warm-up first (§14.2). Raises Refused."""
    global _sitting
    if form not in ("chain", "pair"):
        raise Refused("Choose Find my filter or Two filters, blind")
    with _lock:
        if running():
            raise Refused("A comparison is already going on")
        s = Sitting.read(form, second, fixed_volume_ok)
        _sitting = s
        s.start()
    return s.view()


def command(name: str, sitting: Optional[str] = None, **args) -> Dict[str, Any]:
    """A tap on the sheet, handed to the sitting's thread — `sitting` the
    key of the one the sheet shows: a sheet left from one before (another
    device's) acts on no later one."""
    s = _sitting
    if s is None or (sitting is not None and sitting != s.key):
        raise Refused("No comparison is going on")
    if name == "close":
        return close(sitting)
    if name == "answer" and args.get("answer") not in (("1", "2", "same") if s.form == "pair"
                                                      else ("better", "worse", "same")):
        raise Refused("That answer is not one of this form's")
    if not s.ask(name, args):
        raise Refused("Not now: the comparison is not waiting for that")
    return s.view()


def close(sitting: Optional[str] = None) -> Dict[str, Any]:
    """The sheet closed: a sitting still going stops (put back), and its
    result is let go — only the one the sheet showed (`sitting`, its key)."""
    global _sitting
    s = _sitting
    if s is not None and sitting is not None and sitting != s.key:
        return {"phase": "idle"}
    if s is not None and s.thread is not None and s.thread.is_alive():
        if running():
            s.ask("stop", {})
        s.thread.join(30)       # through its hold's release: the door opens again after it
    with _lock:
        if _sitting is s and not running():
            _sitting = None
    return {"phase": "idle"}


def view() -> Dict[str, Any]:
    s = _sitting
    return s.view() if s is not None else {"phase": "idle"}


def shutdown(timeout: float) -> None:
    """The app is stopping: a sitting stops and puts HQPlayer back within
    `timeout`; one that cannot is put back at the next answer (recover)."""
    s = _sitting
    if s is None or s.thread is None or not s.thread.is_alive():
        return
    s.halt()
    s.thread.join(timeout)


class Sitting:
    def __init__(self) -> None:
        self.thread: Optional[threading.Thread] = None
        self.key = uuid.uuid4().hex
        self.was_playing = False
        self.tapped_at = 0.0
        self._cv = threading.Condition()
        self._asks: deque = deque()
        self._stopping = False
        self._halting = False
        self._reads: List[Tuple[float, float, Tuple[bool, ...]]] = []
        self.seq = 0
        self.phase = "warming"
        self.row: Optional[int] = None
        self.ended: Optional[str] = None
        self.note: Optional[str] = None
        self.say: Optional[Dict[str, Any]] = None
        self.sounding: Optional[Dict[str, Any]] = None
        # Each fragment as the sitting timed it, once its sound showed
        # (_fragment): the anchor its silence is measured from, the meter's
        # position when its pause landed, the resume, its sound at the
        # output, the P it was played at, and whether its silence missed — a
        # silence is checked against the sitting's own events (B-119)
        self.fragments: List[Dict[str, float]] = []
        self.result: Optional[Dict[str, Any]] = None
        self.timings: Dict[str, search.Timing] = {}
        self.lead_s = search.LEAD_MIN_S
        self.silence_s: Optional[float] = None
        self.slipped = 0
        self.learned = 0              # plays of the passage since it or the form changed
        self.learn_total = 0
        self.warmed = (0, 0)
        self.chain: Optional[search.Chain] = None
        self.pair: Optional[search.Pair] = None
        self.step_view: Optional[Dict[str, Any]] = None
        self.set_now: Optional[str] = None
        self.sending: Optional[str] = None
        self.refused: Optional[Tuple[Optional[str], Optional[float]]] = None
        self.link = None
        self._halt = threading.Event()
        self._tokens: Tuple[str, ...] = ()
        # What the sitting waits for an answer to: one answer (or cost
        # sheet's decision) a window, taken under the condition (ask)
        self._open_for: Optional[str] = None
        # The warm-up's cut's lead; one whose pause landed late grows it
        self.warm_lead = search.LEAD_MIN_S
        self.left_rates: Dict[str, int] = {}

    # -- reading the door -------------------------------------------------------------

    @classmethod
    def read(cls, form: str, second: Optional[str], fixed_volume_ok: bool) -> "Sitting":
        from playback import hqp_link
        from playback.manager import manager
        from hqplayer_client import PlaybackState
        import hqp_library
        b = manager.active
        if b is None or b.id != "hqplayer":
            raise Refused("Comparing filters needs HQPlayer as the audio output")
        if manager.held is not None:
            raise Refused(f"{manager.held['label']} — compare once it gives the output back")
        link = hqp_link.chosen()
        busy = link.busy()
        if busy is not None:
            raise Refused(f"{busy}. Compare once it answers again.")
        ep = hqp_library.driven_endpoint()
        if ep is None:
            raise Refused("This HQPlayer is not registered yet — try again once it has played")
        # What it reads may be the last sitting's candidate, lowered: that is
        # put back first, never taken for the person's own — asked before
        # anything is read, so a put-back under way is never read half done
        if owed(ep["id"]) is not None:
            recover(ep["id"], link)
            raise Refused("Sautium is still putting HQPlayer back from the last comparison — "
                          "compare again in a moment")
        try:
            with link.asking() as c:
                info, st, status = c.get_info(), c.get_state(), c.get_status()
                modes, rates, filters = c.get_modes(), c.get_rates(), c.get_filters()
                shapers, vr = c.get_shapers(), c.volume_range()
        except (hqp_link.Building, hqp_link.NotAnswering, hqp_link.Dropped) as e:
            raise Refused(f"{e}. Compare once it answers again.")
        except (BrokenPipeError, ConnectionError, OSError):
            raise Refused(hqp_link.NOT_ANSWERING)
        if None in (info, st, status, modes, rates, filters, shapers):
            raise Refused("HQPlayer did not report its state — try again")
        if b.reads_foreign(status):
            raise Refused("Another controller is playing through HQPlayer — compare once "
                          "Sautium is its source again")
        s = cls()
        s.form = form
        s.ep = ep
        s.engine = s.ep.get("hqp_engine") or info.get("engine") or ""
        s.track = _track(manager)
        s.was_at = min(max(float(manager.latest_status.get("position") or 0.0), 0.0),
                       max(s.track["length"] - LENGTH_DEFAULT_S, 0.0))
        # Where the queue stood: the hold's release leaves it there, stopped
        # (§2.4 — the sitting's own position, never a candidate's playlist)
        from playback.base import Spot
        here = manager.spot()
        s.spot = None if here is None else Spot(here.slot, here.position, False)
        s.from_s, s.length_s = s.was_at, min(LENGTH_DEFAULT_S, s.track["length"])
        mode_name = next((m["name"] for m in modes if m["index"] == st["mode"]), None)
        kind = mode_kind(mode_name) or mode_kind(status.active_mode)
        if kind is None:
            raise Refused("HQPlayer's mode follows the source — choose PCM or SDM to compare")
        playing = status.state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
        s.was_playing = playing
        rate_out = (status.active_rate if playing and status.active_rate
                    else next((r["rate"] for r in rates if r["index"] == st["rate"]), 0))
        if not rate_out:
            raise Refused("HQPlayer is stopped and its output rate is Auto: press Play, then "
                          "compare — or choose a fixed output rate in HQPlayer.")
        s.vr, s.volume = vr, st["volume"]
        s.mute = _mute_level(vr, st["volume"])
        if (vr is None or not vr["enabled"]) and not fixed_volume_ok:
            raise Refused("HQPlayer's volume is fixed, so you will hear short bursts while "
                          "it gets ready.", code="fixed_volume")      # W5
        s.state, s.lists = st, {"filters": filters, "shapers": shapers, "rates": rates, "modes": modes}
        s.index = {f["name"]: f["index"] for f in filters}
        s.before_nx, s.before_x1 = st["filterNx"], st["filter1x"]
        names = {f["index"]: f["name"] for f in filters}
        s.ctx = context_of(kind, st["filterNx"], st["filter1x"], s.track["rate"], rate_out)
        s.tolerance = search.TOLERANCE_S[kind]
        own_index = st["filter1x"] if s.ctx["slot"] == "1x" else st["filterNx"]
        if own_index not in names:
            raise Refused("HQPlayer did not name the filter it runs — try again")
        s.before = {"nx": names.get(st["filterNx"]), "1x": names.get(st["filter1x"])}
        shaper = next((x["name"] for x in shapers if x["index"] == st["shaper"]), "")
        s.atlas, s.screened, s.second_list, s.champion = _screen(
            s, filters, names[own_index], shaper, mode_name or kind.upper())
        s.yours = s.champion.name
        s.picks = picks(s.ep["id"], s.engine, s.ctx)
        s.second = None
        if form == "chain":
            s._chain_pool()
        else:
            s._choose_second(second)
        return s

    def _chain_pool(self) -> None:
        if self.atlas is None:
            raise Refused("Find my filter comes with Sautium's measurements of HQPlayer's "
                          "filters.", code="chain")                                             # W15
        if self.champion.at is None:
            raise Refused("Find my filter needs your filter in Sautium's measurements for "
                          "this context; Two filters, blind works without them.", code="chain")
        if not self.screened.candidates:
            # Nothing to compare with before any answer: why, never the answers
            words, way = self._empty(self.screened)
            raise Refused(words, code="chain", way=way)
        s = search.Search(self.screened.candidates, self.champion)
        search.replay(s, _logged(self.ep["id"], self.engine, self.ctx))
        pool = search.pool(s)
        if not pool:
            raise Refused("Nothing left to compare yours with here: every kind of difference "
                          "was heard as none, or none is left to try.", code="chain")
        self.pool, self.search = pool, s.copy(pool)

    def _empty(self, screened: search.Screened) -> Tuple[str, Optional[str]]:
        """Why a list has no filter to compare with, by the reason that left
        out the most, and the way it offers (the Benchmark's, for filters
        not speed-tested here)."""
        from hqp_atlas import rate_name
        reason = screened.dominant() or "listed"
        return (EMPTY.get(reason, EMPTY["listed"]).format(
                    mode=self.ctx["mode"].upper(), engine=self.engine or "this engine",
                    src=rate_name(self.ctx["src_rate"]), out=rate_name(self.ctx["rate_out"])),
                EMPTY_WAY.get(reason))

    def _choose_second(self, second: Optional[str]) -> None:
        if second is None:
            self.second = None
            return
        if second not in {f.name for f in self.second_list.candidates}:
            why = next((r for r, names in self.second_list.left_out.items() if second in names), None)
            raise Refused(f"{second} cannot be compared here: "
                          + (self._reason_words(why, second) if why else "HQPlayer does not list it now"))
        self.second = second

    # -- the thread ---------------------------------------------------------------------

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True, name="hqp-ab")
        self.thread.start()

    def ask(self, name: str, args: Dict[str, Any]) -> bool:
        """Hand a tap to the thread. An answer or a decision is taken only
        while the sitting waits for one, and only one: a second device's,
        or a late duplicate, never answers a step it did not hear."""
        with self._cv:
            if name in ("answer", "decide"):
                if self._open_for != name:
                    return False
                self._open_for = None
            if name == "stop":
                self._stopping = True
            self._asks.append((name, args))
            self._cv.notify_all()
            return True
        # A Stop never interrupts the link: a command it cut would have gone
        # out unanswered (a Play HQPlayer carries out, unpaused, while the
        # put-back reconnects), and every wait of the sitting wakes on it
        # anyway. Only the app's stop (halt) interrupts.

    def halt(self) -> None:
        self._halt.set()
        with self._cv:
            self._halting = self._stopping = True
            self._cv.notify_all()
        if self.link is not None:
            self.link.interrupt()

    def _publish(self) -> None:
        from playback.manager import manager
        self.seq += 1
        manager.update_hold(phase=self.phase, seq=self.seq)

    def _run(self) -> None:
        from playback.manager import manager
        from playback.hqp_benchmark import _Link, _Lost, _Reopened
        try:
            with manager.hold("ab", LABEL, spot=self.spot):
                self._publish()
                link = _Link(self._halt.wait, answer_s=ANSWER_S)
                if not link.connect():
                    self._finish("failed", "HQPlayer is not answering")
                    return
                self.link = link
                ended, note = "failed", None
                try:
                    self._open_row()
                    self._tap(link)
                    ended, note = self._sitting()
                except _Left as e:
                    ended, note = "left", str(e)
                except (_Stopped, _Lost, _Reopened, Refused) as e:
                    # A Stop wakes a read waiting on HQPlayer by dropping its
                    # connection: what that read raised is the Stop's
                    if self._halting:
                        ended, note = "failed", "the app stopped"
                    elif self._stopping:
                        ended, note = "stopped", self.note       # "left waiting" (IDLE_S)
                        self._stopped_result()
                    else:
                        ended, note = "failed", str(e) or type(e).__name__
                except Exception as e:      # the thread's boundary: recorded, never lost
                    logger.exception("Comparison failed")
                    ended, note = "failed", f"{type(e).__name__}: {e}"
                finally:
                    try:
                        hqp_meter.meter.untap(self)
                        own = ended
                        put = False
                        if ended != "left":
                            try:
                                put = self._put_back()
                            except _Left as e:
                                ended, note = "left", str(e)
                        if not put and self.refused is not None and self.row is not None:
                            _refused_again(self.row, self.refused)      # the first of two
                        self._close_row(ended, put, own)
                    finally:
                        self.link = None
                        link.close()
                self._finish(ended, note)
        except Exception as e:              # the output could not be lent
            logger.warning("Comparison did not start: %s", e)
            self._finish("failed", str(e))

    def _stopped_result(self) -> None:
        """Stop during the test shows what was answered so far, as the end of
        a sitting does (the step owed its answer is not counted)."""
        if self.chain is not None:
            deciding = self.chain.deciding
            self.chain.stop()
            if deciding is not None:
                self._duel_swapped(deciding)        # the cost sheet left open: declined (§5.2)
            self.result, self.phase = self._chain_result(self.chain), "result"
        elif self.pair is not None and self.pair.duels:
            self.pair.stop()
            self.result, self.phase = self._pair_result(self.pair), "result"

    def _finish(self, ended: str, note: Optional[str]) -> None:
        """Said once the sitting is over, before its hold's release: the
        status that release pushes tells the sheet to read the end."""
        from playback.manager import manager
        self.ended, self.note = ended, note
        if self.phase not in ("result", "break"):
            self.phase = "ended"
        self.sounding = None
        self.step_view = None
        logger.info("Comparison ended: %s%s", ended, f" ({note})" if note else "")
        self.seq += 1
        if manager.held is not None and manager.held.get("by") == "ab":
            manager.update_hold(phase=self.phase, seq=self.seq)
        else:
            # It never held the output: no hold's release will say it ended
            manager.publish_status()

    def _tap(self, link) -> None:
        """The meter of the HQPlayer it drives, attached for the sitting:
        the hold detached the backend's (hqp_meter.detach), whose next
        attach takes it back."""
        self.tapped_at = time.time()
        att = hqp_meter.meter.attach(link.c.host, link.c.port + 1)
        hqp_meter.meter.tap(self, att, self._on_read)

    def _on_read(self, at: float, hop: float, sounds: Tuple[bool, ...]) -> None:
        with self._cv:
            self._reads.append((at, hop, sounds))
            self._cv.notify_all()

    # -- what the person asked ------------------------------------------------------------

    def _open(self, kind: str) -> None:
        """An answer (or the cost sheet's decision) is taken from now — set
        before the sheet is told, so the first tap it allows is never refused."""
        with self._cv:
            self._open_for = kind

    def _take(self, wanted: Sequence[str]) -> Tuple[str, Dict[str, Any]]:
        """The next tap among `wanted`, waited for; Stop at any moment, and
        IDLE_S with none ends the sitting as stopped."""
        idle = time.monotonic() + IDLE_S
        with self._cv:
            try:
                while True:
                    if self._stopping:
                        raise _Stopped()
                    while self._asks:
                        name, args = self._asks.popleft()
                        if name in wanted:
                            return name, args
                    left = idle - time.monotonic()
                    if left <= 0:
                        self._stopping = True
                        self.note = "left waiting"
                        raise _Stopped()
                    self._cv.wait(left)
            finally:
                self._open_for = None

    # -- the sitting --------------------------------------------------------------------

    def _sitting(self) -> Tuple[str, Optional[str]]:
        link = self.link
        link("stop")
        self._cut()
        # What the person was listening to plays out its tail first: its
        # all-zero frames would be taken for the first warm-up play's own
        self._end(theirs=True)
        names = [self.yours] + (self.pool if self.form == "chain"
                                else [self.second] if self.second else [])
        self._warm(names)
        self.phase = "ready"
        self._publish()
        while True:
            name, args = self._take(("learn", "passage", "form", "begin"))
            self.say = None
            if name == "passage":
                self._passage(float(args["from_s"]), float(args["length_s"]))
            elif name == "form":
                try:
                    self._form(args["form"], args.get("second"))
                except Refused as e:
                    # A form this sitting cannot take is said on the sheet; the
                    # sitting goes on in the one it had
                    self.say = {"line": str(e)}
            elif name == "learn" and self.form == "pair" and self.second is None:
                self.say = {"line": "Choose the filter to compare yours with"}
            elif name == "learn":
                self._learn()
            elif self.form == "pair" and self.second is None:
                self.say = {"line": "Choose the filter to compare yours with"}
            else:
                break
            self._publish()
        self._note_row(form=self.form)
        return self._test_chain() if self.form == "chain" else self._test_pair()

    def _cut(self) -> None:
        """The passage and the warm-up's play of it, cut at the sitting's lead."""
        from streaming import transcode
        from playback.hqp_benchmark import _signal_uri
        t = self.track
        at = t["offset"] + self.from_s
        self.cut = _signal_uri(_path(transcode.flac_fragment_path_for_file(
            t["path"], at, self.length_s, lead=self.lead_s, fade=FADE_S, bits=t["bits"])))
        self.warm_cut = _signal_uri(_path(transcode.flac_fragment_path_for_file(
            t["path"], at, min(WARM_S, self.length_s), lead=self.warm_lead, fade=FADE_S,
            bits=t["bits"])))
        # How HQPlayer's Status names them: by the proxy token, the URL's last part
        self._tokens = tuple(u.rsplit("/", 1)[-1] for u in (self.cut, self.warm_cut))

    def _passage(self, from_s: float, length_s: float) -> None:
        length_s = min(max(length_s, LENGTH_MIN_S), LENGTH_MAX_S, self.track["length"])
        self.from_s = min(max(from_s, 0.0), self.track["length"] - length_s)
        self.length_s = length_s
        self.learned = 0
        self._cut()
        self._note_row(from_s=self.from_s, length_s=self.length_s)

    def _form(self, form: str, second: Optional[str]) -> None:
        """Another form, or another second: what it plays is warmed up first.
        A refusal on the way — the chain's, the warm-up's — leaves the
        sitting as it was."""
        had = (self.form, self.second, self.__dict__.get("pool"), self.__dict__.get("search"))
        self.learned = 0
        try:
            if form == "chain":
                fresh = "pool" not in self.__dict__
                if fresh:
                    self._chain_pool()
                self.form = "chain"
                if fresh:
                    self._warm([n for n in self.pool if n not in self.timings])
                return
            self._choose_second(second)
            self.form = "pair"
            if self.second is not None and self.second not in self.timings:
                self._warm([self.second])
        except Refused:
            self.form, self.second = had[0], had[1]
            for key, value in (("pool", had[2]), ("search", had[3])):
                if value is None:
                    self.__dict__.pop(key, None)
                else:
                    setattr(self, key, value)
            raise

    # -- the warm-up --------------------------------------------------------------------

    def _warm(self, names: List[str]) -> None:
        """Each filter played once, muted, its timing measured; one at
        another output rate than their own is left out (Q8). P and the lead
        are set from every timing so far."""
        if not names:
            return
        # In a drawn order, theirs first: the order of the candidates is the
        # search's, and with a fixed volume the person hears the warm-up
        others = [n for n in names if n != self.yours]
        names = [n for n in names if n == self.yours] + random.sample(others, len(others))
        phase, self.phase = self.phase, "warming"
        self.warmed = (0, len(names))
        self._publish()
        if self.mute is not None:
            self._volume(self.mute)
        done = left = False
        try:
            ended = time.time()
            for i, name in enumerate(names):
                for _ in range(WARM_TRIES):
                    try:
                        played = self._fragment(name, None, warm=True, since=ended)
                        break
                    except _WarmLate as e:
                        self.warm_lead = max(self.warm_lead + 0.5, e.p + search.LEAD_ABOVE_S)
                        self._cut()
                        ended = time.time()     # the recut is no part of the next try's start
                else:
                    if name == self.yours:
                        raise Refused("HQPlayer starts your filter too late here to compare it")
                    self._leave("late", name)
                    continue
                ended = played.ended
                self.timings[name] = played.timing
                self.warmed = (i + 1, len(names))
                self._publish()
            done = True
        except _Left:
            left = True             # what plays is another controller's: never stopped
            raise
        finally:
            if not done and not left:
                # Cut short: what still plays stops before the volume comes back
                self.link("stop")
            self._unmute()
            self.phase = phase if phase != "warming" else "ready"
        own = self.timings[self.yours].rate
        for name in names:
            if name in self.timings and self.timings[name].rate != own:
                rate = self.timings.pop(name).rate
                self.left_rates[name] = rate
                self._leave("other_rate", name, rate=rate)
        self._settle()

    def _leave(self, reason: str, name: str, **kw) -> None:
        """Out of the sitting, whichever form asked for it, said with its reason."""
        for screened in (self.screened, self.second_list):
            if name not in sum(screened.left_out.values(), []):
                screened.leave(reason, name)
            screened.candidates = [f for f in screened.candidates if f.name != name]
        if name in self.__dict__.get("pool", []):
            self.pool.remove(name)
            self.search = self.search.copy(self.pool)
        if self.second == name:
            self.second = None
            self.say = {"line": self._reason_words(reason, name, **kw)}

    def _settle(self) -> None:
        """The lead and P from the timings measured so far; a longer lead
        cuts the passage again."""
        lead = search.lead(self.timings.values())
        if lead > self.lead_s:
            self.lead_s = lead
            self._cut()
        self.silence_s = max(self.silence_s or 0.0,
                             search.silence(list(self.timings.values()), self.lead_s))
        self._note_row(lead_s=self.lead_s, silence_s=self.silence_s)

    def _unmute(self) -> None:
        """Their volume back — only while it still reads the warm-up's: one
        changed since is someone's own."""
        if self.mute is None:
            return
        st = self.link("get_state")
        if st is not None and abs(st["volume"] - self.mute) <= _DB_EPS:
            self._volume(self.volume)

    def _volume(self, value: float) -> None:
        link = self.link
        link("set_volume", value)
        st = link("get_state")
        if st is None or abs(st["volume"] - value) > _DB_EPS:
            raise Refused("HQPlayer did not take the volume — the comparison stops")

    # -- one fragment (§15.1) -----------------------------------------------------------

    def _fragment(self, name: str, anchor: Optional[float], *, warm: bool = False,
                  since: Optional[float] = None, label: Optional[str] = None) -> Played:
        """Play `name`'s fragment: into its lead, paused at the meter's first
        frame, resumed so its sound leaves the output P after `anchor`
        (the warm-up: at once), and waited out to its end. Raises _Slip
        when its silence missed — the fragment is stopped and waited out."""
        link = self.link
        timing = None if warm else self.timings[name]
        began = time.time() if since is None else since
        self._set(name)
        with self._cv:
            self._reads = []
        lead = self.warm_lead if warm else self.lead_s
        link("playlist_add", self.warm_cut if warm else self.cut, clear=True)
        played_at = time.time()
        if not link("play"):
            raise Refused(f"HQPlayer did not play the passage ({link.refusal()})")
        self._wait(lambda: self._first_frame(played_at), FIRST_FRAME_S, "frame of the passage")
        link("pause")
        landed = time.time()
        p = self._frames_until(landed)
        h = landed - began
        if warm:
            if p > lead - search.LEAD_SLIP_S:
                self._stop_out()
                raise _WarmLate(p)
            d, rate = self._delay()
            resume = time.time()
        else:
            d, rate = timing.d, timing.rate
            # the lead this pause left, not the warm-up's: a pause landing
            # late moves the resume, never the sound
            resume = anchor + self.silence_s - (lead - p + timing.r)
            if landed > resume:
                self._slipped(name, start=h - p, late=landed - resume)
            if p > lead - search.LEAD_SLIP_S:
                self._slipped(name, p=p)
            self._sleep_until(resume, label)
        resumed = time.time()
        link("play")
        if self._sounded_before(resumed):
            if warm:
                self._stop_out()
                raise _WarmLate(lead)
            # sounded before its resume: missed, its sound out all the same
            self.fragments.append({"anchor": anchor, "p": p, "resume": resume, "out_onset": None,
                                   "silence_s": self.silence_s, "slipped": True})
            self._slipped(name, p=lead)
        onset = self._wait(lambda: self._onset(resumed), SOUND_S + self.lead_s, "sound of the passage")
        out_onset = onset + d
        if not warm:
            self.fragments.append({"anchor": anchor, "p": p, "resume": resume,
                                   "out_onset": out_onset, "silence_s": self.silence_s})
            self.sounding = {"which": label, "in_s": round(max(out_onset - time.time(), 0.0), 3),
                             "length_s": self.length_s}
            self._publish()
            realised = out_onset - anchor
            self.fragments[-1]["slipped"] = abs(realised - self.silence_s) > self.tolerance
            if self.fragments[-1]["slipped"]:
                self._slipped(name, r=out_onset - resumed - (lead - p))
        last, ended = self._end()
        end_out = last + d
        self.sounding = None
        t = search.Timing(d=d, start=h - p, r=out_onset - resumed - (lead - p), p=p,
                          tail=max(ended - end_out, 0.0), rate=rate)
        return Played(out_onset, end_out, ended, t)

    def _stop_out(self) -> None:
        self.link("stop")
        self._end()
        self.sounding = None

    def _slipped(self, name: str, *, start: float = 0.0, p: float = 0.0, r: Optional[float] = None,
                 late: float = 0.0) -> None:
        """A silence missed: stopped, waited out, and what was seen taken in —
        a pause that landed `late` after its resume was due grows P by that
        (the end before it, or its start, took longer than the warm-up
        saw); one too near the music grows the lead; a sound that left the
        output early or late replaces the resume's way there `r`. P never
        shrinks."""
        self._stop_out()
        old = self.timings[name]
        self.timings[name] = search.Timing(
            d=old.d, start=max(old.start, start), p=max(old.p, p),
            r=old.r if r is None else r, tail=old.tail, rate=old.rate)
        if late:
            self.silence_s += late + 0.1
        self.slipped += 1
        self._settle()
        self._note_row(slipped=self.slipped)
        self.say = {"line": "The timing slipped — once more.", "slip": True}      # W14
        self._publish()
        raise _Slip()

    def _set(self, name: str) -> None:
        """The context's slot to `name` (§14.5), the other as they had it; a
        slot found changed from what the sitting set is someone else's
        change."""
        link = self.link
        st = link("get_state")
        if st is None:
            raise Refused("HQPlayer did not report its state")
        if self.set_now is not None and (st["filterNx"], st["filter1x"]) != self._slots(self.set_now):
            raise _Left("a DSP change came from elsewhere")
        nx, x1 = self._slots(name)
        if (st["filterNx"], st["filter1x"]) != (nx, x1):
            self.sending = name         # the put-back's own, should its answer be lost
            if not link("set_filter", nx, x1 if x1 is not None and x1 >= 0 else None):
                raise Refused(f"HQPlayer refused the filter ({link.refusal()})")
        self.set_now, self.sending = name, None
        self._note_row(last_set=name)

    def _slots(self, name: str) -> Tuple[int, int]:
        i = self.index[name]
        return (self.before_nx, i) if self.ctx["slot"] == "1x" else (i, self.before_x1)

    def _delay(self) -> Tuple[float, int]:
        """The filter's output delay and output rate, from Status — read
        until it gives the delay (Status lags a long filter's start)."""
        end = time.monotonic() + DELAY_S
        while True:
            status = self.link("get_status")
            if status is not None and status.output_delay is not None and status.active_rate:
                return status.output_delay, status.active_rate
            if time.monotonic() > end:
                raise Refused("HQPlayer did not report its output delay (HQPlayer 6 does)")
            self._pause(STATUS_TICK_S)

    def _end(self, theirs: bool = False) -> Tuple[float, float]:
        """The fragment's end: its last sounding frame's own time, and when
        the end showed — the meter quiet for QUIET_S and Status stopped.
        Status is read at every second of the wait as well: an entry that is
        not the sitting's playing is another controller's."""
        from hqplayer_client import PlaybackState
        deadline = time.monotonic() + END_S
        # What the person heard sends frames through its tail: before the
        # tap's session opened, no read is the meter not there yet, not quiet
        heard = theirs and self.was_playing
        while True:
            quiet = self._quiet(1.0, heard=heard)
            status = self.link("get_status")
            if quiet and status is not None and status.state == PlaybackState.STOPPED:
                with self._cv:
                    last = self._last_sound()
                return (last if last is not None else time.time()), time.time()
            if (not theirs and status is not None and status.state != PlaybackState.STOPPED
                    and status.uri and not any(t in status.uri for t in self._tokens)):
                raise _Left("another controller is playing through HQPlayer")
            if time.monotonic() > deadline:
                raise Refused("HQPlayer did not finish the passage")
            if quiet:
                self._pause(STATUS_TICK_S)

    def _quiet(self, most: float, heard: bool = False) -> bool:
        """Whether the meter has been quiet for QUIET_S, waited for at most
        `most` seconds — `heard`: with no read yet, counted from the tap's
        session's open (a tail over before it connected sends nothing), and
        not before it opened."""
        until = time.time() + most
        opened = self._session_opened() if heard else 0.0
        with self._cv:
            while True:
                if self._stopping:
                    raise _Stopped()
                if not self._reads and opened is None:
                    if time.time() >= until:
                        return False
                    self._cv.wait(until - time.time())
                    continue
                last_read = self._reads[-1][0] if self._reads else opened
                quiet_at = last_read + QUIET_S
                now = time.time()
                if now >= quiet_at:
                    return True
                if now >= until:
                    return False
                self._cv.wait(min(quiet_at, until) - now)

    # -- the meter's reads ----------------------------------------------------------------

    def _session_opened(self) -> Optional[float]:
        """When the meter's connection opened since the tap, or None."""
        return next((x["open"] for x in reversed(hqp_meter.meter.seen()["sessions"])
                     if x["open"] >= self.tapped_at), None)

    def _first_frame(self, after: float) -> Optional[float]:
        return next((at for at, _, _ in self._reads if at > after), None)

    def _frames_until(self, t: float) -> float:
        """The meter's position when the pause landed: the frames shown by
        then, a hop each — and the time since the last read, up to a read's
        length: frames come in bursts, the stream moved on between them."""
        shown = [(at, hop, s) for at, hop, s in self._reads if at <= t]
        if not shown:
            return 0.0
        at, hop, s = shown[-1]
        return sum(len(s) * hop for _, hop, s in shown) + min(t - at, len(s) * hop)

    def _sounded_before(self, t: float) -> bool:
        with self._cv:
            return any(any(s) for at, _, s in self._reads if at <= t)

    def _onset(self, after: float) -> Optional[float]:
        for at, hop, s in self._reads:
            if at > after and any(s):
                return at - (len(s) - 1 - s.index(True)) * hop
        return None

    def _last_sound(self) -> Optional[float]:
        for at, hop, s in reversed(self._reads):
            if any(s):
                last = len(s) - 1 - s[::-1].index(True)
                return at - (len(s) - 1 - last) * hop
        return None

    def _wait(self, pred: Callable[[], Optional[float]], limit: float, what: str) -> float:
        deadline = time.monotonic() + limit
        with self._cv:
            while True:
                if self._stopping:
                    raise _Stopped()
                got = pred()
                if got is not None:
                    return got
                left = deadline - time.monotonic()
                if left <= 0:
                    raise Refused(f"HQPlayer's meter showed no {what}")
                self._cv.wait(left)

    def _sleep_until(self, t: float, label: Optional[str]) -> None:
        """The silence: the one timed wait, ended by the resume."""
        self.sounding = {"which": None, "silence_s": round(self.silence_s, 2)}
        self._publish()
        with self._cv:
            while (left := t - time.time()) > 0:
                if self._stopping:
                    raise _Stopped()
                self._cv.wait(left)
            if self._stopping:
                raise _Stopped()

    def _pause(self, seconds: float) -> None:
        with self._cv:
            if self._stopping:
                raise _Stopped()
            self._cv.wait(seconds)
            if self._stopping:
                raise _Stopped()

    # -- learning the passage, the test -------------------------------------------------------

    def _learn(self) -> None:
        """▶ Play: their filter (the chain), or both, 1 then 2 in a drawn order
        (the pair) — no duel, no listen, a count on the sitting."""
        self.phase = "learning"
        self._publish()
        plays = ([self.yours] if self.form == "chain"
                 else random.sample([self.yours, self.second], 2))
        try:
            self._step(plays, time.time(), ["yours"] if self.form == "chain" else ["1", "2"])
        finally:
            self.phase = "ready"
        self.learned += 1
        self.learn_total += 1
        self._note_row(learn_plays=self.learn_total)

    def _step(self, plays: List[str], anchor: float, labels: List[str]) -> float:
        """One step's fragments in order, each P after the one before; a
        slip plays the whole step again. Returns where its last sound ended."""
        for _ in range(SLIPS_A_STEP):
            at = anchor
            try:
                for name, label in zip(plays, labels):
                    played = self._fragment(name, at, label=label)
                    at = played.end_out
                return at
            except _Slip:
                anchor = time.time()
        raise Refused("The timing kept slipping, so the comparison stops here.")

    def _test_chain(self) -> Tuple[str, Optional[str]]:
        chain = self.chain = search.Chain(self.search, self.champion)
        self.phase = "testing"
        anchor = time.time()
        while True:
            step = chain.step()
            if step is None:
                break
            labels = (["yours", "next"] if step.kind == "first"
                      else ["yours"] if step.kind == "check" else ["next"])
            self.step_view = {"kind": step.kind, "n": search.CHAIN_STEPS - chain.steps_left + 1,
                              "of": min(search.CHAIN_STEPS, len(self.pool)), "answerable": False}
            self._publish()
            self._step(step.plays, anchor, labels)
            self._open("answer")
            self.step_view["answerable"] = True
            self._publish()
            _, args = self._take(("answer",))
            anchor = time.time()
            self.step_view["answerable"] = False
            duel = chain.answer(args["answer"])
            self._duel(duel)
            if chain.deciding is not None:
                self.say = {"cost": chain.note}
                self._open("decide")
                self._publish()
                _, args = self._take(("decide",))
                duel = chain.decide(bool(args.get("make")))
                self._duel_swapped(duel)
                anchor = time.time()
            self.say = _chain_say(chain)
            self._champion(chain.champion)
            if duel.check and not duel.counted:
                self._disqualify(chain)
                self._champion(chain.champion)
                self.phase = "break"
                self.result = {"break": True}
                return "break", None
        self.result = self._chain_result(chain)
        self.phase = "result"
        return ("done" if chain.ended == "done" else "stopped"), None

    def _test_pair(self) -> Tuple[str, Optional[str]]:
        pair = self.pair = search.Pair(self.yours, self.second)
        self.phase = "testing"
        anchor = time.time()
        while True:
            order = pair.order()
            if order is None:
                break
            self.step_view = {"kind": "round", "n": len(pair.duels) + 1, "of": search.PAIR_CAP,
                              "answerable": False}
            self._publish()
            self._step(list(order), anchor, ["1", "2"])
            self.say = None             # a slip's line ends with the step it replayed
            self._open("answer")
            self.step_view["answerable"] = True
            self._publish()
            _, args = self._take(("answer",))
            anchor = time.time()
            self.step_view["answerable"] = False
            answer = {"1": "first", "2": "second", "same": "same"}[args["answer"]]
            self._duel(pair.answer(answer))
        self.result = self._pair_result(pair)
        self.phase = "result"
        self._note_row(picked=pair.winner())
        return "done", None

    def _champion(self, name: str) -> None:
        """A swap at once, before the next step (§5.1): the slot holds the
        champion, and its row says so."""
        if name != self.set_now:
            self._set(name)
        self._note_row(champion=name)

    # -- put back ------------------------------------------------------------------------

    def _put_back(self) -> bool:
        """The champion (the chain) or their filter (the pair) in its slot,
        and their volume — stopped first. Only over the sitting's own marks
        (§2.4): another controller playing, or slots it did not set, raise
        _Left and nothing is sent; the volume only while it reads the
        warm-up's. Run again when the connection was replaced under it (a
        Stop drops the read it woke). False when HQPlayer did not take it:
        the row owes it (recover)."""
        from hqplayer_client import PlaybackState
        from playback.hqp_benchmark import _again, _Lost
        link = self.link
        if not self._tokens:
            return True         # ended before its first Stop: it changed nothing to put back
        want = self.chain.champion if self.chain is not None else self.yours
        nx, x1 = self._slots(want)
        # Slots the sitting set: the filter it set last, or the one it was
        # setting (a SetFilter whose answer was lost) — whatever phase it was
        # in — or the person's own before it. Any other is a change made in
        # HQPlayer's window, a warmed candidate included.
        own = {(self.before_nx, self.before_x1), (nx, x1)} | {
            self._slots(n) for n in (self.set_now, self.sending) if n is not None}

        def once() -> bool:
            st, status = link("get_state"), link("get_status")
            if st is None or status is None:
                return False
            if (status.state != PlaybackState.STOPPED and status.uri
                    and not any(t in status.uri for t in self._tokens)):
                raise _Left("another controller is playing through HQPlayer")
            if (st["filterNx"], st["filter1x"]) not in own:
                raise _Left("a DSP change came from elsewhere")
            link("stop")
            lowered = self.mute is not None and abs(st["volume"] - self.mute) <= _DB_EPS
            if lowered:
                link("set_volume", self.volume)
            if (st["filterNx"], st["filter1x"]) != (nx, x1):
                link("set_filter", nx, x1 if x1 is not None and x1 >= 0 else None)
            st = link("get_state")
            if st is None:
                return False
            self.refused = _refusal(st, (nx, x1), want, self.volume if lowered else None)
            return self.refused is None
        try:
            return _again(once)
        except (_Lost, OSError) as e:
            logger.warning("Comparison: HQPlayer not put back yet (%s) — at its next answer", e)
            return False

    # -- the record ------------------------------------------------------------------------

    def _open_row(self) -> None:
        self.row = db_query_one("""
            INSERT INTO hqp_ab_sittings
                   (hqp_endpoint_id, engine, mode, slot, fractional, form, track_id, from_s,
                    length_s, tolerance_s, before_filter_nx, before_filter_1x, before_volume,
                    muted_to, champion)
            VALUES (%(e)s, %(g)s, %(m)s, %(s)s, %(f)s, %(form)s, %(t)s, %(from)s, %(len)s, %(tol)s,
                    %(bnx)s, %(b1x)s, %(vol)s, %(mute)s, %(champ)s)
            RETURNING id
        """, {"e": self.ep["id"], "g": self.engine, "m": self.ctx["mode"], "s": self.ctx["slot"],
              "f": self.ctx["fractional"], "form": self.form, "t": self.track["track_id"],
              "from": self.from_s, "len": self.length_s, "tol": self.tolerance,
              "bnx": self.before["nx"], "b1x": self.before["1x"], "vol": self.volume,
              "mute": self.mute, "champ": self.yours})["id"]

    _COLUMNS = {"form": "%s::hqp_ab_form", "from_s": "%s", "length_s": "%s", "lead_s": "%s",
                "silence_s": "%s", "slipped": "%s", "learn_plays": "%s", "champion": "%s",
                "picked": "%s", "last_set": "%s"}

    def _note_row(self, **cols) -> None:
        if self.row is None:
            return
        sets = ", ".join(f"{k} = {self._COLUMNS[k] % ('%(' + k + ')s')}" for k in cols)
        db_execute(f"UPDATE hqp_ab_sittings SET {sets} WHERE id = %(id)s", {**cols, "id": self.row})

    def _close_row(self, ended: str, put: bool, own: Optional[str] = None) -> None:
        """`own` how the sitting itself ended, when its put-back then found
        HQPlayer left to someone (`ended` left): its pick stands."""
        if self.row is None:
            return
        own = own or ended
        picked = None
        if self.chain is not None and own in ("done", "stopped") and self.chain.duels:
            picked = self.chain.champion
        elif self.pair is not None and own == "done":
            picked = self.pair.winner()
        # Not put back: the row owes it (recover) — the answers stand
        db_execute("""
            UPDATE hqp_ab_sittings
               SET ended_at = now(), ended = %(ended)s::hqp_ab_end, owed = NOT %(put)s,
                   picked = coalesce(%(picked)s, picked),
                   champion = coalesce(%(champ)s, champion)
             WHERE id = %(id)s
        """, {"put": put or ended == "left", "ended": ended, "picked": picked,
              "champ": self.chain.champion if self.chain is not None else None, "id": self.row})

    def _duel(self, d: search.Duel) -> None:
        db_execute("""
            INSERT INTO hqp_ab_duels (sitting_id, n, first_name, second_name, answer, axis,
                                      check_step, counted, swapped)
            VALUES (%(s)s, %(n)s, %(a)s, %(b)s, %(ans)s::hqp_ab_answer, %(axis)s::hqp_ab_axis,
                    %(check)s, %(counted)s, %(swapped)s)
        """, {"s": self.row, "n": d.n, "a": d.first, "b": d.second, "ans": d.answer,
              "axis": d.axis, "check": d.check, "counted": d.counted, "swapped": d.swapped})

    def _duel_swapped(self, d: search.Duel) -> None:
        db_execute("UPDATE hqp_ab_duels SET swapped = %(w)s WHERE sitting_id = %(s)s AND n = %(n)s",
                   {"w": d.swapped, "s": self.row, "n": d.n})

    def _disqualify(self, chain: search.Chain) -> None:
        ns = [d.n for d in chain.duels if not d.counted]
        db_execute("UPDATE hqp_ab_duels SET counted = FALSE WHERE sitting_id = %(s)s AND n = ANY(%(ns)s)",
                   {"s": self.row, "ns": ns})

    # -- what the sheet shows ---------------------------------------------------------------

    def _reason_words(self, reason: str, name: str, **kw) -> str:
        f = next((x for x in self.all_filters if x.name == name), None)
        from hqp_atlas import rate_name
        other = next((x.name for x in self.all_filters
                      if f is not None and x.group == f.group and x.name != name
                      and (x.name == self.yours or x in self.screened.candidates)), None)
        if reason == "too_slow" and f is not None and f.speed is None and f.implied_from:
            reason = "too_slow_implied"
        return WORDS[reason].format(speed=f"{f.speed:.1f}" if f and f.speed else "?",
                                    src=rate_name(self.ctx["src_rate"]),
                                    out=rate_name(self.ctx["rate_out"]),
                                    other=other or "another one",
                                    rate=rate_name(kw.get("rate") or self.ctx["rate_out"]),
                                    from_rate=rate_name(f.implied_from) if f and f.implied_from else "")

    def _left_out(self, screened: search.Screened) -> List[Dict[str, str]]:
        return [{"name": n, "why": self._reason_words(r, n, rate=self.left_rates.get(n))}
                for r, names in list(screened.left_out.items()) for n in list(names)]

    def view(self) -> Dict[str, Any]:
        """The sheet's state (GET /api/hqplayer/ab), read whole at every
        change the player status announces (its hold's `seq`)."""
        speeds = {f.name: f.speed for f in self.all_filters}
        out: Dict[str, Any] = {
            "phase": self.phase, "seq": self.seq, "key": self.key, "form": self.form,
            "track": {k: self.track[k] for k in ("title", "artist", "length")},
            "passage": {"from_s": round(self.from_s, 3), "length_s": self.length_s,
                        "was_s": round(self.was_at, 3)},
            "context": {**self.ctx, "words": context_words(self.ctx)},
            "silence_s": None if self.silence_s is None else round(self.silence_s, 2),
            "long_silence": bool(self.silence_s and self.silence_s > search.LONG_SILENCE_S),
            "sdm": self.ctx["mode"] == "sdm",
            "warm": {"done": self.warmed[0], "of": self.warmed[1]},
            "learned": self.learned, "slipped": self.slipped,
            "lengths": {"min_s": LENGTH_MIN_S, "max_s": min(LENGTH_MAX_S, self.track["length"]),
                        "default_s": LENGTH_DEFAULT_S},
            "yours": {"name": self.yours, "speed": speeds.get(self.yours)},
            "chain": ({"to_hear": min(search.CHAIN_STEPS, len(self.pool)),
                       "left_out": self._left_out(self.screened)}
                      if hasattr(self, "pool") else None),
            "second": self.second,
            "seconds": {"list": [{"name": f.name, "speed": f.speed, "tight": f.klass == "tight",
                                  "picks": self.picks.get(f.name, 0)}
                                 for f in self.second_list.candidates],
                        "left_out": self._left_out(self.second_list),
                        # None to choose from: why, in place of "one you choose"
                        "empty": (dict(zip(("words", "way"), self._empty(self.second_list)))
                                  if not self.second_list.candidates else None)},
            "picks_yours": self.picks.get(self.yours, 0),
            "step": self.step_view, "sounding": self.sounding, "say": self.say,
            "result": self.result, "ended": self.ended, "note": self.note,
            "mute": self.mute is not None,
            "fixed": self.vr is None or not self.vr["enabled"],
        }
        return out

    def _chain_result(self, chain: search.Chain) -> Dict[str, Any]:
        speeds = {f.name: f.speed for f in self.all_filters}
        heard: Dict[str, Dict[str, Any]] = {}
        heard[chain.start] = {"name": chain.start, "tag": "was yours", "speed": speeds.get(chain.start)}
        for d in chain.duels:
            if d.check:
                continue
            tag = {"second": "better", "first": "worse", "same": "same"}[d.answer]
            heard.setdefault(d.second, {"name": d.second, "speed": speeds.get(d.second)}).update(
                tag=tag, differed=d.axis)
        if chain.champion != chain.start:
            heard[chain.champion]["tag"] = "picked"
        swapped = chain.champion != chain.start
        return {"form": "chain", "picked": chain.champion, "swapped": swapped,
                "head": (f"You picked {chain.champion}." if swapped
                         else f"Your filter stays: {chain.champion}."),
                "sub": (f"It is your filter now · {context_words(self.ctx)}" if swapped
                        else f"Nothing changed in HQPlayer · {context_words(self.ctx)}"),
                "heard": list(heard.values())}

    def _pair_result(self, pair: search.Pair) -> Dict[str, Any]:
        speeds = {f.name: f.speed for f in self.all_filters}
        k, m, same = pair.counts()
        verdict = ("No difference heard here." if pair.why == "same"
                   else "A steady pick in this sitting." if pair.steady()
                   else "A leaning, not shown.")                              # W12, W13
        return {"form": "pair", "head": (f"You picked {pair.yours} {k} times, {pair.other} {m} "
                                         f"times; no difference {same} times."),   # W11
                "verdict": verdict, "winner": pair.winner(),
                "make": pair.winner() == pair.other,
                "rows": [{"name": pair.yours, "picks": k, "speed": speeds.get(pair.yours)},
                         {"name": pair.other, "picks": m, "speed": speeds.get(pair.other)}]}





def _chain_say(chain: search.Chain) -> Optional[Dict[str, Any]]:
    n = chain.note
    if n and n.get("same"):
        if n["faster"]:
            return {"line": f"No difference heard. The faster one stays: {n['a']:.1f}× against "
                            f"{n['b']:.1f}×."}                                     # W9
        return {"line": "No difference heard. Yours stays."}
    return None


def _mute_level(vr: Optional[dict], volume: float) -> Optional[float]:
    from playback.hqp_benchmark import mute_level
    return mute_level(vr, volume)


def _path(p: str):
    from pathlib import Path
    return Path(p)


def _track(manager) -> Dict[str, Any]:
    """The queue's current entry as a sitting cuts it: an owned PCM file
    here (Q4) — its path, the image's offset for a CUE slice, its length,
    rate and depth."""
    from pathlib import Path
    from config import settings
    here = manager.spot()
    item = manager.queue.item_at(here.slot if here is not None
                                 else manager.latest_status.get("track_index") or 1)
    if item is None:
        raise Refused("Play a track first: comparing plays a passage of it")
    src = item.opener()
    where = {"hqp": "the HQPlayer's own library"}.get(src.get("kind"), "a stream")
    if src.get("kind") != "file" or item.media_file_id is None:
        raise Refused(f"Comparing plays a cut of a file on this computer; this track plays "
                      f"from {where}.")                                             # W17
    row = db_query_one("""
        SELECT file_path, file_format::text AS format, sample_rate, bit_depth, channels,
               duration_seconds, cue_start_seconds, cue_end_seconds
          FROM media_files WHERE id = %(id)s
    """, {"id": item.media_file_id})
    if row is None:
        raise Refused("Comparing plays a cut of a file on this computer; this track's file "
                      "is not in the library now.")
    if row["format"] in ("DSF", "DFF") or not row["sample_rate"]:
        raise Refused("Comparing plays a cut of a PCM file; this track is DSD.")
    path = settings.translate_to_local_path(row["file_path"])
    if not Path(path).is_file():
        raise Refused("Comparing plays a cut of a file on this computer; this track's file "
                      "is not there now.")
    start = float(row["cue_start_seconds"] or 0.0)
    length = float(row["duration_seconds"] or 0.0)
    if not length and row["cue_end_seconds"] is not None:
        length = float(row["cue_end_seconds"]) - start
    if length < LENGTH_MIN_S:
        raise Refused("This track is too short to compare a passage of it")
    return {"track_id": item.track_id, "title": item.title, "artist": item.artist, "path": path,
            "offset": start, "length": length, "rate": int(row["sample_rate"]),
            "bits": row["bit_depth"], "channels": row["channels"] or 2}


def _screen(s: Sitting, filters: List[dict], own: str, shaper: str, mode_name: str):
    """Every filter HQPlayer lists as the sitting sees it — its speed here,
    its reach, its atlas facts — screened for the chain and for the pair."""
    from pathlib import Path
    import hqp_atlas
    from config import settings
    from playback import hqp_load
    overlay = settings.hqp_atlas_overlay
    atlas = hqp_atlas.read(hqp_atlas.SHIPPED, Path(overlay) if overlay else None)
    engine = atlas.engine_for(s.engine, "filter") if atlas is not None else None
    if atlas is not None and (engine is None or not atlas.speaks_of(s.engine, "filter")):
        atlas = None
    hr = hqp_load.headroom(hqp_load.context(s.ep, mode=mode_name, state=s.state),
                           rate_out=s.ctx["rate_out"], filter=own, shaper=shaper,
                           src_rate=s.ctx["src_rate"], src_channels=s.track.get("channels") or 2)
    key, fs_in = atlas_key(s.ctx["slot"], s.ctx["src_rate"])
    out = []
    for f in filters:
        mark = hr["filters"].get(f["name"]) or {}
        asked = (hr.get("refused") or {}).get("filters", {}).get(f["name"])
        at = group = None
        if atlas is not None:
            found = atlas.lookup("filter", f["name"], engine, s.ctx["mode"])
            if found is not None and found.state in ("measured", "pcm-measurement"):
                at = search.axes_of(found.entry["contexts"].get(key), fs_in)
                ident = atlas.identical(found)
                group = ident[0]["id"] if ident else None
        out.append(search.Filter(f["name"], f["index"], mark.get("speed"), mark.get("class"),
                                 # the class's arithmetic alone (no measurement passed): a stop
                                 # HQPlayer was measured making is the rate check's to say
                                 refusal(RateFilter(f["name"], (f.get("parsed") or {}).get("ratio")),
                                         s.ctx["src_rate"], s.ctx["rate_out"], None) is None, group, at,
                                 asked=None if mark else asked,
                                 implied_from=mark.get("implied_from")))
    s.all_filters = out
    champion = next(f for f in out if f.name == own)
    return (atlas, search.screen(out, champion, atlas=atlas is not None),
            search.seconds(out, champion, atlas=atlas is not None), champion)


def _logged(endpoint_id: int, engine: str, ctx: Dict[str, Any]) -> List[dict]:
    """The context's counted duels of earlier sittings, oldest first: the
    brackets the search resumes from (search.replay)."""
    return db_query("""
        SELECT d.first_name, d.second_name, d.answer::text AS answer, d.axis::text AS axis,
               d.check_step, d.counted
          FROM hqp_ab_duels d JOIN hqp_ab_sittings s ON s.id = d.sitting_id
         WHERE s.hqp_endpoint_id = %(e)s AND s.engine = %(g)s AND s.mode = %(m)s::hqp_ab_mode
           AND s.slot = %(s)s::hqp_ab_slot AND s.fractional = %(f)s AND s.form = 'chain'
           AND d.counted AND d.axis IS NOT NULL
         ORDER BY s.started_at, d.n
    """, {"e": endpoint_id, "g": engine, "m": ctx["mode"], "s": ctx["slot"], "f": ctx["fractional"]})


# -- a sitting cut short ----------------------------------------------------------------

_OWED = "(ended IS NULL OR owed)"


def owed(endpoint_id: int) -> Optional[dict]:
    """The sitting that still owes HQPlayer its put-back, or None: one the
    process died in (never ended), or one whose put-back HQPlayer did not
    take. A sitting going in this process owes nothing yet."""
    if running():
        return None
    return db_query_one(f"""
        SELECT * FROM hqp_ab_sittings
         WHERE hqp_endpoint_id = %(e)s AND {_OWED} ORDER BY started_at DESC LIMIT 1
    """, {"e": endpoint_id})


def recover(endpoint_id: int, link, again: Callable[[], None] = lambda: None) -> None:
    """A sitting cut short left HQPlayer on a candidate, maybe lowered: put
    back on the playback link the next time HQPlayer answers (the attach),
    on a thread of its own so the attach never waits for it — their volume
    where it still reads the warm-up's first, then the champion (the chain)
    or the filter they had (the pair) in the sitting's slot — and the row
    closed (failed, when it never ended). A person's DSP change or switch
    away first leaves it to them (left_by_person). One HQPlayer does not
    answer is tried again at its next attach. Whatever HQPlayer plays is
    not asked: the sitting's own fragment, paused, reads as no queue's entry
    (AB-1-DESIGN §17.2)."""
    row = owed(endpoint_id)
    if row is None:
        return
    threading.Thread(target=_recover, args=(row, link, again), daemon=True,
                     name="hqp-ab-recover").start()


def _recover(row: dict, link, again: Callable[[], None]) -> None:
    row_id = row["id"]
    try:
        with link.command(item=link.item("dsp_knob", link.accept("dsp"))):
            # One put-back at a time: read again under the command lock, so
            # one that waited for another's acts on the row as it ended — a
            # row it closed (put back, refused twice, left) gets nothing
            row = db_query_one(f"SELECT * FROM hqp_ab_sittings WHERE id = %(id)s AND {_OWED}",
                               {"id": row_id})
            if row is None:
                return
            with _lock:
                _putting.add(row_id)
            try:
                if not _put_row_back(row, link.client, again):
                    return
            finally:
                with _lock:
                    _putting.discard(row_id)
    except Exception as e:      # the thread's boundary: tried again at HQPlayer's next answer
        logger.warning("Comparison %s not put back yet: %s", row_id, e)
        again()
        return
    logger.info("Comparison %s, cut short, put back", row_id)


def _put_row_back(row: dict, c, again: Callable[[], None]) -> bool:
    """_recover's put-back on the command lock: True once HQPlayer reads as
    put back and the row is closed."""
    st, filters = c.get_state(), c.get_filters()
    if st is None or filters is None:
        again()
        return False
    lowered = (row["muted_to"] is not None and row["before_volume"] is not None
               and abs(st["volume"] - float(row["muted_to"])) <= _DB_EPS)
    if lowered:
        c.set_volume(float(row["before_volume"]))
    index = {f["name"]: f["index"] for f in filters}
    names = {i: n for n, i in index.items()}
    holds = names.get(st["filter1x"] if row["slot"] == "1x" else st["filterNx"])
    want = index.get(row["champion"] if row["form"] == "chain" else
                     (row["before_filter_1x"] if row["slot"] == "1x" else row["before_filter_nx"]))
    # Only over the sitting's own mark: a slot holding something else
    # than what it set last is the person's since (HQPlayer's window)
    if want is not None and (row["last_set"] is None or holds == row["last_set"]):
        nx, x1 = ((st["filterNx"], want) if row["slot"] == "1x" else (want, st["filter1x"]))
        if (st["filterNx"], st["filter1x"]) != (nx, x1):
            c.set_filter(nx, x1 if x1 is not None and x1 >= 0 else None)
    else:
        nx = x1 = None
    # Closed only once HQPlayer reads as put back
    after = c.get_state()
    if after is None:
        again()
        return False
    refused = _refusal(after, (nx, x1) if nx is not None else None, names.get(want),
                       float(row["before_volume"]) if lowered else None)
    if refused is not None:
        if _refused_again(row["id"], refused):
            _close_refused(row, refused)
            return False
        logger.warning("Comparison %s not put back yet: HQPlayer did not take it", row["id"])
        again()
        return False
    db_execute(f"""UPDATE hqp_ab_sittings
                    SET ended = coalesce(ended, 'failed'), ended_at = coalesce(ended_at, now()),
                        owed = FALSE
                  WHERE id = %(id)s AND {_OWED}""", {"id": row["id"]})
    return True


def _refusal(st: dict, slots: Optional[Tuple[int, int]], name: Optional[str],
             volume: Optional[float]) -> Optional[Tuple[Optional[str], Optional[float]]]:
    """What HQPlayer, as `st` reads after a put-back, did not take: (the
    filter's `name` when the slots do not hold `slots`, the `volume` when it
    does not read it) — or None when it took both."""
    filt = name if slots is not None and (st["filterNx"], st["filter1x"]) != slots else None
    vol = volume if volume is not None and abs(st["volume"] - volume) > _DB_EPS else None
    return None if filt is None and vol is None else (filt, vol)


def _refused_again(row_id: int, refused: Tuple[Optional[str], Optional[float]]) -> bool:
    """Note a refusal of this row's put-back; True when it is the same as
    the one before — the second (AB-1-DESIGN §17): no third is sent. A
    volume is the same within a read's step: the live put-back holds
    HQPlayer's float, the row its NUMERIC(6,2)."""
    with _lock:
        last = _refusals.get(row_id)
        _refusals[row_id] = refused
    return (last is not None and last[0] == refused[0]
            and (last[1] is None) == (refused[1] is None)
            and (last[1] is None or abs(last[1] - refused[1]) <= _DB_EPS))


def _close_refused(row: dict, refused: Tuple[Optional[str], Optional[float]]) -> None:
    """Refused twice alike: the row is closed as left however the sitting
    ended — nothing owed, the person sets it in HQPlayer, its pick kept —
    and said once (refused_notice)."""
    filt, volume = refused
    db_execute(f"""UPDATE hqp_ab_sittings
                    SET ended = 'left', ended_at = coalesce(ended_at, now()),
                        owed = FALSE, refused_at = now(),
                        refused_filter = %(f)s, refused_volume = %(v)s
                  WHERE id = %(id)s AND {_OWED}""", {"id": row["id"], "f": filt, "v": volume})
    with _lock:
        _refusals.pop(row["id"], None)
    db_execute("NOTIFY sautium_notices")
    logger.warning("Comparison %s: HQPlayer refused its put-back twice (filter %s, volume %s) — "
                   "left to the owner", row["id"], filt, volume)


def refused_notice() -> Optional[dict]:
    """The newest put-back HQPlayer refused twice: a report (the notices
    channel drops it once seen) naming what to set in HQPlayer and where."""
    row = db_query_one("""
        SELECT s.refused_at, s.refused_filter, s.refused_volume, s.mode::text AS mode,
               s.slot::text AS slot, e.name AS hqplayer
          FROM hqp_ab_sittings s JOIN hqp_endpoints e ON e.id = s.hqp_endpoint_id
         WHERE s.refused_at IS NOT NULL ORDER BY s.refused_at DESC LIMIT 1""")
    if row is None:
        return None
    return {"since": row["refused_at"],
            "data": {"filter": row["refused_filter"],
                     "volume": None if row["refused_volume"] is None else float(row["refused_volume"]),
                     "mode": row["mode"], "slot": row["slot"], "hqplayer": row["hqplayer"]}}


def left_by_person(host: str, port: int) -> None:
    """A person's DSP change or switch away supersedes what a sitting cut
    short still owes: its row closes as left, and nothing is put back over
    what they set. A sitting going in this process owes nothing yet, and a
    row being put back is left to that put-back's end (_putting): a switch
    away never closes as left a row put back. The database failing costs
    that record, never the person's change."""
    if running():
        return
    import hqp_library
    with _lock:
        putting = list(_putting)
    try:
        ep = hqp_library.endpoint_by_address(host, port)
        if ep is None:
            return
        db_execute(f"""UPDATE hqp_ab_sittings
                        SET ended = coalesce(ended, 'left'), ended_at = coalesce(ended_at, now()),
                            owed = FALSE
                      WHERE hqp_endpoint_id = %(e)s AND {_OWED} AND NOT (id = ANY(%(p)s))""",
                   {"e": ep["id"], "p": putting})
    except psycopg2.Error as e:
        logger.warning("HQPlayer A/B: a sitting cut short not recorded as left to the owner: %s", e)


def adopt() -> Dict[str, Any]:
    """The pair's result: "Make it my filter" — the other filter into the
    context's slot, through the one DSP change path (the hold is over)."""
    from playback.hqp_backend import change_dsp
    s = _sitting
    if s is not None and s.thread is not None:
        s.thread.join(15)          # the result shows a moment before the hold is let go
    if s is None or s.pair is None or s.result is None or not s.result.get("make") or running():
        raise Refused("Nothing to make your filter now")
    knobs = ({"filter": s.before["nx"], "filter1x": s.pair.other} if s.ctx["slot"] == "1x"
             else {"filter": s.pair.other, "filter1x": s.before["1x"]})
    out = change_dsp({k: v for k, v in knobs.items() if v is not None})
    s.result = {**s.result, "made": True, "make": False}
    s.seq += 1
    return out
