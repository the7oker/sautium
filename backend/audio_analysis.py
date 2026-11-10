"""
Audio feature extraction using librosa (DSP) + the AST/PaSST ensemble.

Extracts (full-track methodology):
- librosa amplitude block (energy, brightness, dynamic range, ZCR): the
  WHOLE track — the middle 30s systematically overstates energy and
  understates dynamic range on quiet-intro/loud-climax pieces
- librosa BPM + key/mode: the middle 30s — a scalar can't express a
  multi-act piece, and the middle is the most representative sample of
  the "main body"
- AST + PaSST ensemble instruments: 10s windows tiled across the WHOLE
  track, max per label (ensemble_instruments.tag_full_track)

Two records, each of its own (record_sig): the features (audio_features,
the nine fields of FEATURE_ORDER at ANALYSIS_VERSION) and the instruments
(track_instruments, at ensemble_instruments.INSTRUMENTS_ANALYSIS_VERSION);
save_features and save_instruments are every first-hand writer's saves.
CLAP's zero-shot values — moods, the vocal score, danceability — are no
analysis here since 2026-11-10: the database derives them from the segment
embeddings a node holds (zero_shot_scores).

Operates on tracks (one analysis per track), using the analysis source media file.
"""

import logging
import subprocess
from typing import Any, Dict, Optional

import librosa
import numpy as np
from scipy.stats import pearsonr
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session
from tqdm import tqdm

import provenance
from config import settings
from database import get_db_context
from ensemble_instruments import INSTRUMENTS_ANALYSIS_VERSION
from models import AudioFeature, TrackInstruments
from record_sig import FEATURE_ORDER

logger = logging.getLogger(__name__)

# Methodology version stamped on audio_features rows and carried through P2P
# sync (peers re-pull rows whose version is older than the source's):
#   1 — middle-30s everything
#   2 — whole-track amplitude block, tempo and key from the middle 30 s
#       (2026-07-03)
ANALYSIS_VERSION = 2

# The rate every track is decoded at: the instrument tagger resamples from it
# to each model's own, librosa to its 22050 Hz.
DECODE_SR = 48000

# Krumhansl-Schmuckler key profiles
_MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                            2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                            2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
_KEY_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def load_full_track_48k(local_path: str, cue_start: Optional[float] = None,
                        cue_end: Optional[float] = None) -> np.ndarray:
    """Decode the whole file to 48kHz mono float32 via ffmpeg — one decode
    path shared by the scanner and backfills so scores stay comparable.
    ffmpeg outruns audioread severalfold on mp3 (which has no seek table and
    must be decoded sequentially anyway). cue_start/cue_end bound a CUE image
    slice: -ss before -i input-seeks (accurate_seek drops samples to the exact
    position), so the returned array IS the track — every downstream window
    grid stays array-relative and slice-agnostic."""
    cmd = ["ffmpeg", "-v", "error"]
    if cue_start is not None:
        cmd += ["-ss", f"{cue_start:.6f}"]
    cmd += ["-i", local_path]
    if cue_end is not None:
        cmd += ["-t", f"{cue_end - (cue_start or 0.0):.6f}"]
    cmd += ["-ac", "1", "-ar", "48000", "-f", "f32le", "pipe:1"]
    out = subprocess.run(cmd, capture_output=True, check=True, timeout=300).stdout
    audio = np.frombuffer(out, dtype=np.float32).copy()
    if not len(audio):
        raise ValueError(f"ffmpeg produced no samples for {local_path}")
    return audio


def middle_segment(audio: np.ndarray, sr: int, seconds: float) -> np.ndarray:
    n = int(seconds * sr)
    if len(audio) <= n:
        return audio
    start = (len(audio) - n) // 2
    return audio[start:start + n]


