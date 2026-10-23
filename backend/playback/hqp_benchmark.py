"""
The HQPlayer benchmark: what this HQPlayer keeps up with, measured for the
settings the owner has not listened through yet.

Every listen already leaves DSP samples (playback.hqp_load). The benchmark
fills the gaps: it plays test signals through the settings of ONE mode — the
one HQPlayer is in; the other only when the owner asks for it — and only
through the points no sample and no earlier run covers yet, so a repeat run
after weeks of listening takes minutes. The grid (build_grid):

- PCM: every filter on a 44.1 and a 96 kHz source, with the owner's dither;
- SDM: every modulator on a 44.1 kHz source with the owner's filter, and
  every filter at the owner's rate with the owner's modulator — once the
  owner's own setting keeps up here (the filters measure the modulator
  otherwise);
- a DSD-source slice when the owner has listened to a DSD file that is still
  there (that file is the source; none is generated);
- a source plays at the rates of its own family (same_family) and at the
  owner's rate: another family takes an asynchronous conversion some
  filters refuse, and nobody plays a 44.1 kHz source at 48k × 1024 by mistake;
- the owner's own setting first and again last — the thermal drift check;
- each modulator (in PCM each filter) on a source is a ladder over the
  rates of its family (LADDER_START): measured from DSD256 (PCM: 8×, 352.8
  or 384 kHz) up while it keeps up, and below that only while the rate
  above it does not — that rung next, before anything else; a rate a
  neighbour on its ladder answers for is never measured (a setting's speed
  falls as its rate rises), and the host's dropout boundary shows where the
  ladders stop. The starts and every point of a single rate go first, then
  the steps by their distance from the start, down before up, each step
  shuffled.

Each point leaves a row in hqp_benchmark_points by what it ASKED — measured,
unsettled, dropped out, refused by HQPlayer, never started here, or
failed — and a later run skips every point asked before that did not fail
(hqp_load.covered): a combination HQPlayer refuses is never a sample, and an
adaptive output rate plays another rate than the one asked. HQPlayer refuses
a combination either at the Set* command or at Play, stopping at once
("Requested filter not possible with this rate combination …" in its log):
a point stopped again after its one re-Play, never having moved, is refused
too. HQPlayer builds a setting in silence, for minutes at times: a point
waits it out and is measured, the build counted into its start; one that
keeps HQPlayer silent past BUILD_LIMIT_S did not start on this host at all
(or HQPlayer hung) — not asked again, and the run ends.

The signals are pink noise at about -20 dBFS, 24-bit stereo FLAC — the
decoder path owned files take — made once into the node's data dir and
handed over as the media proxy's /file/ URLs: a Docker node's container
path is nothing HQPlayer can open, and the input path changes no DSP cost.

One owner of HQPlayer at a time: the run borrows the output
(PlaybackManager.hold) — the backend is detached without a switch away, no
Stop (the run stops HQPlayer itself), every other Sautium path is refused
until the output comes back, and the canonical queue mirrors back into
HQPlayer when it does. The
run drives HQPlayer on a connection of its own (_Link), which waits out
HQPlayer's silence while it builds a setting — its control port answers
nothing until the build is done, and a command given up on would still be
carried out later, out of order — and survives HQPlayer dropping it: a
command that finds it gone reconnects, and what HQPlayer holds is made
again before anything plays — it may have restarted under the run. HQPlayer's volume goes to the bottom of its range first and
is read back: <VolumeMute/> is a toggle no reply reports, so it cannot be
trusted with the owner's ears. Everything the run changed is put back at the
end, on cancel, on failure and when the app stops (shutdown); a run cut
short before that is put back the next time HQPlayer answers — at the
attach or when the poller sees it come back (recover) — while HQPlayer still
shows a mark of the run: its lowered volume, its signals, or the selection
it set last, in the mode it measured too (read there only on an HQPlayer at
rest); one HQPlayer went away under (it closed, reset or refused the
connection) whole, while it shows what a box that just booted shows —
stopped, reading no other controller's entry. A new run takes it first, and
closes it with no notice. A person's switch away, DSP change or knob set in
HQPlayer's own window since leaves HQPlayer to them instead: the run is
closed as left, nothing more is put back, and one notice names what still
holds the run's test values.

Each point waits for HQPlayer's average to settle (Settle) and writes one
sample: the p10 of the accepted readings, whether they settled, how long
the filter took to start and to settle, and whether it dropped out — the
speed seen just before a dropout is the host's boundary (hqp_load.
thresholds).
"""

import logging
import random
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import psycopg2

from config import settings
from db_pool import db_execute, db_query, db_query_one
from hqp_atlas import rate_name
from hqp_rate_check import mode_kind, running_slot, same_family
from hqplayer_client import HQPlayerClient, PlaybackState, TrackStatus

from playback import hqp_load

logger = logging.getLogger(__name__)

LABEL = "The HQPlayer benchmark is running"

# Longer than a point can run (POINT_DEADLINE_S): HQPlayer never reaches the
# end of a signal and moves on to the next one mid-point.
SIGNAL_SECONDS = 120
SIGNAL_AMPLITUDE = 0.1            # pink noise peaking about -20 dBFS
_SIGNAL_DIR = Path(__file__).resolve().parent.parent / "data" / "hqp_bench"

# When a point's readings count (Valerii, 2026-10-02). process_speed is a
# running average of the last processing units, so equal readings can still
# be an average lagging the real value: wait for STABLE_AFTER_S of moving
# position (counted again when it stalls before the readings — a heavy filter
# recovering); accept when the last SETTLE_WINDOW readings stay within
# SETTLE_BAND of their mean AND show no trend (a least-squares slope moving
# the value less than SETTLE_TREND across them); at SETTLE_CAP_S of moving
# position record them anyway, marked unsettled.
STABLE_AFTER_S = 5.0
# An output that keeps no time (HQPlayer's ALSA null device) plays the
# signal at the DSP's own speed — 120 s out in five on a Pi 5 at 24×
# (2026-10-04): its stable seconds are counted in audio as well, and the
# signal ending is no failure. Twice real time is past any DAC's clock.
OUTRAN = 2.0
SETTLE_WINDOW = 5
SETTLE_BAND = 0.02
SETTLE_TREND = 0.01
SETTLE_CAP_S = 15.0
# Play until the position moves: a heavy filter initialises for seconds.
START_DEADLINE_S = 60.0
# How long the run waits on one answer before it asks again on a new
# connection. Building a setting blocks HQPlayer's control port from the
# start until the build is done — on the laptop (Desktop 6.2.3, 2026-10-03)
# 10 to 27 s for sinc-MGa at 512× and 1024×, 56 s for poly-sinc-mp and
# 3 min 13 s for poly-sinc-long-lp at 44.1k × 256 — and a command given up
# on is not withdrawn: HQPlayer carries it out once it is free, after
# anything sent since (a Stop sent to set it up again stopped the setting
# just built, and an interrupted build failed inside HQPlayer). So a point
# asks for <Status/> again until HQPlayer answers (_outlast); HQPlayer
# keeps a design it built, and the same setting starts in a second after.
ANSWER_WAIT_S = 45.0
# Building this long — silent, or answering that it plays with nothing out
# yet (_building, Embedded) — this host does not get the setting going: the
# point is `unstarted`; a silent HQPlayer is taken for hung, and the run ends.
BUILD_LIMIT_S = 600.0
# A point that has not decided by then never will (a frozen output that
# still says it plays).
POINT_DEADLINE_S = 90.0
# Falling behind, and only that, is a dropout — once the readings run: the
# output buffer under DROPOUT_FILL for DRAINED_TICKS ticks in a row while the
# input holds, the position frozen for STALLED_TICKS, or the transport
# stopped. Before the readings the buffer is still filling and the average
# still holds the initialisation, so the same signs count only under 1×.
# A tick counts only when HQPlayer refreshed its <Status/> since the last
# one: Embedded refreshes it per output block — position, speed and fills
# frozen together for up to 7.6 s at 32× (Pi 5, engine 6.2.3, 2026-10-03),
# where Desktop refreshes it every second — so the same snapshot again is
# no news, and a frozen position is a refreshed one that did not move.
DROPOUT_FILL = 0.1
DRAINED_TICKS = 3
STALLED_TICKS = 2
TICK_S = 1.0
# Stopped this many ticks in a row, the position never having moved: HQPlayer
# did not start it — once to start it again, twice to call it refused.
STOP_TICKS = 3
# After a SelectTrack the run reads <Status/> until it names the slot, this
# long at most: a stopped HQPlayer plays the slot it is given (Desktop 6.2.4),
# and the read confirms which entry it took.
SELECT_WAIT_S = 2.0
SELECT_POLL_S = 0.2
# Seconds a point takes before this endpoint has run one: start, five stable
# seconds, five readings.
POINT_S = 14.0
# A ladder — one modulator (PCM: one filter) on one source across the rates
# of its family — starts where most hosts keep up and climbs while the
# setting does (Valerii, 2026-10-03): its speed falls as the rate rises
# (every ladder measured on the laptop, ASDM7ECv2 5.47× → 2.79× → 2.11× →
# 1.09× → 0.64× from 64× to 1024×), so nothing is learnt above a rate that
# is tight or too slow, nor below one that keeps up. It starts at
# LADDER_START × the family's base rate (DSD256 and 352.8/384 kHz) and goes
# below only while the rate above does not keep up — a weaker host, where
# that rung is measured next, before anything else. Refusals say nothing
# about speed and stop nothing: HQPlayer refuses the AHM modulators from 64×
# to 512× and runs them at 1024×.
LADDER_START = {"sdm": 256, "pcm": 8}
# A dropped connection is tried again at once, then after these pauses.
_RECONNECT_PAUSES = (1.0, 2.0, 4.0, 8.0)
_DB_EPS = 0.05                    # volumes read back within this are the same
# Putting HQPlayer back waits this long for each answer: a cancel can land in
# a build HQPlayer is silent through, and what it does not answer at once is
# left to recover — never waited out ANSWER_WAIT_S a command, nor sent again.
RESTORE_ANSWER_S = 5.0

_lock = threading.Lock()
_cancel = threading.Event()
# The app is stopping: putting HQPlayer back does not wait for it to come back.
_halt = threading.Event()
_thread: Optional[threading.Thread] = None
# The run's own connection, for a cancel to wake a read it is waiting on.
_live: Optional["_Link"] = None
# The put-back each run cut short still owes HQPlayer, by run id: a DSP item of
# the HQPlayer's link (link, item) as of the run's end — a person's newer DSP
# change, or their switch away, supersedes it, and HQPlayer is theirs then.
_owed: Dict[int, tuple] = {}
# By endpoint, a run left to the owner there: its row, what of its test
# settings HQPlayer still held when the poller last read it (note_left; None
# until it did), and HQPlayer's lists by the mode they were read in, each
# one it gave — or None: no run left there. Absent: not read yet.
_left: Dict[int, Optional[dict]] = {}
# Bumped by every close and clear of a run left to the owner: a read of the
# row a change overtook is not kept (left_pending)
_left_gen = 0
# Runs whose put-back holds the command lock: a person's switch away, or
# another HQPlayer chosen, is left to that put-back's own end — it went, or
# the switch drops its next request (left_by_person)
_putting: set = set()
# By endpoint, the run whose put-back left the measured mode's selection
# unchecked, its row (None: none) — HQPlayer was busy, or did not take the
# owner's selection there back — read from the database once, then kept;
# the rest a check spent its try at (the entry it rested on: HQPlayer busy,
# or a verdict the database did not take), not tried again until it moves;
# the endpoints a check runs on
_unchecked: Dict[int, Optional[dict]] = {}
_busy_at: Dict[int, tuple] = {}
_checking: set = set()
# Endpoints whose check a run's hold cut once past its way back — a point
# gone, or one kept anew, may stand: that run's end says what stands (_run)
_left_to_run: set = set()
# By endpoint, the run row holding a return point — the mode a check of a
# measured mode found HQPlayer in, kept until HQPlayer is back there or the
# owner leaves it as it is (_keep_point, leave_as_it_is) — or None: none;
# read from the database once, then kept
_point_rows: Dict[int, Optional[dict]] = {}
_state: Dict[str, Any] = {"running": False, "cancel_requested": False, "endpoint_id": None,
                          "mode": None, "point": 0, "total": 0, "point_label": "",
                          "eta_s": None, "started_at": None, "outcome": None, "note": None}


def _bump() -> None:
    """Under _lock: what a run left to the owner says changed."""
    global _left_gen
    _left_gen += 1


class BenchmarkRefused(RuntimeError):
    """Why a benchmark cannot start now, in words for the owner."""


class RunNotFound(LookupError):
    """No benchmark run of that id (leave_as_it_is)."""


def state() -> Dict[str, Any]:
    with _lock:
        return dict(_state)


def running() -> bool:
    return _state["running"]


def measures_here() -> bool:
    """A run going on an HQPlayer on THIS machine — whose CPU and GPU a
    library scan or an analysis run would take from it (main.scan_start,
    enrich_start refuse meanwhile; start refuses the other way round)."""
    from playback.hqp_backend import _stream_mode
    return running() and not _stream_mode()


HERE_BUSY = ("The HQPlayer benchmark is running on this computer — this would skew "
             "what it measures; it can start once the run ends or is cancelled")


def mute_level(volume_range: Optional[dict], volume: float) -> Optional[float]:
    """Where a run lowers HQPlayer to: the bottom of its range, when its
    volume is adjustable on this output and that bottom lies below the
    owner's level. None = it cannot lower it, and the owner turns the
    amplifier down instead."""
    if volume_range is None or not volume_range["enabled"]:
        return None
    return volume_range["min"] if volume_range["min"] < volume - _DB_EPS else None


# -- signals ---------------------------------------------------------------------

def signal_path(rate: int) -> Path:
    return _SIGNAL_DIR / f"pink_{rate}_{SIGNAL_SECONDS}s.flac"


def ensure_signal(rate: int) -> Path:
    """The test signal at one sample rate, made the first time it is needed."""
    from streaming.transcode import run_ffmpeg
    path = signal_path(rate)
    if not path.is_file():
        _SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
        run_ffmpeg(["-f", "lavfi", "-i",
                    (f"anoisesrc=color=pink:amplitude={SIGNAL_AMPLITUDE}:sample_rate={rate}"
                     f":duration={SIGNAL_SECONDS}:seed=7")],
                   ["-ac", "2", "-c:a", "flac", "-sample_fmt", "s32",
                    "-bits_per_raw_sample", "24", "-f", "flac"], path, None)
    return path


def _signal_uri(path: Path) -> str:
    from streaming.service import ensure_proxy
    from playback.hqp_backend import hqp_media_host
    proxy = ensure_proxy()
    return proxy.file_url(proxy.register_file(str(path), "audio/flac"), host=hqp_media_host())


def _signal_tokens() -> set:
    """The proxy tokens of the signals made so far — what marks a playlist
    entry as one of ours when a run is recovered."""
    from streaming.service import ensure_proxy
    proxy = ensure_proxy()
    return {proxy.file_token(str(p)) for p in _SIGNAL_DIR.glob("pink_*.flac")}


def _dsd_file() -> Optional[dict]:
    """The DSD file the owner listened to last, while it is still there: a
    DSD source is measured only on what the owner plays."""
    row = db_query_one("""
        SELECT mf.file_path, mf.file_format::text AS format, mf.sample_rate, mf.channels
          FROM listening_history lh JOIN media_files mf ON mf.id = lh.media_file_id
         WHERE mf.file_format IN ('DSF', 'DFF') AND mf.cue_start_seconds IS NULL
           AND mf.sample_rate > 0 AND mf.channels > 0
         ORDER BY lh.started_at DESC LIMIT 1
    """)
    if row is None:
        return None
    try:
        there = Path(settings.translate_to_local_path(row["file_path"])).is_file()
    except OSError:
        there = False
    return row if there else None


def _dsd_uri(f: dict) -> str:
    """The DSD file as HQPlayer gets any owned file — by path here, as a
    stream from the media proxy anywhere else."""
    from playback.hqp_backend import _owned_play_uri, hqp_media_host
    from playback.queue import QueueItem
    item = QueueItem(track_id=None, media_file_id=None,
                     source={"kind": "file", "path": f["file_path"], "format": f["format"]})
    return _owned_play_uri(item, hqp_media_host())


def sources(kind: str, engine: Optional[str]) -> Dict[str, dict]:
    """What a run of this mode may play: a 44.1 kHz signal, in PCM a 96 kHz
    one too, and the owner's DSD file when there is one (not on 5.17.0,
    whose speed for a DSD source is wrong). A run loads only those its
    points use."""
    out: Dict[str, dict] = {"pcm44": {"rate": 44100, "channels": 2}}
    if kind == "pcm":
        out["pcm96"] = {"rate": 96000, "channels": 2}
    dsd = None if hqp_load.dsd_speed_wrong(engine) else _dsd_file()
    if dsd is not None:
        out["dsd"] = {"rate": dsd["sample_rate"], "channels": dsd["channels"], "file": dsd}
    return out


# -- the grid --------------------------------------------------------------------

@dataclass(frozen=True)
class Point:
    rate: int                 # GetRates index
    rate_hz: int              # 0 = auto: the output rate shows once it plays
    filter: int               # SetFilter value (the Nx slot)
    filter1x: int             # SetFilter value1x
    shaper: int
    source: str               # a key of sources()
    label: str
    key: tuple                # what was asked: (rate_hz, filter, shaper, src_rate, src_channels)
    own: bool = False
    ladder: Optional[tuple] = None    # a ladder's rung: (filter, filter1x, shaper, source)
    step: int = 0                     # rungs above the ladder's start; below it, negative


