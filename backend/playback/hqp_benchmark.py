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
(PlaybackManager.hold) — the backend is detached as an output switch
detaches it, every other Sautium path is refused until the output comes
back, and the canonical queue mirrors back into HQPlayer when it does. The
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
it set last.

Each point waits for HQPlayer's average to settle (Settle) and writes one
sample: the p10 of the accepted readings, whether they settled, how long
the filter took to start and to settle, and whether it dropped out — the
speed seen just before a dropout is the host's boundary (hqp_load.
thresholds).
"""

import logging
import random
import socket
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import settings
from db_pool import db_execute, db_query_one
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
# A SelectTrack on a stopped HQPlayer needs a beat to register before Play
# honours it (the resume watcher's quirk, 04c46b8): the run reads <Status/>
# until it names the slot, this long at most.
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
_state: Dict[str, Any] = {"running": False, "cancel_requested": False, "endpoint_id": None,
                          "mode": None, "point": 0, "total": 0, "point_label": "",
                          "eta_s": None, "started_at": None, "outcome": None, "note": None}


class BenchmarkRefused(RuntimeError):
    """Why a benchmark cannot start now, in words for the owner."""


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

def mode_kind(name: Optional[str]) -> Optional[str]:
    """'pcm' or 'sdm' for a mode's name; None for [source]."""
    n = (name or "").upper()
    if n == "PCM":
        return "pcm"
    if "SDM" in n or "DSD" in n:
        return "sdm"
    return None


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


def same_family(rate_hz: int, src_rate: int) -> bool:
    """An output rate the source reaches by a whole power of two — its own
    family (44.1 kHz: 88.2 kHz … 44.1k × 1024). Every filter runs those;
    another family takes an asynchronous conversion some refuse at Play
    (sinc-MGa: "Requested filter not possible with this rate combination
    44100/49152000", Desktop 6.2.3, 2026-10-03)."""
    high, low = max(rate_hz, src_rate), min(rate_hz, src_rate)
    if low <= 0 or high % low:
        return False
    ratio = high // low
    return ratio & (ratio - 1) == 0


def running_filter(names: Dict[int, str], nx: int, x1: int, src_rate: int) -> str:
    """The filter HQPlayer runs for a source: the 1x slot for 44.1/48 kHz
    when one is set, the Nx slot otherwise."""
    return names.get(x1 if src_rate in (44100, 48000) and x1 >= 0 else nx, "")


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
        rate_txt = _fmt_rate(rate_hz) if rate_hz else "auto rate"
        label = f"{running} · {sname.get(shaper, '')} · {rate_txt} · {_fmt_rate(s['rate'])} source"
        return Point(rate, rate_hz, nx, x1, shaper, src, label, key, own)

    cur_rate, nx, x1, cur_shaper = (current["rate"], current["filterNx"],
                                    current["filter1x"], current["shaper"])
    real_rates = [r["index"] for r in rates if r["rate"] > 0]

    def family(src: str) -> List[int]:
        return [i for i in real_rates if same_family(hz[i], srcs[src]["rate"])]

    def rates_for(src: str) -> List[int]:
        """Its family's rates and the owner's own, whatever its family —
        never under a PCM source's own rate: HQPlayer does not downsample PCM
        ("clHQPlayerEngine::Execute(): lInRate > lOutRate", then it stops —
        Embedded 6.2.3, a 96 kHz source at 48 kHz, 2026-10-03). A DSD source
        goes down to PCM rates, which is its conversion."""
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


