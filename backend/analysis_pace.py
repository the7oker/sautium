"""What the analysis passes cost on this machine, and the instrument pass's
estimate.

A node analyses its files in two passes, each one decode a track: the sonic
pass (the fingerprint, the segment grid, CLAP, librosa — embeddings.py) and
the instrument pass (the AST+PaSST tagger — audio_analysis.py), the second
behind the owner's switch (enrichment.instruments). The tagger is most of the
analysis on a CPU, so the Library screen says beside the switch what it would
cost here.

The pace is the wall seconds a pass spends per minute of audio on this
machine — its device and its core count, so a GPU added is another machine —
kept as the mean over the batches the pass finished here, each weighed by
its audio, forgetting what lies past the last ten hours of audio
(WINDOW_AUDIO_SECONDS). Each pass records after every batch, committed with
the batch; a seam's wait for another pass, a model's load and a batch whose
decode failed are no part of it.

The estimate is the instrument pass's pending audio (its own queue,
INSTRUMENTS_PENDING_SQL) at this machine's instrument pace. Until the pass
has run a batch here, the sonic pass's pace times the tagger-to-sonic ratio
stands in: on a CPU by the tagger's torch threads, between the two machines
it was measured on (CPU_RATIO_POINTS), and on CUDA (a laptop RTX 4090, where
the GPU's tagger is cheaper than the sonic pass's CPU work); a CPU node that
has run neither pass takes the tagger's own measured pace at its threads
(CPU_TAGGER_PACE_POINTS). On MPS nothing was measured, nor on a CUDA node
before its first sonic batch, so the row says it is measuring until a batch
records a pace. A track longer than this machine can hold now
(longest_track_seconds) is in no queue and in no estimate; the estimate
counts such tracks apart. Nothing here loads a model or imports the ML
stack: the screen asks on every library event, a lite Pi included.
"""

import math
import os
from typing import Any, Dict, Optional

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

import provenance

SONIC = "sonic"
INSTRUMENTS = "instruments"

# The tagger's pace against the sonic pass's, per device class where it was
# measured. On a CPU it falls with the torch threads the tagger gets, and two
# points were measured (s per minute of audio):
#   - a Raspberry Pi 5, four cores, 2 torch threads: 18.9 / 2.2 = 8.5;
#   - a 32-core x86 CPU on the lite profile, 16 torch threads: 2.89 / 1.09 =
#     2.66 (the synthetic lab library, 2026-11-11).
# Between them the ratio is linear in the log of the threads, and held at the
# nearer point outside them; a measurement on the Pi through the two passes
# (to come) refines both. On a CPU node with the instruments off no
# instrument batch ever replaces this figure, so it is the line the owner
# decides by. CUDA (2026-11-11): a laptop RTX 4090 with 32 cores on the same
# library, 0.15-0.19 / 0.50-0.82 s per minute (the sonic pass is librosa,
# decoding and fpcalc on the CPU) — 0.2 to 0.3, the warm runs 0.3.
CPU_RATIO_POINTS = ((2, 8.5), (16, 2.66))
CUDA_RATIO = 0.3

# The tagger's own pace on a CPU at the same two points (s per minute of
# audio), the estimate before this machine has run either pass: interpolated
# by the tagger's threads as the ratio is — the threads already say how much
# of the machine the tagger takes, so the cores scale nothing further.
CPU_TAGGER_PACE_POINTS = ((2, 18.9), (16, 2.89))

# A file of unknown length counts as five minutes, as the passes' batches do.
UNKNOWN_SECONDS = 300.0

# The instrument pass's queue, from the analysis-source files: no
# instruments record yet, a record linked to no source, or one made from
# material other than this file's group holds at its rank (provenance.
# file_in_group_sql — a better rip elected, a stream's record awaiting the
# owned file) — an imported record only under enrichment.reanalyze_imported,
# as the sonic pass's records. Material under one grid window is never
# queued (provenance.MIN_MATERIAL_SECONDS), nor a file of a copy that is
# away (album_variants.absent_since). Kept here, not with the pass, so the
# estimate reads it without the ML stack.
INSTRUMENTS_ROWS_SQL = """
    SELECT mf.track_id, mf.id AS media_file_id, mf.file_path, mf.bit_depth,
           mf.sample_rate, mf.is_lossless, mf.duration_seconds,
           mf.cue_start_seconds, mf.cue_end_seconds, mf.file_modified_at,
           ti.id IS NULL AS gap
      FROM media_files mf
      JOIN album_variants av ON av.id = mf.album_variant_id
      LEFT JOIN track_instruments ti ON ti.track_id = mf.track_id
      LEFT JOIN analysis_sources asrc ON asrc.id = ti.analysis_source_id
     WHERE mf.is_analysis_source AND av.absent_since IS NULL
       AND (mf.duration_seconds IS NULL OR mf.duration_seconds >= :min_seconds)
"""