@dataclass
class Plan:
    """A run's points in order; every rung of every ladder, rates ascending
    and covered ones included, with what is known of them — the run steps
    down a ladder by these when its start does not keep up."""
    points: List[Point]
    rungs: Dict[tuple, List[Point]]
    known: Dict[tuple, bool]          # key → keeps up here (hqp_load.keeps_up)
    covered: frozenset
    # SDM: every filter at the owner's rate with the owner's modulator — what
    # waits for a setting of the owner's that keeps up here (_points)
    sweep: frozenset = frozenset()

    def deferred(self, p: Point, known: Dict[tuple, bool]) -> bool:
        """A filter of the sweep while the owner's own setting does not keep
        up here: every one would measure the modulator, not the filter (on a
        Pi 5, ASDM7EC-ul at DSD128 ran 0.26–0.30× under four filters from
        halfband to long-ip-2s, 2026-10-03). The ladders show what does keep
        up; the filters wait for it."""
        return not p.own and p.key in self.sweep and known.get(self.points[0].key) is False

    def asks(self) -> int:
        """The points a run would ask by what is known now — the estimate's."""
        return sum(1 for p in self.points if not self.deferred(p, self.known))


def running_filter(names: Dict[int, str], nx: int, x1: int, src_rate: int) -> str:
    """The name of the filter HQPlayer runs for a source, of the two slots'
    indices — `x1` -1 when no 1x filter is set (hqp_rate_check.running_slot)."""
    return names.get(running_slot(nx, x1 if x1 >= 0 else None, src_rate), "")


def build_grid(kind: str, *, filters: List[dict], shapers: List[dict], rates: List[dict],
               current: Dict[str, int], srcs: Dict[str, dict], covered: set,
               keeps_up: Dict[tuple, bool], rng: random.Random) -> Plan:
    """The points of one run (see the module docstring): the owner's own
    setting first and last; between them the ladders' starts with every
    point of one rate, shuffled, then each step up the ladders and down
    those whose start already does not keep up, each shuffled — less every
    key covered and every rung a neighbour already answers for."""
    fname = {f["index"]: f["name"] for f in filters}
    sname = {s["index"]: s["name"] for s in shapers}
    hz = {r["index"]: r["rate"] for r in rates}

    def point(rate: int, nx: int, x1: int, shaper: int, src: str, own: bool = False) -> Point:
        s = srcs[src]
        running = running_filter(fname, nx, x1, s["rate"])
        rate_hz = hz.get(rate, 0)
        key = (rate_hz, running, sname.get(shaper, ""), s["rate"], s["channels"])
        rate_txt = rate_name(rate_hz) if rate_hz else "auto rate"
        label = f"{running} · {sname.get(shaper, '')} · {rate_txt} · {rate_name(s['rate'])} source"
        return Point(rate, rate_hz, nx, x1, shaper, src, label, key, own)

    cur_rate, nx, x1, cur_shaper = (current["rate"], current["filterNx"],
                                    current["filter1x"], current["shaper"])
    real_rates = [r["index"] for r in rates if r["rate"] > 0]

    def family(src: str) -> List[int]:
        return [i for i in real_rates if same_family(hz[i], srcs[src]["rate"])]

    def rates_for(src: str) -> List[int]:
        """Its family's rates and the owner's own, whatever its family —
        never under a PCM source's own rate: the benchmark measures
        upsampling. (Embedded 6.2.3 stopped a 96 kHz source at 48 kHz with
        "clHQPlayerEngine::Execute(): lInRate > lOutRate", 2026-10-03; the
        filter was not recorded.) A DSD source goes down to PCM rates, which
        is its conversion."""
        floor = 0 if src == "dsd" else srcs[src]["rate"]
        owners = [cur_rate] if cur_rate in real_rates else []
        return [i for i in dict.fromkeys(family(src) + owners) if hz[i] >= floor]

    ladders: Dict[tuple, Dict[int, Point]] = {}
    singles: List[Point] = []

    def add(rate: int, f_nx: int, f_x1: int, shaper: int, src: str) -> None:
        """A rate of the source's family is a rung of its ladder; an
        automatic rate, or the owner's from another family, a point alone."""
        p = point(rate, f_nx, f_x1, shaper, src)
        if p.rate_hz and same_family(p.rate_hz, srcs[src]["rate"]):
            ladders.setdefault((f_nx, f_x1, shaper, src), {})[p.rate_hz] = p
        else:
            singles.append(p)

    sweep: List[Point] = []
    if kind == "pcm":
        for src in ("pcm44", "pcm96"):
            for f in filters:
                for rate in rates_for(src):
                    add(rate, f["index"], f["index"], cur_shaper, src)
    else:
        for sh in shapers:
            for rate in rates_for("pcm44"):
                add(rate, nx, x1, sh["index"], "pcm44")
        for f in filters:
            sweep.append(point(cur_rate, f["index"], f["index"], cur_shaper, "pcm44"))
        singles += sweep
    if "dsd" in srcs:
        for rate in rates_for("dsd"):
            add(rate, nx, x1, cur_shaper, "dsd")

    own = point(cur_rate, nx, x1, cur_shaper, "pcm44", own=True)
    rungs: Dict[tuple, List[Point]] = {}
    steps: Dict[int, List[Point]] = {0: [p for p in singles if p.key not in covered]}
    for lid, by_rate in ladders.items():
        ordered = [by_rate[r] for r in sorted(by_rate)]
        first = _ladder_start(kind, ordered, srcs[lid[3]]["rate"])
        ladder = [replace(p, ladder=lid, step=i - first) for i, p in enumerate(ordered)]
        rungs[lid] = ladder
        for p in ladder:
            if p.key == own.key:            # the owner's setting is one of its rungs
                own = replace(own, ladder=lid, step=p.step)
        for p in ladder[first:]:
            if p.key != own.key and p.key not in covered and not _implied(p, ladder, keeps_up):
                steps.setdefault(p.step, []).append(p)
        below = _descend(ladder, keeps_up, set(covered))
        if below is not None:
            steps.setdefault(below.step, []).append(below)

    seen = {own.key}
    points = [own]
    for step in sorted(steps, key=lambda n: (abs(n), n > 0)):
        batch = []
        for p in steps[step]:
            if p.key not in seen:
                seen.add(p.key)
                batch.append(p)
        rng.shuffle(batch)
        points += batch
    return Plan(points=[*points, own], rungs=rungs, known=dict(keeps_up),
                covered=frozenset(covered), sweep=frozenset(p.key for p in sweep))


def _ladder_start(kind: str, rungs: List[Point], src_rate: int) -> int:
    """Where a ladder starts: at its highest rung up to LADDER_START × the
    source family's base rate — its lowest when none is that low."""
    base = 44100 if src_rate % 11025 == 0 else 48000
    return max((i for i, p in enumerate(rungs) if p.rate_hz <= LADDER_START[kind] * base),
               default=0)


def _implied(p: Point, rungs: List[Point], known: Dict[tuple, bool]) -> bool:
    """A neighbour on its ladder already answers for this rung: one below
    it that does not keep up, or one above it that does."""
    for r in rungs:
        k = known.get(r.key)
        if (k is False and r.rate_hz < p.rate_hz) or (k is True and r.rate_hz > p.rate_hz):
            return True
    return False


def _descend(rungs: List[Point], known: Dict[tuple, bool], passed: set) -> Optional[Point]:
    """The next rung down a ladder while nothing at or under its start keeps
    up: below the lowest rung known not to, the first not yet asked
    (`passed`: asked, or refused — a refusal says nothing about speed). None
    once a rung there keeps up, or while none is known not to. A start
    HQPlayer refused leaves the rungs above to say it: one of them too slow
    sends the ladder under the start all the same."""
    under = [r for r in rungs if r.step <= 0]
    failing = [r for r in rungs if known.get(r.key) is False]
    if not failing or any(known.get(r.key) is True for r in under):
        return None
    lowest = min(r.rate_hz for r in failing)
    return next((r for r in reversed(under) if r.rate_hz < lowest and r.key not in passed), None)


# -- settling ----------------------------------------------------------------------