def _fmt_rate(hz: int) -> str:
    """A rate as every HQPlayer screen names it (app-shell.js fmtRateLabel):
    kHz up to 768 kHz, base × power-of-two multiple from the DSD rates up."""
    if not hz:
        return "—"
    if hz <= 768000:
        return f"{hz // 1000} kHz" if hz % 1000 == 0 else f"{hz / 1000:.1f} kHz"
    for base, name in ((44100, "44.1k"), (48000, "48k"), (32000, "32k"), (22050, "22.05k")):
        mult = hz // base
        if hz % base == 0 and mult >= 64 and mult & (mult - 1) == 0:
            return f"{name} × {mult}"
    mhz = hz / 1_000_000
    return f"{mhz:.0f} MHz" if mhz % 1 == 0 else f"{mhz:.2f} MHz"


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
        self.c.disconnect()

    def interrupt(self) -> None:
        """Wake a read waiting on HQPlayer's answer — a cancel from another
        thread: it ends as a dropped connection. A shutdown wakes it on
        Linux; Windows leaves the read blocked until the socket is closed."""
        sock = self.c.socket
        if sock is not None:
            for wake in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
                try:
                    wake()
                except OSError:
                    pass    # closed meanwhile: nothing waits on it

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
    global _thread
    from playback.manager import manager
    if running():
        raise BenchmarkRefused("A benchmark is already running")
    b = manager.active
    if b is None or b.id != "hqplayer":
        raise BenchmarkRefused("Pick this HQPlayer as the audio output first")
    from playback.hqp_backend import _stream_mode
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
    link = _Link(lambda s: True)
    if not link.connect():
        raise BenchmarkRefused("HQPlayer is not answering")
    try:
        status, st = link("get_status"), link("get_state")
        modes, vr = link("get_modes"), link("volume_range")
    except (_Reopened, _Lost):
        raise BenchmarkRefused("HQPlayer dropped the connection — try again")
    finally:
        link.close()
    if status is None or st is None:
        raise BenchmarkRefused("HQPlayer did not report its state")
    # HQPlayer answering is the whole precondition — a queue mirror the
    # attach could not make (HQPlayer not up yet, 2026-10-03) is none: the
    # run sets HQPlayer's playlist itself, and the attach that takes the
    # output back mirrors the queue afresh. Another controller is, while it
    # plays.
    if b.drift and status.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
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
        _thread = threading.Thread(target=_run, args=(ep, kind), daemon=True,
                                   name="hqp-benchmark")
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


def _run(ep: dict, kind: str) -> None:
    """The job's thread. Whatever ends it is recorded where the owner reads
    it (state(), and the run row once there is one); the job is closed
    before the output goes back — the attach that takes it back then finds
    no run going, and puts back one whose restore HQPlayer did not answer
    (recover)."""
    from playback.manager import manager
    try:
        with manager.hold("benchmark", LABEL):
            try:
                outcome, note = _measure(ep, kind)
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


def _finish(outcome: str, note: Optional[str]) -> None:
    with _lock:
        _state.update(running=False, outcome=outcome, note=note, point_label="", eta_s=None)
    logger.info("HQPlayer benchmark %s%s", outcome, f": {note}" if note else "")


def _measure(ep: dict, kind: str) -> Tuple[str, Optional[str]]:
    """The run proper, inside the hold: read what is to be put back, open
    the run row, measure, and put everything back whatever happens."""
    global _live
    link = _Link(_cancel.wait, answer_s=ANSWER_WAIT_S)
    if not link.connect():
        return "failed", "HQPlayer is not answering"
    _live = link
    try:
        pre, modes, vr = _again(lambda: (link("get_state"), link("get_modes"),
                                         link("volume_range")))
        if pre is None or not modes:
            return "failed", "HQPlayer did not report its state"
        mode_index = _mode_index(modes, kind)
        if mode_index is None:
            return "failed", f"This HQPlayer's output offers no {kind.upper()} mode"
        mode_name = _mode_name(modes, mode_index)
        mute = mute_level(vr, pre["volume"])
        run_id = _open_run(ep, mode_name, pre, mute)
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
            db_execute("UPDATE hqp_benchmark_runs SET points_planned = %(n)s WHERE id = %(id)s",
                       {"n": grid.asks(), "id": run_id})
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
            reached, restore_note = _restore(link, pre, own, mute)
            note = "; ".join(x for x in (note, restore_note) if x) or None
            # An HQPlayer that did not answer the restore keeps the run
            # open: the next attach, or its return, puts it back (recover).
            _close_run(run_id, outcome, note, stats, finished=reached)
        return outcome, note
    finally:
        _live = None
        link.close()


