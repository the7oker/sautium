"""
What a DSP setting costs on an HQPlayer's host — measured, never predicted.

HQPlayer reports `process_speed` in every <Status/> since 5.17.0: its
processing speed over the playback speed, a running average of its last
processing units (Jussi Laako; Signalyst publishes no definition). Near 1×
drop-outs become likely, under 1× playback cannot keep up. Whether a filter
or a modulator keeps up depends on the host — the CPU build, CUDA offload
(filters can run on the GPU, modulators never do), memory, cooling — too
many factors to predict from a spec sheet. So Sautium measures:

- every listen leaves samples (`Sampler` on the playback poller, `write`):
  at most one a minute, and only once HQPlayer's average has left the
  setting's initialisation behind;
- the benchmark (playback.hqp_benchmark) measures the settings no sample
  covers yet (`covered`).

A sample is keyed by what HQPlayer reports it RUNS — <Status active_*>, by
name, because the lists differ per mode and per build — and by the build it
ran on (<GetInfo engine>), the CUDA offload, the matrix profile, convolution
and the source HQPlayer decodes (<metadata samplerate channels>): each one
changes the cost. hqp_dsp_speed is the per-key rollup the pickers badge
from, refreshed in the transaction that writes the sample: the key's newest
benchmark point and every listen after it, all of them while it has none.
Samples live RETENTION_DAYS; the rollup keeps what they said. What the
engine itself answered a benchmark — a refusal, a setting it never got
going, one it did not start within the build limit — holds for as long as
the engine build does (ENGINE_ANSWERS).
"""

import logging
import math
import re
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from db_pool import db_query, db_query_one, transaction
from hqp_rate_check import Sources
from hqplayer_client import PlaybackState

logger = logging.getLogger(__name__)

# The headroom classes: "ok" from OK_AT up, "tight" from NO_BELOW, "no" under
# it — until a benchmark point dropped out on the endpoint, and the host's own
# boundary takes over (thresholds).
OK_AT = 1.25
NO_BELOW = 1.0

# A listen's sample: this far into the track AND past the last change of the
# setting or the source — before it, HQPlayer's average still holds the
# initialisation — then on every change, or once a minute.
SAMPLE_AFTER_S = 15.0
SAMPLE_EVERY_S = 60.0

RETENTION_DAYS = 90
# What a run asked that the ENGINE answered — refused, never got going, or
# not started within the build limit — holds while the engine build is the
# same (BENCH-GUARD-1 Q1): a build that took past the limit does not get
# faster with time, and a refusal is the engine's own rule. A measured speed
# moves with the host's state (B-142) and keeps the retention.
ENGINE_ANSWERS = ("refused", "unstarted", "slow_start")
# Listens that make a benchmark point needless — as one benchmark point does.
PASSIVE_COVER = 3
# A dropout above this speed is not the DSP falling behind (a setting that
# runs half again faster than real time does not starve its output): the
# output device or the network hiccupped. It is recorded, and never moves
# the host's boundary (thresholds).
DROPOUT_CEILING = 1.5

_PRUNE_EVERY_S = 24 * 3600.0
_prune_lock = threading.Lock()
_pruned_at: Optional[float] = None

# 5.17.0 computed process_speed wrongly for a DSD source; the next release
# fixed it (Jussi Laako, Roon community, 2026-03-03).
_DSD_SPEED_WRONG = (5, 17, 0)
# HQPlayer's engine is read as a build up to this length, a longer one as none:
# the string is HQPlayer's own, and int() raises on a digit run past 4300
# digits — the status tick would fail while HQPlayer plays a DSD slot of ours.
# hqp_atlas reads it with the same bound.
MAX_ENGINE_LEN = 64


def build(engine: Optional[str]) -> Tuple[int, ...]:
    """<GetInfo engine="6.2.3"/> as a comparable tuple; () for none, and for
    one past MAX_ENGINE_LEN."""
    engine = engine or ""
    if len(engine) > MAX_ENGINE_LEN:
        return ()
    return tuple(int(x) for x in re.findall(r"\d+", engine)[:3])


def dsd_speed_wrong(engine: Optional[str]) -> bool:
    return build(engine) == _DSD_SPEED_WRONG


class Sampler:
    """When a listen yields a sample — one per HQPlayer backend, fed every
    status tick. Pure: the poller reads <State/> and writes only when due."""

    def __init__(self) -> None:
        self._key: Optional[tuple] = None
        self._since = 0.0
        self._written: Optional[tuple] = None
        self._written_at = 0.0
        self._dsp: Optional[tuple] = None

    def due(self, status, *, ours: bool, engine: Optional[str], now: float,
            metering: bool = False) -> bool:
        """HQPlayer plays a slot of ours (not another controller's playlist),
        reports its speed and the source it decodes; 15 s into the track and
        15 s past the last change of the setting or the source; then on a
        change or once a minute. Never 5.17.0 with a DSD source, and never
        while our meter stream is open (playback.hqp_meter): metering costs
        HQPlayer work, and the average it leaves is waited out like any
        change of setting once the meter closes."""
        if status.state != PlaybackState.PLAYING or not ours:
            self._key = None    # a stop, a pause, a foreign playlist: the next play starts over
            return False
        key = (status.active_mode, status.active_rate, status.active_filter,
               status.active_shaper, status.src_rate, status.src_channels, metering)
        if key != self._key:
            self._key, self._since = key, now
        if metering:
            return False
        if not (status.process_speed and status.src_rate and status.src_channels
                and status.active_mode and status.active_filter and status.active_rate):
            return False
        if status.src_sdm and dsd_speed_wrong(engine):
            return False
        if status.position < SAMPLE_AFTER_S or now - self._since < SAMPLE_AFTER_S:
            return False
        return key != self._written or now - self._written_at >= SAMPLE_EVERY_S

    def steady(self, state: dict, now: float) -> bool:
        """The knobs only <State/> reports — the matrix profile, convolution —
        are read when a sample is due; one that changed since the last read
        is a change of setting like any other: the sample waits out its
        initialisation (SAMPLE_AFTER_S from now) instead of carrying the
        average of the setting before."""
        dsp = (state.get("matrix_profile") or None, bool(state.get("convolution")))
        if self._dsp is not None and dsp != self._dsp:
            self._dsp, self._since = dsp, now
            return False
        self._dsp = dsp
        return True

    def taken(self, now: float) -> None:
        """The sample due now is on its way (or lost with a database that
        cannot take it): the next one waits its turn either way."""
        self._written, self._written_at = self._key, now


