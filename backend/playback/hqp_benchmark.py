"""
The HQPlayer benchmark: what this HQPlayer keeps up with, measured for the
settings the owner has not listened through yet.

Every listen already leaves DSP samples (playback.hqp_load). The benchmark
fills the gaps: it plays test signals through the settings of ONE mode — the
one HQPlayer is in; the other only when the owner asks for it — and only
through the points no sample and no earlier run covers yet, so a repeat run
after weeks of listening takes minutes. The grid (build_grid):

- PCM: every filter × {the owner's output rate, the highest} × {44.1, 96 kHz
  sources}, with the owner's dither;
- SDM: every modulator × every DSD rate on a 44.1 kHz source with the owner's
  filter, and every filter at the owner's rate with the owner's modulator;
- a DSD-source slice when the owner has listened to a DSD file that is still
  there (that file is the source; none is generated);
- the owner's own setting first and again last — the thermal drift check;
  on an endpoint's first run the probe row right after it: every modulator
  (in PCM every filter) at the highest rate, where the host's dropout
  boundary shows (which one is heaviest is what is being measured).

Each point leaves a row in hqp_benchmark_points by what it ASKED — measured,
unsettled, dropped out, refused by HQPlayer, or failed — and a later run
skips every point asked before that did not fail (hqp_load.covered): a
combination HQPlayer refuses is never a sample, and an adaptive output rate
plays another rate than the one asked.

The signals are pink noise at about -20 dBFS, 24-bit stereo FLAC — the
decoder path owned files take — made once into the node's data dir and
handed over as the media proxy's /file/ URLs: a Docker node's container
path is nothing HQPlayer can open, and the input path changes no DSP cost.

One owner of HQPlayer at a time: the run borrows the output
(PlaybackManager.hold) — the backend is detached as an output switch
detaches it, every other Sautium path is refused until the output comes
back, and the canonical queue mirrors back into HQPlayer when it does. The
run drives HQPlayer on a connection of its own (_Link), which survives
HQPlayer dropping it: a command that finds it gone reconnects, and what
HQPlayer holds is made again before anything plays — it may have restarted
under the run. HQPlayer's volume goes to the bottom of its range first and
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
SETTLE_WINDOW = 5
SETTLE_BAND = 0.02
SETTLE_TREND = 0.01
SETTLE_CAP_S = 15.0
# Play until the position moves: a heavy filter initialises for seconds.
START_DEADLINE_S = 60.0
# A point that has not decided by then never will (a frozen output that
# still says it plays).
POINT_DEADLINE_S = 90.0
# Falling behind, and only that, is a dropout — once the readings run: the
# output buffer under DROPOUT_FILL for DRAINED_TICKS ticks in a row while the
# input holds, the position frozen for STALLED_TICKS, or the transport
# stopped. Before the readings the buffer is still filling and the average
# still holds the initialisation, so the same signs count only under 1×.
DROPOUT_FILL = 0.1
DRAINED_TICKS = 3
STALLED_TICKS = 2
TICK_S = 1.0
# A SelectTrack on a stopped HQPlayer needs a beat to register before Play
# honours it (the resume watcher's quirk, 04c46b8): the run reads <Status/>
# until it names the slot, this long at most.
SELECT_WAIT_S = 2.0
SELECT_POLL_S = 0.2
# Seconds a point takes before this endpoint has run one: start, five stable
# seconds, five readings.
POINT_S = 14.0
# A dropped connection is tried again at once, then after these pauses.
_RECONNECT_PAUSES = (1.0, 2.0, 4.0, 8.0)
_DB_EPS = 0.05                    # volumes read back within this are the same

_lock = threading.Lock()
_cancel = threading.Event()
# The app is stopping: putting HQPlayer back does not wait for it to come back.
_halt = threading.Event()
_thread: Optional[threading.Thread] = None
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


def running_filter(names: Dict[int, str], nx: int, x1: int, src_rate: int) -> str:
    """The filter HQPlayer runs for a source: the 1x slot for 44.1/48 kHz
    when one is set, the Nx slot otherwise."""
    return names.get(x1 if src_rate in (44100, 48000) and x1 >= 0 else nx, "")


def build_grid(kind: str, *, filters: List[dict], shapers: List[dict], rates: List[dict],
               current: Dict[str, int], srcs: Dict[str, dict], covered: set,
               first_run: bool, rng: random.Random) -> List[Point]:
    """The points of one run (see the module docstring), minus every key
    already covered — the owner's own setting, first and last, excepted."""
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
    top = max(real_rates, key=lambda i: hz[i]) if real_rates else cur_rate
    grid: List[Point] = []
    probe: List[Point] = []
    if kind == "pcm":
        for f in filters:
            for rate in dict.fromkeys((cur_rate, top)):
                for src in ("pcm44", "pcm96"):
                    p = point(rate, f["index"], f["index"], cur_shaper, src)
                    (probe if rate == top and src == "pcm44" else grid).append(p)
    else:
        for sh in shapers:
            for rate in real_rates:
                p = point(rate, nx, x1, sh["index"], "pcm44")
                (probe if rate == top else grid).append(p)
        for f in filters:
            grid.append(point(cur_rate, f["index"], f["index"], cur_shaper, "pcm44"))
    if "dsd" in srcs:
        for rate in real_rates:
            grid.append(point(rate, nx, x1, cur_shaper, "dsd"))

    own = point(cur_rate, nx, x1, cur_shaper, "pcm44", own=True)
    seen = {own.key}

    def fresh(points: List[Point]) -> List[Point]:
        out = []
        for p in points:
            if p.key in seen or p.key in covered:
                continue
            seen.add(p.key)
            out.append(p)
        return out

    if first_run:
        probe = fresh(probe)
        rest = fresh(grid)
    else:                       # on a later run the probe row is grid like the rest
        rest, probe = fresh(probe + grid), []
    rng.shuffle(rest)
    return [own, *probe, *rest, own]


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
    Play that started it. feed() answers None until it has decided, then a
    dict: `speed` (the p10 of the accepted readings, or the speed seen just
    before a dropout), `settled`, `dropout`, `init_s` (Play until the
    position moved), `settle_s` (the position moving until accepted),
    `out_fill`/`in_fill` (the lowest seen once it moved), `readings`,
    `status` (the last one taken while it played); or `failed` with why —
    a point that failed is measured again by a later run."""

    def __init__(self, t0: float):
        self.t0 = t0
        self.init_s: Optional[float] = None
        # The last <Status/> taken while it played — what the sample keys on:
        # a stopped one carries no <metadata> and may name the next setting.
        self.playing: Optional[TrackStatus] = None
        self._moving_since: Optional[float] = None
        self._last_pos = 0.0
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

    def feed(self, now: float, status: TrackStatus) -> Optional[dict]:
        if now - self.t0 > POINT_DEADLINE_S:
            return {"failed": f"no verdict within {POINT_DEADLINE_S:.0f} s of Play"}
        playing = status.state == PlaybackState.PLAYING
        if playing and status.src_rate:
            self.playing = status
        if playing and status.process_speed:
            self._last_speed = status.process_speed
        moving = playing and status.position > self._last_pos
        self._last_pos = status.position if playing else 0.0
        self._stalls = 0 if moving or not playing else self._stalls + 1
        if not self.moved:
            if moving:
                self.init_s, self._moving_since = now - self.t0, now
            elif now - self.t0 > START_DEADLINE_S:
                return {"failed": f"HQPlayer did not start it within {START_DEADLINE_S:.0f} s"}
            return None
        if not playing:
            return self._behind(now, "it stopped")
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
                self._moving_since = now
            return None
        if now - self._moving_since < STABLE_AFTER_S:
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


class _Link:
    """The run's own connection to HQPlayer: it never meets the playback
    backend's sockets, and its expected refusals stay out of the diagnostics
    ring (ring=False). HQPlayer drops connections — a trial-mode Embedded
    stops every 30 minutes, the WSL2 hop flaps for seconds — so a command
    that finds it gone, or loses it, connects again (at once, then after
    each of _RECONNECT_PAUSES; `wait` answering True gives up early) and
    raises _Reopened instead of carrying on blind; one that cannot is
    _Lost."""

    def __init__(self, wait: Callable[[float], bool]):
        self.c = HQPlayerClient(host=settings.hqplayer_host, port=settings.hqplayer_port,
                                timeout=10.0, ring=False)
        self.wait = wait

    def connect(self) -> bool:
        return self.c.connect()

    def close(self) -> None:
        self.c.disconnect()

    def refusal(self) -> str:
        return self.c.refusal()

    def __call__(self, method: str, *args, **kwargs):
        if not self.c.is_connected():
            self._reconnect()
        result = getattr(self.c, method)(*args, **kwargs)
        if not self.c.is_connected():
            self._reconnect()
        return result

    def _reconnect(self) -> None:
        why = self.c.refusal()
        for pause in (0.0, *_RECONNECT_PAUSES):
            if pause and self.wait(pause):
                break
            if self.c.connect():
                logger.info("HQPlayer benchmark: connected again after %s", why)
                raise _Reopened(why)
        raise _Lost(f"HQPlayer stopped answering ({why})")


def _again(step: Callable[[], Any]) -> Any:
    """A step that comes out the same however often it runs, run again
    whenever the connection was replaced under it."""
    for _ in range(3):
        try:
            return step()
        except _Reopened:
            continue
    raise _Lost("HQPlayer kept dropping the connection")


# -- the run ---------------------------------------------------------------------

def _endpoint() -> Optional[dict]:
    import hqp_library
    return hqp_library.endpoint_by_address(settings.hqplayer_host, settings.hqplayer_port)


def _mode_index(modes: List[dict], kind: str) -> Optional[int]:
    return next((m["index"] for m in modes if mode_kind(m["name"]) == kind), None)


def _mode_name(modes: List[dict], index: int) -> Optional[str]:
    return next((m["name"] for m in modes if m["index"] == index), None)


def _first_run(endpoint_id: int) -> bool:
    return db_query_one("SELECT 1 AS x FROM hqp_benchmark_runs WHERE hqp_endpoint_id = %(e)s LIMIT 1",
                        {"e": endpoint_id}) is None


def pace(endpoint_id: int) -> float:
    """Seconds a point takes here — the pace of the last run that ran to its
    end (a cut-short one ends in a wait for HQPlayer, not in points)."""
    row = db_query_one("""
        SELECT extract(epoch FROM finished_at - started_at) / points_measured AS s
          FROM hqp_benchmark_runs
         WHERE hqp_endpoint_id = %(e)s AND outcome = 'done' AND points_measured >= 5
         ORDER BY started_at DESC LIMIT 1
    """, {"e": endpoint_id})
    return float(row["s"]) if row else POINT_S


def plan(endpoint: dict, *, kind: str, mode_name: str, state: dict, filters: List[dict],
         shapers: List[dict], rates: List[dict], srcs: Optional[Dict[str, dict]] = None,
         first_run: Optional[bool] = None, rng: Optional[random.Random] = None) -> List[Point]:
    """The points a run of this mode would measure now. `first_run` is
    known to a run before its own row exists; anyone else asks the table."""
    srcs = srcs if srcs is not None else sources(kind, endpoint.get("hqp_engine"))
    ctx = hqp_load.context(endpoint, mode=mode_name, state=state)
    return build_grid(kind, filters=filters, shapers=shapers, rates=rates,
                      current={"rate": state["rate"], "filterNx": state["filterNx"],
                               "filter1x": state["filter1x"], "shaper": state["shaper"]},
                      srcs=srcs, covered=hqp_load.covered(ctx),
                      first_run=_first_run(endpoint["id"]) if first_run is None else first_run,
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
    if not b.healthy():
        raise BenchmarkRefused("HQPlayer has to answer Sautium first — press Play once, "
                               "or check that it runs")
    if b.drift:
        raise BenchmarkRefused("Another controller is playing through HQPlayer — the "
                               "benchmark waits until Sautium is its source again")
    from playback.hqp_backend import _stream_mode
    if not _stream_mode():
        import main
        if main._scan_state["running"] or main._enrich_state["running"]:
            raise BenchmarkRefused("A library scan or analysis is running on this computer "
                                   "— it would skew what HQPlayer measures here")
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
    return True


def shutdown(timeout: float) -> None:
    """The app is stopping: a run is cancelled and given `timeout` seconds
    to put HQPlayer back — what the process has left (Docker allows ten).
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
    link = _Link(_cancel.wait)
    if not link.connect():
        return "failed", "HQPlayer is not answering"
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
        first_run = _first_run(ep["id"])          # before this run's own row exists
        run_id = _open_run(ep, mode_name, pre, mute)
        switched = pre["mode"] != mode_index
        own: Optional[dict] = None
        stats: Dict[str, Any] = {"measured": 0, "dropouts": [], "own": [], "refused": 0,
                                 "failed": 0}
        outcome, note = "failed", None
        try:
            st, filters, shapers, rates = _again(lambda: _enter_mode(link, mode_index, switched))
            if switched:
                own = {"mode": mode_index, "rate": st["rate"], "filterNx": st["filterNx"],
                       "filter1x": st["filter1x"], "shaper": st["shaper"]}
                _note_own(run_id, own)
            srcs = sources(kind, ep.get("hqp_engine"))
            points = plan(ep, kind=kind, mode_name=mode_name, state=st, filters=filters,
                          shapers=shapers, rates=rates, srcs=srcs, first_run=first_run)
            _progress(point=0, total=len(points), point_label="Preparing the test signals…",
                      eta_s=None)
            used = {p.source for p in points}
            uris = {name: (_dsd_uri(s["file"]) if name == "dsd"
                           else _signal_uri(ensure_signal(s["rate"])))
                    for name, s in srcs.items() if name in used}
            db_execute("UPDATE hqp_benchmark_runs SET points_planned = %(n)s WHERE id = %(id)s",
                       {"n": len(points), "id": run_id})
            slots = _prepare(link, uris, mute)
            outcome, note = _points(link, points, slots, uris, srcs, mute, ep, run_id, st,
                                    mode_name, stats)
        except _Abort as e:
            outcome, note = "failed", str(e)
        except _Lost as e:
            outcome, note = ("cancelled", None) if _cancel.is_set() else ("failed", str(e))
        except Exception as e:          # recorded on the run, whatever it was
            logger.exception("HQPlayer benchmark failed")
            outcome, note = "failed", _why(e)
        finally:
            reached, restore_note = _restore(link, pre, own, mute)
            note = "; ".join(x for x in (note, restore_note) if x) or None
            # An HQPlayer that did not answer the restore keeps the run
            # open: the next attach, or its return, puts it back (recover).
            _close_run(run_id, outcome, note, stats, finished=reached)
        return outcome, note
    finally:
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