def _enter_mode(link: _Link, mode_index: int, switched: bool) -> tuple:
    """Into the measured mode — HQPlayer keeps one selection per mode, read
    as it is there — and its lists."""
    if switched and not link("set_mode", mode_index):
        raise _Abort(f"HQPlayer did not switch modes: {link.refusal()}")
    st = link("get_state")
    if st is None:
        raise _Abort("HQPlayer did not report its state")
    return st, link("get_filters"), link("get_shapers"), link("get_rates")


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
            if _lists_key(link("get_filters"), link("get_shapers"), link("get_rates")) != lists:
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
                    # built and does not start again by itself (the DSP
                    # resume watcher's quirk), or it stopped at once: start
                    # it again, once.
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


def _restore(link: _Link, pre: dict, own: Optional[dict],
             mute: Optional[float]) -> Tuple[bool, Optional[str]]:
    """Put back what the run changed, whatever ended it. Returns (HQPlayer
    answered, what could not be put back). Only the app stopping cuts the
    wait for an HQPlayer that is away short; one that is there but silent —
    a cancel in the middle of a build — is left to recover (RESTORE_ANSWER_S)."""
    link.wait = _halt.wait
    link.c.timeout = RESTORE_ANSWER_S
    sock = link.c.socket
    if sock is not None:
        if sock.fileno() < 0:
            link.close()        # closed under it by a cancel's interrupt: connects anew
        else:
            sock.settimeout(RESTORE_ANSWER_S)
    try:
        problems = _again(lambda: _put_back(link, pre=pre, own=own, mute=mute, clear=True),
                          silent_ends=True)
        after = _again(lambda: link("get_state"), silent_ends=True)
    except _Lost:
        return False, "HQPlayer did not answer to be put back — it is when it answers again"
    if after is None:
        return True, "HQPlayer did not report its state after the run"
    diff = [k for k in ("mode", "rate", "filterNx", "filter1x", "shaper")
            if after.get(k) != pre.get(k)]
    if mute is not None and abs(after["volume"] - pre["volume"]) > _DB_EPS:
        diff.append("volume")
    if diff or problems:
        logger.warning("HQPlayer benchmark: not put back as it was — %s %s", diff, problems)
        return True, "not everything was put back: " + ", ".join(diff + problems)
    return True, None


def _put_back(link: _Link, *, pre: dict, own: Optional[dict], mute: Optional[float],
              clear: bool) -> List[str]:
    """Stop; the run's sources out of the playlist (`clear`); the measured
    mode's own selection when the run switched into it, then the mode it
    found and that mode's selection; the volume, when the run lowered it.
    What HQPlayer refused, as "knob: its words"."""
    link("stop")
    if clear:
        link("playlist_clear")
    problems: List[str] = []
    now = link("get_state")
    if own is not None and now is not None and now["mode"] == own["mode"]:
        _, failed = link("apply_settings", rate=own["rate"], filter=own["filterNx"],
                         filter1x=own["filter1x"], shaper=own["shaper"])
        problems += [f"{k}: {v}" for k, v in failed.items()]
    if now is None or now["mode"] != pre["mode"]:
        _, failed = link("apply_settings", mode=pre["mode"])
        problems += [f"{k}: {v}" for k, v in failed.items()]
    _, failed = link("apply_settings", rate=pre["rate"], filter=pre["filterNx"],
                     filter1x=pre["filter1x"], shaper=pre["shaper"])
    problems += [f"{k}: {v}" for k, v in failed.items()]
    if mute is not None:
        link("set_volume", pre["volume"])
    return problems


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
               finished: bool) -> None:
    own = stats["own"]
    db_execute("""
        UPDATE hqp_benchmark_runs
           SET finished_at = CASE WHEN %(fin)s THEN now() END,
               outcome = CASE WHEN %(fin)s THEN %(o)s::hqp_bench_outcome END,
               note = %(note)s, points_measured = %(n)s, dropout_speed = %(d)s,
               first_speed = %(first)s, last_speed = %(last)s
         WHERE id = %(id)s
    """, {"id": run_id, "fin": finished, "o": outcome, "note": note, "n": stats["measured"],
          "d": max(stats["dropouts"]) if stats["dropouts"] else None,
          "first": own[0] if own else None, "last": own[-1] if len(own) > 1 else None})


