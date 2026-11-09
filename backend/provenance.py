"""Content-address provenance for audio analysis (analysis_sources rows).

Every analysis pass — scanner embedding/feature runs and streamed preview
enrichment alike — registers WHAT material it analyzed before its results are
saved: the AcoustID chromaprint of the decoded audio (fpcalc) plus its
duration. Record signatures bind these values, so they must exist at analysis
time, not be recomputed later — recomputing after the fact is exactly the
design flaw this module replaced (sign_audio's lazy second decode could hash
a different file than the one the analysis actually saw).

The fingerprint is the whole content address (2026-09-18; a BLAKE2b hash of
the decoded PCM stood beside it before): a public recording identity that any
node's decode of the same material reproduces, where the PCM hash changed
with the decoder build and with every lossy decode. Rows are keyed on
(track_id, chromaprint_key) — the database's digest of the fingerprint, since
~3 KB of base64 is too long for a btree row: re-analyzing unchanged material
reuses the row, a different master mints a new one, two rips of one master
collapse. No fingerprint means no address, and an analysis with no address is
never saved — it could neither sign nor travel, and the pending predicates
would re-derive it on every run.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Optional

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from config import settings

logger = logging.getLogger(__name__)

# Material shorter than one grid window (embeddings.WINDOW_SECONDS) is not
# analysed: one window is the smallest thing the analysis means, and fpcalc
# needs a few seconds of audio for any fingerprint at all — the floor is what
# guarantees an address (Valerii, 2026-09-18).
MIN_MATERIAL_SECONDS = 10

# Overwrite precedence between analysis passes of the same track, read from the
# MATERIAL alone: the node's own file beats any stream, a lossless stream beats
# a lossy one, an imported (second-hand) source and a record linked to no
# source at all rank below everything and are always upgradable. Never the
# provider's brand — the sync ranks peers' sources on is_lossless alone, and
# this is the same rule. The SQL form expects analysis_sources aliased `s`
# (LEFT JOINed or not: a missing row ranks -1).
MATERIAL_RANK_SQL = ("CASE WHEN s.id IS NULL OR s.imported THEN -1 "
                     "WHEN s.provider_id IS NULL THEN 2 "
                     "WHEN s.is_lossless THEN 1 ELSE 0 END")


def material_rank(provider_id: Optional[str], is_lossless: Optional[bool],
                  imported: bool = False) -> int:
    """MATERIAL_RANK_SQL for a pass about to be written (or a row already
    read): provider_id None = the node's own file."""
    if imported:
        return -1
    if provider_id is None:
        return 2
    return 1 if is_lossless else 0


def _rank_sql(row: str) -> str:
    """MATERIAL_RANK_SQL over a row that exists — the upsert compares the
    registration (EXCLUDED) against the row already holding the address."""
    return (f"CASE WHEN {row}.imported THEN -1 "
            f"WHEN {row}.provider_id IS NULL THEN 2 "
            f"WHEN {row}.is_lossless THEN 1 ELSE 0 END")


_UPSERT_SQL = sa_text(f"""
    INSERT INTO analysis_sources
        (track_id, provider_id, media_file_id, chromaprint,
         duration_seconds, sample_rate, bit_depth, is_lossless)
    VALUES (:tid, :pid, :mfid, :fp, :dur, :sr, :bd, :ll)
    ON CONFLICT (track_id, chromaprint_key) DO UPDATE
       SET provider_id = EXCLUDED.provider_id,
           media_file_id = EXCLUDED.media_file_id,
           duration_seconds = COALESCE(EXCLUDED.duration_seconds,
                                       analysis_sources.duration_seconds),
           sample_rate = EXCLUDED.sample_rate,
           bit_depth = EXCLUDED.bit_depth,
           is_lossless = EXCLUDED.is_lossless,
           -- any registration through this module means THIS node decoded
           -- the material itself: a previously synced-in row for the same
           -- material becomes first-hand (and thus signable)
           imported = false,
           computed_at = now()
    -- the row describes the best material this node has registered under the
    -- address: an own-file registration upgrades any row, a stream never
    -- downgrades one (own file > lossless stream > lossy stream > imported)
    WHERE {_rank_sql('EXCLUDED')} >= {_rank_sql('analysis_sources')}
    RETURNING id
""")


def require_fpcalc() -> None:
    """Analysis passes call this first: without fpcalc nothing they compute
    can be addressed, and unaddressed analysis is never saved — stop before
    decoding a library rather than skip every track of it."""
    if shutil.which("fpcalc") is None:
        raise RuntimeError("fpcalc (Chromaprint) is not on PATH — audio analysis "
                           "registers no material without it")


def _fpcalc(path: str) -> Optional[str]:
    try:
        out = subprocess.run(["fpcalc", "-plain", path], capture_output=True,
                             text=True, timeout=120, check=True)
    except (subprocess.SubprocessError, OSError) as e:
        logger.debug("fpcalc failed for %s: %s", path, e)
        return None
    return out.stdout.strip() or None