def _prepare(link: _Link, uris: Dict[str, str], mute: Optional[float]) -> Dict[str, int]:
    """What every point stands on — made again whenever the connection was
    replaced, before anything plays: HQPlayer stopped, lowered (read back),
    and holding the run's sources in order. The slot of each source,
    1-based."""
    def once() -> Dict[str, int]:
        link("stop")
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
    return _again(once)


def _points(link: _Link, points: List[Point], slots: Dict[str, int], uris: Dict[str, str],
            srcs: Dict[str, dict], mute: Optional[float], ep: dict, run_id: int, st: dict,
            mode_name: str, stats: dict) -> Tuple[str, Optional[str]]:
    applied: Dict[str, Any] = {}
    failures = 0
    t_run = time.monotonic()
    for i, p in enumerate(points, 1):
        done = i - 1
        eta = (time.monotonic() - t_run) / done * (len(points) - done) if done else \
            len(points) * pace(ep["id"])
        _progress(point=i, total=len(points), point_label=p.label, eta_s=round(eta))
        if _cancel.is_set():
            return "cancelled", None
        try:
            result = _point(link, p, slots, applied, run_id, srcs)
        except _Reopened:
            # HQPlayer may have restarted under the point: lowered again, its
            # sources loaded again, the knobs sent again — then once more.
            applied.clear()
            slots = _prepare(link, uris, mute)
            try:
                result = _point(link, p, slots, applied, run_id, srcs)
            except _Reopened:
                applied.clear()
                slots = _prepare(link, uris, mute)
                result = {"failed": "HQPlayer dropped the connection during it twice"}
        if result.get("cancelled"):
            return "cancelled", None
        line = f"benchmark {i}/{len(points)} · {mode_name} · {p.label}: "
        if "refused" in result:
            logger.info(line + "refused — " + result["refused"])
            _ledger(run_id, p, "refused", result["refused"])
            stats["refused"] += 1
            failures = 0
            continue
        status: Optional[TrackStatus] = result.get("status")
        if "failed" not in result and status is None:
            result = {"failed": "it never played with its source reported"}
        if "failed" in result:
            logger.info(line + result["failed"])
            _ledger(run_id, p, "failed", result["failed"])
            stats["failed"] += 1
            failures += 1
            if failures >= 3:
                raise _Abort(f"three points in a row did not play: {result['failed']}")
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
        if result["dropout"]:
            stats["dropouts"].append(result["speed"])
        if p.own:
            stats["own"].append(result["speed"])
        logger.info(line + _verdict(result))
    return "done", _tally(stats)