@dataclass
class Sample:
    endpoint_id: int
    source: str                      # 'listen' | 'benchmark'
    mode: str
    rate_out: int
    filter: str
    shaper: str
    matrix_profile: Optional[str]
    convolution: bool
    adaptive: bool
    src_rate: int
    src_bits: Optional[int]
    src_channels: int
    src_sdm: bool
    process_speed: float
    input_fill: Optional[float] = None
    output_fill: Optional[float] = None
    run_id: Optional[int] = None
    dropout: Optional[bool] = None
    settled: Optional[bool] = None
    init_s: Optional[float] = None
    settle_s: Optional[float] = None
    speed_lo: Optional[float] = None         # benchmark only: the lowest and highest reading
    speed_hi: Optional[float] = None

    @classmethod
    def of(cls, status, state: dict, *, endpoint_id: int) -> "Sample":
        """A listen: what <Status/> says HQPlayer runs, with the knobs only
        <State/> reports — the matrix profile, convolution, adaptive volume."""
        return cls(endpoint_id=endpoint_id, source="listen",
                   mode=status.active_mode, rate_out=status.active_rate,
                   filter=status.active_filter, shaper=status.active_shaper,
                   matrix_profile=state.get("matrix_profile") or None,
                   convolution=bool(state.get("convolution")),
                   adaptive=bool(state.get("adaptive")),
                   src_rate=status.src_rate, src_bits=status.src_bits,
                   src_channels=status.src_channels, src_sdm=bool(status.src_sdm),
                   process_speed=status.process_speed,
                   input_fill=status.input_fill, output_fill=status.output_fill)


# The build and the CUDA offload come from the endpoint row — what the last
# answered <GetInfo/> and HQPlayer's settings said (hqp_library.note_info).
_INSERT_SQL = """
    INSERT INTO hqp_dsp_samples
        (hqp_endpoint_id, hqp_engine, cuda, source, run_id, mode, rate_out, filter, shaper,
         matrix_profile, convolution, adaptive, src_rate, src_bits, src_channels, src_sdm,
         process_speed, input_fill, output_fill, dropout, settled, init_s, settle_s,
         speed_lo, speed_hi)
    SELECT e.id, e.hqp_engine, e.cuda, %(source)s::hqp_sample_source, %(run_id)s,
           %(mode)s, %(rate_out)s, %(filter)s, %(shaper)s,
           %(matrix_profile)s, %(convolution)s, %(adaptive)s,
           %(src_rate)s, %(src_bits)s, %(src_channels)s, %(src_sdm)s,
           %(process_speed)s, %(input_fill)s, %(output_fill)s,
           %(dropout)s, %(settled)s, %(init_s)s, %(settle_s)s,
           %(speed_lo)s, %(speed_hi)s
      FROM hqp_endpoints e
     WHERE e.id = %(endpoint_id)s AND e.hqp_engine IS NOT NULL
    RETURNING id, hqp_engine, cuda::text
"""

_KEY = """hqp_endpoint_id = %(endpoint_id)s AND hqp_engine = %(engine)s
          AND cuda IS NOT DISTINCT FROM %(cuda)s::hqp_cuda AND mode = %(mode)s
          AND matrix_profile IS NOT DISTINCT FROM %(matrix_profile)s
          AND convolution = %(convolution)s"""
# The same context on a benchmark run (`r`), for the points it asked.
_RUN_KEY = """r.hqp_endpoint_id = %(endpoint_id)s AND r.hqp_engine = %(engine)s
          AND r.mode = %(mode)s AND r.cuda IS NOT DISTINCT FROM %(cuda)s::hqp_cuda
          AND r.matrix_profile IS NOT DISTINCT FROM %(matrix_profile)s
          AND r.convolution = %(convolution)s"""


# A ledger row's answer as the readers that show it name it: a slow start
# HQPlayer did not come out of — it went away or was restarted in the build
# (BENCH-GUARD-1 Gap 2) — is "too heavy here"; planning reads `result` alone.
def _shown(p: str) -> str:
    return (f"CASE WHEN {p}.result = 'slow_start' AND {p}.build_end = 'ended' THEN 'too_heavy' "
            f"ELSE {p}.result::text END")


def _lives(p: str, days: str = "%(days)s") -> str:
    """A ledger row (`p`) that still counts: an engine's own answer
    (ENGINE_ANSWERS) for as long as its key — the engine build among it —
    holds, any other within the retention."""
    answers = ", ".join(f"'{a}'" for a in ENGINE_ANSWERS)
    return f"({p}.result IN ({answers}) OR {p}.at >= now() - make_interval(days => {days}))"

