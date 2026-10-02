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
Samples live RETENTION_DAYS; the rollup keeps what they said.
"""

import logging
import math
import re
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Tuple

from db_pool import db_query, db_query_one, transaction
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


def build(engine: Optional[str]) -> Tuple[int, ...]:
    """<GetInfo engine="6.2.3"/> as a comparable tuple."""
    return tuple(int(x) for x in re.findall(r"\d+", engine or "")[:3])


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

    def due(self, status, *, ours: bool, engine: Optional[str], now: float) -> bool:
        """HQPlayer plays a slot of ours (not another controller's playlist),
        reports its speed and the source it decodes; 15 s into the track and
        15 s past the last change of the setting or the source; then on a
        change or once a minute. Never 5.17.0 with a DSD source."""
        if status.state != PlaybackState.PLAYING or not ours:
            self._key = None    # a stop, a pause, a foreign playlist: the next play starts over
            return False
        key = (status.active_mode, status.active_rate, status.active_filter,
               status.active_shaper, status.src_rate, status.src_channels)
        if key != self._key:
            self._key, self._since = key, now
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
         process_speed, input_fill, output_fill, dropout, settled, init_s, settle_s)
    SELECT e.id, e.hqp_engine, e.cuda, %(source)s::hqp_sample_source, %(run_id)s,
           %(mode)s, %(rate_out)s, %(filter)s, %(shaper)s,
           %(matrix_profile)s, %(convolution)s, %(adaptive)s,
           %(src_rate)s, %(src_bits)s, %(src_channels)s, %(src_sdm)s,
           %(process_speed)s, %(input_fill)s, %(output_fill)s,
           %(dropout)s, %(settled)s, %(init_s)s, %(settle_s)s
      FROM hqp_endpoints e
     WHERE e.id = %(endpoint_id)s AND e.hqp_engine IS NOT NULL
    RETURNING id, hqp_engine, cuda::text
"""

_KEY = """hqp_endpoint_id = %(endpoint_id)s AND hqp_engine = %(engine)s
          AND cuda IS NOT DISTINCT FROM %(cuda)s::hqp_cuda AND mode = %(mode)s
          AND matrix_profile IS NOT DISTINCT FROM %(matrix_profile)s
          AND convolution = %(convolution)s"""

_ROLLUP_SQL = f"""
    WITH s AS (
        SELECT process_speed, source, sampled_at, dropout, settled, init_s, settle_s
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
         bench_at, bench_settled, bench_init_s, bench_settle_s, dropout)
    SELECT %(endpoint_id)s, %(engine)s, %(cuda)s::hqp_cuda, %(mode)s, %(rate_out)s,
           %(filter)s, %(shaper)s, %(matrix_profile)s, %(convolution)s,
           %(src_rate)s, %(src_channels)s,
           count(*),
           percentile_cont(0.1) WITHIN GROUP (ORDER BY process_speed),
           percentile_cont(0.5) WITHIN GROUP (ORDER BY process_speed),
           max(sampled_at),
           (SELECT sampled_at FROM bench), (SELECT settled FROM bench),
           (SELECT init_s FROM bench), (SELECT settle_s FROM bench),
           COALESCE((SELECT dropout FROM bench), FALSE)
      FROM s
     WHERE sampled_at >= COALESCE((SELECT sampled_at FROM bench), '-infinity')
    ON CONFLICT ON CONSTRAINT uq_hqp_dsp_speed_key DO UPDATE SET
        n = EXCLUDED.n, speed_p10 = EXCLUDED.speed_p10, speed_median = EXCLUDED.speed_median,
        last_seen = EXCLUDED.last_seen, bench_at = EXCLUDED.bench_at,
        bench_settled = EXCLUDED.bench_settled, bench_init_s = EXCLUDED.bench_init_s,
        bench_settle_s = EXCLUDED.bench_settle_s, dropout = EXCLUDED.dropout
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
    """The keys that need no benchmark point, within the retention and for
    this build and context — (rate, filter, shaper, src_rate, src_channels):
    a benchmark point or PASSIVE_COVER listens of what HQPlayer PLAYED, and
    every point a run ASKED for that ended in an answer (measured, unsettled,
    dropped out, refused by HQPlayer — not a point that failed to run): a
    refused combination, or an adaptive rate that plays another one, is
    never a sample of the key that was asked."""
    rows = db_query(f"""
        SELECT rate_out AS rate, filter, shaper, src_rate, src_channels FROM hqp_dsp_speed
         WHERE {_KEY}
           AND last_seen >= now() - make_interval(days => %(days)s)
           AND (bench_at IS NOT NULL OR n >= %(cover)s)
        UNION
        SELECT p.rate_hz, p.filter, p.shaper, p.src_rate, p.src_channels
          FROM hqp_benchmark_points p JOIN hqp_benchmark_runs r ON r.id = p.run_id
         WHERE r.hqp_endpoint_id = %(endpoint_id)s AND r.hqp_engine = %(engine)s
           AND r.mode = %(mode)s AND r.cuda IS NOT DISTINCT FROM %(cuda)s::hqp_cuda
           AND r.matrix_profile IS NOT DISTINCT FROM %(matrix_profile)s
           AND r.convolution = %(convolution)s
           AND p.result <> 'failed' AND p.at >= now() - make_interval(days => %(days)s)
    """, {**ctx, "days": RETENTION_DAYS, "cover": PASSIVE_COVER})
    return {(r["rate"], r["filter"], r["shaper"], r["src_rate"], r["src_channels"]) for r in rows}


def thresholds(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """The class boundaries on this HQPlayer, for its build and mode. The
    defaults hold until a benchmark point dropped out there: then "no" starts
    just above the fastest speed one dropped out at — the host's own
    boundary, its buffers and its load spikes — and "tight" keeps the
    default proportion above. Only a run that ran to its end or was
    cancelled says it (a failed one lost its connection), and only a
    dropout under DROPOUT_CEILING was the DSP (runs record that already)."""
    row = db_query_one("""
        SELECT dropout_speed FROM hqp_benchmark_runs
         WHERE hqp_endpoint_id = %(endpoint_id)s AND hqp_engine = %(engine)s AND mode = %(mode)s
           AND outcome IN ('done', 'cancelled') AND dropout_speed IS NOT NULL
         ORDER BY started_at DESC LIMIT 1
    """, ctx)
    dropout = row["dropout_speed"] if row else None
    no = NO_BELOW if dropout is None else max(NO_BELOW, math.floor(dropout * 20) / 20 + 0.05)
    return {"ok": round(max(OK_AT, no * OK_AT / NO_BELOW), 2), "no": round(no, 2),
            "dropout_speed": dropout}


_CLASS = "CASE WHEN speed_p10 < %(no)s THEN 'no' WHEN speed_p10 < %(ok)s THEN 'tight' ELSE 'ok' END"


def headroom(ctx: Dict[str, Any], *, rate_out: int, filter: str, shaper: str,
             src_rate: int, src_channels: int) -> Dict[str, Any]:
    """How each entry of the three pickers runs here, next to the current
    setting: every filter at the current shaper and rate, every shaper at
    the current filter and rate, every rate at the current filter and shaper
    — for the source the owner hears. A key never measured is absent:
    unmarked, never guessed."""
    th = thresholds(ctx)
    rows = db_query(f"""
        SELECT filter, shaper, rate_out, n, speed_p10, bench_settled, {_CLASS} AS class
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
                 "settled": r["bench_settled"] is not False}
        if r["rate_out"] == rate_out and r["shaper"] == shaper:
            out["filters"][r["filter"]] = entry
        if r["rate_out"] == rate_out and r["filter"] == filter:
            out["shapers"][r["shaper"]] = entry
        if r["filter"] == filter and r["shaper"] == shaper:
            out["rates"][str(r["rate_out"])] = entry
    return out