def _tally(stats: dict) -> Optional[str]:
    """What a finished run says besides its counts."""
    parts = []
    if stats["refused"]:
        parts.append(f"HQPlayer refused {stats['refused']} combination"
                     f"{'s' if stats['refused'] != 1 else ''}")
    if stats["failed"]:
        parts.append(f"{stats['failed']} point{'s' if stats['failed'] != 1 else ''} did not "
                     "play through and are measured again next run")
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
           run_id: int, srcs: Dict[str, dict]) -> dict:
    """One point: stop, send only the knobs that change and note what
    HQPlayer holds now (the run's mark, recover), start its source, feed
    Settle a <Status/> a second until it decides."""
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
    verdict = _play(link, slot, src_rate, len(slots))
    if verdict.get("elsewhere"):
        verdict = _play(link, slot, src_rate, len(slots))
    return verdict


def _select(link: _Link, slot: int) -> None:
    """SelectTrack, then <Status/> until it names the slot — at most
    SELECT_WAIT_S: HQPlayer cannot say when it is ready, and the Play right
    after must find the slot taken (a misplay is caught on the ticks)."""
    if not link("select_track", slot):
        return
    deadline = time.monotonic() + SELECT_WAIT_S
    while time.monotonic() < deadline:
        now = link("get_status")
        if now is not None and now.track_index == slot:
            return
        if _cancel.wait(SELECT_POLL_S):
            return