_ROLLUP_SQL = f"""
    WITH s AS (
        SELECT process_speed, source, sampled_at, dropout, settled, init_s, settle_s,
               speed_lo, speed_hi
          FROM hqp_dsp_samples
         WHERE {_KEY}
           AND rate_out = %(rate_out)s AND filter = %(filter)s AND shaper = %(shaper)s
           AND src_rate = %(src_rate)s AND src_channels = %(src_channels)s
    ), bench AS (
        SELECT * FROM s WHERE source = 'benchmark' ORDER BY sampled_at DESC LIMIT 1
    )
    INSERT INTO hqp_dsp_speed
        (hqp_endpoint_id, hqp_engine, cuda, mode, rate_out, filter, shaper, matrix_profile,
         convolution, src_rate, src_channels, n, speed_p10, speed_median, last_seen,
         bench_at, bench_settled, bench_init_s, bench_settle_s, bench_lo, bench_hi, dropout)
    SELECT %(endpoint_id)s, %(engine)s, %(cuda)s::hqp_cuda, %(mode)s, %(rate_out)s,
           %(filter)s, %(shaper)s, %(matrix_profile)s, %(convolution)s,
           %(src_rate)s, %(src_channels)s,
           count(*),
           percentile_cont(0.1) WITHIN GROUP (ORDER BY process_speed),
           percentile_cont(0.5) WITHIN GROUP (ORDER BY process_speed),
           max(sampled_at),
           (SELECT sampled_at FROM bench), (SELECT settled FROM bench),
           (SELECT init_s FROM bench), (SELECT settle_s FROM bench),
           (SELECT speed_lo FROM bench), (SELECT speed_hi FROM bench),
           COALESCE((SELECT dropout FROM bench), FALSE)
      FROM s
     WHERE sampled_at >= COALESCE((SELECT sampled_at FROM bench), '-infinity')
    ON CONFLICT ON CONSTRAINT uq_hqp_dsp_speed_key DO UPDATE SET
        n = EXCLUDED.n, speed_p10 = EXCLUDED.speed_p10, speed_median = EXCLUDED.speed_median,
        last_seen = EXCLUDED.last_seen, bench_at = EXCLUDED.bench_at,
        bench_settled = EXCLUDED.bench_settled, bench_init_s = EXCLUDED.bench_init_s,
        bench_settle_s = EXCLUDED.bench_settle_s, bench_lo = EXCLUDED.bench_lo,
        bench_hi = EXCLUDED.bench_hi, dropout = EXCLUDED.dropout
"""


def write(sample: Sample) -> Optional[int]:
    """One sample and its key's rollup, in one transaction; the sample's id.
    None when the endpoint is gone (forgotten) or has no build on record
    yet (no <GetInfo/> answered)."""
    params = asdict(sample)
    with transaction() as cur:
        cur.execute(_INSERT_SQL, params)
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(_ROLLUP_SQL, {**params, "engine": row[1], "cuda": row[2]})
    _prune()
    return row[0]


def _prune() -> None:
    """Samples older than the retention go — at most once a day, from the
    writer: only a new sample can make one due. The rollup keeps what they
    said."""
    global _pruned_at
    now = time.monotonic()
    with _prune_lock:
        if _pruned_at is not None and now - _pruned_at < _PRUNE_EVERY_S:
            return
        _pruned_at = now
    with transaction() as cur:
        cur.execute("DELETE FROM hqp_dsp_samples WHERE sampled_at < now() - make_interval(days => %(d)s)",
                    {"d": RETENTION_DAYS})
        if cur.rowcount:
            logger.info("DSP samples: %d older than %d days removed", cur.rowcount, RETENTION_DAYS)


def context(endpoint: dict, *, mode: str, state: dict) -> Dict[str, Any]:
    """What a key holds besides the setting itself: the endpoint, its build
    and CUDA offload, the mode, the matrix profile and convolution."""
    return {"endpoint_id": endpoint["id"], "engine": endpoint.get("hqp_engine"),
            "cuda": endpoint.get("cuda"), "mode": mode,
            "matrix_profile": state.get("matrix_profile") or None,
            "convolution": bool(state.get("convolution"))}


def covered(ctx: Dict[str, Any]) -> set:
    """The keys that need no benchmark point, for this build and context —
    (rate, filter, shaper, src_rate, src_channels): a benchmark point or
    PASSIVE_COVER listens of what HQPlayer PLAYED within the retention, and
    every point a run ASKED for that ended in an answer (measured, unsettled,
    dropped out within the retention; refused by HQPlayer, never started or
    not started within the build limit on this host for as long as the engine
    is the same — not a point that failed to run): a refused combination, or
    an adaptive rate that plays another one, is never a sample of the key
    that was asked."""
    rows = db_query(f"""
        SELECT rate_out AS rate, filter, shaper, src_rate, src_channels FROM hqp_dsp_speed
         WHERE {_KEY}
           AND last_seen >= now() - make_interval(days => %(days)s)
           AND (bench_at IS NOT NULL OR n >= %(cover)s)
        UNION
        SELECT p.rate_hz, p.filter, p.shaper, p.src_rate, p.src_channels
          FROM hqp_benchmark_points p JOIN hqp_benchmark_runs r ON r.id = p.run_id
         WHERE {_RUN_KEY}
           AND p.result NOT IN ('failed', 'implied')
           AND {_lives("p")}
    """, {**ctx, "days": RETENTION_DAYS, "cover": PASSIVE_COVER})
    return {(r["rate"], r["filter"], r["shaper"], r["src_rate"], r["src_channels"]) for r in rows}


def keeps_up(ctx: Dict[str, Any]) -> Dict[tuple, bool]:
    """Whether each key known in this context keeps up here — at the host's
    "ok" boundary or above, and never one that dropped out. Keyed both by
    what a benchmark point ASKED (its sample: a DSD source plays HQPlayer's
    integrator, "FIR2/XFi", whatever filter was asked) and by what HQPlayer
    PLAYED (the keys covered() counts as measured, listens included — what
    played outweighs what was asked); a point that never started
    (unstarted), or did not start within the run's build limit (slow_start),
    does not keep up. A rung a
    neighbour answered for (implied) is not read here: what answers for it
    is read afresh. The benchmark's ladders climb by it."""
    rows = db_query(f"""
        SELECT 0 AS prio, p.at, p.rate_hz AS rate, p.filter, p.shaper, p.src_rate,
               p.src_channels, (p.result NOT IN ('unstarted', 'slow_start') AND s.process_speed >= %(ok)s
                                AND NOT s.dropout) AS ok
          FROM hqp_benchmark_points p JOIN hqp_benchmark_runs r ON r.id = p.run_id
          LEFT JOIN hqp_dsp_samples s ON s.id = p.sample_id
         WHERE {_RUN_KEY}
           AND (s.id IS NOT NULL OR p.result IN ('unstarted', 'slow_start'))
           AND {_lives("p")}
        UNION ALL
        SELECT 1, last_seen, rate_out, filter, shaper, src_rate, src_channels,
               speed_p10 >= %(ok)s AND NOT coalesce(dropout, FALSE)
          FROM hqp_dsp_speed
         WHERE {_KEY}
           AND last_seen >= now() - make_interval(days => %(days)s)
           AND (bench_at IS NOT NULL OR n >= %(cover)s)
         ORDER BY prio, at
    """, {**ctx, **thresholds(ctx), "days": RETENTION_DAYS, "cover": PASSIVE_COVER})
    return {(r["rate"], r["filter"], r["shaper"], r["src_rate"], r["src_channels"]): r["ok"]
            for r in rows}