INSTRUMENTS_PENDING_SQL = INSTRUMENTS_ROWS_SQL + f"""
       AND (ti.id IS NULL
            OR ti.analysis_source_id IS NULL
            OR (NOT {provenance.file_in_group_sql()}
                AND (:reanalyze OR NOT asrc.imported)))
"""


# The running mean forgets: past this much audio (ten hours) the sums are
# scaled down before a batch is added, so the pace follows the machine as it is
# now — a card replaced at the same core count, a backup restored on another
# machine of the same shape — rather than all it ever was.
WINDOW_AUDIO_SECONDS = 36000.0


def _machine():
    from hardware_profile import resolve
    profile = resolve()
    return profile.device, profile.cores


# What one track holds at once while a pass works on it, in MiB per minute of
# its audio (measured 2026-11-11 on x86, the release image's librosa and soxr):
# the sonic pass holds the 48 kHz decode (11.0) and its 22 050 Hz resample
# with the resampler's transient (10.1), the amplitude block a constant ~50
# MiB whatever the length (audio_analysis.amplitude_frames); the instrument
# pass holds the decode and the tagger's two resamples, to 16 and 32 kHz,
# with their transients (18.3). A stream's analysis holds what both hold.
TRACK_MB_PER_MINUTE = {SONIC: 22.0, INSTRUMENTS: 30.0}
TRACK_FIXED_MB = 64.0
# The memory a model a pass will still load takes from what a track may hold,
# in GB: its weights and runtime in RAM on a CPU and in unified memory (MPS) —
# the AST+PaSST pair's ~0.9 GiB a lite x86 node's peak rose by with it, CLAP's
# 0.6 GB of fp32 weights with its runtime — and only their host side on CUDA,
# where the weights sit in the card's memory.
MODEL_RAM_GB = {"clap": {"cpu": 1.0, "mps": 1.0, "cuda": 0.3},
                "tagger": {"cpu": 1.0, "mps": 1.0, "cuda": 0.3}}


def model_ram_gb(model: str) -> float:
    return MODEL_RAM_GB[model][_machine()[0]]


_CGROUPS = (("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current",
             "/sys/fs/cgroup/memory.stat", "inactive_file"),
            ("/sys/fs/cgroup/memory/memory.limit_in_bytes",
             "/sys/fs/cgroup/memory/memory.usage_in_bytes",
             "/sys/fs/cgroup/memory/memory.stat", "total_inactive_file"))


def _cgroup_room_bytes() -> Optional[int]:
    """What the process's control group still allows — a container's memory
    limit less its use, the page cache it may drop not counted as use — or
    None where no limit is set."""
    for limit_path, used_path, stat_path, reclaimable in _CGROUPS:
        try:
            limit = open(limit_path).read().strip()
            used = int(open(used_path).read().strip())
        except (OSError, ValueError):
            continue
        if limit == "max" or int(limit) >= 1 << 60:
            return None
        try:
            stat = dict(line.split() for line in open(stat_path))
            used -= int(stat.get(reclaimable, 0))
        except (OSError, ValueError):
            pass
        return int(limit) - used
    return None


def available_memory_gb() -> float:
    """The memory this machine can still give, in GB: what the system says
    is available, within the container's limit where one is set."""
    try:
        import psutil
        available = psutil.virtual_memory().available
    except ImportError:
        available = None
        try:
            for line in open("/proc/meminfo"):
                if line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) * 1024
        except OSError:
            pass
        if available is None:
            available = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    room = _cgroup_room_bytes()
    if room is not None:
        available = min(available, room)
    return available / 1e9


def longest_track_seconds(kinds, reserve_gb: float = 0.0) -> float:
    """The longest track the passes `kinds` (SONIC, INSTRUMENTS, or both for a
    stream) may take on this machine now: what one track holds per minute of
    its audio (TRACK_MB_PER_MINUTE, the larger of the kinds) within what is
    available, less the usual headroom (as the batch budget leaves 15 %) and
    a model the pass will still load (`reserve_gb`). The decode and its
    resample scale with the track's length, so a track past this is left out
    of the queue by its known duration — never decoded, nothing deleted; its
    analysis can come from the network."""
    per_minute = max(TRACK_MB_PER_MINUTE[k] for k in kinds)
    usable_mb = (available_memory_gb() * 0.85 - reserve_gb) * 1e3 / 1.048576 - TRACK_FIXED_MB
    return max(0.0, usable_mb / per_minute * 60.0)


def _interpolated(points, threads: int) -> float:
    (t0, v0), (t1, v1) = points
    t = min(max(threads, t0), t1)
    return v0 + (v1 - v0) * math.log(t / t0) / math.log(t1 / t0)


def _tagger_threads() -> int:
    """The torch threads the tagger runs with on a CPU (the profile's cap,
    else every core)."""
    from hardware_profile import resolve
    profile = resolve()
    return profile.torch_cpu_threads or profile.cores