def _play(link: _Link, slot: int, src_rate: Optional[int], entries: int) -> dict:
    """Play the slot and feed Settle until it decides. HQPlayer playing
    another entry than the slot (or a generated signal at another rate) is
    `elsewhere` — the caller selects it once more."""
    _select(link, slot)
    if _cancel.is_set():
        return {"cancelled": True}
    if not link("play"):
        return {"failed": f"HQPlayer did not play it: {link.refusal()}"}
    settle = Settle(time.monotonic())
    misses = 0
    replayed = False
    while True:
        if _cancel.wait(TICK_S):
            return {"cancelled": True}
        status = link("get_status")
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
            return {"elsewhere": True,
                    "failed": f"HQPlayer played entry {status.track_index} instead of {slot}"}
        if (status.state == PlaybackState.STOPPED and not settle.moved and not replayed
                and settle.playing is not None):
            # It stopped to rebuild for a heavy setting before its position
            # ever moved: start it again, once.
            replayed = True
            _select(link, slot)
            link("play")
            continue
        verdict = settle.feed(time.monotonic(), status)
        if verdict is not None:
            return verdict


def _restore(link: _Link, pre: dict, own: Optional[dict],
             mute: Optional[float]) -> Tuple[bool, Optional[str]]:
    """Put back what the run changed, whatever ended it. Returns (HQPlayer
    answered, what could not be put back). Only the app stopping cuts the
    wait for an HQPlayer that is away short."""
    link.wait = _halt.wait
    try:
        problems = _again(lambda: _put_back(link, pre=pre, own=own, mute=mute, clear=True))
        after = _again(lambda: link("get_state"))
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