def auto_rates(ctx: Dict[str, Any]) -> Dict[tuple, int]:
    """What an automatic output rate played in this context, by the key a
    benchmark point asked there less its rate — (filter, shaper, src_rate,
    src_channels) → the rate its sample played, the newest within the
    retention: a run at Auto plans the rest at those rates."""
    rows = db_query(f"""
        SELECT DISTINCT ON (p.filter, p.shaper, p.src_rate, p.src_channels)
               p.filter, p.shaper, p.src_rate, p.src_channels, s.rate_out
          FROM hqp_benchmark_points p JOIN hqp_benchmark_runs r ON r.id = p.run_id
          JOIN hqp_dsp_samples s ON s.id = p.sample_id
         WHERE {_RUN_KEY} AND p.rate_hz = 0
           AND p.at >= now() - make_interval(days => %(days)s)
         ORDER BY p.filter, p.shaper, p.src_rate, p.src_channels, p.at DESC, p.id DESC
    """, {**ctx, "days": RETENTION_DAYS})
    return {(r["filter"], r["shaper"], r["src_rate"], r["src_channels"]): r["rate_out"] for r in rows}


def thresholds(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """The class boundaries on this HQPlayer, for its build and mode. The
    defaults hold until a benchmark point dropped out there: then "no" starts
    just above the fastest speed one dropped out at — the host's own
    boundary, its buffers and its load spikes — and "tight" keeps the
    default proportion above. Every key whose newest benchmark point dropped
    out within the retention counts, however its run ended — one a
    trial-mode Embedded cut short included: a dropout is recorded only as
    the DSP falling behind, under DROPOUT_CEILING, with HQPlayer still
    answering after it (playback.hqp_benchmark), and a key measured again
    without one no longer counts. The speed is that point's own — the
    rollup's p10 takes in every listen after it, so twenty listens at 2×
    would have moved the boundary off the dropout."""
    row = db_query_one("""
        SELECT max(s.process_speed) AS dropout_speed
          FROM hqp_dsp_speed d
          JOIN hqp_dsp_samples s
            ON s.source = 'benchmark' AND s.sampled_at = d.bench_at
           AND s.hqp_endpoint_id = d.hqp_endpoint_id AND s.hqp_engine = d.hqp_engine
           AND s.cuda IS NOT DISTINCT FROM d.cuda AND s.mode = d.mode
           AND s.rate_out = d.rate_out AND s.filter = d.filter AND s.shaper = d.shaper
           AND s.matrix_profile IS NOT DISTINCT FROM d.matrix_profile
           AND s.convolution = d.convolution
           AND s.src_rate = d.src_rate AND s.src_channels = d.src_channels
         WHERE d.hqp_endpoint_id = %(endpoint_id)s AND d.hqp_engine = %(engine)s
           AND d.mode = %(mode)s
           AND d.dropout AND d.bench_at >= now() - make_interval(days => %(days)s)
    """, {**ctx, "days": RETENTION_DAYS})
    dropout = row["dropout_speed"] if row else None
    no = NO_BELOW if dropout is None else max(NO_BELOW, math.floor(dropout * 20) / 20 + 0.05)
    return {"ok": round(max(OK_AT, no * OK_AT / NO_BELOW), 2), "no": round(no, 2),
            "dropout_speed": dropout}


_CLASS = "CASE WHEN speed_p10 < %(no)s THEN 'no' WHEN speed_p10 < %(ok)s THEN 'tight' ELSE 'ok' END"


# An implied row (`p`, of run `r`) stands while the neighbour that answered
# for it still says so, read as keeps_up reads a key — what HQPlayer played
# there (the rollup, a benchmark point and every listen after it), else its
# newest benchmark answer (a dropout, a point that never started or took
# over the build limit to: "does not keep up") — at today's line (`%(ok)s`), within the
# retention. A neighbour measured again, listens since, or a line a dropout
# moved take the implication back: never a stale class.
_IMPLIED_HOLDS = f"""(p.result <> 'implied' OR p.implied_ok = COALESCE(
        (SELECT n.speed_p10 >= %(ok)s AND NOT coalesce(n.dropout, FALSE)
           FROM hqp_dsp_speed n
          WHERE n.hqp_endpoint_id = r.hqp_endpoint_id AND n.hqp_engine = r.hqp_engine
            AND n.mode = r.mode AND n.cuda IS NOT DISTINCT FROM r.cuda
            AND n.matrix_profile IS NOT DISTINCT FROM r.matrix_profile
            AND n.convolution = r.convolution
            AND n.rate_out = p.implied_from AND n.filter = p.filter AND n.shaper = p.shaper
            AND n.src_rate = p.src_rate AND n.src_channels = p.src_channels
            AND n.last_seen >= now() - make_interval(days => {RETENTION_DAYS})
            AND (n.bench_at IS NOT NULL OR n.n >= {PASSIVE_COVER})),
        (SELECT CASE WHEN q.result IN ('unstarted', 'slow_start') THEN FALSE
                     ELSE s.process_speed >= %(ok)s AND NOT s.dropout END
           FROM hqp_benchmark_points q JOIN hqp_benchmark_runs x ON x.id = q.run_id
           LEFT JOIN hqp_dsp_samples s ON s.id = q.sample_id
          WHERE x.hqp_endpoint_id = r.hqp_endpoint_id AND x.hqp_engine = r.hqp_engine
            AND x.mode = r.mode AND x.cuda IS NOT DISTINCT FROM r.cuda
            AND x.matrix_profile IS NOT DISTINCT FROM r.matrix_profile
            AND x.convolution = r.convolution
            AND q.rate_hz = p.implied_from AND q.filter = p.filter AND q.shaper = p.shaper
            AND q.src_rate = p.src_rate AND q.src_channels = p.src_channels
            AND (s.id IS NOT NULL OR q.result IN ('unstarted', 'slow_start'))
            AND {_lives("q", str(RETENTION_DAYS))}
          ORDER BY q.at DESC, q.id DESC LIMIT 1)))"""


def implied(ctx: Dict[str, Any]) -> Dict[tuple, Tuple[int, bool]]:
    """The rungs a neighbour answered for in this context, by key — the newest
    such row within the retention: (that neighbour's rate, its answer). A run
    writes a rung's row again only when what answers for it changed."""
    rows = db_query(f"""
        SELECT DISTINCT ON (p.rate_hz, p.filter, p.shaper, p.src_rate, p.src_channels)
               p.rate_hz, p.filter, p.shaper, p.src_rate, p.src_channels, p.implied_from,
               p.implied_ok
          FROM hqp_benchmark_points p JOIN hqp_benchmark_runs r ON r.id = p.run_id
         WHERE {_RUN_KEY} AND p.result = 'implied'
           AND p.at >= now() - make_interval(days => %(days)s)
         ORDER BY p.rate_hz, p.filter, p.shaper, p.src_rate, p.src_channels, p.at DESC, p.id DESC
    """, {**ctx, "days": RETENTION_DAYS})
    return {(r["rate_hz"], r["filter"], r["shaper"], r["src_rate"], r["src_channels"]):
            (r["implied_from"], r["implied_ok"]) for r in rows}


# An engine's build as PostgreSQL compares it — "6.3.3" → {6,3,3} — NULL for
# one that starts with no number (build() in SQL)
_BUILD = r"string_to_array(substring({col} from '^[0-9]+(?:\.[0-9]+)*'), '.')::int[]"


def headroom(ctx: Dict[str, Any], *, rate_out: int, filter: str, shaper: str,
             src_rate: int, src_channels: int, asked_rate: Optional[int] = None) -> Dict[str, Any]:
    """How each entry of the three pickers runs here, next to the current
    setting: every filter at the current shaper and rate, every shaper at
    the current filter and rate, every rate at the current filter and shaper
    — for the source the owner hears. A key never measured is absent:
    unmarked, never guessed — unless a benchmark's ladder answered for it
    (`implied`: its class, no speed, the rate that answers for it). Beside
    them, for a key this engine has not measured, the newest earlier
    engine's mark in the same context (`earlier`): a hint, never a class."""
    th = thresholds(ctx)
    rows = db_query(f"""
        SELECT filter, shaper, rate_out, n, speed_p10, bench_settled, bench_lo, bench_hi,
               {_CLASS} AS class
          FROM hqp_dsp_speed
         WHERE {_KEY}
           AND src_rate = %(src_rate)s AND src_channels = %(src_channels)s
           AND ((rate_out = %(rate_out)s AND (shaper = %(shaper)s OR filter = %(filter)s))
                OR (filter = %(filter)s AND shaper = %(shaper)s))
    """, {**ctx, **th, "rate_out": rate_out, "filter": filter, "shaper": shaper,
          "src_rate": src_rate, "src_channels": src_channels})
    out: Dict[str, Any] = {"filters": {}, "shapers": {}, "rates": {}, "thresholds": th,
                           "source": {"rate": src_rate, "channels": src_channels}}
    for r in rows:
        entry = {"class": r["class"], "speed": round(r["speed_p10"], 2), "n": r["n"],
                 "settled": r["bench_settled"] is not False,
                 "lo": None if r["bench_lo"] is None else round(r["bench_lo"], 2),
                 "hi": None if r["bench_hi"] is None else round(r["bench_hi"], 2)}
        if r["rate_out"] == rate_out and r["shaper"] == shaper:
            out["filters"][r["filter"]] = entry
        if r["rate_out"] == rate_out and r["filter"] == filter:
            out["shapers"][r["shaper"]] = entry
        if r["filter"] == filter and r["shaper"] == shaper:
            out["rates"][str(r["rate_out"])] = entry
    # What a benchmark ASKED at a picker entry's key with no sample there:
    # HQPlayer refused it, never got it going, did not start it within the
    # build limit, or ended in that build ("refused" | "unstarted" |
    # "slow_start" | "too_heavy") — no speed, so no mark, yet
    # measured all the same: a row says so instead of offering a run that
    # ends alike — or a neighbour on its ladder answered for it ("implied":
    # a mark of its class, no speed). Its newest answer there within the
    # retention, a sample of the key aside. A point is keyed by the rate it
    # asked — the owner's rate slot, 0 at an automatic one (`asked_rate`) — a
    # mark by the rate that plays.
    asked = db_query(f"""
        SELECT * FROM (
            SELECT DISTINCT ON (p.rate_hz, p.filter, p.shaper)
                   p.rate_hz, p.filter, p.shaper, {_shown("p")} AS result, p.implied_from,
                   p.implied_ok, p.at, p.id, {_IMPLIED_HOLDS} AS holds
              FROM hqp_benchmark_points p JOIN hqp_benchmark_runs r ON r.id = p.run_id
             WHERE {_RUN_KEY} AND p.result <> 'failed'
               AND p.src_rate = %(src_rate)s AND p.src_channels = %(src_channels)s
               AND ((p.rate_hz IN (%(rate_out)s, %(asked)s)
                     AND (p.shaper = %(shaper)s OR p.filter = %(filter)s))
                    OR (p.filter = %(filter)s AND p.shaper = %(shaper)s))
               AND {_lives("p")}
             ORDER BY p.rate_hz, p.filter, p.shaper, p.at DESC, p.id DESC) newest
         WHERE result IN ('refused', 'unstarted', 'slow_start', 'too_heavy', 'implied') AND holds
         ORDER BY at DESC, id DESC
    """, {**ctx, **th, "rate_out": rate_out, "filter": filter, "shaper": shaper,
          "src_rate": src_rate, "src_channels": src_channels, "days": RETENTION_DAYS,
          "asked": rate_out if asked_rate is None else asked_rate})
    out["refused"] = {"filters": {}, "shapers": {}}
    for r in asked:
        here = r["rate_hz"] in (rate_out, asked_rate if asked_rate is not None else rate_out)
        if r["result"] == "implied":
            mark = {"class": "ok" if r["implied_ok"] else "no", "speed": None, "n": 0,
                    "settled": True, "lo": None, "hi": None, "implied_from": r["implied_from"]}
            if here and r["shaper"] == shaper:
                out["filters"].setdefault(r["filter"], mark)
            if here and r["filter"] == filter:
                out["shapers"].setdefault(r["shaper"], mark)
            if r["filter"] == filter and r["shaper"] == shaper:
                out["rates"].setdefault(str(r["rate_hz"]), mark)
            continue
        if not here:
            continue
        if r["shaper"] == shaper:
            out["refused"]["filters"].setdefault(r["filter"], r["result"])
        if r["filter"] == filter:
            out["refused"]["shapers"].setdefault(r["shaper"], r["result"])
    for knob in ("filters", "shapers"):
        for name in list(out["refused"][knob]):
            if name in out[knob]:
                del out["refused"][knob][name]      # measured: its mark says it
    earlier = db_query(f"""
        SELECT DISTINCT ON (filter, shaper) filter, shaper, hqp_engine AS engine,
               round(speed_p10::numeric, 2)::float AS speed
          FROM hqp_dsp_speed
         WHERE hqp_endpoint_id = %(endpoint_id)s
           AND {_BUILD.format(col="hqp_engine")} < {_BUILD.format(col="%(engine)s")}
           AND cuda IS NOT DISTINCT FROM %(cuda)s::hqp_cuda AND mode = %(mode)s
           AND matrix_profile IS NOT DISTINCT FROM %(matrix_profile)s
           AND convolution = %(convolution)s
           AND src_rate = %(src_rate)s AND src_channels = %(src_channels)s
           AND rate_out = %(rate_out)s AND (shaper = %(shaper)s OR filter = %(filter)s)
         ORDER BY filter, shaper, {_BUILD.format(col="hqp_engine")} DESC
    """, {**ctx, "rate_out": rate_out, "filter": filter, "shaper": shaper,
          "src_rate": src_rate, "src_channels": src_channels})
    out["earlier"] = {"filters": {}, "shapers": {}}
    for r in earlier:
        hint = {"engine": r["engine"], "speed": r["speed"]}
        if r["shaper"] == shaper and r["filter"] not in out["filters"]:
            out["earlier"]["filters"][r["filter"]] = hint
        if r["filter"] == filter and r["shaper"] not in out["shapers"]:
            out["earlier"]["shapers"][r["shaper"]] = hint
    return out


# Every key measured in a context, classed at its thresholds, and every
# combination a run asked for that HQPlayer refused, never got going or
# did not start within the build limit (class 'refused' / 'unstarted' /
# 'slow_start', no speed; how long a slow start took once HQPlayer answered:
# `build_s`) or ended HQPlayer in that build ('too_heavy'), or that a
# neighbour on its ladder answered for
# (its class, no speed, the rate that answered: `implied_from`) — the newest
# answer — unordered.
_RESULTS_SQL = f"""
        SELECT filter, shaper, rate_out, src_rate, src_channels, n,
               round(speed_p10::numeric, 2)::float AS speed, {_CLASS} AS class,
               bench_settled, round(bench_init_s::numeric, 1)::float AS init_s, dropout,
               round(bench_lo::numeric, 2)::float AS lo, round(bench_hi::numeric, 2)::float AS hi,
               NULL::integer AS implied_from, NULL::float AS build_s
          FROM hqp_dsp_speed
         WHERE {_KEY}
        UNION ALL
        SELECT filter, shaper, rate_hz, src_rate, src_channels, 0, NULL::float,
               CASE WHEN result <> 'implied' THEN result WHEN implied_ok THEN 'ok' ELSE 'no' END,
               NULL::boolean, NULL::float, FALSE, NULL::float, NULL::float, implied_from, build_s
          FROM (SELECT DISTINCT ON (p.rate_hz, p.filter, p.shaper, p.src_rate, p.src_channels)
                       p.filter, p.shaper, p.rate_hz, p.src_rate, p.src_channels,
                       {_shown("p")} AS result, p.implied_from, p.implied_ok, p.build_s,
                       {_IMPLIED_HOLDS} AS holds
                  FROM hqp_benchmark_points p JOIN hqp_benchmark_runs r ON r.id = p.run_id
                 WHERE p.result IN ('refused', 'unstarted', 'slow_start', 'implied') AND {_RUN_KEY}
                   AND NOT EXISTS (
                       SELECT 1 FROM hqp_dsp_speed s
                        WHERE s.hqp_endpoint_id = r.hqp_endpoint_id AND s.hqp_engine = r.hqp_engine
                          AND s.mode = r.mode AND s.cuda IS NOT DISTINCT FROM r.cuda
                          AND s.matrix_profile IS NOT DISTINCT FROM r.matrix_profile
                          AND s.convolution = r.convolution AND s.rate_out = p.rate_hz
                          AND s.filter = p.filter AND s.shaper = p.shaper
                          AND s.src_rate = p.src_rate AND s.src_channels = p.src_channels)
                 ORDER BY p.rate_hz, p.filter, p.shaper, p.src_rate, p.src_channels,
                          p.at DESC) asked
         WHERE holds
"""

# The most rows of one name the assistant is told (hqplayer_host_fit)
FIT_ROWS = 24

# A build silent this long is long on any host — the Benchmark's own line
# (hqp_benchmark.LONG_BUILD_S); a host whose Benchmark measured a longer
# start within a rate family takes that as its usual build (build_facts)
LONG_BUILD_FLOOR_S = 20.0


def _same_family(a: str, b: str) -> str:
    """hqp_rate_check.same_family in SQL: the two rates a whole power of two apart."""
    hi, lo = f"greatest({a}, {b})", f"least({a}, {b})"
    return f"({lo} > 0 AND mod({hi}, {lo}) = 0 AND (({hi} / {lo}) & ({hi} / {lo} - 1)) = 0)"


def build_facts(endpoint_id: int, *, rate_out: Optional[int] = None, filter: Optional[str] = None,
                shaper: Optional[str] = None, src_rate: Optional[int] = None) -> Dict[str, Any]:
    """What this HQPlayer's Benchmark measured of its builds, for the word
    said while HQPlayer builds in silence (BENCH-GUARD-1): its usual build —
    the longest start measured within a rate family, LONG_BUILD_FLOOR_S at
    least (`measured`: whether a start was measured at all) — and across
    families (a point asked at an automatic rate is no such point) the longest start measured, the longest a
    slow start took once HQPlayer answered (`built_s`), how many slow starts
    there were and whether one ended HQPlayer; and, for a setting named
    (its output rate, the filter running for `src_rate`, its shaper or none
    for any), its own answer here: a start measured, a slow start and how
    long it took, or an end. On the engine build and CUDA state the endpoint
    holds now, every mode."""
    within, across = _same_family("s.rate_out", "s.src_rate"), _same_family("p.rate_hz", "p.src_rate")
    row = db_query_one(f"""
        WITH e AS (SELECT id, hqp_engine, cuda FROM hqp_endpoints WHERE id = %(e)s),
        s AS (
            SELECT s.init_s, s.rate_out, s.src_rate, s.filter, s.shaper
              FROM hqp_dsp_samples s JOIN e ON e.id = s.hqp_endpoint_id AND e.hqp_engine = s.hqp_engine
                                           AND e.cuda IS NOT DISTINCT FROM s.cuda
             WHERE s.source = 'benchmark' AND s.init_s IS NOT NULL),
        p AS (
            SELECT p.build_end, p.build_s, p.rate_hz, p.src_rate, p.filter, p.shaper, p.at
              FROM hqp_benchmark_points p
              JOIN hqp_benchmark_runs r ON r.id = p.run_id
              JOIN e ON e.id = r.hqp_endpoint_id AND e.hqp_engine = r.hqp_engine
                    AND e.cuda IS NOT DISTINCT FROM r.cuda
             WHERE p.result = 'slow_start')
        SELECT (SELECT max(init_s) FROM s WHERE {within}) AS within_s,
               (SELECT max(init_s) FROM s WHERE NOT {within}) AS across_s,
               (SELECT max(build_s) FROM p WHERE build_end = 'answered' AND p.rate_hz > 0
                   AND NOT {across}) AS built_s,
               (SELECT count(*) FROM p WHERE p.rate_hz > 0 AND NOT {across}) AS slow,
               (SELECT coalesce(bool_or(build_end = 'ended'), FALSE) FROM p
                 WHERE p.rate_hz > 0 AND NOT {across}) AS ended,
               (SELECT max(init_s) FROM s
                 WHERE s.rate_out = %(rate)s AND s.filter = %(filter)s AND s.src_rate = %(src)s
                   AND (%(shaper)s::text IS NULL OR s.shaper = %(shaper)s)) AS own_s,
               (SELECT row_to_json(x) FROM (
                    SELECT build_end, build_s FROM p
                     WHERE p.rate_hz = %(rate)s AND p.filter = %(filter)s AND p.src_rate = %(src)s
                       AND (%(shaper)s::text IS NULL OR p.shaper = %(shaper)s)
                     ORDER BY (build_end = 'ended') DESC NULLS LAST, p.at DESC LIMIT 1) x) AS own_slow
    """, {"e": endpoint_id, "rate": rate_out, "filter": filter, "shaper": shaper, "src": src_rate})
    own = None
    if row["own_slow"] is not None:
        own = {"slow_start": True, "ended": row["own_slow"]["build_end"] == "ended",
               "built_s": row["own_slow"]["build_s"]}
    elif row["own_s"] is not None:
        own = {"start_s": round(row["own_s"], 1)}
    return {"usual_s": round(max(LONG_BUILD_FLOOR_S, row["within_s"] or 0.0), 1),
            "measured": row["within_s"] is not None,
            "across": {"start_s": None if row["across_s"] is None else round(row["across_s"], 1),
                       "built_s": row["built_s"], "slow": row["slow"], "ended": row["ended"]},
            "own": own}


def results(ctx: Dict[str, Any]) -> list:
    """Every key measured in this context, classed, and every combination a
    run asked for that HQPlayer refused, never got going or did not start
    here within the build limit (class 'refused' / 'unstarted' / 'slow_start',
    no speed), or ended HQPlayer in that build ('too_heavy'), or
    that a neighbour answered for (its class, no speed; the newest answer) —
    the benchmark's results sheet lays them out per axis."""
    return db_query(f"{_RESULTS_SQL} ORDER BY src_rate, rate_out, shaper, filter", {**ctx, **thresholds(ctx)})


def fit(ctx: Dict[str, Any], *, filter: Optional[str] = None, shaper: Optional[str] = None,
        rate_out: Optional[int] = None, limit: int = FIT_ROWS) -> Dict[str, Any]:
    """What one filter (`filter`) or one shaper (`shaper`) costs on this
    HQPlayer in this context — results' rows of it, at `rate_out` when
    given — for the assistant: ordered by labels, never by a speed (the
    source's rate and channels, the output rate, the other knob), the first
    `limit` of them, how many there are, and the thresholds they are
    classed at."""
    th = thresholds(ctx)
    rows = db_query(f"""
        SELECT r.*, count(*) OVER () AS total
          FROM ({_RESULTS_SQL}) r
         WHERE (%(f)s::text IS NULL OR r.filter = %(f)s)
           AND (%(s)s::text IS NULL OR r.shaper = %(s)s)
           AND (%(r)s::int IS NULL OR r.rate_out = %(r)s)
         ORDER BY r.src_rate, r.src_channels, r.rate_out, r.{"shaper" if filter is not None else "filter"}
         LIMIT %(limit)s
    """, {**ctx, **th, "f": filter, "s": shaper, "r": rate_out, "limit": limit})
    return {"thresholds": th, "rows": rows, "total": rows[0]["total"] if rows else 0}


def names(ctx: Dict[str, Any]) -> Dict[str, list]:
    """The filters, the shapers and the output rates this context holds a key
    of, or a combination a run asked for that HQPlayer refused, never got
    going or did not start within the build limit, or that a neighbour
    answered for — every
    name and rate fit() finds rows of; the rates in order."""
    row = db_query_one(f"""
        SELECT coalesce(array_agg(DISTINCT filter), '{{}}') AS filters,
               coalesce(array_agg(DISTINCT shaper), '{{}}') AS shapers,
               coalesce(array_agg(DISTINCT rate_out ORDER BY rate_out), '{{}}') AS rates
          FROM (SELECT filter, shaper, rate_out FROM hqp_dsp_speed WHERE {_KEY}
                UNION ALL
                SELECT p.filter, p.shaper, p.rate_hz
                  FROM hqp_benchmark_points p JOIN hqp_benchmark_runs r ON r.id = p.run_id
                 WHERE p.result IN ('refused', 'unstarted', 'slow_start', 'implied') AND {_RUN_KEY}) k
    """, ctx)
    return {"filters": row["filters"], "shapers": row["shapers"], "rates": row["rates"]}


def file_source(opener: dict) -> Optional[Tuple[int, int]]:
    """(sample rate, channels) of the file a queue slot opens — what
    HQPlayer will decode when it plays it — from the row the scan or the
    library sync wrote. None for a stream: its format is known once it plays."""
    kind = opener.get("kind")
    if kind == "file":
        row = db_query_one("""
            SELECT sample_rate, channels FROM media_files
             WHERE file_path = %(p)s AND sample_rate > 0 AND channels > 0 LIMIT 1
        """, {"p": opener.get("path")})
    elif kind == "hqp":
        row = db_query_one("""
            SELECT f.sample_rate, f.channels
              FROM hqp_library_files f JOIN album_variants av ON av.id = f.album_variant_id
             WHERE av.hqp_endpoint_id = %(e)s AND f.hqp_path = %(p)s
               AND f.sample_rate > 0 AND f.channels > 0 LIMIT 1
        """, {"e": opener.get("endpoint"), "p": opener.get("path")})
    else:
        return None
    return (row["sample_rate"], row["channels"]) if row else None


# What a queue slot opens when no file row names it: a stream, whose rate is
# known once it plays (playback.queue.QueueItem's kinds).
_STREAMED = frozenset({"proxy", "uri", "track", "pending"})


def sources(openers: List[dict]) -> Sources:
    """What HQPlayer will decode from these queue slots, the first being the
    one it reads — their source rates for the rate check, in one query: a
    file's from the row the scan or the library sync wrote (a CUE image's
    every slice its own slot), a DSD file counted apart — DSF, DFF, or one
    bit a sample (WavPack holds DSD too) — the first slot's own rate kept,
    and so is a stream. A file whose row gives no rate, and a slot nothing
    can serve, are in none of them."""
    files = [(n, o) for n, o in enumerate(openers, 1) if o.get("kind") in ("file", "hqp")]
    rows = db_query("""
        SELECT r.sample_rate AS rate, r.dsd, count(*) AS slots, bool_or(s.n = 1) AS first
          FROM unnest(%(ns)s::int[], %(kinds)s::text[], %(paths)s::text[], %(endpoints)s::int[])
               AS s(n, kind, path, endpoint)
         CROSS JOIN LATERAL (
               SELECT mf.sample_rate, coalesce(mf.file_format IN ('DSF', 'DFF') OR mf.bit_depth = 1, false) AS dsd
                 FROM media_files mf
                WHERE s.kind = 'file' AND mf.file_path = s.path AND mf.sample_rate > 0
               UNION ALL
               SELECT f.sample_rate, coalesce(f.file_format IN ('DSF', 'DFF') OR f.bit_depth = 1, false)
                 FROM hqp_library_files f JOIN album_variants av ON av.id = f.album_variant_id
                WHERE s.kind = 'hqp' AND av.hqp_endpoint_id = s.endpoint AND f.hqp_path = s.path
                  AND f.sample_rate > 0
                LIMIT 1) r
         GROUP BY r.sample_rate, r.dsd
    """, {"ns": [n for n, _ in files], "kinds": [o["kind"] for _, o in files],
          "paths": [o.get("path") for _, o in files],
          "endpoints": [o.get("endpoint") for _, o in files]}) if files else []
    pcm: Dict[int, int] = {}
    dsd, first, first_dsd = 0, None, None
    for r in rows:
        if r["dsd"]:
            dsd += r["slots"]
            first_dsd = r["rate"] if r["first"] else first_dsd
        else:
            pcm[r["rate"]] = r["slots"]
            first = r["rate"] if r["first"] else first
    return Sources(pcm, dsd, sum(1 for o in openers if o.get("kind") in _STREAMED), first, first_dsd)
