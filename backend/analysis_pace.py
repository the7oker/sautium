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
kept as the mean over every batch the pass finished here, each weighed by
its audio (analysis_pace sums the batches' wall and audio seconds). Each pass
records after every batch, committed with the batch.

The estimate is the instrument pass's pending audio (its own queue,
INSTRUMENTS_PENDING_SQL) at this machine's instrument pace. Until the pass
has run a batch here, the sonic pass's pace times the tagger-to-sonic ratio
measured for the device class stands in: on a CPU only, the one class it was
measured on — a Raspberry Pi 5 on four cores, the tagger 18.9 s per minute of
audio against the sonic pass's ~2.2 — and a CPU node that has run no sonic
batch takes that measurement's sonic pace, scaled by its cores. On CUDA and
MPS nothing was measured, so the row says it is measuring until the first
instrument batch records a pace. Nothing here loads a model or imports the
ML stack: the screen asks on every library event, a lite Pi included.
"""

from typing import Any, Dict, Optional

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

import provenance

SONIC = "sonic"
INSTRUMENTS = "instruments"

# The tagger's pace against the sonic pass's, per device class where it was
# measured (the Pi 5, four cores: 18.9 / 2.2 s per minute of audio).
TAGGER_TO_SONIC = {"cpu": 8.5}

# The sonic pass's pace a CPU node takes before it has run a batch of its
# own: the same measurement's, s per minute of audio and the cores it ran on.
REFERENCE_SONIC_PACE = {"cpu": (2.2, 4)}

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


def _machine():
    from hardware_profile import resolve
    profile = resolve()
    return profile.device, profile.cores


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
               audio_seconds = analysis_pace.audio_seconds + EXCLUDED.audio_seconds,
               wall_seconds = analysis_pace.wall_seconds + EXCLUDED.wall_seconds,
               updated_at = CURRENT_TIMESTAMP"""),
        {"kind": kind, "device": device, "cores": cores,
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


def instruments_pending(db: Session, reanalyze: bool) -> tuple:
    """(tracks, seconds of audio) in the instrument pass's queue."""
    row = db.execute(sa_text(f"""
        SELECT count(*), COALESCE(sum(COALESCE(q.duration_seconds, :unknown)), 0)
          FROM ({INSTRUMENTS_PENDING_SQL}) q"""),
        {"min_seconds": provenance.MIN_MATERIAL_SECONDS, "reanalyze": reanalyze,
         "unknown": UNKNOWN_SECONDS}).one()
    return int(row[0]), float(row[1])


def estimate(db: Session, reanalyze: bool) -> Dict[str, Any]:
    """What the instrument pass has left here and how long it would take:
    `tracks` and `audio_seconds` pending, `pace` (s per minute of audio) and
    `seconds` of analysis — both None while there is no pace to say —, and
    `basis`: 'measured' (this machine's instrument batches), 'sonic' (its
    sonic pace × the device class's ratio), 'reference' (the measured
    reference, scaled by the cores) or 'measuring' (no ratio for this device
    class: the first instrument batch says)."""
    tracks, audio = instruments_pending(db, reanalyze)
    measured = pace(db, INSTRUMENTS)
    device, cores = _machine()
    if measured is not None:
        per_minute, basis = measured, "measured"
    elif device in TAGGER_TO_SONIC:
        sonic = pace(db, SONIC)
        if sonic is None:
            reference, on_cores = REFERENCE_SONIC_PACE[device]
            sonic, basis = reference * on_cores / cores, "reference"
        else:
            basis = "sonic"
        per_minute = sonic * TAGGER_TO_SONIC[device]
    else:
        per_minute, basis = None, "measuring"
    return {"tracks": tracks, "audio_seconds": audio, "pace": per_minute, "basis": basis,
            "seconds": audio / 60.0 * per_minute if per_minute is not None else None}
