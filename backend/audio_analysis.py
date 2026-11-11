"""
The features (librosa) and the instruments (the AST + PaSST ensemble) of a
track, from its whole decoded audio.

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

The two passes over the library are split by what they make (2026-11-11):
the features go with the segments in the sonic pass
(embeddings.AudioEmbeddingGenerator.sonic_pass, which calls
features_from_array on its one decode), the instruments in the instrument
pass here (AudioAnalyzer.instrument_pass), after it and behind the owner's
switch (enrichment.instruments). Each decodes a track once.
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

    # --- The two passes' parts ---

    def features_from_array(self, audio: np.ndarray, sr: int = DECODE_SR) -> Dict[str, Any]:
        """A track's features from its whole decoded audio — the sonic
        pass's librosa part, and the stream enricher's."""
        y_librosa = librosa.resample(audio, orig_sr=sr, target_sr=self.librosa_sr)
        return self._librosa_features(y_librosa)

    def instrument_pass(
        self,
        limit: Optional[int] = None,
        force: bool = False,
        order_by_date: bool = False,
        max_duration_seconds: Optional[int] = None,
        track_ids: Optional[list] = None,
        worker_id: Optional[int] = None,
        worker_count: Optional[int] = None,
        cancel_flag=None,
        on_batch=None,
        attempted: Optional[set] = None,
    ) -> Dict[str, int]:
        """The instrument pass over the tracks whose instruments record is
        pending (analysis_pace.INSTRUMENTS_PENDING_SQL): each track's
        analysis-source file registered first (the sonic pass before it
        normally has), skipped when the file's material group already holds
        instruments at a rank no lower (provenance.held_in_group), then one
        decode and the tagger, saved by the rank (save_instruments). After
        each batch: the pace (analysis_pace) and the notary's wake — sign()
        leaves a record re-saved while it signed unsigned until the next
        wake.

        Architecture: a batch is registered in the main thread, decoded in
        the profile's prefetch pool, tagged one whole track at a time on the
        accelerator (its windows are batched inside), saved with a savepoint
        per track and committed per batch.

        Args:
            limit: Max tracks to process.
            force: Re-tag every analysis-source track regardless of state.
            order_by_date: Process newest tracks first.
            max_duration_seconds: Maximum duration in seconds.
            track_ids: If provided, only process these track IDs.
            worker_id: Worker index (0-based) for parallel processing.
            worker_count: Total number of workers for parallel processing.
            cancel_flag: Asked before every batch; True ends the pass there.
            on_batch: Called with (tracks done, tracks queued) after each batch.
            attempted: media_files ids this run's earlier instrument pass
                took already — left out, and every file this one takes is
                added: a pass resumed after giving way does not try again a
                file it could not address or decode.

        Returns:
            processed, success (saved), failed, skipped (the group holds
            them, or a better copy's record stands).
        """
        import time
        from concurrent.futures import ThreadPoolExecutor

        import analysis_pace
        import notary

        provenance.require_fingerprinter()
        if not force:
            # A failed look costs only the work it would have saved: the
            # pass analyses what stays pending, as before.
            try:
                provenance.relink_returned_files(track_ids, cancel_check=cancel_flag)
            except Exception as e:
                logger.error(f"Re-link before the pass failed: {e}", exc_info=True)
        stats = {"processed": 0, "success": 0, "failed": 0, "skipped": 0}
        start_time = time.time()
        batch_size = 16
        from hardware_profile import resolve as _hw
        prefetch_workers = _hw().prefetch_workers

        with get_db_context() as db:
            from embeddings import _reanalyze_imported
            query_sql = (analysis_pace.INSTRUMENTS_ROWS_SQL if force
                         else analysis_pace.INSTRUMENTS_PENDING_SQL)
            params = {"min_seconds": provenance.MIN_MATERIAL_SECONDS,
                      "reanalyze": _reanalyze_imported()}
            if track_ids is not None:
                query_sql += " AND mf.track_id = ANY(:track_ids)"
                params["track_ids"] = track_ids

            # Gaps first — coverage before recomputation (as the sonic pass)
            if order_by_date:
                query_sql += " ORDER BY gap DESC, mf.file_modified_at DESC NULLS LAST"
            else:
                query_sql += " ORDER BY gap DESC, mf.track_id"
            if limit:
                query_sql += f" LIMIT {limit}"

            rows = db.execute(sa_text(query_sql), params).fetchall()
            if attempted is not None:
                rows = [r for r in rows if r.media_file_id not in attempted]
            total = len(rows)
            if total == 0:
                logger.info("No tracks pending the instrument pass")
                return stats

            logger.info(f"Instrument pass: {total} tracks "
                        f"(prefetch={prefetch_workers}, batch={batch_size})")

            # Batches are cut by BOTH row count and total audio duration —
            # whole tracks are held decoded in RAM for the batch, and 16
            # Schulze-length pieces at once would be ~14GB.
            # The tagger loads at the first batch that has something to tag;
            # the batches are cut before that, so the budget leaves room for it
            from device import audio_batch_budget_seconds, empty_cache
            from ensemble_instruments import RESIDENT_GB
            batch_budget_seconds = audio_batch_budget_seconds(
                reserve_gb=0.0 if self.ensemble is not None else RESIDENT_GB)
            batches = []
            cur, cur_seconds = [], 0.0
            for row in rows:
                dur = float(row.duration_seconds) if row.duration_seconds else analysis_pace.UNKNOWN_SECONDS
                if cur and (len(cur) >= batch_size
                            or cur_seconds + dur > batch_budget_seconds):
                    batches.append(cur)
                    cur, cur_seconds = [], 0.0
                cur.append(row)
                cur_seconds += dur
            if cur:
                batches.append(cur)

            pbar = tqdm(total=total, desc="Instrument pass", unit="track")
            io_pool = ThreadPoolExecutor(max_workers=prefetch_workers)
            try:
                for batch_rows in batches:
                    if cancel_flag and cancel_flag():
                        logger.info("Instrument pass cancelled")
                        break
                    if max_duration_seconds:
                        elapsed = time.time() - start_time
                        if elapsed >= max_duration_seconds:
                            logger.info(f"Time limit reached ({elapsed:.1f}s), stopping")
                            break

                    batch_began = time.monotonic()
                    stats["processed"] += len(batch_rows)
                    if attempted is not None:
                        attempted.update(r.media_file_id for r in batch_rows)
                    todo = self._register_batch(db, batch_rows, force, stats)
                    # The registrations commit before the long part, so no
                    # other pass waits on them while the batch is tagged
                    db.commit()
                    if todo and self.ensemble is None:
                        # loaded once there is something to tag: a queue the
                        # groups hold whole never takes the pair's memory
                        self.load_model()
                        batch_began = time.monotonic()

                    decoded = list(io_pool.map(self._decode, [row for row, _ in todo]))
                    analysed = 0.0
                    wrote = False
                    # a decode that failed spent time on no audio: the batch
                    # says nothing true about the pace
                    whole = all(audio is not None for audio in decoded)
                    for (row, src_id), audio in zip(todo, decoded):
                        if audio is None:
                            stats["failed"] += 1
                            continue
                        outcome = self._tag_track(db, row, src_id, audio)
                        stats[outcome] += 1
                        wrote = wrote or outcome == "success"
                        if outcome != "failed":
                            analysed += len(audio) / DECODE_SR
                    del decoded
                    # Batch GPU work (AST/PaSST) is done — return the
                    # allocator's free blocks so the footprint tracks the live
                    # per-batch working set, not the run's cumulative peak (MPS
                    # unified memory otherwise holds ~28 GB for the whole run).
                    empty_cache(self.device)

                    if whole:
                        analysis_pace.record(db, analysis_pace.INSTRUMENTS,
                                             time.monotonic() - batch_began, analysed)
                    db.commit()
                    if wrote:
                        notary.wake("instrument pass")
                    if on_batch:
                        on_batch(stats["processed"], total)
                    pbar.update(len(batch_rows))
            finally:
                io_pool.shutdown(wait=True, cancel_futures=True)
                pbar.close()
            logger.info(f"Instrument pass complete: {stats['success']} success, "
                        f"{stats['skipped']} skipped, {stats['failed']} failed")

        return stats

    def _register_batch(self, db: Session, batch_rows, force: bool, stats: Dict[str, int]) -> list:
        """The batch's tracks registered before anything is decoded: (row,
        source id) for each that has instruments to make. No fingerprint =
        no address, and nothing is saved (the queue asks again next run, as
        for a file that fails to decode). Each under a savepoint of its own,
        as the sonic pass registers: a track renamed or merged by the canon
        since the queue was read fails alone."""
        todo = []
        for row in batch_rows:
            try:
                with db.begin_nested():
                    src_id = provenance.get_or_create_local(
                        db, row.track_id, row.media_file_id, row.file_path,
                        row.sample_rate, row.bit_depth, row.is_lossless,
                        row.duration_seconds, row.cue_start_seconds, row.cue_end_seconds)
                    held = src_id is not None and not force and provenance.held_in_group(
                        db, src_id, "track_instruments")
            except Exception as e:
                stats["failed"] += 1
                logger.error(f"Registration failed for {row.track_id}: {e}")
                continue
            if src_id is None:
                stats["failed"] += 1
                logger.warning(f"Skipping track {row.track_id}: no content address (fingerprint)")
            elif held:
                stats["skipped"] += 1       # the same material, tagged at a rank no lower
            else:
                todo.append((row, src_id))
        return todo

    def _decode(self, row) -> Optional[np.ndarray]:
        try:
            return load_full_track_48k(settings.translate_to_local_path(row.file_path),
                                       row.cue_start_seconds, row.cue_end_seconds)
        except Exception as e:
            logger.error(f"Decode failed for {row.file_path}: {e}")
            return None

    def _tag_track(self, db: Session, row, src_id: int, audio: np.ndarray) -> str:
        """One decoded track tagged and saved under a savepoint of its own:
        success, skipped (a better copy's record stands) or failed."""
        try:
            instruments = self.ensemble.tag_full_track(audio)
        except Exception as e:
            logger.warning(f"Instrument tagging failed for {row.track_id}: {e}")
            return "failed"
        try:
            with db.begin_nested():
                saved = save_instruments(db, row.track_id, instruments, src_id)
        except Exception as e:
            logger.error(f"Failed to save instruments for {row.track_id}: {e}")
            return "failed"
        return "success" if saved else "skipped"