def _p10(values: List[float]) -> float:
    """The 10th percentile, interpolated as PostgreSQL's percentile_cont."""
    v = sorted(values)
    pos = 0.1 * (len(v) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def _building(status: TrackStatus) -> bool:
    """HQPlayer answers and says it plays, but nothing has come out yet —
    still building the setting: Embedded on a Pi 5 answers all through a
    25–70 s build like this (position 0, speed 0, output buffer empty;
    2026-10-03), where Desktop falls silent. A state the SDK does not name
    counts as it: Embedded said 5 while it started a play."""
    return (status.state not in (PlaybackState.STOPPED, PlaybackState.PAUSED,
                                 PlaybackState.STOPREQ)
            and status.position <= 0
            and not status.process_speed and not (status.output_fill or 0) > 0)


def _steady(window: List[float]) -> bool:
    """Within the band of their mean and no trend across them."""
    n = len(window)
    mean = sum(window) / n
    if mean <= 0 or any(abs(x - mean) > SETTLE_BAND * mean for x in window):
        return False
    xbar = (n - 1) / 2
    slope = (sum((i - xbar) * (x - mean) for i, x in enumerate(window))
             / sum((i - xbar) ** 2 for i in range(n)))
    return abs(slope * (n - 1)) < SETTLE_TREND * mean


class Settle:
    """When one point's readings count — fed one <Status/> per tick from the
    select that started it; one HQPlayer has not refreshed since the last is
    no news (see STALLED_TICKS). feed() answers None until it has decided, then
    a dict: `speed` (the p10 of the accepted readings, or the speed seen
    just before a dropout), `settled`, `dropout`, `init_s` (the start until
    the position moved, a silent build included), `settle_s` (the position
    moving until accepted), `out_fill`/`in_fill` (the lowest seen once it
    moved), `readings`, `status` (the last one taken while it played); or
    `failed` with why — a point that failed is measured again by a later
    run. The time HQPlayer spent building — silent (paused), or answering
    that it plays with nothing out yet (_building) — is not held against its
    deadlines: they bound a start, a build is bounded by BUILD_LIMIT_S, past
    which the point is `unstarted`."""

    def __init__(self, t0: float, *, build_limit: Optional[float] = None,
                 start_only: bool = False):
        self.t0 = t0
        self.build_limit = BUILD_LIMIT_S if build_limit is None else build_limit
        # Decided once the position moves: the control of a point that never
        # started asks only whether HQPlayer still starts anything (_points)
        self.start_only = start_only
        self._silent = 0.0
        self._last: Optional[float] = None
        self.init_s: Optional[float] = None
        # The last <Status/> taken while it played — what the sample keys on:
        # a stopped one carries no <metadata> and may name the next setting.
        self.playing: Optional[TrackStatus] = None
        self._moving_since: Optional[float] = None
        self._seen: Optional[tuple] = None
        self._last_pos = 0.0
        # Where the position stood when it first moved, when the stable
        # count began, and where it played last (OUTRAN)
        self._first_pos = 0.0
        self._moved_from = 0.0
        self._play_pos = 0.0
        self._fresh_at: Optional[float] = None    # when HQPlayer last refreshed its status
        self._outran_start = False                # its first move already past real time
        self._last_speed: Optional[float] = None
        self._stalls = 0
        self._drained = 0
        self._outs: List[float] = []
        self._ins: List[float] = []
        self._readings: List[float] = []

    @property
    def moved(self) -> bool:
        return self.init_s is not None

    def _done(self, now: float, *, settled: bool, dropout: bool = False) -> dict:
        window = self._readings[-SETTLE_WINDOW:]
        return {"speed": self._last_speed if dropout else _p10(window),
                "settled": settled, "dropout": dropout, "init_s": self.init_s,
                "settle_s": (now - self._moving_since) if settled else None,
                "out_fill": min(self._outs) if self._outs else None,
                "in_fill": min(self._ins) if self._ins else None,
                "readings": list(self._readings), "status": self.playing}

    def _behind(self, now: float, what: str) -> dict:
        """A sign of falling behind: a dropout at the speed HQPlayer showed
        last — under DROPOUT_CEILING once the readings run, under 1× before
        them. Faster than that it is not the DSP (the output path, another
        controller), and the point failed."""
        speed = self._last_speed
        if speed is None:
            return {"failed": f"{what} before HQPlayer reported a speed"}
        if speed < (hqp_load.DROPOUT_CEILING if self._readings else hqp_load.NO_BELOW):
            return self._done(now, settled=False, dropout=True)
        return {"failed": f"{what} at {speed:.2f}× — not the DSP falling behind"}

    def outran(self, now: float) -> bool:
        """HQPlayer played the slot faster than real time (OUTRAN): its stop,
        or its going on to the next entry, is the signal running out."""
        return self.moved and (self._outran_start or self._play_pos - self._first_pos
                               > OUTRAN * (now - self.t0 - self.init_s) + 2.0)

    def ran_out(self, now: float) -> dict:
        """The signal ran out on an output that keeps no time: the readings
        so far are the point, unsettled — the DSP ran flat out under them.
        None yet: it played, and could not be read (`outran` — no sign of a
        broken output, which three points in a row that do not play are)."""
        if self._readings:
            return self._done(now, settled=False)
        return {"failed": "HQPlayer played the signal out before its speed could be read — "
                          "its output keeps no time (its null device?)", "outran": True}

    def paused(self, seconds: float) -> None:
        self._silent += seconds
        # Counted whole: a first answer that still says it builds must not
        # add the same gap again from the tick before the silence
        self._last = None
        if self.moved:
            # Stuck silent while it played: what follows is read afresh — the
            # stable and settle clocks run on wall time, and the first answer
            # after a minute's silence would have closed the point "unsettled"
            # on one reading, the drained output never ticked
            self._moving_since, self._readings, self._drained, self._stalls = None, [], 0, 0

    def feed(self, now: float, status: TrackStatus) -> Optional[dict]:
        building = not self.moved and _building(status)
        if building and self._last is not None:
            self._silent += now - self._last        # the build's time, not the start's
        self._last = now
        if building and now - self.t0 > self.build_limit:
            return {"unstarted": f"HQPlayer was still starting it {self.build_limit / 60:.0f} min "
                                 "after Play — this host does not get it going"}
        elapsed = now - self.t0 - self._silent
        if elapsed > POINT_DEADLINE_S:
            return {"failed": f"no verdict within {POINT_DEADLINE_S:.0f} s of Play"}
        playing = status.state == PlaybackState.PLAYING
        if playing and status.src_rate:
            self.playing = status
        if playing and status.process_speed:
            self._last_speed = status.process_speed
        seen = (status.state, status.track_index, status.position, status.process_speed,
                status.output_fill, status.input_fill)
        fresh, self._seen = seen != self._seen, seen
        moving = fresh and playing and status.position > self._last_pos
        if fresh:
            if moving and not self.moved and self._fresh_at is not None:
                # a filter so light the signal is out within the next tick
                # leaves only this jump to tell (`none`, ~100×, 2026-10-04)
                self._outran_start = (status.position - self._last_pos
                                      > OUTRAN * (now - self._fresh_at) + 2.0)
            self._fresh_at = now
            self._last_pos = status.position if playing else 0.0
            self._stalls = 0 if moving or not playing else self._stalls + 1
        if not self.moved:
            if moving:
                self.init_s, self._moving_since = now - self.t0, now
                self._first_pos = self._moved_from = self._play_pos = status.position
                if self.start_only:
                    return {"started": True, "init_s": self.init_s}
            elif elapsed > START_DEADLINE_S:
                return {"failed": f"HQPlayer answered, but it did not move within "
                                  f"{START_DEADLINE_S:.0f} s of Play"}
            return None
        if not fresh or not status.state.known:
            return None             # unnamed: HQPlayer between two states, no stop
        if not playing:
            return self.ran_out(now) if self.outran(now) else self._behind(now, "it stopped")
        self._play_pos = status.position
        for v, seen in ((status.output_fill, self._outs), (status.input_fill, self._ins)):
            if v is not None:
                seen.append(v)
        reading = bool(self._readings)
        under_one = (self._last_speed or 0.0) < hqp_load.NO_BELOW
        # A low buffer before the readings at 1× or more is still filling.
        low = status.output_fill is not None and status.output_fill < DROPOUT_FILL
        self._drained = self._drained + 1 if low and (reading or under_one) else 0
        if self._drained >= DRAINED_TICKS:
            if status.input_fill is not None and 0 <= status.input_fill < 0.5:
                return {"failed": "the test signal's stream starved — the media proxy "
                                  "did not keep HQPlayer fed"}
            return self._behind(now, "its output drained")
        if self._stalls >= STALLED_TICKS:
            if reading or under_one:
                return self._behind(now, "its position froze")
            self._moving_since = None       # a heavy start recovering: count again
        if self._moving_since is None:
            if moving:
                self._moving_since, self._moved_from = now, status.position
            return None
        if (now - self._moving_since < STABLE_AFTER_S
                and status.position - self._moved_from < STABLE_AFTER_S):
            return None
        if status.process_speed:
            self._readings.append(status.process_speed)
        window = self._readings[-SETTLE_WINDOW:]
        if len(window) == SETTLE_WINDOW and _steady(window):
            return self._done(now, settled=True)
        if now - self._moving_since >= SETTLE_CAP_S:
            if self._readings:
                return self._done(now, settled=False)
            return {"failed": "HQPlayer played it but reported no processing speed"}
        return None


# -- the connection ----------------------------------------------------------------

class _Abort(Exception):
    """The run cannot go on; the message is the owner's explanation."""


class _Lost(Exception):
    """HQPlayer stopped answering the run and did not come back in time."""


class _Reopened(Exception):
    """The connection was replaced in the middle of something. HQPlayer may
    have restarted under the run — its volume, its playlist and its
    transport are not what the run left — so whatever the run was doing
    starts again from what HQPlayer holds."""


class _Silent(_Reopened):
    """HQPlayer took a command and answered nothing for the whole wait —
    still building a setting, or gone without closing. The connection was
    replaced all the same. Within a point it is waited out (_outlast),
    anywhere else a _Reopened like any other."""


class _Hung(_Lost):
    """HQPlayer stayed silent past BUILD_LIMIT_S."""


class _Link:
    """The run's own connection to HQPlayer: it never meets the playback
    backend's sockets, and its expected refusals stay out of the diagnostics
    ring (ring=False). It waits `answer_s` for each answer — the run's own
    link ANSWER_WAIT_S. HQPlayer drops
    connections — a trial-mode Embedded stops every 30 minutes, the WSL2
    hop flaps for seconds — so a command that finds it gone, or loses it,
    connects again (at once, then after each of _RECONNECT_PAUSES; `wait`
    answering True gives up early) and raises _Reopened (_Silent when
    HQPlayer never answered) instead of carrying on blind; one that cannot
    is _Lost."""

    def __init__(self, wait: Callable[[float], bool], *, answer_s: float = 10.0):
        self.c = HQPlayerClient(host=settings.hqplayer_host, port=settings.hqplayer_port,
                                timeout=answer_s, ring=False)
        self.wait = wait

    def connect(self) -> bool:
        return self.c.connect()

    def close(self) -> None:
        """The run is over: its connection and its client's wake channel go."""
        self.c.disconnect()
        self.c.close()

    def interrupt(self) -> None:
        """Wake a read — or a connect — waiting on HQPlayer, from another
        thread (a cancel): it ends as a connection let go, and the run's own
        thread closes its socket (HQPlayerClient.interrupt)."""
        self.c.interrupt()

    def refusal(self) -> str:
        return self.c.refusal()

    def __call__(self, method: str, *args, **kwargs):
        if not self.c.is_connected():
            self._reconnect(silent=False)      # nothing went out on this one
        result = getattr(self.c, method)(*args, **kwargs)
        if not self.c.is_connected():
            self._reconnect(silent=self.c.timed_out)
        return result

    def _reconnect(self, *, silent: bool) -> None:
        why = self.c.refusal()
        for pause in (0.0, *_RECONNECT_PAUSES):
            if pause and self.wait(pause):
                break
            if self.c.connect():
                logger.info("HQPlayer benchmark: connected again after %s", why)
                raise (_Silent if silent else _Reopened)(why)
        if not self.wait(0):
            # The tries ran out, and nothing let them go — a cancel or a halt,
            # one during the last try included, is no failed connect to say:
            # the last connect's own reason, which dial() keeps at debug
            logger.warning("HQPlayer benchmark: not connected again: %s", self.c.refusal())
        raise _Lost(f"HQPlayer stopped answering ({why})")


def _again(step: Callable[[], Any], *, silent_ends: bool = False,
           stop: Optional[Callable[[], bool]] = None) -> Any:
    """A step that comes out the same however often it runs, run again
    whenever the connection was replaced under it — not after a silence
    when `silent_ends`: HQPlayer took what was sent and carries it out once
    it is free; nor once `stop` says so (a cancel wakes a read by dropping
    its connection)."""
    for _ in range(3):
        try:
            return step()
        except _Silent as e:
            if silent_ends:
                raise _Lost("HQPlayer is busy and did not answer") from e
            last = e
        except _Reopened as e:
            last = e
        if stop is not None and stop():
            raise _Lost("cancelled") from last
    raise _Lost("HQPlayer stopped answering" if isinstance(last, _Silent)
                else "HQPlayer kept dropping the connection")


# -- the run ---------------------------------------------------------------------

def _endpoint() -> Optional[dict]:
    import hqp_library
    return hqp_library.endpoint_by_address(settings.hqplayer_host, settings.hqplayer_port)


def _mode_index(modes: List[dict], kind: str) -> Optional[int]:
    return next((m["index"] for m in modes if mode_kind(m["name"]) == kind), None)


def _mode_name(modes: List[dict], index: int) -> Optional[str]:
    return next((m["name"] for m in modes if m["index"] == index), None)


def pace(endpoint_id: int) -> float:
    """Seconds a point takes here — the median of the points of the last run
    that ran to its end or was cancelled (a failed or cut-short one ends in a
    wait for HQPlayer, not in points; on a trial-mode Embedded the owner
    cancels each run before the half hour is up): its wall time over the
    points it measured counted a 10-minute "never started" and a silent
    build as points' time, and put a typical point at ten times its length."""
    row = db_query_one("""
        WITH last AS (
            SELECT id, started_at FROM hqp_benchmark_runs
             WHERE hqp_endpoint_id = %(e)s AND outcome IN ('done', 'cancelled')
               AND points_measured >= 5
             ORDER BY started_at DESC LIMIT 1
        ), each AS (
            SELECT extract(epoch FROM p.at - lag(p.at, 1, l.started_at) OVER (ORDER BY p.at)) AS s
              FROM hqp_benchmark_points p JOIN last l ON l.id = p.run_id
        )
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY s) AS s FROM each
    """, {"e": endpoint_id})
    return float(row["s"]) if row and row["s"] is not None else POINT_S


def plan(endpoint: dict, *, kind: str, mode_name: str, state: dict, filters: List[dict],
         shapers: List[dict], rates: List[dict], srcs: Optional[Dict[str, dict]] = None,
         rng: Optional[random.Random] = None) -> Plan:
    """What a run of this mode would measure now."""
    srcs = srcs if srcs is not None else sources(kind, endpoint.get("hqp_engine"))
    ctx = hqp_load.context(endpoint, mode=mode_name, state=state)
    return build_grid(kind, filters=filters, shapers=shapers, rates=rates,
                      current={"rate": state["rate"], "filterNx": state["filterNx"],
                               "filter1x": state["filter1x"], "shaper": state["shaper"]},
                      srcs=srcs, covered=hqp_load.covered(ctx), keeps_up=hqp_load.keeps_up(ctx),
                      rng=rng or random.Random())


def summary(endpoint: Optional[dict]) -> Dict[str, Any]:
    """The Benchmark block: the job now, and this HQPlayer's last run — stale
    once HQPlayer is another build than the one it measured."""
    out: Dict[str, Any] = {"job": state(), "last_run": None}
    if endpoint is None:
        return out
    run = db_query_one("""
        SELECT id, hqp_engine, mode, started_at, finished_at, outcome::text AS outcome,
               points_planned, points_measured, dropout_speed, first_speed, last_speed, note,
               round(extract(epoch FROM finished_at - started_at))::int AS seconds
          FROM hqp_benchmark_runs WHERE hqp_endpoint_id = %(e)s
         ORDER BY started_at DESC LIMIT 1
    """, {"e": endpoint["id"]})
    if run is not None:
        run["stale"] = bool(endpoint.get("hqp_engine")) and run["hqp_engine"] != endpoint["hqp_engine"]
        out["last_run"] = run
    return out


def start(mode: Optional[str] = None, *, fixed_volume_ok: bool = False) -> Dict[str, Any]:
    """Start a run of `mode` ('pcm' | 'sdm'; None = the mode HQPlayer is in).
    Raises BenchmarkRefused with the reason when it cannot start now."""
    from playback.manager import manager
    if running():
        raise BenchmarkRefused("A benchmark is already running")
    b = manager.active
    if b is None or b.id != "hqplayer":
        raise BenchmarkRefused("Pick this HQPlayer as the audio output first")
    from playback import hqp_link
    from playback.hqp_backend import _stream_mode
    link = hqp_link.selected()
    busy = link.busy()
    if busy is not None:
        raise BenchmarkRefused(f"{busy}. Start the benchmark once it answers again.")
    if not _stream_mode():
        import main
        import model_cache
        if main._scan_state["running"] or main._enrich_state["running"]:
            raise BenchmarkRefused("A library scan or analysis is running on this computer "
                                   "— it would skew what HQPlayer measures here")
        if model_cache.loading():
            # A run started a minute after a restart (2026-10-03): the
            # translation model loading on the CPU ran through its first
            # point — the owner's own, the drift check's reference.
            raise BenchmarkRefused("Sautium is still loading its models on this computer — "
                                   "it would skew what HQPlayer measures here; try again in "
                                   "a minute")
    ep = _endpoint()
    if ep is None or not ep.get("hqp_engine"):
        raise BenchmarkRefused("This HQPlayer is not registered yet — try again once it "
                               "has played")
    # A run cut short before still owes HQPlayer its put-back: it goes first,
    # and this run reads what HQPlayer holds after it — never the old run's
    # test mode or lowered volume — as the owner's: closed with no notice
    # (quiet), as the run's own first step closes one (_take_owed). One that
    # sets off a build refuses this start (busy below): the owner starts
    # again once HQPlayer answers.
    unsaid = recover(ep["id"], link, gave=b.gave, quiet=True)
    try:
        return _start_run(ep, link, b, mode, fixed_volume_ok, unsaid)
    except BenchmarkRefused:
        if unsaid:
            _notify()       # the mark the run would have judged at once: no run follows
        raise


def _start_run(ep: dict, link, b, mode: Optional[str], fixed_volume_ok: bool,
               unsaid: bool) -> Dict[str, Any]:
    """start() once a run cut short is put back: what HQPlayer answers is the
    precondition, and the run's thread starts — `unsaid`: the put-back left
    a mark for it to judge, said at its end (_run). A run of "the mode
    HQPlayer is in" (`mode` None) takes that mode at its own rest (_measure):
    this read may find HQPlayer in the measured mode, where a check of ours
    holds it a moment or left it."""
    global _thread
    from playback import hqp_link
    busy = link.busy()
    if busy is not None:
        raise BenchmarkRefused(f"{busy}. Start the benchmark once it answers again.")
    try:
        with link.asking() as c:
            status, st = c.get_status(), c.get_state()
            modes, vr = c.get_modes(), c.volume_range()
    except (hqp_link.Building, hqp_link.NotAnswering) as e:
        raise BenchmarkRefused(f"{e}. Start the benchmark once it answers again.")
    except (BrokenPipeError, ConnectionError, OSError):
        raise BenchmarkRefused(hqp_link.NOT_ANSWERING)
    if status is None or st is None:
        raise BenchmarkRefused("HQPlayer did not report its state")
    if modes is None:
        raise BenchmarkRefused("HQPlayer did not list its modes — try again")
    # HQPlayer answering is the whole precondition — a queue mirror the
    # attach could not make (HQPlayer not up yet, 2026-10-03) is none: the
    # run sets HQPlayer's playlist itself, and the attach that takes the
    # output back mirrors the queue afresh. Another controller is, while it
    # plays: the status just read names an entry that is not in the queue.
    if b.reads_foreign(status):
        raise BenchmarkRefused("Another controller is playing through HQPlayer — the "
                               "benchmark waits until Sautium is its source again")
    if status.process_speed is None:
        raise BenchmarkRefused("This HQPlayer does not report its processing speed "
                               "(HQPlayer 5.17 and later do)")
    kind = mode or mode_kind(_mode_name(modes, st["mode"]))
    if kind is None:
        raise BenchmarkRefused("HQPlayer's mode is [source] — choose PCM or SDM to measure")
    if _mode_index(modes, kind) is None:
        raise BenchmarkRefused(f"This HQPlayer's output offers no {kind.upper()} mode")
    if mute_level(vr, st["volume"]) is None and not fixed_volume_ok:
        raise BenchmarkRefused("HQPlayer cannot lower its volume below your level on this "
                               "output — turn your amplifier down and confirm")
    with _lock:
        if _state["running"]:
            raise BenchmarkRefused("A benchmark is already running")
        _cancel.clear()
        _halt.clear()
        _state.update(running=True, cancel_requested=False, endpoint_id=ep["id"], mode=kind,
                      point=0, total=0, point_label="Preparing…", eta_s=None,
                      started_at=time.time(), outcome=None, note=None)
        _thread = threading.Thread(target=_run, args=(ep, kind, b.gave, mode, unsaid),
                                   daemon=True, name="hqp-benchmark")
        _thread.start()
    return state()


def cancel() -> bool:
    with _lock:
        if not _state["running"]:
            return False
        _state["cancel_requested"] = True
    _cancel.set()
    live = _live
    if live is not None:
        live.interrupt()            # not after ANSWER_WAIT_S of a silent build
    return True


def shutdown(timeout: float) -> None:
    """The app is stopping: a run is cancelled and given `timeout` seconds
    to put HQPlayer back — what the process has left of the stop's ten.
    Putting back does not wait for an HQPlayer that is away; a run that
    could not be put back stays open, and the next start puts it back
    (recover)."""
    t = _thread
    if t is None or not t.is_alive():
        return
    _halt.set()
    cancel()
    t.join(timeout)
    if t.is_alive():
        logger.warning("HQPlayer benchmark still putting HQPlayer back after %.0f s — "
                       "the next start finishes it", timeout)


def _progress(**kw) -> None:
    from playback.manager import manager
    with _lock:
        _state.update(kw)
        snap = {k: _state[k] for k in ("point", "total", "point_label", "eta_s", "mode")}
    manager.update_hold(**snap)


def _why(e: Exception) -> str:
    return str(e) or type(e).__name__


def _run(ep: dict, kind: str, gave: Callable[[str], bool], asked: Optional[str],
         unsaid: bool) -> None:
    """The job's thread. Whatever ends it is recorded where the owner reads
    it (state(), and the run row once there is one); the job is closed
    before the output goes back — the attach that takes it back then finds
    no run going, and puts back one whose restore HQPlayer did not answer
    (recover)."""
    from playback.manager import manager
    try:
        with manager.hold("benchmark", LABEL):
            try:
                outcome, note = _measure(ep, kind, gave, asked)
            except (_Abort, _Lost) as e:
                outcome, note = "failed", str(e)
            except Exception as e:      # the job's boundary: recorded, never lost
                logger.exception("HQPlayer benchmark failed")
                outcome, note = "failed", _why(e)
            _finish(outcome, note)
    except Exception as e:              # the output could not be lent
        logger.warning("HQPlayer benchmark did not start: %s", _why(e))
        if running():
            _finish("failed", _why(e))
    with _lock:
        word = ep["id"] in _left_to_run
        _left_to_run.discard(ep["id"])
    if unsaid or word:
        # A mark start()'s put-back left for this run to judge, or a check
        # its hold cut once past its way back, is said once the run is over
        # — judged, or not reached: HQPlayer gone at its start, the output
        # not lent
        _notify()


def _finish(outcome: str, note: Optional[str]) -> None:
    with _lock:
        _state.update(running=False, outcome=outcome, note=note, point_label="", eta_s=None)
    logger.info("HQPlayer benchmark %s%s", outcome, f": {note}" if note else "")


def _measure(ep: dict, kind: str, gave: Callable[[str], bool],
             asked: Optional[str]) -> Tuple[str, Optional[str]]:
    """The run proper, inside the hold: put back what a run cut short before
    it still owes (`gave` tells an entry the node gave HQPlayer), read what
    is to be put back, open the run row, measure, and put everything back
    whatever happens. A run of the mode HQPlayer is in (`asked` None) takes
    that mode from what it reads here, never from start()'s read."""
    global _live
    link = _Link(_cancel.wait, answer_s=ANSWER_WAIT_S)
    if not link.connect():
        logger.warning("HQPlayer benchmark: no connection to HQPlayer: %s", link.refusal())
        return "failed", "HQPlayer is not answering"
    _live = link
    try:
        _take_owed(link, ep["id"], gave, asked)
        pre, modes, vr = _again(lambda: (link("get_state"), link("get_modes"),
                                         link("volume_range")))
        if pre is None:
            return "failed", "HQPlayer did not report its state"
        if modes is None:
            return "failed", "HQPlayer did not list its modes"
        if asked is None:
            kind = mode_kind(_mode_name(modes, pre["mode"]))
            if kind is None:
                return "failed", "HQPlayer's mode is [source] — choose PCM or SDM to measure"
            _progress(mode=kind)
        mode_index = _mode_index(modes, kind)
        if mode_index is None:
            return "failed", f"This HQPlayer's output offers no {kind.upper()} mode"
        mode_name = _mode_name(modes, mode_index)
        mute = mute_level(vr, pre["volume"])
        run_id = _open_run(ep, mode_name, pre, mute)
        # It read what HQPlayer holds as the owner's: a run left to them there
        # is no one's to name now
        clear_left(ep["id"])
        switched = pre["mode"] != mode_index
        own: Optional[dict] = None
        stats: Dict[str, Any] = {"measured": 0, "dropouts": [], "own": [], "refused": 0,
                                 "unstarted": 0, "failed": 0, "implied": 0, "deferred": 0}
        outcome, note = "failed", None
        try:
            st, filters, shapers, rates = _again(lambda: _enter_mode(link, mode_index, switched))
            if switched:
                own = {"mode": mode_index, "rate": st["rate"], "filterNx": st["filterNx"],
                       "filter1x": st["filter1x"], "shaper": st["shaper"]}
                _note_own(run_id, own)
            srcs = sources(kind, ep.get("hqp_engine"))
            grid = plan(ep, kind=kind, mode_name=mode_name, state=st, filters=filters,
                        shapers=shapers, rates=rates, srcs=srcs)
            _progress(point=0, total=grid.asks(), point_label="Preparing the test signals…",
                      eta_s=None)
            used = {p.source for p in grid.points}
            uris = {name: (_dsd_uri(s["file"]) if name == "dsd"
                           else _signal_uri(ensure_signal(s["rate"])))
                    for name, s in srcs.items() if name in used}
            db_execute("UPDATE hqp_benchmark_runs SET points_planned = %(n)s, dsd_uri = %(d)s "
                       "WHERE id = %(id)s", {"n": grid.asks(), "d": uris.get("dsd"), "id": run_id})
            mode = (mode_index, _lists_key(filters, shapers, rates))
            slots = _prepare(link, uris, mute, mode)
            outcome, note = _points(link, grid, slots, uris, srcs, mute, mode, ep, run_id, st,
                                    hqp_load.context(ep, mode=mode_name, state=st), mode_name,
                                    stats)
        except _Abort as e:
            outcome, note = "failed", str(e)
        except _Lost as e:
            outcome, note = ("cancelled", None) if _cancel.is_set() else ("failed", str(e))
        except Exception as e:          # recorded on the run, whatever it was
            logger.exception("HQPlayer benchmark failed")
            outcome, note = "failed", _why(e)
        finally:
            _live = None                # a second cancel must not cut the put-back
            reached, restore_note, gone, measured_left = _restore(link, pre, own, mute,
                                                                  switched)
            note = "; ".join(x for x in (note, restore_note) if x) or None
            # An HQPlayer that did not answer the restore keeps the run
            # open: the next attach, or its next answer, puts it back
            # (recover) — unless the owner changes it first. One that went
            # away — it closed, reset or refused the connection, the app not
            # stopping — may come back with what it booted with (lost_at);
            # one that stayed silent, busy, did not.
            if not reached:
                _owe(run_id)
            _close_run(run_id, outcome, note, stats, finished=reached,
                       lost=gone and not _halt.is_set(), unchecked=reached and measured_left)
        return outcome, note
    finally:
        _live = None
        link.close()


def _enter_mode(link: _Link, mode_index: int, switched: bool) -> tuple:
    """Into the measured mode — HQPlayer keeps one selection per mode, read
    as it is there — and its lists. Stopped first: the hold leaves HQPlayer
    as it played, and a mode switched under the owner's track would rebuild
    it while it sounds."""
    link("stop")
    if switched and not link("set_mode", mode_index):
        raise _Abort(f"HQPlayer did not switch modes: {link.refusal()}")
    st = link("get_state")
    if st is None:
        raise _Abort("HQPlayer did not report its state")
    return (st, *_lists(link))


def _lists(link: _Link) -> tuple:
    """The measured mode's filters, shapers and rates: a run neither plans
    on, nor checks its plan against, a list HQPlayer did not give."""
    lists = link("get_filters"), link("get_shapers"), link("get_rates")
    missing = [name for name, items in zip(("filters", "shapers", "rates"), lists) if items is None]
    if missing:
        raise _Abort(f"HQPlayer did not list its {', '.join(missing)}")
    return lists


def _lists_key(filters: List[dict], shapers: List[dict], rates: List[dict]) -> tuple:
    """What the indices a point sends name — the run's plan holds only as
    long as HQPlayer's lists do."""
    return (tuple((f["index"], f["name"]) for f in filters),
            tuple((x["index"], x["name"]) for x in shapers),
            tuple((r["index"], r["rate"]) for r in rates))


def _prepare(link: _Link, uris: Dict[str, str], mute: Optional[float], mode: tuple, *,
             again: bool = False) -> Dict[str, int]:
    """What every point stands on — made again whenever the connection was
    replaced, before anything plays: HQPlayer stopped, in the measured mode
    with the lists the plan was made from (`again`: it may have restarted
    under the run, back in the mode it saved — an Embedded restarted into
    PCM read the run's SDM modulators as "invalid shaper" and played PCM,
    2026-10-03), lowered (read back), and holding the run's sources in
    order. The slot of each source, 1-based."""
    mode_index, lists = mode

    def once() -> Dict[str, int]:
        link("stop")
        if again:
            st = link("get_state")
            if st is None:
                raise _Abort("HQPlayer did not report its state")
            if st["mode"] != mode_index and not link("set_mode", mode_index):
                raise _Abort(f"HQPlayer did not go back to the measured mode: {link.refusal()}")
            if _lists_key(*_lists(link)) != lists:
                raise _Abort("HQPlayer came back with other lists — the run's settings would "
                             "name others")
        if mute is not None:
            link("set_volume", mute)
            got = link("get_state")
            if got is None or abs(got["volume"] - mute) > _DB_EPS:
                raise _Abort("HQPlayer did not lower its volume — nothing was played")
        for i, (name, uri) in enumerate(uris.items()):
            if not link("playlist_add", uri, clear=(i == 0)):
                what = "the DSD file" if name == "dsd" else "the test signal"
                raise _Abort(f"HQPlayer did not take {what}: {link.refusal()}")
        if len(link("get_playlist")) != len(uris):
            raise _Abort("HQPlayer took the run's sources but does not hold them — "
                         "it could not open them (see its log)")
        return {name: i + 1 for i, name in enumerate(uris)}
    return _again(once, stop=_cancel.is_set)


def _points(link: _Link, grid: Plan, slots: Dict[str, int], uris: Dict[str, str],
            srcs: Dict[str, dict], mute: Optional[float], mode: tuple, ep: dict, run_id: int,
            st: dict, ctx: Dict[str, Any], mode_name: str,
            stats: dict) -> Tuple[str, Optional[str]]:
    """The plan's points in order, its ladders kept as the run goes: a rung
    a neighbour has answered for is passed over, and a rung at or under a
    start that does not keep up (or, under it, says nothing; or above a
    start HQPlayer refused) sends the run a rung further down, before the
    owner's setting comes again."""
    points = list(grid.points)
    known = dict(grid.known)
    passed = set(grid.covered)          # and what this run has asked so far

    def step_down(p: Point) -> None:
        if p.ladder is None or i == len(points):
            return
        below = _descend(grid.rungs[p.ladder], known, passed)
        if below is None:
            return
        # A rung the plan holds further on is asked next instead — the
        # ladder goes down a rung at a time, never past one not yet asked
        later = next((j for j in range(i, len(points) - 1) if points[j].key == below.key), None)
        points.insert(i, points.pop(later) if later is not None else below)

    applied: Dict[str, Any] = {}
    failures = 0
    asked = 0
    # The first point this run saw start, and how long its start took: the
    # control a point that never starts is checked against
    control: Optional[Tuple[Point, float]] = None
    t_run = time.monotonic()
    i = 0
    while i < len(points):
        p = points[i]
        i += 1
        if not p.own and p.ladder is not None and _implied(p, grid.rungs[p.ladder], known):
            stats["implied"] += 1
            continue
        if grid.deferred(p, known):
            stats["deferred"] += 1
            continue
        asked += 1
        passed.add(p.key)
        total = len(points) - stats["implied"] - stats["deferred"]
        each = (time.monotonic() - t_run) / (asked - 1) if asked > 1 else pace(ep["id"])
        _progress(point=asked, total=total, point_label=p.label,
                  eta_s=round(each * (total - asked + 1)))
        if _cancel.is_set():
            return "cancelled", None
        try:
            result = _point(link, p, slots, applied, run_id, srcs)
        except _Reopened:
            if _cancel.is_set():
                return "cancelled", None        # the cancel woke a read (interrupt)
            # HQPlayer may have restarted under the point: back in the
            # measured mode, lowered again, its sources loaded again, the
            # knobs sent again — then once more.
            applied.clear()
            slots = _prepare(link, uris, mute, mode, again=True)
            try:
                result = _point(link, p, slots, applied, run_id, srcs)
            except _Reopened as e:
                if _cancel.is_set():
                    return "cancelled", None
                applied.clear()
                slots = _prepare(link, uris, mute, mode, again=True)
                result = {"failed": f"HQPlayer dropped the connection during it twice ({e})"}
        if result.get("cancelled"):
            return "cancelled", None
        if result.get("at_play") or ("unstarted" in result and not result.get("hung")):
            # A stop at Play and a build past the limit are kept for good
            # ("refused", "never started here"), so HQPlayer must still
            # start what it started earlier in this run: an Embedded whose
            # output engine had stopped starting anything (its stream fed
            # the same file over and over, 2026-10-03) built every setting
            # past the limit, and an output that went away stops every Play.
            # Nothing started yet, nothing tells them apart — an engine hung
            # before the run (2026-10-04) would have marked the owner's own
            # setting "never started" for good, and an owner's setting HQPlayer
            # refuses from the 44.1 kHz signal must not stop every run: the
            # point failed, asked again next run, and three in a row stop it.
            if control is None:
                result = {"failed": f"{result.get('refused') or result['unstarted']} — not "
                                    "kept: nothing had started yet in this run to check "
                                    "HQPlayer against"}
            else:
                c, c_init = control
                limit = max(START_DEADLINE_S, 3 * c_init)
                try:
                    again = _point(link, c, slots, applied, run_id, srcs,
                                   build_limit=limit, start_only=True)
                except _Reopened:
                    if _cancel.is_set():
                        return "cancelled", None
                    applied.clear()
                    slots = _prepare(link, uris, mute, mode, again=True)
                    continue              # it says nothing of this point: asked next run
                if again.get("cancelled"):
                    return "cancelled", None
                if not again.get("started"):
                    raise _Abort(f"HQPlayer stopped starting anything: {c.label} started "
                                 f"earlier in this run and did not start again within "
                                 f"{limit:.0f} s — restart HQPlayer, and check its output is there")
        line = f"benchmark {asked}/{total} · {mode_name} · {p.label}: "
        if "refused" in result:
            logger.info(line + "refused — " + result["refused"])
            _ledger(run_id, p, "refused", result["refused"])
            stats["refused"] += 1
            failures = 0
            if p.step < 0:
                step_down(p)
            continue
        status: Optional[TrackStatus] = result.get("status")
        if not ("failed" in result or "unstarted" in result) and status is None:
            result = {"failed": "it never played with its source reported"}
        # Not played: measured again next run when it failed, not asked again
        # when this host did not get it going — HQPlayer silent past
        # BUILD_LIMIT_S, which ends the run too (`hung`). Three failures in a
        # row stop it: the output is broken.
        missed = "unstarted" if "unstarted" in result else "failed" if "failed" in result else None
        if missed is not None:
            logger.info(line + result[missed])
            _ledger(run_id, p, missed, result[missed])
            if result.get("hung"):
                raise _Lost(result[missed])
            stats[missed] += 1
            failures = 0 if result.get("outran") else failures + 1     # that one played
            if failures >= 3:
                raise _Abort(f"three points in a row did not play: {result[missed]}")
            if missed == "unstarted":
                known[p.key] = False                # it does not keep up, by any measure
                step_down(p)
            elif p.step < 0:
                step_down(p)
            continue
        failures = 0
        sample = hqp_load.Sample.of(status, st, endpoint_id=ep["id"])
        sample = replace(sample, source="benchmark", run_id=run_id,
                         process_speed=round(result["speed"], 4),
                         output_fill=result["out_fill"], input_fill=result["in_fill"],
                         dropout=result["dropout"], settled=result["settled"],
                         init_s=result["init_s"], settle_s=result["settle_s"])
        sample_id = hqp_load.write(sample)
        if sample_id is None:
            raise _Abort(_FORGOTTEN)
        _ledger(run_id, p, "dropout" if result["dropout"]
                else "measured" if result["settled"] else "unsettled", None, sample_id)
        stats["measured"] += 1
        if control is None:
            control = (p, result["init_s"])
        if result["dropout"]:
            stats["dropouts"].append(result["speed"])
        if p.own:
            stats["own"].append(result["speed"])
        logger.info(line + _verdict(result))
        known[p.key] = (not result["dropout"]
                        and result["speed"] >= hqp_load.thresholds(ctx)["ok"])
        if not known[p.key]:
            step_down(p)
    return "done", _tally(stats)


def _tally(stats: dict) -> Optional[str]:
    """What a finished run says besides its counts."""
    parts = []
    if stats["refused"]:
        parts.append(f"HQPlayer refused {stats['refused']} combination"
                     f"{'s' if stats['refused'] != 1 else ''}")
    if stats["unstarted"]:
        parts.append(f"{stats['unstarted']} setting{'s' if stats['unstarted'] != 1 else ''} "
                     f"never started here within {BUILD_LIMIT_S / 60:.0f} min — not asked again")
    if stats["failed"]:
        parts.append(f"{stats['failed']} point{'s' if stats['failed'] != 1 else ''} did not "
                     "play through and are measured again next run")
    if stats["implied"]:
        parts.append(f"{stats['implied']} rate{'s' if stats['implied'] != 1 else ''} not "
                     "measured — the rate next to it on its ladder answers for it")
    if stats["deferred"]:
        parts.append(f"{stats['deferred']} filter{'s' if stats['deferred'] != 1 else ''} left "
                     "for later — your own setting does not keep up here; they are measured "
                     "with one that does")
    return "; ".join(parts) or None


def _verdict(r: dict) -> str:
    """The per-point log line's verdict: the speed, how the readings went,
    how long the setting took to start."""
    rs = r["readings"][-SETTLE_WINDOW:]
    spread = f"{min(rs):.2f}–{max(rs):.2f}" if rs else "—"
    init = f"init {r['init_s']:.1f} s" if r["init_s"] is not None else "init —"
    fill = f"{r['out_fill']:.2f}" if r["out_fill"] is not None else "—"
    if r["dropout"]:
        return f"dropped out at {r['speed']:.2f}× ({init}, output fill down to {fill})"
    if not r["settled"]:
        return (f"not settled in {SETTLE_CAP_S:.0f} s: {r['speed']:.2f}× "
                f"(last {spread} of {' '.join(f'{x:.2f}' for x in r['readings'])}, {init})")
    return (f"{r['speed']:.2f}× (p10 of {spread}, {init}, settled in {r['settle_s']:.1f} s, "
            f"output fill ≥ {fill})")


def _point(link: _Link, p: Point, slots: Dict[str, int], applied: Dict[str, Any],
           run_id: int, srcs: Dict[str, dict], **watch) -> dict:
    """One point: stop, send only the knobs that change and note what
    HQPlayer holds now (the run's mark, recover), start its source, feed
    Settle a <Status/> a second until it decides."""
    if _cancel.is_set():
        return {"cancelled": True}      # before its SelectTrack starts it
    link("stop")
    knobs = {"rate": p.rate, "filter": p.filter, "filter1x": p.filter1x, "shaper": p.shaper}
    change = {k: v for k, v in knobs.items() if applied.get(k) != v}
    if "filter" in change or "filter1x" in change:
        change.update(filter=p.filter, filter1x=p.filter1x)
    if change:
        _, failed = link("apply_settings", **change)
        now = link("get_state")
        applied.clear()
        if now is not None:
            applied.update(rate=now["rate"], filter=now["filterNx"], filter1x=now["filter1x"],
                           shaper=now["shaper"])
            _note_selection(run_id, now)
        if failed:
            return {"refused": "; ".join(f"{k}: {v}" for k, v in failed.items())}
    slot = slots[p.source]
    src_rate = None if p.source == "dsd" else srcs[p.source]["rate"]
    verdict = _play(link, slot, src_rate, len(slots), **watch)
    if verdict.get("elsewhere"):
        verdict = _play(link, slot, src_rate, len(slots), **watch)
    return verdict


def _select(send: Callable[..., Any], read: Callable[[], Optional[TrackStatus]],
            slot: int) -> None:
    """SelectTrack, then <Status/> until it names the slot — at most
    SELECT_WAIT_S: HQPlayer cannot say when it is ready, and the Play right
    after must find the slot taken (a misplay is caught on the ticks). A
    stopped HQPlayer starts playing the slot it is given (Desktop 5 and 6:
    "GoTo 1", then "Play (1/0)" in its log): the point starts here, and the
    build of its setting may silence HQPlayer from here."""
    if not send("select_track", slot):
        return
    deadline = time.monotonic() + SELECT_WAIT_S
    while time.monotonic() < deadline:
        now = read()
        if now is not None and now.track_index == slot:
            return
        if _cancel.wait(SELECT_POLL_S):
            return


def _play(link: _Link, slot: int, src_rate: Optional[int], entries: int, *,
          build_limit: Optional[float] = None, start_only: bool = False) -> dict:
    """Start the slot and feed Settle until it decides. HQPlayer playing
    another entry than the slot (or a generated signal at another rate) is
    `elsewhere` — the caller selects it once more. Whatever HQPlayer is
    asked from the select on may meet the silence of a build: it is waited
    out (_outlast) and the point watched on; past the build limit the point
    is `unstarted` (`failed` once it had moved) and `hung` ends the run."""
    settle = Settle(time.monotonic(), build_limit=build_limit, start_only=start_only)

    def read() -> Optional[TrackStatus]:
        try:
            return link("get_status")
        except _Silent:
            return _outlast(link, settle)

    def send(method: str, *args) -> Any:
        try:
            return link(method, *args)
        except _Silent:
            # Taken before HQPlayer fell silent: it carries it out once free.
            _outlast(link, settle)
            return True

    try:
        _select(send, read, slot)
        if _cancel.is_set():
            return {"cancelled": True}
        if not send("play"):
            return {"failed": f"HQPlayer did not play it: {link.refusal()}"}
        misses = 0
        stopped = 0
        replayed = False
        while True:
            if _cancel.wait(TICK_S):
                return {"cancelled": True}
            status = read()
            if status is None:
                misses += 1
                if misses >= 5:
                    raise _Abort(f"HQPlayer stopped reporting its status: {link.refusal()}")
                continue
            misses = 0
            if status.tracks_total and status.tracks_total != entries:
                raise _Abort("HQPlayer's playlist changed under the benchmark — another "
                             "controller is using it")
            if status.state == PlaybackState.PLAYING and (
                    status.track_index != slot
                    or (src_rate is not None and status.src_rate and status.src_rate != src_rate)):
                if settle.outran(time.monotonic()):     # went on to the next entry by itself
                    return settle.ran_out(time.monotonic())
                return {"elsewhere": True,
                        "failed": f"HQPlayer played entry {status.track_index} instead of {slot}"}
            if status.state == PlaybackState.STOPPED and not settle.moved:
                stopped += 1
                if not replayed and (settle.playing is not None or stopped >= STOP_TICKS):
                    # HQPlayer stops the transport once a heavy setting is
                    # built and does not start again by itself (Desktop 6.2.3
                    # on Windows; 6.2.4 plays on), or it stopped at once:
                    # start it again, once.
                    replayed, stopped = True, 0
                    _select(send, read, slot)
                    send("play")
                    continue
                if replayed and stopped >= STOP_TICKS:
                    # Stopped again, never having moved: HQPlayer does not
                    # run this combination ("Requested filter not possible
                    # with this rate combination …, stop" in its log). Asked,
                    # answered — a later run does not ask again.
                    return {"refused": "HQPlayer stopped it again without playing — it does "
                                       "not run this combination (its log says why)",
                            "at_play": True}
            else:
                stopped = 0
            verdict = settle.feed(time.monotonic(), status)
            if verdict is not None:
                if verdict.get("dropout"):
                    # The DSP falling behind, or HQPlayer going away — a
                    # trial-mode Embedded stops its transport and closes
                    # every connection: the next read tells them apart
                    # (_Reopened makes the point again, _Lost ends the run;
                    # neither records a dropout).
                    if _cancel.wait(TICK_S):
                        return {"cancelled": True}
                    read()
                return verdict
    except _Hung as e:
        return {"failed" if settle.moved else "unstarted": str(e), "hung": True}


def _outlast(link: _Link, settle: Settle) -> TrackStatus:
    """HQPlayer took the point's setting and fell silent building it (or
    stuck while it played): ask for its <Status/> again on each new
    connection until it answers — the build is done then. The silence is
    given back to the point's deadlines; its start keeps it (init_s). A
    cancel ends the wait at the next ask; past BUILD_LIMIT_S, _Hung."""
    began = time.monotonic() - ANSWER_WAIT_S          # the ask that met the silence
    while True:
        if _cancel.is_set():
            raise _Lost("cancelled while HQPlayer was still building a setting")
        if time.monotonic() - began >= settle.build_limit:
            raise _Hung(f"HQPlayer stayed silent {settle.build_limit / 60:.0f} min building it — "
                        "this host does not get it going, or HQPlayer hung")
        try:
            status = link("get_status")
        except _Silent:
            continue
        except _Reopened:
            if not _cancel.is_set():
                raise
            continue                # the cancel woke the read (interrupt)
        silent = time.monotonic() - began
        if status is not None and status.state in (PlaybackState.STOPPED, PlaybackState.STOPREQ):
            # Built and stopped, or restarted — a box reset mid-point goes
            # silent the same way, and its stop read as a dropout: made
            # again (a design it built starts in a second the next time)
            raise _Reopened(f"HQPlayer came back stopped after {silent:.0f} s of silence")
        settle.paused(silent)
        logger.info("HQPlayer benchmark: HQPlayer answered again after %.0f s of silence", silent)
        return status


def _restore(link: _Link, pre: dict, own: Optional[dict], mute: Optional[float],
             switched: bool) -> Tuple[bool, Optional[str], bool, bool]:
    """Put back what the run changed, whatever ended it. Returns (HQPlayer
    answered, what could not be put back, whether HQPlayer went away under
    the put-back — it closed, reset or refused the connection, counted as
    it happened (the client's losses, the poller's own rule), whatever
    failure came last — rather than only stayed silent, whether the
    owner's selection of the mode measured — the one the run switched into
    (`switched`), else the one it found — was left as the run set it:
    _put_back; False when HQPlayer did not answer, the put-back owed in
    full: recover). Only the app stopping cuts the wait for an HQPlayer
    that is away short; one that is there but silent — a cancel in the
    middle of a build — is left to recover (RESTORE_ANSWER_S)."""
    losses = link.c.losses
    link.wait = _halt.wait
    link.c.timeout = RESTORE_ANSWER_S
    # A cancel's wake-up no wait of the run met is spent here: the run acted
    # on the cancel, and the put-back is no part of it
    link.c.interrupted()
    sock = link.c.socket
    if sock is not None:
        sock.settimeout(RESTORE_ANSWER_S)
    try:
        problems, unset = _again(lambda: _put_back(link, pre=pre, own=own,
                                                   volume=mute is not None, clear=True,
                                                   enter=True),
                                 silent_ends=True)
        after = _again(lambda: link("get_state"), silent_ends=True)
    except _Lost:
        return (False, "HQPlayer did not answer to be put back — it is when it answers again",
                link.c.losses > losses, False)
    # Switched with no own selection read: the run set nothing there
    measured_left = unset["own" if switched else "pre"]
    if after is None:
        return True, "HQPlayer did not report its state after the run", False, measured_left
    diff = [k for k in ("mode", "rate", "filterNx", "filter1x", "shaper")
            if after.get(k) != pre.get(k)]
    if mute is not None and abs(after["volume"] - pre["volume"]) > _DB_EPS:
        diff.append("volume")
    if diff or problems:
        logger.warning("HQPlayer benchmark: not put back as it was — %s %s", diff, problems)
        return (True, "not everything was put back: " + ", ".join(diff + problems), False,
                measured_left)
    return True, None, False, measured_left


def _put_back(link: _Link, *, pre: dict, own: Optional[dict], volume: bool,
              clear: bool, enter: bool) -> Tuple[List[str], Dict[str, bool]]:
    """Stop; the run's sources out of the playlist (`clear`); the measured
    mode's own selection when the run switched into it — HQPlayer keeps one
    selection per mode: in that mode, entered for it when `enter` (the
    run's own end, a whole put-back: what the run left there is known),
    else only while HQPlayer is in it (one it does not show is no one's to
    judge) — then the mode it found and that mode's selection; the owner's
    volume (`volume`: the run lowered it, or HQPlayer came back louder). A
    selection goes back only while HQPlayer shows its mode, read after the
    switch: in a mode HQPlayer did not take, a selection of another mode's
    indices would set what neither holds. What HQPlayer refused, as "knob:
    its words", and by selection — the mode the run switched into (`own`),
    the mode it found (`pre`) — whether it was not set back: refused, or its
    mode not shown; in the mode the run measured, a test point HQPlayer may
    still hold."""
    link("stop")
    if clear:
        link("playlist_clear")
    problems: List[str] = []
    now = link("get_state")
    mode = now["mode"] if now is not None else None

    def switched(to: int) -> Optional[int]:
        _, failed = link("apply_settings", mode=to)
        problems.extend(f"{k}: {v}" for k, v in failed.items())
        there = None if failed else link("get_state")
        if not failed and (there is None or there["mode"] != to):
            problems.append("mode: HQPlayer did not show the mode it was switched to")
        return there["mode"] if there is not None else None
    unset = {"own": own is not None, "pre": True}
    if own is not None:
        if mode != own["mode"] and enter:
            mode = switched(own["mode"])
        if mode == own["mode"]:
            _, failed = link("apply_settings", rate=own["rate"], filter=own["filterNx"],
                             filter1x=own["filter1x"], shaper=own["shaper"])
            problems += [f"{k}: {v}" for k, v in failed.items()]
            unset["own"] = bool(failed)
    if mode != pre["mode"]:
        mode = switched(pre["mode"])
    if mode == pre["mode"]:
        _, failed = link("apply_settings", rate=pre["rate"], filter=pre["filterNx"],
                         filter1x=pre["filter1x"], shaper=pre["shaper"])
        problems += [f"{k}: {v}" for k, v in failed.items()]
        unset["pre"] = bool(failed)
    if volume:
        link("set_volume", pre["volume"])
    return problems, unset


def _open_run(ep: dict, mode_name: str, pre: dict, mute: Optional[float]) -> int:
    row = db_execute("""
        INSERT INTO hqp_benchmark_runs
            (hqp_endpoint_id, hqp_engine, mode, points_planned, pre_mode, pre_rate, pre_filter,
             pre_filter1x, pre_shaper, pre_matrix_profile, pre_volume, mute_volume,
             cuda, matrix_profile, convolution)
        SELECT e.id, e.hqp_engine, %(mode)s, 0, %(m)s, %(r)s, %(f)s, %(f1)s, %(s)s, %(mp)s,
               %(v)s, %(mute)s, e.cuda, %(mp)s, %(conv)s
          FROM hqp_endpoints e WHERE e.id = %(e)s
        RETURNING id
    """, {"e": ep["id"], "mode": mode_name, "m": pre["mode"], "r": pre["rate"],
          "f": pre["filterNx"], "f1": pre["filter1x"], "s": pre["shaper"],
          "mp": pre.get("matrix_profile") or None, "v": pre["volume"], "mute": mute,
          "conv": bool(pre.get("convolution"))})
    if row is None:
        raise _Abort("This HQPlayer is no longer registered")
    return row["id"]


def _note_own(run_id: int, own: dict) -> None:
    db_execute("""UPDATE hqp_benchmark_runs
                     SET own_mode = %(mode)s, own_rate = %(rate)s, own_filter = %(filterNx)s,
                         own_filter1x = %(filter1x)s, own_shaper = %(shaper)s
                   WHERE id = %(id)s""", {**own, "id": run_id})


def _note_selection(run_id: int, st: dict) -> None:
    db_execute("""UPDATE hqp_benchmark_runs
                     SET last_rate = %(rate)s, last_filter = %(filterNx)s,
                         last_filter1x = %(filter1x)s, last_shaper = %(shaper)s
                   WHERE id = %(id)s""", {**st, "id": run_id})


_FORGOTTEN = "This HQPlayer was forgotten during the run — its measurements went with it"


def _ledger(run_id: int, p: Point, result: str, note: Optional[str] = None,
            sample_id: Optional[int] = None) -> None:
    """The point's outcome by what it asked. The run's row gone means its
    HQPlayer was forgotten (the rows go with the endpoint's): the run
    ends."""
    rate_hz, filt, shaper, src_rate, src_channels = p.key
    row = db_execute("""
        INSERT INTO hqp_benchmark_points
            (run_id, rate_hz, filter, shaper, src_rate, src_channels, result, note, sample_id)
        SELECT r.id, %(rate)s, %(f)s, %(s)s, %(sr)s, %(sc)s, %(r)s::hqp_point_result,
               %(note)s, %(sample)s
          FROM hqp_benchmark_runs r WHERE r.id = %(run)s
        RETURNING id
    """, {"run": run_id, "rate": rate_hz, "f": filt, "s": shaper, "sr": src_rate,
          "sc": src_channels, "r": result, "note": note, "sample": sample_id})
    if row is None:
        raise _Abort(_FORGOTTEN)


def _close_run(run_id: int, outcome: str, note: Optional[str], stats: dict, *,
               finished: bool, lost: bool, unchecked: bool = False) -> None:
    """The run's row as it ended. `unchecked`: the measured mode's own
    selection HQPlayer did not take back at the run's end — a test point
    may stand there: marked, judged at HQPlayer's nearest rest or a new
    run's start, and said."""
    own = stats["own"]
    row = db_execute("""
        UPDATE hqp_benchmark_runs
           SET finished_at = CASE WHEN %(fin)s THEN now() END,
               outcome = CASE WHEN %(fin)s THEN %(o)s::hqp_bench_outcome END,
               lost_at = CASE WHEN %(lost)s THEN now() END,
               measured_unchecked_at = CASE WHEN %(u)s THEN now() END,
               note = %(note)s, points_measured = %(n)s, dropout_speed = %(d)s,
               first_speed = %(first)s, last_speed = %(last)s
         WHERE id = %(id)s
     RETURNING hqp_endpoint_id
    """, {"id": run_id, "fin": finished, "lost": lost, "o": outcome, "note": note,
          "u": unchecked, "n": stats["measured"],
          "d": max(stats["dropouts"]) if stats["dropouts"] else None,
          "first": own[0] if own else None, "last": own[-1] if len(own) > 1 else None})
    if unchecked and row is not None:      # none: forgotten with its HQPlayer, no mark to set
        with _lock:
            _unchecked.pop(row["hqp_endpoint_id"], None)    # read again: a new mark
            _busy_at.pop(row["hqp_endpoint_id"], None)
            _bump()
        _notify()


class _OnLink:
    """The playback link (playback.hqp_link) in _Link's shape, for putting a
    run back outside a hold: its requests go where every other request of
    this node to HQPlayer goes. A connection replaced under it is a
    _Reopened (_Silent after a silence), as on the run's own."""

    def __init__(self, link):
        self.c = link.client

    def refusal(self) -> str:
        return self.c.refusal()

    def __call__(self, method: str, *args, **kwargs):
        result = getattr(self.c, method)(*args, **kwargs)
        if not self.c.is_connected():
            raise (_Silent if self.c.timed_out else _Reopened)(self.c.refusal())
        return result


def _owe(run_id: int, link=None) -> tuple:
    """The put-back of a run cut short, owed from now: a DSP item of the
    HQPlayer's link (dsp_knob) — a person's DSP change or switch away after
    it supersedes it. Made once per run and link; a link made afresh owes it
    anew — unless a person superseded it on the link it was owed on: then it
    stays superseded."""
    from playback import hqp_link
    link = link if link is not None else hqp_link.selected()
    with _lock:
        owed = _owed.get(run_id)
        if owed is None or (owed[0] is not link and owed[0].superseded(owed[1]) is None):
            owed = _owed[run_id] = (link, link.item("dsp_knob", link.accept("dsp")))
        return owed


def recover(endpoint_id: int, link, *, gave: Optional[Callable[[str], bool]] = None,
            quiet: bool = False, again: Callable[[], None] = lambda: None) -> bool:
    """A run cut short — the process died in the middle of it, or HQPlayer
    went away or stayed silent through the restore — left HQPlayer stopped
    on its test settings, maybe lowered and holding the signals. The next
    time HQPlayer answers (the attach, or the poller's next answer), before
    anything reads its playlist: put the owner's settings back on its link —
    only while HQPlayer still shows a mark of the run (its signals in the
    playlist, the volume it lowered to, the selection it set last — in the
    mode it measured too, read there only at rest) — whole
    when HQPlayer went away under the run (lost_at) and shows what a box
    that just booted shows (_booted; `gave` tells an entry this node gave
    it) — and never past a change the owner made since: the put-back is a
    DSP item (_owe), a newer DSP change or a switch away supersedes it —
    written in the run's row where it happens (left_by_person) — and so does
    a knob they set in HQPlayer's own window (_owners); the run is closed as
    left to the owner. `quiet`: at a new run's start — closed with no
    notice, the new run reads what HQPlayer holds as the owner's from then
    (_take_owed) — a measured mode left unchecked stays marked, for the new
    run to judge at the rest its own Stop makes, and is said only once no
    run will judge it: start() refuses after all, the put-back outlived its
    wait (a build), or the run is over before it got there (_run). One
    command sequence: a setting HQPlayer builds holds the link as any other,
    and the caller — the attach, the poller — goes on once the build begins,
    the rest put back behind it (BuildWatch.waking). Not put back — HQPlayer
    building, silent, gone — `again` asks for it at HQPlayer's next
    answer. Returns whether a quiet put-back left a mark it did not say."""
    if running():
        return False
    run = db_query_one("""
        SELECT * FROM hqp_benchmark_runs
         WHERE hqp_endpoint_id = %(e)s AND finished_at IS NULL
         ORDER BY started_at DESC LIMIT 1
    """, {"e": endpoint_id})
    if run is None:
        return False
    owed = _owe(run["id"], link)
    ready = threading.Event()
    unsaid = False

    def put_back() -> None:
        nonlocal unsaid
        try:
            with link.build.waking(ready):
                unsaid = _put_run_back(link, run, owed, again, gave, quiet, ready)
        except Exception:       # the thread's boundary: recorded, never lost
            logger.exception("HQPlayer benchmark run %s: putting it back failed", run["id"])
        finally:
            ready.set()
    threading.Thread(target=put_back, daemon=True, name="hqp-benchmark-recover").start()
    ready.wait()
    # Read once ready is set by the put-back's end; one a build woke early
    # says its mark itself (_put_run_back)
    return unsaid


def _put_run_back(link, run: dict, owed: tuple, again: Callable[[], None],
                  gave: Optional[Callable[[str], bool]], quiet: bool,
                  ready: threading.Event) -> bool:
    """recover()'s put-back on the command lock. Returns whether it closed
    the run quietly with a mark it did not say."""
    from playback.hqp_link import Dropped
    owed_on, item = owed
    why, done = owed_on.superseded(item), None
    if why is None:
        try:
            with link.command(item=item):
                # Closed while the command lock is the put-back's: a person's
                # DSP change waiting for it finds the run closed, and a switch
                # away meanwhile leaves the run to this end (left_by_person) —
                # never closed as left over a put-back that went
                with _lock:
                    _putting.add(run["id"])
                try:
                    done = _recover(_OnLink(link), run, gave)
                    if done is not None:
                        if quiet:
                            # A mark the new run judges at once wakes nobody
                            # — start() says it if it refuses after all, the
                            # run's end if it never got there — and a put-back
                            # that outlived start()'s wait (a build) says it
                            # itself: no run follows it
                            done = {k: v for k, v in done.items()
                                    if k in ("note", "unchecked", "refused")}
                            done["notify"] = ready.is_set()
                        _close_owed(run, **done)
                finally:
                    with _lock:
                        _putting.discard(run["id"])
                        _bump()     # a State read before this end is stale (note_point)
        except Dropped as e:
            why = link.superseded(item)
            if why is None:
                # Lent, let go or closed: the next attach tries again
                logger.info("HQPlayer benchmark run %s not put back yet: %s", run["id"], e)
                return False
        except (_Reopened, _Lost, BrokenPipeError, ConnectionError, OSError) as e:
            # A switch away, or another HQPlayer chosen, that woke the request
            # on its way left the run to its end (left_by_person): this one
            why = link.superseded(item)
            if why is None:
                logger.info("HQPlayer benchmark run %s not put back yet: %s", run["id"], e)
                again()
                return False
        else:
            if done is None:
                why = link.superseded(item)
                if why is None:
                    again()
                    return False
    if why is not None:
        _close_owed(run, f"cut short; not put back — {why}: HQPlayer was left as it was set "
                         "since", left=not quiet, theirs=True)
        return False
    return bool(quiet and done.get("unchecked") and not done["notify"])


def _close_owed(run: dict, note: str, left: bool = False, restored: bool = False,
                volume_left: Optional[float] = None, unchecked: bool = False,
                refused: bool = False, seen: Optional[List[dict]] = None,
                notify: bool = True, theirs: bool = False) -> None:
    """A run cut short is over: put back, or — `left` — its put-back
    superseded by a person's DSP change or switch away, or by a knob they
    changed in HQPlayer's own window. HQPlayer is theirs then: nothing more
    is put back — its volume raised for them only from the run's own level,
    when a knob of theirs left the run (_recover) — and what still holds
    the run's test values is said in one notice until they changed it all
    (note_left), or a new run read HQPlayer. `restored`: put back after
    HQPlayer went away under the run — one notice says so, and the level its
    volume was left at (`volume_left`) when the put-back did not raise it.
    `unchecked`: the mode it measured not read yet, or the owner's selection
    there not taken back — the row marked; `refused`: HQPlayer was at rest
    and did not let it be read, so only a new run's start tries it again
    (_measured_refused).
    `notify`: False where a run judges the mark at once — its own first step
    (_take_owed), or start()'s put-back the run follows: start() says it
    when it refuses instead, the run's end when it never got there.
    `theirs`: a person's change superseded the put-back — a return point its
    round trip left (_keep_point) goes too, HQPlayer theirs. Once only: a
    run another path closed meanwhile stays as that path closed it."""
    row = db_execute("""UPDATE hqp_benchmark_runs SET finished_at = now(), outcome = 'interrupted',
                               note = concat_ws('; ', note, %(n)s::text),
                               left_at = CASE WHEN %(left)s THEN now() END,
                               restored_at = CASE WHEN %(restored)s THEN now() END,
                               volume_left = %(vl)s,
                               measured_unchecked_at = CASE WHEN %(unchecked)s THEN now() END,
                               measured_refused_at = CASE WHEN %(refused)s THEN now() END,
                               measured_from_mode = CASE WHEN %(theirs)s THEN NULL
                                                         ELSE measured_from_mode END,
                               measured_from_mode_name = CASE WHEN %(theirs)s THEN NULL
                                                              ELSE measured_from_mode_name END,
                               measured_from_rate = CASE WHEN %(theirs)s THEN NULL
                                                         ELSE measured_from_rate END,
                               measured_from_filter = CASE WHEN %(theirs)s THEN NULL
                                                           ELSE measured_from_filter END,
                               measured_from_filter1x = CASE WHEN %(theirs)s THEN NULL
                                                             ELSE measured_from_filter1x END,
                               measured_from_shaper = CASE WHEN %(theirs)s THEN NULL
                                                           ELSE measured_from_shaper END,
                               measured_from_at = CASE WHEN %(theirs)s THEN NULL
                                                       ELSE measured_from_at END
                         WHERE id = %(id)s AND finished_at IS NULL
                        RETURNING id""", {"n": note, "left": left, "restored": restored,
                                          "vl": volume_left, "unchecked": unchecked,
                                          "refused": unchecked and refused, "theirs": theirs,
                                          "id": run["id"]})
    with _lock:
        _owed.pop(run["id"], None)
        _left.pop(run["hqp_endpoint_id"], None)         # read again at the next answer
        if left and seen is not None and row is not None:
            # What the mode the run measured was read to hold, the moment
            # it was: the notice names it as read, never as the run set it
            _left[run["hqp_endpoint_id"]] = {"run": run, "items": None, "lists": {},
                                             "seen": seen}
        _unchecked.pop(run["hqp_endpoint_id"], None)
        _point_rows.pop(run["hqp_endpoint_id"], None)
        if unchecked:
            _busy_at.pop(run["hqp_endpoint_id"], None)  # a new mark: its own nearest rest
        _bump()
    if row is not None:
        logger.info("HQPlayer benchmark run %s %s: %s", run["id"],
                    "left to the owner" if left else "recovered", note)
        if (left or restored or unchecked) and notify:
            _notify()


def left_by_person(host: str, port: int, why: str) -> None:
    """A person's switch away from the HQPlayer at this address, or their
    DSP change on it, supersedes the put-back a run cut short there still
    owes (_owe): the run is closed as left to them where it happens — in its
    row, so neither a later answer of HQPlayer nor a restart puts it back
    over what they set. A run going in this process is no run cut short.
    The database failing costs that record, never the person's change: the
    link's own supersession still holds in this process."""
    if running():
        return
    import hqp_library
    try:
        ep = hqp_library.endpoint_by_address(host, port)
        runs = [] if ep is None else db_query(
            """SELECT * FROM hqp_benchmark_runs
                WHERE hqp_endpoint_id = %(e)s AND finished_at IS NULL""", {"e": ep["id"]})
        for run in runs:
            with _lock:
                if run["id"] in _putting:
                    continue        # its put-back's own end decides (_put_run_back)
            _close_owed(run, f"cut short; not put back — {why}: HQPlayer was left as it was "
                             "set since", left=True, theirs=True)
        # A return point a check left (_keep_point): HQPlayer is theirs now
        for point in [] if ep is None else db_query(
                """SELECT * FROM hqp_benchmark_runs
                    WHERE hqp_endpoint_id = %(e)s AND measured_from_mode IS NOT NULL""",
                {"e": ep["id"]}):
            _point_cleared(point, f"the way back to {point['measured_from_mode_name']} left "
                                  f"to the owner — {why}")
    except psycopg2.Error as e:
        logger.warning("HQPlayer benchmark: a run cut short not recorded as left to the owner "
                       "(%s): %s", why, e)


def _take_owed(link: _Link, endpoint_id: int, gave: Callable[[str], bool],
               kind: Optional[str]) -> None:
    """A new run's first step: a run cut short before it still owes HQPlayer
    its put-back — it goes first, on this run's connection, so the new run
    reads what HQPlayer holds after it, never the old run's test settings;
    one a person superseded is left as they set it. Closed with no notice —
    the new run reads what HQPlayer holds as the owner's from now — and a
    notice of a run left before ends once the new run opened its row
    (_measure), never before it read anything. The mode a run before
    measured, its selection left unchecked, is judged here too, before the
    new run reads the owner's selection — a test point is never taken for
    theirs — on HQPlayer at the rest the run's own Stop makes: stopped, on
    whatever entry — the start is the owner's own act, and the run takes
    HQPlayer over; the hold leaves it as it played, and a mode switched
    under a track restarts the output — and HQPlayer goes back first to a
    mode a check left it in (_go_back): the new run never reads that mode
    as the owner's, and is refused when HQPlayer does not go back. Not
    judged, the mode stays marked: the new run, of `kind` (None: the mode
    HQPlayer is in), goes only if it reads nothing of it — a run of another
    mode, HQPlayer in another. What it changes of the notices — a mark it
    closed quietly, a return point gone, or one its check keeps — is said
    whatever ends it, a run that cannot go on included."""
    unsaid = False      # what the notices show changed here, and nothing said it yet
    try:
        run = db_query_one("""
            SELECT * FROM hqp_benchmark_runs
             WHERE hqp_endpoint_id = %(e)s AND finished_at IS NULL
             ORDER BY started_at DESC LIMIT 1
        """, {"e": endpoint_id})
        if run is not None:
            with _lock:
                owed = _owed.get(run["id"])
            why = owed[0].superseded(owed[1]) if owed is not None else None
            if why is not None:
                _close_owed(run, f"cut short; not put back — {why}: HQPlayer was left as it "
                                 "was set since", theirs=True)
            else:
                done = _again(lambda: _recover(link, run, gave))
                if done is None:
                    raise _Abort("HQPlayer did not report its state")
                # No notice: the new run reads what HQPlayer holds as the
                # owner's now; the mode it measured, if marked, is judged below
                _close_owed(run, done["note"], unchecked=done.get("unchecked", False),
                            refused=done.get("refused", False), notify=False)
                unsaid = done.get("unchecked", False)
        run = db_query_one("""
            SELECT * FROM hqp_benchmark_runs
             WHERE hqp_endpoint_id = %(e)s AND measured_unchecked_at IS NOT NULL
             ORDER BY started_at DESC LIMIT 1
        """, {"e": endpoint_id})
        point = _return_point(endpoint_id)
        if run is None and point is None:
            return
        st, status = _again(lambda: (link("stop"), link("get_state"), link("get_status"))[1:])
        stuck = point is not None       # in a mode a check left it in, until it goes back
        if st is None or status is None:
            why = "HQPlayer did not report its state"
        elif status.state != PlaybackState.STOPPED:
            why = "HQPlayer is not at rest"
        else:
            why = _again(lambda: _go_back(link, point)) if point is not None else None
            if why is None:
                stuck, unsaid = False, unsaid or point is not None
                if run is None:
                    return
                st = _again(lambda: link("get_state"))
                # The check may keep a return point before it leaves the mode
                # HQPlayer is in (_keep_point): cut after that, it is said too
                unsaid = True
                checked, why = (_again(lambda: _check_measured(link, run, st))
                                if st is not None else (None, "HQPlayer did not report its state"))
                if checked is not None:
                    _measured_checked(run, checked, notice=False)    # says what stands
                    unsaid = False
                    return
                stuck = _return_point(endpoint_id) is not None
            if run is not None:
                _measured_refused(run, why)
        _notify()                   # the mark, or the way back, stands: said
        unsaid = False
    finally:
        if unsaid:
            _notify()       # changed here — the run over before it said so
    if stuck:
        raise _Abort(f"{why}: HQPlayer is in the mode the last check left it in")
    if run is not None and (st is None or st["mode"] == _measured(run)
                            or mode_kind(run["mode"]) == kind):
        raise _Abort(f"{why}: the mode the last run measured is not checked yet")
    logger.info("HQPlayer benchmark run %s: the mode it measured not checked yet (%s) — a run "
                "of another mode goes", run["id"], why)


def unchecked_run(endpoint_id: int) -> Optional[dict]:
    """The run whose put-back left the measured mode of this HQPlayer
    unchecked — HQPlayer busy, or not taking the owner's selection there
    back — the mark HQPlayer's nearest rest tries (its row; None: none; one
    HQPlayer refused at rest waits for a new run's start, or the owner's word
    that leaves it as it is), read from the database once, then known
    (_close_owed, _close_run, _measured_checked, _measured_refused,
    leave_as_it_is keep it). A read the database fails, or a close or a
    check overtook, is asked again at the poller's next answer: it costs
    the check, never the poll."""
    with _lock:
        if endpoint_id in _unchecked:
            return _unchecked[endpoint_id]
        gen = _left_gen
    try:
        run = db_query_one("""
            SELECT * FROM hqp_benchmark_runs
             WHERE hqp_endpoint_id = %(e)s AND measured_unchecked_at IS NOT NULL
               AND measured_refused_at IS NULL
             ORDER BY started_at DESC LIMIT 1
        """, {"e": endpoint_id})
    except psycopg2.Error as e:
        logger.warning("HQPlayer benchmark: a run left unchecked not read: %s", e)
        return None
    with _lock:
        if _left_gen != gen:
            return None
        return _unchecked.setdefault(endpoint_id, run)


def check_measured(endpoint_id: int, link, status: TrackStatus, *,
                   gave: Callable[[str], bool]) -> None:
    """HQPlayer stopped, the mode a run measured unchecked (unchecked_run):
    judged now on its link (_check_measured) if HQPlayer rests there —
    stopped, reading no other controller's entry (_booted) — and the left
    notice says what is the owner's. One try per rest: busy, the verdict not
    written, or the connection lost under it, the entry it rests on is not
    tried again until it moves; at rest and not let through — a return to
    where a check left it refused first (_go_back), a SetMode refused or not
    shown, no State there, a selection or the way back refused, the lists of
    the mode it was found in not given — no later rest tries it, a new run's
    start does (_measured_refused). One check at a time per HQPlayer, behind
    the command lock; a person's DSP change or switch away made meanwhile
    drops it — HQPlayer's next answer asks again — and so does a run's start,
    which judges the mark first (_take_owed)."""
    run = unchecked_run(endpoint_id)
    if run is None or running():
        return
    at = (status.track_index, status.uri)
    with _lock:
        if endpoint_id in _checking or _busy_at.get(endpoint_id) == at:
            return
        _checking.add(endpoint_id)
    from playback.hqp_link import Dropped
    item = link.item("dsp_knob", link.newest("dsp", "switch"))

    def check() -> None:
        spent = True                # this rest's try, unless a newer word cut it
        back = False                # past the way back, the mark not judged
        # The rest the try is spent at: the one the check reads HQPlayer in
        # itself — the poller's read came first, and HQPlayer may have moved
        # since (one rest tried twice, 2026-10-21) — else the poller's
        rest = at
        try:
            with link.command(item=item):
                on = _OnLink(link)
                st, listed, now = on("get_state"), on("get_playlist"), on("get_status")
                if now is not None and now.state == PlaybackState.STOPPED:
                    rest = (now.track_index, now.uri)
                if st is None or now is None or not _booted(now, listed, gave, run):
                    return
                point = _return_point(endpoint_id)
                checked, why = None, _go_back(on, point) if point is not None else None
                # From here what the notices show may change: the point gone
                # with the way back, or one the check keeps (_keep_point)
                back = why is None
                if why is None:
                    st = on("get_state") if point is not None else st
                    checked, why = (_check_measured(on, run, st) if st is not None
                                    else (None, "HQPlayer showed no state"))
            if checked is None:
                _measured_refused(run, why)
            else:
                _measured_checked(run, checked, notice=True)
            spent = back = False    # the mark itself says what follows now
        except Dropped as e:
            spent = False           # a newer word: HQPlayer's next answer asks again
            logger.info("HQPlayer benchmark run %s: the mode it measured not checked yet: %s",
                        run["id"], e)
        except (_Reopened, _Silent, BrokenPipeError, ConnectionError, OSError) as e:
            logger.info("HQPlayer benchmark run %s: the mode it measured not checked yet: %s",
                        run["id"], e)
        except Exception:       # the thread's boundary: recorded, never lost
            logger.exception("HQPlayer benchmark run %s: checking the mode it measured failed",
                             run["id"])
        finally:
            with _lock:
                _checking.discard(endpoint_id)
                if spent:           # busy, the verdict not written, the connection lost
                    _busy_at[endpoint_id] = rest
                # Cut by a run's hold: the run judges the mark at its start,
                # and its end says what stands — read under the lock that end
                # takes, after its hold
                to_run = back and link.lent
                if to_run:
                    _left_to_run.add(endpoint_id)
                _bump()             # a State read before this end is stale (note_point)
            if back and not to_run:
                _notify()           # the point gone, or kept anew; the mark standing
    threading.Thread(target=check, daemon=True, name="hqp-benchmark-check").start()


def _measured_checked(run: dict, checked: Tuple[List[str], dict], notice: bool) -> None:
    """The mode a run measured, left unchecked at its put-back, judged: the
    mark goes. Knobs the owner set there leave the run to them — said in the
    left notice (`notice`), what the mode holds named as read there; at a
    new run's start quietly, the new run reading HQPlayer as theirs."""
    theirs, there = checked
    left = bool(theirs) and notice
    note = (f"{', '.join(theirs)} of {run['mode']} changed in HQPlayer since: left as it was set"
            if theirs else f"the selection of {run['mode']} was put back")
    row = db_execute("""UPDATE hqp_benchmark_runs SET measured_unchecked_at = NULL,
                               measured_refused_at = NULL,
                               left_at = CASE WHEN %(left)s THEN now() ELSE left_at END,
                               note = concat_ws('; ', note, %(n)s::text)
                         WHERE id = %(id)s AND measured_unchecked_at IS NOT NULL
                        RETURNING id""", {"left": left, "n": note, "id": run["id"]})
    with _lock:
        _unchecked.pop(run["hqp_endpoint_id"], None)
        _busy_at.pop(run["hqp_endpoint_id"], None)
        if left and row is not None:
            _left[run["hqp_endpoint_id"]] = {"run": run, "items": None, "lists": {},
                                             "seen": _still_left(run, there)}
        _bump()
    if row is not None:
        logger.info("HQPlayer benchmark run %s: %s", run["id"], note)
        _notify()


def _measured(run: dict) -> int:
    """The mode a run measured: the one it switched into, else the one it found."""
    return run["own_mode"] if run["own_mode"] is not None else run["pre_mode"]


# A knob of State: what it is named, its State key, the run row's column
_KNOBS = (("rate", "rate", "rate"), ("filter", "filterNx", "filter"),
          ("filter1x", "filter1x", "filter1x"), ("shaper", "shaper", "shaper"))


def _still_left(run: dict, st: dict, seen: Optional[List[dict]] = None) -> List[dict]:
    """What of a run's test settings HQPlayer still holds, by State indices,
    each with what it was before the run: the volume it lowered to; in the
    measured mode, the switch into it and the selection it set last. In
    another mode State shows that mode's selection, and HQPlayer keeps one
    per mode: what the measured mode holds is named as the knobs the run
    left there (`what` "elsewhere", its mode by name) — as it was last read
    there (`seen`, those items), else as the run set them."""
    items: List[dict] = []
    if run["mute_volume"] is not None and abs(st["volume"] - run["mute_volume"]) < _DB_EPS:
        items.append({"what": "volume", "now": st["volume"], "before": run["pre_volume"]})
    switched = run["own_mode"] is not None
    if st["mode"] != _measured(run):
        before = "own" if switched else "pre"
        knobs = ([it["what"] for it in seen if it["what"] not in ("volume", "mode")]
                 if seen is not None else
                 [what for what, _, col in _KNOBS if run[f"last_{col}"] is not None
                  and run[f"last_{col}"] != run[f"{before}_{col}"]])
        if knobs:
            items.append({"what": "elsewhere", "mode": run["mode"], "knobs": knobs})
        return items
    if switched:
        items.append({"what": "mode", "now": st["mode"], "before": run["pre_mode"]})
    before = "own" if switched else "pre"
    for what, key, col in _KNOBS:
        test, was = run[f"last_{col}"], run[f"{before}_{col}"]
        if test is not None and st[key] == test and test != was:
            items.append({"what": what, "now": test, "before": was})
    return items


def _owners(run: dict, st: dict) -> List[str]:
    """The knobs HQPlayer holds that are neither the run's — its last point,
    the volume it lowered to — nor what was there before it: each one the
    owner set in HQPlayer's own window, whatever the others still hold.
    Judged on State as HQPlayer answers it: a Desktop says nothing while it
    builds, an Embedded box answers with the selection it builds — a knob's
    value either way, never one between. A run HQPlayer went away under is
    judged here only once HQPlayer shows more than what it booted with —
    no one's change (_recover, _booted)."""
    changed: List[str] = []
    measured, switched = _measured(run), run["own_mode"] is not None
    if st["mode"] not in (measured, run["pre_mode"]):
        changed.append("mode")
    elif st["mode"] == measured:
        before = "own" if switched else "pre"
        changed += [what for what, key, col in _KNOBS
                    if st[key] not in (run[f"last_{col}"], run[f"{before}_{col}"])]
    else:
        # Back in the mode it found: the run set nothing there
        changed += [what for what, key, col in _KNOBS if st[key] != run[f"pre_{col}"]]
    if (run["mute_volume"] is not None and abs(st["volume"] - run["mute_volume"]) >= _DB_EPS
            and abs(st["volume"] - run["pre_volume"]) >= _DB_EPS):
        changed.append("volume")
    return changed


def _named(items: List[dict], lists: Dict[str, Optional[list]]) -> List[dict]:
    """`items` with what they name: a mode, filter or modulator by name, a
    rate in Hz — from HQPlayer's lists of the mode they belong to; None for
    one a list does not hold (or not read yet)."""
    by = {"mode": {m["index"]: m["name"] for m in lists.get("modes") or []},
          "rate": {r["index"]: r["rate"] for r in lists.get("rates") or []},
          "filter": {f["index"]: f["name"] for f in lists.get("filters") or []},
          "shaper": {s["index"]: s["name"] for s in lists.get("shapers") or []}}
    by["filter1x"] = by["filter"]
    return [it if it["what"] in ("volume", "elsewhere") else
            {**it, "now": by[it["what"]].get(it["now"]),
             "before": by[it["what"]].get(it["before"])}
            for it in items]


def _left_run(endpoint_id: int) -> Optional[dict]:
    return db_query_one("""
        SELECT * FROM hqp_benchmark_runs
         WHERE hqp_endpoint_id = %(e)s AND left_at IS NOT NULL
         ORDER BY started_at DESC LIMIT 1
    """, {"e": endpoint_id})


def left_pending(endpoint_id: int) -> bool:
    """Is a run left to the owner on this HQPlayer, its notice standing —
    read from the database once, then known (_close_owed, note_left,
    clear_left keep it). A read the database fails, or a close or a clear
    overtook, is asked again at the poller's next answer: it costs the
    notice, never the poll."""
    with _lock:
        if endpoint_id in _left:
            return _left[endpoint_id] is not None
        gen = _left_gen
    try:
        run = _left_run(endpoint_id)
    except psycopg2.Error as e:
        logger.warning("HQPlayer benchmark: a run left to the owner not read: %s", e)
        return False
    with _lock:
        if _left_gen != gen:
            return False
        _left.setdefault(endpoint_id, None if run is None else
                         {"run": run, "items": None, "lists": {}, "seen": None})
        return _left[endpoint_id] is not None


# A mode's lists the notice names knobs from
_LISTS = ("modes", "rates", "filters", "shapers")


def wants_lists(endpoint_id: int, mode: int) -> bool:
    """Does the notice of a run left on this HQPlayer need its lists of
    `mode` — the mode HQPlayer is in — to name what it says? Only those of
    the mode the run measured, at an answer in that mode, until HQPlayer
    gave each one: what the run left in it is named from them, never from
    another mode's."""
    with _lock:
        left = _left.get(endpoint_id)
        return (left is not None and mode == _measured(left["run"])
                and any(k not in left["lists"].get(mode, {}) for k in _LISTS))


def note_left(endpoint_id: int, st: dict, lists: Optional[Dict[str, Optional[list]]]) -> None:
    """What HQPlayer holds now (State, read by the poller while a run left
    to the owner stands; `lists`, read with it, of the mode it is in — each
    one HQPlayer gave) against the run's test settings: once the person
    changed every one, the notice ends for good; else it says what is left,
    and the channel is woken when that changed. A write the database
    refuses — the end, the wake — costs the notice: it is written again at a
    later answer, never the poll."""
    with _lock:
        left = _left.get(endpoint_id)
        if left is None:
            return
        if lists is not None:
            left["lists"].setdefault(st["mode"], {}).update(
                {k: v for k, v in lists.items() if v is not None})
        run = left["run"]
        here = st["mode"] == _measured(run)
        raw = _still_left(run, st, left["seen"])
        items = _named(raw, left["lists"].get(_measured(run)) or {})
        changed = items != left["items"]
    if not items:
        try:
            db_execute("UPDATE hqp_benchmark_runs SET left_at = NULL WHERE id = %(id)s",
                       {"id": run["id"]})
        except psycopg2.Error as e:
            logger.warning("HQPlayer benchmark run %s: its notice not ended yet: %s",
                           run["id"], e)
            return
        logger.info("HQPlayer benchmark run %s: every setting it left was changed", run["id"])
    if changed:
        try:
            _notify()
        except psycopg2.Error as e:
            logger.warning("HQPlayer benchmark run %s: its notice not sent yet: %s", run["id"], e)
            return                  # kept as it was: the next answer sends it again
    with _lock:
        if _left.get(endpoint_id) is left:
            if here:
                left["seen"] = raw
            if items:
                left["items"] = items
            else:
                _left[endpoint_id] = None
                _bump()


def clear_left(endpoint_id: int) -> None:
    """A new run opened its row, reading what HQPlayer holds as the owner's:
    a notice of a run left to the owner there ends, and so does one of a
    run put back after HQPlayer went away under it."""
    row = db_execute("""UPDATE hqp_benchmark_runs SET left_at = NULL, restored_at = NULL
                         WHERE hqp_endpoint_id = %(e)s
                           AND (left_at IS NOT NULL OR restored_at IS NOT NULL)
                        RETURNING id""", {"e": endpoint_id})
    with _lock:
        _left[endpoint_id] = None
        _bump()
    if row is not None:
        _notify()


def left_notice() -> Optional[dict]:
    """The notice of a run left to the owner — the newest, on any HQPlayer,
    whatever the output is now. Its run row is its source and its onset
    (left_at): a restart, or another output picked, never ends it to raise
    it again. `items`: what still holds the run's test values (what, now →
    before), as the poller last read it of that HQPlayer — None until it
    read it."""
    run = db_query_one("""SELECT id, hqp_endpoint_id, left_at FROM hqp_benchmark_runs
                           WHERE left_at IS NOT NULL ORDER BY left_at DESC LIMIT 1""")
    if run is None:
        return None
    with _lock:
        left = _left.get(run["hqp_endpoint_id"])
        items = (list(left["items"]) if left is not None and left["run"]["id"] == run["id"]
                 and left["items"] is not None else None)
    return {"since": run["left_at"], "items": items}


def restored_notice() -> Optional[dict]:
    """The notice of a run put back after HQPlayer went away under it — the
    newest, on any HQPlayer: a report, not a condition. It stands until a
    new run opened its row there (clear_left), or the owner saw it (the
    notices channel drops it then). Its onset is `restored_at`; `run_at`
    dates the run, so a put-back days later does not read as a change made
    on its own; `volume_left`, with the owner's level, when the put-back
    left HQPlayer's volume as it came back."""
    run = db_query_one("""SELECT restored_at, started_at, volume_left, pre_volume
                            FROM hqp_benchmark_runs
                           WHERE restored_at IS NOT NULL ORDER BY restored_at DESC LIMIT 1""")
    if run is None:
        return None
    return {"since": run["restored_at"], "run_at": run["started_at"],
            "volume_left": run["volume_left"], "volume_before": run["pre_volume"]}


def unchecked_notice() -> Optional[dict]:
    """The notice of a run whose put-back left the mode it measured
    unchecked — HQPlayer did not let it be read, or did not take the
    owner's selection there back — HQPlayer back where the check found it:
    the newest, on any HQPlayer, until that mode is judged at HQPlayer's
    nearest rest or a new run's start, or the owner leaves it as it is.
    `mode` names the mode measured, `run` the run, and `refused` whether
    HQPlayer at rest did not let it be read — the owner may then leave it as
    it is (leave_as_it_is). Its onset is `measured_unchecked_at`, or the
    refusal's: news, the toast that offers the owner's word."""
    run = db_query_one("""SELECT id, COALESCE(measured_refused_at, measured_unchecked_at) AS since,
                                 measured_refused_at, mode
                            FROM hqp_benchmark_runs
                           WHERE finished_at IS NOT NULL AND measured_unchecked_at IS NOT NULL
                             AND measured_from_mode IS NULL
                           ORDER BY measured_unchecked_at DESC LIMIT 1""")
    if run is None:
        return None
    return {"since": run["since"], "mode": run["mode"], "run": run["id"],
            "refused": run["measured_refused_at"] is not None}


def stayed_notice() -> Optional[dict]:
    """The notice of a run whose check left HQPlayer in the mode it measured
    (a return point, _keep_point), the run closed: the newest, on any
    HQPlayer, until HQPlayer is back where the check found it or the owner
    leaves it as it is. Its onset is the point's own (`measured_from_at`): a
    new condition, said anew even over a "not checked yet" already seen —
    and so is HQPlayer at rest refusing it after (`measured_refused_at`),
    the toast that offers the owner's word. `back_to`: the mode found and its
    selection, by name (the rate in Hz); `unchecked` and `refused` as the
    mark stands, `run` the run. A run still owed is in its put-back's hands:
    none."""
    run = db_query_one("""SELECT id, GREATEST(measured_from_at, measured_refused_at) AS since,
                                 measured_unchecked_at, measured_refused_at,
                                 mode, measured_from_mode_name, measured_from_rate,
                                 measured_from_filter, measured_from_filter1x,
                                 measured_from_shaper
                            FROM hqp_benchmark_runs
                           WHERE finished_at IS NOT NULL AND measured_from_mode IS NOT NULL
                           ORDER BY measured_from_at DESC LIMIT 1""")
    if run is None:
        return None
    return {"since": run["since"], "mode": run["mode"], "run": run["id"],
            "unchecked": run["measured_unchecked_at"] is not None,
            "refused": run["measured_refused_at"] is not None,
            "back_to": {"mode": run["measured_from_mode_name"], "rate": run["measured_from_rate"],
                        "filter": run["measured_from_filter"],
                        "filter1x": run["measured_from_filter1x"],
                        "shaper": run["measured_from_shaper"]}}


def left_on(endpoint_id: Optional[int], st: Optional[dict],
            lists: Dict[str, Optional[list]]) -> Optional[List[dict]]:
    """For the HQPlayer screen, from what it read itself: what a run left to
    the owner still holds of its test settings, named — None when no run is
    left, or HQPlayer did not report its state."""
    if endpoint_id is None or not st or not left_pending(endpoint_id):
        return None
    with _lock:
        left = _left.get(endpoint_id)
        run, seen = (left["run"], left["seen"]) if left is not None else (None, None)
    return _named(_still_left(run, st, seen), lists) if run is not None else None


def _notify() -> None:
    db_execute("NOTIFY sautium_notices")


def _booted(status: TrackStatus, listed: list, gave: Optional[Callable[[str], bool]],
            run: dict) -> bool:
    """Does HQPlayer show what a box that just booted shows — stopped,
    reading no entry of another controller's: none, a source of `run`
    (_run_source), or one this node gave it (`gave`; None: no entry but the
    run's own is the node's)? An entry the playlist as read does not show —
    not given, or moved since — is no one's it can tell: not."""
    if status.state != PlaybackState.STOPPED:
        return False
    i = status.track_index
    if i < 1:
        return True
    if i > len(listed):
        return False
    uri = listed[i - 1].get("uri", "")
    return _run_source(uri, run) or (gave is not None and gave(uri))


def _run_source(uri: str, run: dict) -> bool:
    """Is `uri` a source `run` handed HQPlayer — a signal, or the owner's DSD
    file it measured, known by the URI its row keeps (`dsd_uri`)? A row
    from before it was kept asks the owner's newest DSD listen, as it did."""
    if any(t in uri for t in _signal_tokens()):
        return True
    from playback.hqp_backend import _same_entry
    if run["dsd_uri"] is not None:
        return _same_entry(uri, run["dsd_uri"])
    f = _dsd_file()
    return f is not None and _same_entry(uri, _dsd_uri(f))


def _recover(link: _Link, run: dict,
             gave: Optional[Callable[[str], bool]] = None) -> Optional[dict]:
    """The put-back of a run cut short, once HQPlayer answers: how the run
    closes (_close_owed's words), None when HQPlayer did not report its
    state or its transport. A run HQPlayer went away under
    (`lost_at`) is put back whole while HQPlayer shows what a box that just
    booted shows (_booted) — what it booted with is never a person's change
    — its volume lowered to the owner's level when it came back louder,
    raised only from the level the run itself set, else left as it is, and
    said. Otherwise — and for a lost run HQPlayer plays, pauses or reads
    another controller's entry for — a knob the owner set in HQPlayer's own
    window since (_owners) leaves it all to them — nothing more is sent, the
    volume raised for them only while it still stands at the run's own
    level (_left_volume), the notice naming what still holds the run's test
    values — and only what still bears a mark of the run is put back. The
    mode the run switched into, which State does not show while HQPlayer is
    in another, is a mark of its own: read and judged there
    (_check_measured) only on an HQPlayer at rest (_booted) — a mode
    switched under a track restarts the output — else the run is marked
    unchecked, judged at HQPlayer's nearest rest or a new run's start
    (check_measured, _take_owed); so is any run whose owner's selection in
    the mode it measured — the one it found, for a run that did not
    switch — HQPlayer does not take back (_put_back), a row with no own
    selection only once it noted a point there: one cut after its switch,
    before it read that mode, set none. A return point its own round trip
    left, cut short before (_keep_point), is where HQPlayer goes back
    first, at rest (_go_back): the mode Sautium left it in is never judged
    as the owner's — not at rest, or not going back, the run is marked and
    nothing is judged; only a volume still at the run's level goes back."""
    st, listed, status = link("get_state"), link("get_playlist"), link("get_status")
    if st is None or status is None:
        return None
    if run["measured_from_mode"] is not None:
        at_rest = _booted(status, listed, gave, run)
        why = _go_back(link, run) if at_rest else "HQPlayer was busy"
        if why is not None:
            return {"note": "cut short; " + _unchecked_note(why) + _left_volume(link, run, st),
                    "unchecked": True, "refused": at_rest}
        st, listed, status = link("get_state"), link("get_playlist"), link("get_status")
        if st is None or status is None:
            return None
    whole = run["lost_at"] is not None and _booted(status, listed, gave, run)
    owners = [] if whole else _owners(run, st)
    if owners:
        return {"note": f"cut short; not put back — {', '.join(owners)} changed in HQPlayer "
                        f"since: HQPlayer was left as it was set{_left_volume(link, run, st)}",
                "left": True}
    tokens = _signal_tokens()
    ours = any(t in e.get("uri", "") for e in listed for t in tokens)
    lowered = (run["mute_volume"] is not None
               and abs(st["volume"] - run["mute_volume"]) < _DB_EPS)
    on_point = (run["last_rate"] is not None and st["mode"] == _measured(run)
                and (st["rate"], st["filterNx"], st["filter1x"], st["shaper"])
                == (run["last_rate"], run["last_filter"], run["last_filter1x"], run["last_shaper"]))
    hidden = run["own_mode"] is not None and not whole and st["mode"] != run["own_mode"]
    if not (whole or ours or lowered or on_point or hidden):
        return {"note": "cut short; HQPlayer had been changed since and was left as it is"}
    unchecked = refused = False
    if hidden:
        refused = _booted(status, listed, gave, run)
        checked, why = (_check_measured(link, run, st) if refused
                        else (None, "HQPlayer was busy"))
        if checked is not None and checked[0]:
            theirs, there = checked
            return {"note": f"cut short; not put back — {', '.join(theirs)} of {run['mode']} "
                            "changed in HQPlayer since: HQPlayer was left as it was set"
                            f"{_left_volume(link, run, st)}", "left": True,
                    "seen": _still_left(run, there)}
        unchecked = checked is None
        refused = refused and unchecked
        if not (ours or lowered or on_point):
            return {"note": "cut short; " + (_unchecked_note(why) if unchecked else
                                             f"the selection of {run['mode']} was put back"),
                    "unchecked": unchecked, "refused": refused}
    louder = st["volume"] > run["pre_volume"] + _DB_EPS
    volume = lowered or (whole and louder)
    left_at = (st["volume"] if whole and not volume
               and abs(st["volume"] - run["pre_volume"]) >= _DB_EPS else None)
    pre = {"mode": run["pre_mode"], "rate": run["pre_rate"], "filterNx": run["pre_filter"],
           "filter1x": run["pre_filter1x"], "shaper": run["pre_shaper"],
           "volume": run["pre_volume"]}
    own = None if run["own_mode"] is None or hidden else {
        "mode": run["own_mode"], "rate": run["own_rate"], "filterNx": run["own_filter"],
        "filter1x": run["own_filter1x"], "shaper": run["own_shaper"]}
    problems, unset = _put_back(link, pre=pre, own=own, volume=volume, clear=ours,
                                enter=whole)
    # No own selection on the row: a run of the mode HQPlayer was in — or one
    # cut after its switch before it read that mode, which set no point
    if (unset["own"] if run["own_mode"] is not None
            else unset["pre"] and run["last_rate"] is not None):
        unchecked, why = True, f"HQPlayer did not take the selection of {run['mode']} back"
    if whole:
        note = ("cut short; HQPlayer went away under the run — its settings were put back when "
                "it answered again")
        if left_at is not None:
            note += f" (its volume left as it is, {left_at:g} dB)"
    else:
        note = ("cut short; HQPlayer's settings" + (" and volume" if lowered else "")
                + " were put back when it answered again")
    if problems:
        note += " (not all: " + ", ".join(problems) + ")"
    if unchecked:
        note += "; " + _unchecked_note(why)
    return {"note": note, "restored": whole, "volume_left": left_at, "unchecked": unchecked,
            "refused": refused}


def _unchecked_note(why: str) -> str:
    """What a run's note says of a measured mode left unchecked, and why."""
    return f"the mode it measured not checked yet: {why}"


def _measured_refused(run: dict, why: str) -> None:
    """HQPlayer at rest did not let the mode a run measured be read (a
    SetMode refused or not shown, no State there, a selection or the way
    back refused, the lists of the mode it was found in not given): no later
    rest tries it — only a new run's start, unless the owner leaves it as it
    is; why, in the run's note."""
    row = db_execute("""UPDATE hqp_benchmark_runs SET measured_refused_at = now(),
                               note = concat_ws('; ', note, %(n)s::text)
                         WHERE id = %(id)s AND measured_unchecked_at IS NOT NULL
                           AND measured_refused_at IS NULL
                        RETURNING id""", {"n": _unchecked_note(f"{why} at rest"),
                                          "id": run["id"]})
    with _lock:
        _unchecked.pop(run["hqp_endpoint_id"], None)
        _busy_at.pop(run["hqp_endpoint_id"], None)
        _bump()
    if row is not None:
        logger.info("HQPlayer benchmark run %s: the mode it measured not let be read at rest "
                    "(%s): tried again before the next run", run["id"], why)
        _notify()


def leave_as_it_is(run_id: int) -> bool:
    """The owner's word on a mark HQPlayer at rest did not let be judged — a
    mode the output no longer offers among its causes, which no check would
    ever get past: the mode the run measured is left as it is. The mark, a
    return point still standing and their notices go, the run's note says
    whose word it was, and no rest or run's start tries that mode again: what
    HQPlayer holds there — a test point too — and the mode it stayed in are
    the owner's. True once the run holds no mark: left now, or judged or
    left before (a second press, another tab); False while it holds one
    HQPlayer at rest has not refused yet, its check still to come. Refused
    while a run measures; RunNotFound for a run there is none of."""
    note = "the mode it measured left as it is at the owner's word"
    # Under the lock a run's start takes: a run that starts judges the mark
    # itself, and never meets it half left
    with _lock:
        if _state["running"]:
            raise BenchmarkRefused(LABEL)
        row = db_execute("""UPDATE hqp_benchmark_runs SET measured_unchecked_at = NULL,
                                   measured_refused_at = NULL, measured_from_mode = NULL,
                                   measured_from_mode_name = NULL, measured_from_rate = NULL,
                                   measured_from_filter = NULL, measured_from_filter1x = NULL,
                                   measured_from_shaper = NULL, measured_from_at = NULL,
                                   note = concat_ws('; ', note, %(n)s::text)
                             WHERE id = %(id)s AND measured_refused_at IS NOT NULL
                            RETURNING hqp_endpoint_id""", {"n": note, "id": run_id})
        if row is None:
            run = db_query_one("""SELECT measured_unchecked_at IS NOT NULL AS marked
                                    FROM hqp_benchmark_runs WHERE id = %(id)s""", {"id": run_id})
            if run is None:
                raise RunNotFound(f"no benchmark run {run_id}")
            return not run["marked"]
        _unchecked.pop(row["hqp_endpoint_id"], None)
        _busy_at.pop(row["hqp_endpoint_id"], None)
        _point_rows.pop(row["hqp_endpoint_id"], None)
        _bump()
    logger.info("HQPlayer benchmark run %s: %s", run_id, note)
    _notify()
    return True


def _left_volume(link, run: dict, st: dict) -> str:
    """A run left to the owner: a volume HQPlayer still holds at the level the
    run lowered it to is the run's, not theirs — it goes back. What the run's
    note says of it."""
    if run["mute_volume"] is None or abs(st["volume"] - run["mute_volume"]) >= _DB_EPS:
        return ""
    link("set_volume", run["pre_volume"])
    return "; its volume, still at the run's level, went back"


def _return_point(endpoint_id: int) -> Optional[dict]:
    """The run row of this HQPlayer holding a return point (_keep_point),
    the newest; None: none."""
    return db_query_one("""
        SELECT * FROM hqp_benchmark_runs
         WHERE hqp_endpoint_id = %(e)s AND measured_from_mode IS NOT NULL
         ORDER BY started_at DESC LIMIT 1
    """, {"e": endpoint_id})


def _keep_point(link, run: dict, found: dict) -> Optional[str]:
    """Before a check of the mode `run` measured leaves the mode HQPlayer is
    in (`found`): that mode and its selection, named from its own lists —
    names hold across a restart, an index is read again there — kept on the
    run's row as the return point, until HQPlayer is back there or the owner
    leaves it as it is. The mode's
    name; None when HQPlayer did not give a list to name them by: no round
    trip without a way back."""
    modes, filters = link("get_modes"), link("get_filters")
    shapers, rates = link("get_shapers"), link("get_rates")
    if any(x is None for x in (modes, filters, shapers, rates)):
        return None
    names = {"mode": {m["index"]: m["name"] for m in modes},
             "filter": {f["index"]: f["name"] for f in filters},
             "shaper": {x["index"]: x["name"] for x in shapers},
             "rate": {r["index"]: r["rate"] for r in rates}}
    name = names["mode"].get(found["mode"])
    db_execute("""UPDATE hqp_benchmark_runs SET measured_from_mode = %(m)s,
                         measured_from_mode_name = %(n)s, measured_from_rate = %(r)s,
                         measured_from_filter = %(f)s, measured_from_filter1x = %(f1)s,
                         measured_from_shaper = %(s)s, measured_from_at = now()
                   WHERE id = %(id)s""",
               {"m": found["mode"], "n": name, "r": names["rate"].get(found["rate"]),
                "f": names["filter"].get(found["filterNx"]),
                "f1": names["filter"].get(found["filter1x"]),
                "s": names["shaper"].get(found["shaper"]), "id": run["id"]})
    with _lock:
        _point_rows.pop(run["hqp_endpoint_id"], None)
        _bump()
    return name


def _point_cleared(run: dict, note: Optional[str] = None, notify: bool = True) -> None:
    """HQPlayer is back in the mode a check found it in, or a person's change
    made it theirs: the run's return point goes, `note` said in its row.
    `notify`: False inside a check, whose verdict or refusal says what
    stands right after."""
    row = db_execute("""UPDATE hqp_benchmark_runs SET measured_from_mode = NULL,
                               measured_from_mode_name = NULL, measured_from_rate = NULL,
                               measured_from_filter = NULL, measured_from_filter1x = NULL,
                               measured_from_shaper = NULL, measured_from_at = NULL,
                               note = CASE WHEN %(n)s::text IS NULL THEN note
                                           ELSE concat_ws('; ', note, %(n)s::text) END
                         WHERE id = %(id)s AND measured_from_mode IS NOT NULL
                        RETURNING id""", {"n": note, "id": run["id"]})
    with _lock:
        _point_rows.pop(run["hqp_endpoint_id"], None)
        _bump()
    if row is not None:
        if note is not None:
            logger.info("HQPlayer benchmark run %s: %s", run["id"], note)
        if notify:
            _notify()


def _go_back(link, point: dict) -> Optional[str]:
    """HQPlayer back in the mode a check found it in (`point`: the run row
    holding the return point) — into that mode, unless it is there already
    (the owner's own switch: what it holds there is theirs), and that mode's
    selection put back by name from its lists. None once HQPlayer is there,
    the point gone — said by the check that follows, whatever ends it, never
    over the mark it judges a moment later; else why not."""
    name = point["measured_from_mode_name"]
    st = link("get_state")
    if st is None:
        return "HQPlayer did not report its state"
    if st["mode"] == point["measured_from_mode"]:
        _point_cleared(point, f"HQPlayer was back in {name}", notify=False)
        return None
    _, failed = link("apply_settings", mode=point["measured_from_mode"])
    st = None if failed else link("get_state")
    if st is None or st["mode"] != point["measured_from_mode"]:
        return f"HQPlayer did not go back to {name}"
    filters, shapers, rates = link("get_filters"), link("get_shapers"), link("get_rates")

    def index(items, key, value):
        return next((x["index"] for x in items or [] if x[key] == value), None)
    want = {"rate": index(rates, "rate", point["measured_from_rate"]),
            "filterNx": index(filters, "name", point["measured_from_filter"]),
            "filter1x": index(filters, "name", point["measured_from_filter1x"]),
            "shaper": index(shapers, "name", point["measured_from_shaper"])}
    failed = {}
    if any(v is not None and st[k] != v for k, v in want.items()):
        _, failed = link("apply_settings", rate=want["rate"], filter=want["filterNx"],
                         filter1x=want["filter1x"], shaper=want["shaper"])
    lost = [k for k, v in want.items() if v is None]
    said = ([f"not named in its lists: {', '.join(lost)}"] if lost else []) + [
        f"{k}: {v}" for k, v in failed.items()]
    _point_cleared(point, f"HQPlayer went back to {name}"
                          + (f" (not all: {'; '.join(said)})" if said else ""), notify=False)
    return None


def point_pending(endpoint_id: int) -> bool:
    """Does a return point stand on this HQPlayer — read from the database
    once, then known (_keep_point, _point_cleared, leave_as_it_is keep it)? The poller then
    reads State at each answer (note_point). A read the database fails, or
    a change overtook, is asked again at the next answer."""
    with _lock:
        if endpoint_id in _point_rows:
            return _point_rows[endpoint_id] is not None
        gen = _left_gen
    try:
        point = _return_point(endpoint_id)
    except psycopg2.Error as e:
        logger.warning("HQPlayer benchmark: a return point not read: %s", e)
        return False
    with _lock:
        if _left_gen != gen:
            return False
        _point_rows.setdefault(endpoint_id, point)
        return _point_rows[endpoint_id] is not None


def watch_gen() -> int:
    """The generation a State read for note_point is taken at."""
    with _lock:
        return _left_gen


def note_point(endpoint_id: int, st: dict, gen: int) -> None:
    """What HQPlayer's State shows while a return point stands (read by the
    poller at generation `gen`, watch_gen): back in that mode — the owner
    switched it themselves — the point goes, no SetMode of ours. Never on a
    read older than a round trip of ours — one on its way keeps the point
    first, and every one's end, a point written or cleared, moves the
    generation."""
    with _lock:
        point = _point_rows.get(endpoint_id)
        if (point is None or st["mode"] != point["measured_from_mode"] or _left_gen != gen
                or endpoint_id in _checking or point["id"] in _putting):
            return
    try:
        _point_cleared(point, f"HQPlayer was back in {point['measured_from_mode_name']}")
    except psycopg2.Error as e:
        logger.warning("HQPlayer benchmark run %s: its return point not cleared yet: %s",
                       point["id"], e)


def _check_measured(link, run: dict,
                    found: dict) -> Tuple[Optional[Tuple[List[str], dict]], Optional[str]]:
    """The selection of the mode `run` measured (_measured), which State
    shows only in that mode (HQPlayer keeps one per mode): into it and read,
    each knob judged as any of a run cut short — the run's last point goes
    back to the owner's selection there (`own_*`, or `pre_*` for a run of
    the mode HQPlayer was in); a knob that is neither is theirs, and leaves
    the mode as it is. Then back into the mode HQPlayer was found in
    (`found`), its selection read again: one the round trip moved is put
    back as found. Before it leaves, the mode found is kept on the run's
    row as the return point (_keep_point), cleared once HQPlayer is back
    there. Returns the owner's knobs there — none: put back, or theirs
    already — and the State read there; or None and why, when HQPlayer did
    not list the settings of the mode it is in, took no mode change, said no
    state there, did not take the owner's selection back or did not go back:
    unchecked still, and — not back — the return point standing."""
    measured, name = _measured(run), run["mode"]
    before = "own" if run["own_mode"] is not None else "pre"
    trip = found["mode"] != measured
    if trip:
        back_to = _keep_point(link, run, found)
        if back_to is None:
            return None, "HQPlayer did not list the settings of the mode it is in"
        _, failed = link("apply_settings", mode=measured)
        if failed:
            _point_cleared(run, notify=False)
            return None, f"HQPlayer did not switch to {name}"
    there = link("get_state")
    checked = None
    why = (f"HQPlayer showed no state in {name}" if there is None
           else f"HQPlayer did not switch to {name}")
    if there is not None and there["mode"] == measured:
        theirs = [what for what, key, col in _KNOBS
                  if there[key] not in (run[f"last_{col}"], run[f"{before}_{col}"])]
        checked = (theirs, there)
        if not theirs and any(there[key] != run[f"{before}_{col}"] for _, key, col in _KNOBS):
            _, failed = link("apply_settings", rate=run[f"{before}_rate"],
                             filter=run[f"{before}_filter"],
                             filter1x=run[f"{before}_filter1x"],
                             shaper=run[f"{before}_shaper"])
            if failed:
                # The test point still there: no verdict, never the owner's
                checked = None
                why = (f"HQPlayer did not take the selection of {name} back ("
                       + ", ".join(f"{k}: {v}" for k, v in failed.items()) + ")")
    if trip:
        _, failed = link("apply_settings", mode=found["mode"])
        back = None if failed else link("get_state")
        if back is None or back["mode"] != found["mode"]:
            return None, f"HQPlayer did not go back to {back_to}"
        failed = {}
        if any(back[key] != found[key] for _, key, _ in _KNOBS):
            _, failed = link("apply_settings", rate=found["rate"], filter=found["filterNx"],
                             filter1x=found["filter1x"], shaper=found["shaper"])
        _point_cleared(run, (f"the selection of {back_to} not all put back: "
                             + ", ".join(f"{k}: {v}" for k, v in failed.items()))
                       if failed else None, notify=False)
    return checked, None if checked is not None else why