def chromaprint_file(local_path: str, cue_start=None,
                     cue_end=None) -> Optional[str]:
    """AcoustID fingerprint (fpcalc, its default 120 s window) of a file or of
    a CUE image slice. fpcalc takes only a path, so a slice is decoded to a
    temp WAV first — fingerprinting the image would stamp every track of the
    disc with one identical anchor. The same WAV path is the fallback when
    fpcalc's own decoder rejects a whole file that ffmpeg can read. None when
    neither yields a fingerprint: material with no address."""
    if cue_start is None and cue_end is None:
        fp = _fpcalc(local_path)
        if fp:
            return fp
        logger.info("fpcalc could not read %s directly — decoding through ffmpeg",
                    local_path)
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    try:
        cmd = ["ffmpeg", "-v", "error", "-y"]
        if cue_start is not None:
            cmd += ["-ss", f"{cue_start:.6f}"]
        cmd += ["-i", local_path]
        if cue_end is not None:
            cmd += ["-t", f"{cue_end - (cue_start or 0.0):.6f}"]
        cmd += [tmp.name]
        try:
            subprocess.run(cmd, capture_output=True, timeout=300, check=True)
        except (subprocess.SubprocessError, OSError) as e:
            logger.warning("ffmpeg decode for the fingerprint failed for %s: %s",
                           local_path, e)
            return None
        fp = _fpcalc(tmp.name)
        if fp is None:
            logger.warning("no fingerprint for %s: fpcalc produced nothing", local_path)
        return fp
    finally:
        os.unlink(tmp.name)


def get_or_create_local(db: Session, track_id, media_file_id: int,
                        file_path: str, sample_rate, bit_depth,
                        is_lossless, duration_seconds=None,
                        cue_start=None, cue_end=None) -> Optional[int]:
    """analysis_sources.id for a local analysis-source file, fingerprinting it
    on first sight. duration_seconds is the scan-known material duration
    (media_files.duration_seconds) — part of the signed material declaration.
    cue_start/cue_end bound a CUE image slice so its fingerprint addresses the
    track's material, not the whole disc. None when the file yields no
    fingerprint: the caller saves nothing (an analysis with no address could
    neither sign nor travel) and the pending predicate retries next run, as
    for any file that fails to decode."""
    sid = db.execute(sa_text(
        "SELECT id FROM analysis_sources "
        "WHERE track_id = :tid AND media_file_id = :mfid "
        "ORDER BY id DESC LIMIT 1"),
        {"tid": str(track_id), "mfid": media_file_id}).scalar()
    if sid is not None:
        return sid

    fp = chromaprint_file(settings.translate_to_local_path(file_path),
                          cue_start, cue_end)
    if fp is None:
        return None
    # An own-file registration ranks above every row, so the upsert always
    # returns the id.
    return db.execute(_UPSERT_SQL, {
        "tid": str(track_id), "pid": None, "mfid": media_file_id, "fp": fp,
        "dur": int(round(float(duration_seconds))) if duration_seconds else None,
        "sr": sample_rate, "bd": bit_depth, "ll": is_lossless,
    }).scalar()


# The node's own analysis whose file left the library (media_file_id SET NULL
# on the file row's delete) and the analysis-source file now holding its
# track: a file that came back, at its old path or a new one. Only first-hand
# file rows — a stream row is the stream→owned upgrade policy's, an imported
# one the reanalyze_imported policy's, and neither is this node's file. Read
# from the state alone, so whatever a cancel, a restart or a missing fpcalc
# left is found again by the next run.
_RETURNED_SQL = """
    SELECT mf.id, mf.track_id::text AS track_id, mf.file_path,
           mf.cue_start_seconds, mf.cue_end_seconds, mf.duration_seconds
    FROM media_files mf
    WHERE mf.is_analysis_source
      AND (mf.duration_seconds IS NULL OR mf.duration_seconds >= :min_seconds)
      {scope}
      AND NOT EXISTS (SELECT 1 FROM analysis_sources s WHERE s.media_file_id = mf.id)
      AND EXISTS (SELECT 1 FROM analysis_sources s
                  WHERE s.track_id = mf.track_id AND s.media_file_id IS NULL
                    AND s.provider_id IS NULL AND NOT s.imported)
    ORDER BY mf.id
"""

# The fingerprint covers fpcalc's first 120 s; the duration is the other half
# of the material declaration a seal binds (record_sig.segment_payload), so a
# longer or shorter cut of the same opening is another material.
_RELINK_SQL = sa_text("""
    UPDATE analysis_sources SET media_file_id = :mfid
    WHERE track_id = :tid AND chromaprint_key = sha256(decode(:fp, 'escape'))
      AND media_file_id IS NULL AND provider_id IS NULL AND NOT imported
      AND (duration_seconds IS NULL OR CAST(:dur AS int) IS NULL
           OR duration_seconds = CAST(:dur AS int))
""")

_RELINK_CHUNK = 100