def tagger_to_sonic(device: str) -> Optional[float]:
    """The tagger's pace against the sonic pass's on this device class, or
    None where it was never measured (MPS)."""
    if device == "cuda":
        return CUDA_RATIO
    if device != "cpu":
        return None
    return _interpolated(CPU_RATIO_POINTS, _tagger_threads())


def record(db: Session, kind: str, wall_seconds: float, audio_seconds: float) -> None:
    """A batch of the pass `kind` (SONIC, INSTRUMENTS) finished: `audio_seconds`
    of audio analysed in `wall_seconds`. A batch that analysed nothing says
    nothing about the pace."""
    if audio_seconds <= 0:
        return
    device, cores = _machine()
    db.execute(sa_text("""
        INSERT INTO analysis_pace (pass, device, cores, batches, audio_seconds, wall_seconds)
        VALUES (CAST(:kind AS analysis_pass), CAST(:device AS compute_device), :cores, 1,
                :audio, :wall)
        ON CONFLICT (pass, device, cores) DO UPDATE
           SET batches = analysis_pace.batches + 1,
               audio_seconds = LEAST(analysis_pace.audio_seconds, :window)
                               + EXCLUDED.audio_seconds,
               wall_seconds = analysis_pace.wall_seconds
                              * LEAST(1.0, :window / analysis_pace.audio_seconds)
                              + EXCLUDED.wall_seconds,
               updated_at = CURRENT_TIMESTAMP"""),
        {"kind": kind, "device": device, "cores": cores, "window": WINDOW_AUDIO_SECONDS,
         "audio": audio_seconds, "wall": max(0.0, wall_seconds)})


def pace(db: Session, kind: str) -> Optional[float]:
    """The pass's wall seconds per minute of audio on this machine, or None
    before its first batch here."""
    device, cores = _machine()
    return db.execute(sa_text("""
        SELECT wall_seconds / (audio_seconds / 60.0) FROM analysis_pace
         WHERE pass = CAST(:kind AS analysis_pass) AND device = CAST(:device AS compute_device)
           AND cores = :cores"""),
        {"kind": kind, "device": device, "cores": cores}).scalar()


# A queue's row this machine can hold now (longest_track_seconds), by the
# file's known duration — a file of unknown length is taken, as before.
def within_reach_sql(alias: str = "mf") -> str:
    return f" AND ({alias}.duration_seconds IS NULL OR {alias}.duration_seconds <= :longest)"


def beyond_reach_sql(alias: str = "mf") -> str:
    return f" AND {alias}.duration_seconds > :longest"


def instruments_pending(db: Session, reanalyze: bool, longest: float) -> tuple:
    """(tracks, seconds of audio) in the instrument pass's queue that this
    machine can hold (`longest`), and the tracks past it."""
    params = {"min_seconds": provenance.MIN_MATERIAL_SECONDS, "reanalyze": reanalyze,
              "unknown": UNKNOWN_SECONDS, "longest": longest}
    row = db.execute(sa_text(f"""
        SELECT count(*), COALESCE(sum(COALESCE(q.duration_seconds, :unknown)), 0)
          FROM ({INSTRUMENTS_PENDING_SQL}{within_reach_sql()}) q"""), params).one()
    beyond = db.execute(sa_text(f"SELECT count(*) FROM ({INSTRUMENTS_PENDING_SQL}{beyond_reach_sql()}) q"),
                        params).scalar()
    return int(row[0]), float(row[1]), int(beyond)


def estimate(db: Session, reanalyze: bool) -> Dict[str, Any]:
    """What the instrument pass has left here and how long it would take:
    `tracks` and `audio_seconds` pending, `pace` (s per minute of audio) and
    `seconds` of analysis — both None while there is no pace to say —, and
    `basis`: 'measured' (this machine's instrument batches), 'sonic' (its
    sonic pace × the device class's ratio), 'reference' (the tagger's
    measured CPU pace at this machine's threads) or 'measuring' (no pace to
    start from — MPS, or a CUDA node before its first sonic batch: the first
    batch says); `too_long`, the pending tracks this machine cannot hold now,
    left out of the rest."""
    tracks, audio, beyond = instruments_pending(
        db, reanalyze, longest_track_seconds([INSTRUMENTS], reserve_gb=model_ram_gb("tagger")))
    measured = pace(db, INSTRUMENTS)
    device, cores = _machine()
    ratio = tagger_to_sonic(device) if measured is None else None
    sonic = pace(db, SONIC) if ratio is not None else None
    if measured is not None:
        per_minute, basis = measured, "measured"
    elif sonic is not None:
        per_minute, basis = sonic * ratio, "sonic"
    elif device == "cpu":
        per_minute, basis = _interpolated(CPU_TAGGER_PACE_POINTS, _tagger_threads()), "reference"
    else:
        per_minute, basis = None, "measuring"
    return {"tracks": tracks, "audio_seconds": audio, "pace": per_minute, "basis": basis,
            "seconds": audio / 60.0 * per_minute if per_minute is not None else None,
            "too_long": beyond}
