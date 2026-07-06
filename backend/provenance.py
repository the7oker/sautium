"""Content-address provenance for audio analysis (analysis_sources rows).

Every analysis pass — scanner embedding/feature runs and streamed preview
enrichment alike — registers WHAT material it analyzed before its results are
saved: pcm_hash (BLAKE2b of the natively-decoded PCM; deterministic for
lossless across ffmpeg builds) plus the AcoustID chromaprint (the robust
recording-identity anchor for cross-rip verification). Record signatures bind
these values, so they must exist at analysis time, not be recomputed later —
recomputing after the fact is exactly the design flaw this module replaced
(sign_audio's lazy second decode could hash a different file than the one the
analysis actually saw).

Rows are content-keyed on (track_id, pcm_hash): re-analyzing unchanged
material reuses the row, a re-rip mints a new one, and byte-identical copies
in different folders collapse. A same-bytes stream row upgrades to 'local'
when the material turns out to be owned. (backfill_analysis_sources.py, the
one-shot pre-M2 pass, carries its own psycopg2 copy of the fingerprint
helpers — it must run against the pre-cutover schema.)
"""

import hashlib
import json
import logging
import os
import subprocess
import tempfile
from typing import Optional

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from config import settings

logger = logging.getLogger(__name__)

# Overwrite precedence between analysis passes of the same track: a local file
# beats any stream, a lossless stream beats a lossy one. Rows linked to no
# source (legacy) rank below everything and are always upgradable.
ORIGIN_RANK = {"local": 2, "deezer": 1, "youtube": 0}

_UPSERT_SQL = sa_text("""
    INSERT INTO analysis_sources
        (track_id, origin, media_file_id, pcm_hash, chromaprint,
         duration_seconds, sample_rate, bit_depth, is_lossless)
    VALUES (:tid, CAST(:origin AS analysis_origin), :mfid, :ph, :fp,
            :dur, :sr, :bd, :ll)
    ON CONFLICT (track_id, pcm_hash) DO UPDATE
       SET origin = EXCLUDED.origin,
           media_file_id = EXCLUDED.media_file_id,
           chromaprint = COALESCE(analysis_sources.chromaprint,
                                  EXCLUDED.chromaprint),
           duration_seconds = COALESCE(analysis_sources.duration_seconds,
                                       EXCLUDED.duration_seconds),
           -- any registration through this module means THIS node decoded
           -- the material itself: a previously synced-in row for the same
           -- bytes becomes first-hand (and thus signable)
           imported = false
    WHERE EXCLUDED.origin = 'local' OR analysis_sources.origin <> 'local'
    RETURNING id
""")


def pcm_hash_file(local_path: str) -> str:
    """BLAKE2b-256 of the natively-decoded PCM (source rate & channels,
    f32le). Streamed chunk-wise — hi-res long-form tracks decode to >1 GB."""
    h = hashlib.blake2b(digest_size=32)
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", local_path, "-f", "f32le", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    total = 0
    while chunk := proc.stdout.read(1 << 20):
        h.update(chunk)
        total += len(chunk)
    proc.stdout.close()
    err = proc.stderr.read().decode(errors="replace").strip()
    proc.stderr.close()
    if proc.wait(timeout=60) != 0:
        raise RuntimeError(f"ffmpeg rc={proc.returncode}: {err[:200]}")
    if total == 0:
        raise ValueError("ffmpeg produced no samples")
    return h.hexdigest()


def chromaprint_file(local_path: str) -> Optional[str]:
    """AcoustID fingerprint (fpcalc, default 120s window). None on failure —
    the source row is still valid, records just sign without the cross-rip
    anchor (and never gain it later: it is bound into signatures from the
    start or not at all)."""
    try:
        out = subprocess.run(["fpcalc", "-plain", local_path],
                             capture_output=True, text=True, timeout=120,
                             check=True)
        return out.stdout.strip() or None
    except Exception as e:
        logger.warning("fpcalc failed for %s: %s", local_path, e)
        return None


def get_or_create_local(db: Session, track_id, media_file_id: int,
                        file_path: str, sample_rate, bit_depth,
                        is_lossless, duration_seconds=None) -> Optional[int]:
    """analysis_sources.id for a local analysis-source file, fingerprinting it
    on first sight. duration_seconds is the scan-known material duration
    (media_files.duration_seconds) — part of the signed material declaration.
    Returns None when the file can't be decoded (the analysis row then saves
    unlinked and the staleness predicate retries next run)."""
    sid = db.execute(sa_text(
        "SELECT id FROM analysis_sources "
        "WHERE track_id = :tid AND media_file_id = :mfid "
        "ORDER BY id DESC LIMIT 1"),
        {"tid": str(track_id), "mfid": media_file_id}).scalar()
    if sid is not None:
        return sid

    local_path = settings.translate_to_local_path(file_path)
    try:
        ph = pcm_hash_file(local_path)
    except Exception as e:
        logger.warning("provenance decode failed for %s: %s", local_path, e)
        return None
    fp = chromaprint_file(local_path)

    return db.execute(_UPSERT_SQL, {
        "tid": str(track_id), "origin": "local", "mfid": media_file_id,
        "ph": ph, "fp": fp,
        "dur": int(round(float(duration_seconds))) if duration_seconds else None,
        "sr": sample_rate, "bd": bit_depth, "ll": is_lossless,
    }).scalar()


def create_stream_source(db: Session, track_id, audio_bytes: bytes,
                         provider_id: str, is_lossless: bool) -> Optional[int]:
    """analysis_sources.id for streamed provider audio held in memory.
    Unknown providers get no provenance (and their analysis stays unlinked)
    rather than a guessed origin."""
    if provider_id not in ORIGIN_RANK or provider_id == "local":
        logger.warning("unknown stream provider %r — no provenance row",
                       provider_id)
        return None

    tmp = tempfile.NamedTemporaryFile(suffix=".audio", delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.close()
        ph = pcm_hash_file(tmp.name)
        fp = chromaprint_file(tmp.name)
        sample_rate, bit_depth, duration = _probe_audio(tmp.name)
    except Exception as e:
        logger.warning("stream provenance failed for track %s (%s): %s",
                       track_id, provider_id, e)
        return None
    finally:
        os.unlink(tmp.name)

    sid = db.execute(_UPSERT_SQL, {
        "tid": str(track_id), "origin": provider_id, "mfid": None,
        "ph": ph, "fp": fp, "dur": duration,
        "sr": sample_rate, "bd": bit_depth, "ll": is_lossless,
    }).scalar()
    if sid is None:
        # The upsert's anti-downgrade WHERE skipped the update: these exact
        # bytes are already registered as a LOCAL source — reuse that row.
        sid = db.execute(sa_text(
            "SELECT id FROM analysis_sources "
            "WHERE track_id = :tid AND pcm_hash = :ph"),
            {"tid": str(track_id), "ph": ph}).scalar()
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


def origin_of(db: Session, analysis_source_id: Optional[int]) -> Optional[str]:
    """Origin of a linked source; None for unlinked (legacy) rows."""
    if analysis_source_id is None:
        return None
    return db.execute(sa_text(
        "SELECT origin::text FROM analysis_sources WHERE id = :sid"),
        {"sid": analysis_source_id}).scalar()