def amplitude_features(y: np.ndarray, sr: int) -> Dict[str, Any]:
    """RMS/dynamic-range/brightness/zcr block, shared by the scanner and the
    full-track backfill so both produce methodologically identical values.
    brightness (centroid/nyquist) and zcr are SR-relative — callers must pass
    the same 22050Hz signal the scanner has always used, or historic values
    stop being comparable. Frame-wise stats over the WHOLE given signal: mean
    for level/spectral features, p95-p5 for dynamic range (a mean would
    erase exactly the quiet-intro-vs-loud-climax spread it measures)."""
    features: Dict[str, Any] = {}

    rms = librosa.feature.rms(y=y)[0]
    features["energy"] = round(float(np.mean(rms)), 6)
    rms_db = librosa.amplitude_to_db(rms)
    features["energy_db"] = round(float(np.mean(rms_db)), 2)

    if len(rms_db) > 1:
        features["dynamic_range_db"] = round(
            float(np.percentile(rms_db, 95) - np.percentile(rms_db, 5)), 2
        )
    else:
        features["dynamic_range_db"] = 0.0

    spectral_centroid = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
    nyquist = sr / 2.0
    features["brightness"] = round(float(np.mean(spectral_centroid) / nyquist), 4)

    zcr = librosa.feature.zero_crossing_rate(y)[0]
    features["zero_crossing_rate"] = round(float(np.mean(zcr)), 6)

    return features



# Pending = no features yet, OR unlinked provenance (legacy rows), OR made
# from material other than the current analysis source holds at its rank
# (provenance.file_in_group_sql: source moved to a better rip, or a stream
# preview awaiting its owned upgrade — see embeddings.PENDING_SQL). Material
# under one grid window is never pending (provenance.MIN_MATERIAL_SECONDS),
# nor is a file of a copy that is away (album_variants.absent_since).
# P2P-imported analysis re-enters only when the enrichment.reanalyze_imported
# toggle says so — see the policy comment in embeddings.py, which owns the
# flag. A file that returned after a prune is linked back to its analysis
# (provenance.relink_returned_files) after the import's canon and again before
# each pass reads this queue, so it is not pending here. This pass registers
# only at its save, after the compute, so it has no compute to skip by the
# group (provenance.held_in_group, asked by a pass that registers first).
PENDING_SQL = f"""
    SELECT t.id as track_id, mf.id as media_file_id,
           mf.file_path, mf.bit_depth,
           mf.sample_rate as mf_sample_rate, mf.is_lossless,
           mf.duration_seconds,
           mf.cue_start_seconds, mf.cue_end_seconds
    FROM media_files mf
    JOIN tracks t ON t.id = mf.track_id
    JOIN album_variants av ON av.id = mf.album_variant_id
    LEFT JOIN audio_features af ON af.track_id = t.id
    LEFT JOIN analysis_sources asrc ON asrc.id = af.analysis_source_id
    WHERE mf.is_analysis_source = true
      AND av.absent_since IS NULL
      AND (mf.duration_seconds IS NULL
           OR mf.duration_seconds >= :min_seconds)
      AND (af.id IS NULL
           OR af.analysis_source_id IS NULL
           OR (NOT {provenance.file_in_group_sql()}
               AND (:reanalyze OR NOT asrc.imported)))
"""

def save_features(db: Session, track_id, features: Dict[str, Any], src_id: int) -> bool:
    """The track's features record from the copy `src_id` describes — the
    fields of record_sig.FEATURE_ORDER at ANALYSIS_VERSION — unless a better
    copy's stand (provenance.outranked): each pass replaces its own kind at
    its save by the rank. A changed value sheds the row's seal (its guard),
    and sign_audio signs the new one. Returns whether it wrote."""
    existing = db.query(AudioFeature).filter(AudioFeature.track_id == track_id).first()
    if existing and provenance.outranked(db, existing.analysis_source_id, src_id):
        return False
    values = {k: features.get(k) for k in FEATURE_ORDER}
    if existing:
        for k, v in values.items():
            setattr(existing, k, v)
        existing.analysis_source_id = src_id
        existing.analysis_version = ANALYSIS_VERSION
    else:
        db.add(AudioFeature(track_id=track_id, analysis_source_id=src_id,
                            analysis_version=ANALYSIS_VERSION, **values))
    return True


def save_instruments(db: Session, track_id, instruments: Dict[str, float], src_id: int) -> bool:
    """The track's instruments record from the copy `src_id` describes — the
    tagger's raw score per label at INSTRUMENTS_ANALYSIS_VERSION — unless a
    better copy's stand, by the rank as save_features. Returns whether it
    wrote."""
    existing = db.query(TrackInstruments).filter(TrackInstruments.track_id == track_id).first()
    if existing and provenance.outranked(db, existing.analysis_source_id, src_id):
        return False
    if existing:
        existing.instruments = instruments
        existing.analysis_source_id = src_id
        existing.analysis_version = INSTRUMENTS_ANALYSIS_VERSION
    else:
        db.add(TrackInstruments(track_id=track_id, instruments=instruments,
                                analysis_source_id=src_id,
                                analysis_version=INSTRUMENTS_ANALYSIS_VERSION))
    return True