def results(ctx: Dict[str, Any]) -> list:
    """Every key measured in this context, classed, and every combination a
    run asked for and HQPlayer refused (class 'refused', no speed) — the
    benchmark's results sheet lays them out per axis."""
    th = thresholds(ctx)
    return db_query(f"""
        SELECT filter, shaper, rate_out, src_rate, src_channels, n,
               round(speed_p10::numeric, 2)::float AS speed, {_CLASS} AS class,
               bench_settled, round(bench_init_s::numeric, 1)::float AS init_s, dropout
          FROM hqp_dsp_speed
         WHERE {_KEY}
        UNION ALL
        SELECT DISTINCT ON (p.rate_hz, p.filter, p.shaper, p.src_rate, p.src_channels)
               p.filter, p.shaper, p.rate_hz, p.src_rate, p.src_channels, 0,
               NULL::float, 'refused', NULL::boolean, NULL::float, FALSE
          FROM hqp_benchmark_points p JOIN hqp_benchmark_runs r ON r.id = p.run_id
         WHERE p.result = 'refused' AND r.hqp_endpoint_id = %(endpoint_id)s
           AND r.hqp_engine = %(engine)s AND r.mode = %(mode)s
           AND r.cuda IS NOT DISTINCT FROM %(cuda)s::hqp_cuda
           AND r.matrix_profile IS NOT DISTINCT FROM %(matrix_profile)s
           AND r.convolution = %(convolution)s
           AND NOT EXISTS (
               SELECT 1 FROM hqp_dsp_speed s
                WHERE s.hqp_endpoint_id = r.hqp_endpoint_id AND s.hqp_engine = r.hqp_engine
                  AND s.mode = r.mode AND s.cuda IS NOT DISTINCT FROM r.cuda
                  AND s.matrix_profile IS NOT DISTINCT FROM r.matrix_profile
                  AND s.convolution = r.convolution AND s.rate_out = p.rate_hz
                  AND s.filter = p.filter AND s.shaper = p.shaper
                  AND s.src_rate = p.src_rate AND s.src_channels = p.src_channels)
        ORDER BY src_rate, rate_out, shaper, filter
    """, {**ctx, **th})


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