def recover(endpoint_id: int) -> None:
    """A run cut short — the process died in the middle of it, or HQPlayer
    went away and did not answer the restore — left HQPlayer stopped on its
    test settings, maybe lowered and holding the signals. The next time
    HQPlayer answers (the attach, or the poller seeing it come back), before
    anything reads its playlist: put the owner's settings back — only while
    HQPlayer still shows a mark of the run (its signals in the playlist, the
    volume it lowered to, the selection it set last); a HQPlayer changed
    since is the owner's again. A HQPlayer that drops the connection
    meanwhile is tried at its next return."""
    if running():
        return
    run = db_query_one("""
        SELECT * FROM hqp_benchmark_runs
         WHERE hqp_endpoint_id = %(e)s AND finished_at IS NULL
         ORDER BY started_at DESC LIMIT 1
    """, {"e": endpoint_id})
    if run is None:
        return
    link = _Link(lambda s: True)       # never holds up the attach: tried again at the next one
    if not link.connect():
        return
    try:
        note = _recover(link, run)
    except (_Reopened, _Lost) as e:
        logger.info("HQPlayer benchmark run %s not put back yet: %s", run["id"], e)
        return
    finally:
        link.close()
    if note is None:
        return
    db_execute("""UPDATE hqp_benchmark_runs SET finished_at = now(), outcome = 'interrupted',
                         note = concat_ws('; ', note, %(n)s::text)
                   WHERE id = %(id)s""", {"n": note, "id": run["id"]})
    logger.info("HQPlayer benchmark run %s recovered: %s", run["id"], note)


def _recover(link: _Link, run: dict) -> Optional[str]:
    st, listed = link("get_state"), link("get_playlist")
    if st is None:
        return None
    tokens = _signal_tokens()
    ours = any(t in e.get("uri", "") for e in listed for t in tokens)
    lowered = (run["mute_volume"] is not None
               and abs(st["volume"] - run["mute_volume"]) < _DB_EPS)
    measured = run["own_mode"] if run["own_mode"] is not None else run["pre_mode"]
    on_point = (run["last_rate"] is not None and st["mode"] == measured
                and (st["rate"], st["filterNx"], st["filter1x"], st["shaper"])
                == (run["last_rate"], run["last_filter"], run["last_filter1x"], run["last_shaper"]))
    note = "cut short"
    if not (ours or lowered or on_point):
        return note + "; HQPlayer had been changed since and was left as it is"
    pre = {"mode": run["pre_mode"], "rate": run["pre_rate"], "filterNx": run["pre_filter"],
           "filter1x": run["pre_filter1x"], "shaper": run["pre_shaper"],
           "volume": run["pre_volume"]}
    own = None if run["own_mode"] is None else {
        "mode": run["own_mode"], "rate": run["own_rate"], "filterNx": run["own_filter"],
        "filter1x": run["own_filter1x"], "shaper": run["own_shaper"]}
    problems = _put_back(link, pre=pre, own=own, mute=run["mute_volume"] if lowered else None,
                         clear=ours)
    note += "; HQPlayer's settings" + (" and volume" if lowered else "") + \
        " were put back when it answered again"
    if problems:
        note += " (not all: " + ", ".join(problems) + ")"
    return note