def relink_returned_files(track_ids=None, cancel_check=None, progress_cb=None) -> int:
    """Link the node's own analysis back to the file it was made from when
    that file returns — a folder moved back, a drive re-attached, a library
    moved under a new root. The analysis passes would see the track as
    pending (its source names no file, or another one) and recompute it in
    full; the fingerprint and the duration say the material is the same, and
    the content address is what the analysis describes. A file that matches
    no unlinked row (another master, another cut) stays pending, as any new
    file is. `track_ids` narrows the look to those tracks; without it every
    such file is looked at.

    Each chunk's fingerprints are taken in a pool before its transaction
    opens (fpcalc decodes up to 120 s of audio per file), and each chunk
    commits on its own, so a cancel or a crash keeps what was linked.
    progress_cb(done, total) after each chunk. Returns how many rows were
    linked."""
    from concurrent.futures import ThreadPoolExecutor

    from database import get_db_context

    params = {"min_seconds": MIN_MATERIAL_SECONDS}
    scope = ""
    if track_ids is not None:
        if not track_ids:
            return 0
        scope = "AND mf.track_id = ANY(CAST(:tids AS uuid[]))"
        params["tids"] = [str(t) for t in track_ids]
    with get_db_context() as db:
        files = db.execute(sa_text(_RETURNED_SQL.format(scope=scope)), params).fetchall()
    if not files:
        return 0
    if shutil.which("fpcalc") is None:
        logger.warning("%d returned file(s) keep their analysis unlinked: fpcalc "
                       "(Chromaprint) is not on PATH, so their material cannot be "
                       "compared", len(files))
        return 0

    def fingerprint(f):
        return f, chromaprint_file(settings.translate_to_local_path(f.file_path),
                                   f.cue_start_seconds, f.cue_end_seconds)

    linked = done = 0
    with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 4)) as pool:
        for start in range(0, len(files), _RELINK_CHUNK):
            if cancel_check and cancel_check():
                break
            chunk = list(pool.map(fingerprint, files[start:start + _RELINK_CHUNK]))
            with get_db_context() as db:
                for f, fp in chunk:
                    if fp:
                        linked += db.execute(_RELINK_SQL, {
                            "mfid": f.id, "tid": f.track_id, "fp": fp,
                            # rounded as get_or_create_local declares it
                            "dur": (int(round(float(f.duration_seconds)))
                                    if f.duration_seconds else None)}).rowcount
            done += len(chunk)
            if progress_cb:
                progress_cb(done, len(files))
    logger.info("Re-linked the analysis of %d of %d returned file(s) by their "
                "fingerprint", linked, done)
    return linked


def create_stream_source(db: Session, track_id, audio_bytes: bytes,
                         provider_id: str, is_lossless: bool) -> Optional[int]:
    """analysis_sources.id for streamed provider audio held in memory. None
    when the bytes yield no fingerprint, or when the same material is already
    registered here at a higher rank (the node's own file, or a lossless
    stream over a lossy one) — the stream then has nothing to add and the
    caller skips the analysis. provider_id is the serving provider's manifest
    id — registered in stream_providers at start, which is what the row's FK
    holds it to."""
    tmp = tempfile.NamedTemporaryFile(suffix=".audio", delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.close()
        fp = chromaprint_file(tmp.name)
        if fp is None:
            return None
        try:
            sample_rate, bit_depth, duration = _probe_audio(tmp.name)
        except (subprocess.SubprocessError, OSError, ValueError, LookupError) as e:
            logger.warning("stream provenance failed for track %s (%s): %s",
                           track_id, provider_id, e)
            return None
    finally:
        os.unlink(tmp.name)

    sid = db.execute(_UPSERT_SQL, {
        "tid": str(track_id), "pid": provider_id, "mfid": None, "fp": fp,
        "dur": duration, "sr": sample_rate, "bd": bit_depth, "ll": is_lossless,
    }).scalar()
    if sid is None:
        logger.info("stream provenance for track %s (%s): the material is already "
                    "registered first-hand at a higher rank — nothing to add",
                    track_id, provider_id)
    return sid


def _probe_audio(local_path: str):
    """(sample_rate, bit_depth, duration_seconds) of the container's audio
    stream; bit_depth is None for lossy codecs that carry no raw sample size,
    duration rounds to whole seconds (the signed-declaration granularity)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries",
         "stream=sample_rate,bits_per_raw_sample,bits_per_sample,duration",
         "-show_entries", "format=duration",
         "-of", "json", local_path],
        capture_output=True, text=True, timeout=30, check=True)
    probed = json.loads(out.stdout)
    stream = probed["streams"][0]
    rate = int(stream["sample_rate"]) if stream.get("sample_rate") else None
    bits = int(stream.get("bits_per_raw_sample") or
               stream.get("bits_per_sample") or 0) or None
    raw_dur = stream.get("duration") or probed.get("format", {}).get("duration")
    duration = int(round(float(raw_dur))) if raw_dur else None
    return rate, bits, duration


def material_rank_of(db: Session, analysis_source_id: Optional[int]) -> Optional[int]:
    """Material rank of a linked source; None for unlinked (legacy) rows."""
    if analysis_source_id is None:
        return None
    return db.execute(sa_text(
        f"SELECT {MATERIAL_RANK_SQL} FROM analysis_sources s WHERE s.id = :sid"),
        {"sid": analysis_source_id}).scalar()