class AudioAnalyzer:
    """Extract a track's features (librosa) and instruments (the AST+PaSST
    ensemble) from its whole decoded audio."""

    def __init__(
        self,
        sample_rate: Optional[int] = None,
        duration: Optional[int] = None,
        device: Optional[str] = None,
    ):
        self.librosa_sr = sample_rate or settings.audio_analysis_sample_rate
        self.duration = duration or settings.audio_analysis_duration

        from device import get_device
        self.device = device or get_device()
        self.ensemble = None

    # --- Model management ---

    def load_model(self):
        """Acquire the process-wide AST+PaSST ensemble singleton."""
        if self.ensemble is not None:
            return
        from instrument_tagger import get_instrument_tagger
        self.ensemble = get_instrument_tagger(self.device)

    # --- librosa DSP features ---

    def _detect_key(self, y: np.ndarray, sr: int) -> Dict[str, Any]:
        """Detect musical key using Krumhansl-Schmuckler algorithm."""
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        chroma_mean = np.mean(chroma, axis=1)

        best_corr = -2.0
        best_key = 0
        best_mode = "major"

        for shift in range(12):
            rolled = np.roll(chroma_mean, -shift)

            corr_major, _ = pearsonr(rolled, _MAJOR_PROFILE)
            if corr_major > best_corr:
                best_corr = corr_major
                best_key = shift
                best_mode = "major"

            corr_minor, _ = pearsonr(rolled, _MINOR_PROFILE)
            if corr_minor > best_corr:
                best_corr = corr_minor
                best_key = shift
                best_mode = "minor"

        # Normalize confidence to 0-1 range (pearson is -1 to 1, typical range 0.3-0.9)
        confidence = max(0.0, min(1.0, (best_corr + 1.0) / 2.0))

        return {
            "key": _KEY_NAMES[best_key],
            "mode": best_mode,
            "key_confidence": round(float(confidence), 3),
        }

    def _extract_tempo_key(self, y: np.ndarray, sr: int) -> Dict[str, Any]:
        """BPM + key/mode — callers pass the middle segment, not the whole
        track (see the module docstring)."""
        features = {}

        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        # librosa may return an array; extract scalar
        if hasattr(tempo, '__len__'):
            tempo = float(tempo[0]) if len(tempo) > 0 else 0.0
        features["bpm"] = round(float(tempo), 2) if tempo > 0 else None

        features.update(self._detect_key(y, sr))
        return features

    def _librosa_features(self, y22_full: np.ndarray) -> Dict[str, Any]:
        """Tempo/key from the middle segment + amplitude block from the whole
        given 22050Hz signal."""
        y_mid = middle_segment(y22_full, self.librosa_sr, self.duration)
        features = self._extract_tempo_key(y_mid, self.librosa_sr)
        features.update(amplitude_features(y22_full, self.librosa_sr))
        return features

    # --- Main analysis pipeline ---

    def analyze_from_array(self, audio: np.ndarray, sr: int = DECODE_SR) -> Optional[Dict[str, Any]]:
        """A track's analysis from its whole decoded audio: the features, and
        under "instruments" the tagger's scores once load_model() has run."""
        y_librosa = librosa.resample(audio, orig_sr=sr, target_sr=self.librosa_sr)
        features = self._librosa_features(y_librosa)
        del y_librosa

        if self.ensemble is not None:
            y48 = audio if sr == DECODE_SR else librosa.resample(audio, orig_sr=sr,
                                                                 target_sr=DECODE_SR)
            features["instruments"] = self.ensemble.tag_full_track(y48)
        return features

    def _load_and_extract_librosa(self, file_path: str, duration_seconds: float = None,
                                  cue_start: float = None, cue_end: float = None) -> Optional[Dict]:
        """Load the whole track + extract librosa features (I/O + CPU).
        Thread-safe."""
        try:
            local_path = settings.translate_to_local_path(file_path)
            y_48k = load_full_track_48k(local_path, cue_start, cue_end)
            y_librosa = librosa.resample(y_48k, orig_sr=DECODE_SR, target_sr=self.librosa_sr)
            librosa_features = self._librosa_features(y_librosa)
            del y_librosa

            return {"audio_48k": y_48k, "librosa_features": librosa_features}
        except Exception as e:
            logger.error(f"Load+librosa failed for {file_path}: {e}")
            return None

    def _tag(self, track_id, audio_48k: np.ndarray) -> Optional[Dict[str, float]]:
        """The tagger's scores for one track, or None when it failed on it —
        the track's features are saved all the same."""
        try:
            return self.ensemble.tag_full_track(audio_48k)
        except Exception as e:
            logger.warning(f"Instrument tagging failed for {track_id}: {e}")
            return None

    def analyze_all(
        self,
        limit: Optional[int] = None,
        force: bool = False,
        order_by_date: bool = False,
        librosa_only: bool = False,
        max_duration_seconds: Optional[int] = None,
        track_ids: Optional[list] = None,
        worker_id: Optional[int] = None,
        worker_count: Optional[int] = None,
        cancel_flag=None,
    ) -> Dict[str, int]:
        """
        Batch analyze tracks with pipelined I/O, CPU, and GPU.

        Architecture:
          - ThreadPool (4 workers): load files + librosa features (I/O + CPU)
          - Main thread: collect batch → the instrument tagger on the
            accelerator → save both records to DB

        Args:
            limit: Max tracks to process.
            force: Re-analyze even if features exist.
            order_by_date: Process newest tracks first.
            librosa_only: Skip the instrument tagger (faster, DSP only).
            max_duration_seconds: Maximum duration in seconds.
            track_ids: If provided, only process these track IDs.
            worker_id: Worker index (0-based) for parallel processing.
            worker_count: Total number of workers for parallel processing.

        Returns:
            Statistics dict: processed, success (features saved), failed,
            skipped (a better copy's features stand), instruments (saved).
        """
        import time
        from concurrent.futures import ThreadPoolExecutor, as_completed

        provenance.require_fingerprinter()
        if not force:
            # A failed look costs only the work it would have saved: the
            # pass analyses what stays pending, as before.
            try:
                provenance.relink_returned_files(track_ids, cancel_check=cancel_flag)
            except Exception as e:
                logger.error(f"Re-link before the pass failed: {e}", exc_info=True)
        stats = {"processed": 0, "success": 0, "failed": 0, "skipped": 0, "instruments": 0}
        start_time = time.time()
        batch_size = 16
        from hardware_profile import resolve as _hw
        prefetch_workers = _hw().prefetch_workers

        with get_db_context() as db:
            # Query tracks to analyze
            if force:
                query_sql = """
                    SELECT t.id as track_id, mf.id as media_file_id,
                           mf.file_path, mf.bit_depth,
                           mf.sample_rate as mf_sample_rate, mf.is_lossless,
                           mf.duration_seconds,
                           mf.cue_start_seconds, mf.cue_end_seconds
                    FROM media_files mf
                    JOIN tracks t ON t.id = mf.track_id
                    JOIN album_variants av ON av.id = mf.album_variant_id
                    WHERE mf.is_analysis_source = true
                      AND av.absent_since IS NULL
                      AND (mf.duration_seconds IS NULL
                           OR mf.duration_seconds >= :min_seconds)
                """
            else:
                query_sql = PENDING_SQL

            params = {"min_seconds": provenance.MIN_MATERIAL_SECONDS}
            if not force:
                from embeddings import _reanalyze_imported
                params["reanalyze"] = _reanalyze_imported()

            if track_ids is not None:
                query_sql += " AND t.id = ANY(:track_ids)"
                params["track_ids"] = track_ids

            # Gaps first — coverage before recomputation (mirrors embeddings)
            gap_first = "" if force else "(af.id IS NOT NULL), "
            if order_by_date:
                query_sql += (f" ORDER BY {gap_first}"
                              "mf.file_modified_at DESC NULLS LAST")
            else:
                query_sql += f" ORDER BY {gap_first}t.id"

            if limit:
                query_sql += f" LIMIT {limit}"

            rows = db.execute(sa_text(query_sql), params).fetchall()
            total = len(rows)

            if total == 0:
                logger.info("No tracks pending audio analysis")
                return stats

            if not librosa_only:
                self.load_model()

            logger.info(f"Analyzing {total} tracks (librosa_only={librosa_only}, "
                        f"prefetch={prefetch_workers}, batch={batch_size})")

            # Process in batches: prefetch + librosa in threads, the tagger
            # on the accelerator.
            # Batches are cut by BOTH row count and total audio duration —
            # whole tracks are held decoded in RAM for the batch, and 16
            # Schulze-length pieces at once would be ~14GB.
            from device import audio_batch_budget_seconds
            batch_budget_seconds = audio_batch_budget_seconds()
            batches = []
            cur, cur_seconds = [], 0.0
            for row in rows:
                dur = float(row.duration_seconds) if row.duration_seconds else 300.0
                if cur and (len(cur) >= batch_size
                            or cur_seconds + dur > batch_budget_seconds):
                    batches.append(cur)
                    cur, cur_seconds = [], 0.0
                cur.append(row)
                cur_seconds += dur
            if cur:
                batches.append(cur)

            pbar = tqdm(total=total, desc="Analyzing audio", unit="track")
            # Single thread pool for all batches (avoid 2000+ pool creations)
            _io_pool = ThreadPoolExecutor(max_workers=prefetch_workers)

            for batch_rows in batches:
                if cancel_flag and cancel_flag():
                    logger.info("Audio analysis cancelled by user")
                    break

                if max_duration_seconds:
                    elapsed = time.time() - start_time
                    if elapsed >= max_duration_seconds:
                        logger.info(f"Time limit reached ({elapsed:.1f}s), stopping")
                        break

                # Phase 1: parallel load + librosa (I/O + CPU)
                prepared = []  # (row, librosa_features, audio_48k)
                future_to_row = {
                    _io_pool.submit(
                        self._load_and_extract_librosa, row.file_path,
                        float(row.duration_seconds) if row.duration_seconds else None,
                        row.cue_start_seconds, row.cue_end_seconds,
                    ): row
                    for row in batch_rows
                }
                for future in as_completed(future_to_row):
                    row = future_to_row[future]
                    stats["processed"] += 1
                    try:
                        result = future.result()
                        if result is not None:
                            prepared.append((row, result))
                        else:
                            stats["failed"] += 1
                    except Exception as e:
                        stats["failed"] += 1
                        logger.error(f"Prefetch failed for {row.track_id}: {e}")

                if not prepared:
                    pbar.update(len(batch_rows))
                    continue

                # Phase 2: the instrument tagger, one whole track at a time
                # (its windows are batched inside)
                if not librosa_only and self.ensemble is not None:
                    tagged = [self._tag(row.track_id, prep["audio_48k"])
                              for row, prep in prepared]
                else:
                    tagged = [None] * len(prepared)

                # Free audio arrays
                for _, prep in prepared:
                    del prep["audio_48k"]

                # Batch GPU work (AST/PaSST) is done — return the
                # allocator's free blocks so the footprint tracks the live
                # per-batch working set, not the run's cumulative peak (MPS
                # unified memory otherwise holds ~28 GB for the whole run).
                from device import empty_cache
                empty_cache(self.device)

                # Phase 3: save both records (savepoint per track, commit per batch)
                for (row, prep), instruments in zip(prepared, tagged):
                    try:
                        savepoint = db.begin_nested()
                        try:
                            # Registered before the results it describes; no
                            # fingerprint = no address, and the row is not
                            # saved (the pending predicate retries next run,
                            # as for a file that fails to decode).
                            src_id = provenance.get_or_create_local(
                                db, row.track_id, row.media_file_id,
                                row.file_path, row.mf_sample_rate,
                                row.bit_depth, row.is_lossless,
                                row.duration_seconds,
                                row.cue_start_seconds, row.cue_end_seconds)
                            if src_id is None:
                                raise RuntimeError("no content address (fingerprint)")
                            # a better copy's records stay, as the embedding
                            # save keeps a better copy's embedding
                            saved = save_features(db, row.track_id, prep["librosa_features"],
                                                  src_id)
                            if instruments is not None and save_instruments(
                                    db, row.track_id, instruments, src_id):
                                stats["instruments"] += 1
                            savepoint.commit()
                        except Exception:
                            savepoint.rollback()
                            raise
                        stats["success" if saved else "skipped"] += 1

                    except Exception as e:
                        stats["failed"] += 1
                        logger.error(f"Failed to save features for {row.track_id}: {e}")

                db.commit()
                pbar.update(len(batch_rows))

            _io_pool.shutdown(wait=True, cancel_futures=True)
            pbar.close()
            logger.info(
                f"Audio analysis complete: {stats['success']} success, {stats['failed']} failed"
            )


        return stats
