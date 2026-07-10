"""
Audio feature extraction using librosa (DSP) + CLAP zero-shot
+ AST/PaSST ensemble.

Extracts (full-track methodology, aligned with backfill_instruments):
- librosa amplitude block (energy, brightness, dynamic range, ZCR): the
  WHOLE track — the middle 30s systematically overstates energy and
  understates dynamic range on quiet-intro/loud-climax pieces
- librosa BPM + key/mode: the middle 30s — a scalar can't express a
  multi-act piece, and the middle is the most representative sample of
  the "main body"
- CLAP zero-shot (moods, vocal/instrumental, danceability): middle 30s
  (its processor takes 10s internally; windowed CLAP is the segment-
  embeddings stage)
- AST + PaSST ensemble instruments: 10s windows tiled across the WHOLE
  track, max per label (ensemble_instruments.tag_full_track)

Operates on tracks (one analysis per track), using the analysis source media file.
"""

import logging
import subprocess
from typing import Any, Dict, List, Optional

import librosa
import numpy as np
import torch
from scipy.stats import pearsonr
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session
from tqdm import tqdm

import provenance
from config import settings
from database import get_db_context
from models import AudioFeature, Track, MediaFile

logger = logging.getLogger(__name__)

# Methodology version stamped on audio_features rows and carried through P2P
# sync (peers re-pull rows whose version is older than the source's):
#   1 — middle-30s everything, single-window instruments
#   2 — whole-track amplitude block + windowed-max instruments (2026-07-03)
ANALYSIS_VERSION = 2


# --- CLAP zero-shot label sets (instruments moved to ensemble_instruments) ---

MOOD_LABELS = [
    "happy and upbeat", "sad and melancholic",
    "energetic and intense", "calm and relaxing",
    "dark and ominous", "romantic and dreamy",
    "aggressive and angry", "mysterious and atmospheric",
]

VOCAL_LABELS = [
    "singing vocals",
    "instrumental music without vocals",
]

DANCE_LABELS = [
    "highly danceable music with strong beat",
    "music that is not danceable",
]

# Krumhansl-Schmuckler key profiles
_MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                            2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                            2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
_KEY_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def load_full_track_48k(local_path: str) -> np.ndarray:
    """Decode the whole file to 48kHz mono float32 via ffmpeg — one decode
    path shared by the scanner and backfills so scores stay comparable.
    ffmpeg outruns audioread severalfold on mp3 (which has no seek table and
    must be decoded sequentially anyway)."""
    cmd = ["ffmpeg", "-v", "error", "-i", local_path,
           "-ac", "1", "-ar", "48000", "-f", "f32le", "pipe:1"]
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


class AudioAnalyzer:
    """Extract audio features from audio files using librosa and CLAP zero-shot."""

    def __init__(
        self,
        sample_rate: Optional[int] = None,
        duration: Optional[int] = None,
        device: Optional[str] = None,
    ):
        self.librosa_sr = sample_rate or settings.audio_analysis_sample_rate
        self.clap_sr = 48000  # CLAP model requirement
        self.duration = duration or settings.audio_analysis_duration
        self.model_name = settings.embedding_model

        from device import get_device
        self.device = device or get_device()
        if self.device == "cpu":
            logger.warning("No GPU accelerator detected, CLAP zero-shot will be slow on CPU")

        self.model = None
        self.processor = None
        self._text_embeddings_cache = {}
        self.ensemble = None

    # --- Model management ---

    def load_model(self):
        """Acquire process-wide singleton CLAP + AST+PaSST ensemble."""
        if self.model is not None:
            return
        from clap_model import get_clap_model
        from instrument_tagger import get_instrument_tagger
        self.processor, self.model = get_clap_model(self.model_name, self.device)
        self._encode_text_labels()
        self.ensemble = get_instrument_tagger(self.device)

    def _encode_text_labels(self):
        """Pre-encode CLAP text label sets (moods/vocal/dance). Called once."""
        from device import autocast, cast_inputs
        label_sets = {
            "moods": [f"This is {l} music" for l in MOOD_LABELS],
            "vocal": [f"This is {l}" for l in VOCAL_LABELS],
            "dance": [f"This is {l}" for l in DANCE_LABELS],
        }

        for key, prompts in label_sets.items():
            inputs = self.processor(text=prompts, return_tensors="pt", padding=True)
            inputs = cast_inputs(inputs, self.model.dtype)
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
            with torch.no_grad(), autocast(self.device):
                text_features = self.model.get_text_features(**inputs)
            # Keep cached label embeddings in fp32 so cosine math against
            # audio features stays stable across precision boundaries
            text_features = torch.nn.functional.normalize(text_features.float(), p=2, dim=1)
            self._text_embeddings_cache[key] = text_features

        logger.info(f"Pre-encoded {sum(len(v) for v in label_sets.values())} text labels")

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

    # --- CLAP zero-shot classification ---

    def _classify_zero_shot(
        self, audio_embedding: torch.Tensor, label_key: str, labels: List[str]
    ) -> Dict[str, float]:
        """
        Classify audio against pre-encoded text labels using cosine similarity + softmax.
        Returns dict of label -> probability.
        """
        text_embeddings = self._text_embeddings_cache[label_key]

        # Cosine similarity (audio_embedding is already L2-normalized)
        logits = audio_embedding @ text_embeddings.T

        # Apply CLAP's learned logit_scale for sharper probabilities
        logit_scale = self.model.logit_scale_a.exp()
        logits = logits * logit_scale

        probs = torch.nn.functional.softmax(logits, dim=-1)
        probs = probs[0].cpu().detach().numpy()

        return {label: round(float(prob), 3) for label, prob in zip(labels, probs)}

    def _extract_clap_features(self, audio_full_48k: np.ndarray) -> Dict[str, Any]:
        """CLAP moods/vocal/dance (middle segment) + AST+PaSST ensemble
        instruments (whole track, windowed max)."""
        from device import autocast, cast_inputs
        audio_mid = middle_segment(audio_full_48k, self.clap_sr, self.duration)
        # Encode audio
        inputs = self.processor(
            audio=[audio_mid],
            sampling_rate=self.clap_sr,
            return_tensors="pt",
            padding=True,
        )
        inputs = cast_inputs(inputs, self.model.dtype)
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        with torch.no_grad(), autocast(self.device):
            audio_features = self.model.get_audio_features(**inputs)
        audio_features = torch.nn.functional.normalize(audio_features.float(), p=2, dim=1)

        features = {}

        # Moods
        mood_probs = self._classify_zero_shot(audio_features, "moods", MOOD_LABELS)
        features["moods"] = {
            k: v for k, v in sorted(mood_probs.items(), key=lambda x: -x[1]) if v > 0.05
        }

        # Vocal / instrumental
        vocal_probs = self._classify_zero_shot(audio_features, "vocal", VOCAL_LABELS)
        vocal_score = vocal_probs.get("singing vocals", 0.5)
        features["vocal_score"] = round(vocal_score, 3)
        if vocal_score > 0.65:
            features["vocal_instrumental"] = "vocal"
        elif vocal_score < 0.35:
            features["vocal_instrumental"] = "instrumental"
        else:
            features["vocal_instrumental"] = "mixed"

        # Danceability
        dance_probs = self._classify_zero_shot(audio_features, "dance", DANCE_LABELS)
        features["danceability"] = round(
            dance_probs.get("highly danceable music with strong beat", 0.5), 3
        )

        # Instruments via AST + PaSST ensemble (native multi-label)
        features["instruments"] = self.ensemble.tag_full_track(audio_full_48k)

        return features

    # --- Main analysis pipeline ---

    def analyze_from_array(self, audio: np.ndarray, sr: int = 48000) -> Optional[Dict[str, Any]]:
        """
        Full analysis from pre-loaded audio array.
        Avoids redundant file I/O when audio is already in memory.
        """
        features = {}

        # Phase 1: librosa DSP — resample to 22kHz in memory (fast)
        y_librosa = librosa.resample(audio, orig_sr=sr, target_sr=self.librosa_sr)
        features.update(self._librosa_features(y_librosa))
        del y_librosa

        # Phase 2: CLAP zero-shot (audio already at 48kHz if sr matches)
        if self.model is not None:
            if sr == self.clap_sr:
                y_clap = audio
            else:
                y_clap = librosa.resample(audio, orig_sr=sr, target_sr=self.clap_sr)
            clap_features = self._extract_clap_features(y_clap)
            features.update(clap_features)

        return features

    def analyze_track(self, file_path: str) -> Optional[Dict[str, Any]]:
        """
        Full analysis pipeline for a single audio file.
        Phase 1: whole-track decode + librosa DSP (CPU)
        Phase 2: CLAP zero-shot + windowed ensemble (GPU)
        """
        try:
            y_48k = load_full_track_48k(file_path)
        except Exception as e:
            logger.error(f"Failed to load audio {file_path}: {e}")
            return None
        return self.analyze_from_array(y_48k, sr=self.clap_sr)

    def _extract_clap_features_batch(
        self, audio_arrays: List[np.ndarray]
    ) -> List[Dict[str, Any]]:
        """CLAP moods/vocal/dance for a batch + AST+PaSST per-track ensemble.

        audio_arrays hold WHOLE tracks: CLAP encodes their middle segments in
        one batched GPU pass; the ensemble tags each full track with windowed
        max (its windows are batched internally).
        """
        from device import autocast, cast_inputs
        mids = [middle_segment(a, self.clap_sr, self.duration) for a in audio_arrays]
        inputs = self.processor(
            audio=mids,
            sampling_rate=self.clap_sr,
            return_tensors="pt",
            padding=True,
        )
        inputs = cast_inputs(inputs, self.model.dtype)
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        with torch.no_grad(), autocast(self.device):
            all_audio_features = self.model.get_audio_features(**inputs)
        all_audio_features = torch.nn.functional.normalize(all_audio_features.float(), p=2, dim=1)

        results = []
        for i, audio_48k in enumerate(audio_arrays):
            audio_emb = all_audio_features[i:i+1]
            features = {}

            mood_probs = self._classify_zero_shot(audio_emb, "moods", MOOD_LABELS)
            features["moods"] = {
                k: v for k, v in sorted(mood_probs.items(), key=lambda x: -x[1]) if v > 0.05
            }

            vocal_probs = self._classify_zero_shot(audio_emb, "vocal", VOCAL_LABELS)
            vocal_score = vocal_probs.get("singing vocals", 0.5)
            features["vocal_score"] = round(vocal_score, 3)
            if vocal_score > 0.65:
                features["vocal_instrumental"] = "vocal"
            elif vocal_score < 0.35:
                features["vocal_instrumental"] = "instrumental"
            else:
                features["vocal_instrumental"] = "mixed"

            dance_probs = self._classify_zero_shot(audio_emb, "dance", DANCE_LABELS)
            features["danceability"] = round(
                dance_probs.get("highly danceable music with strong beat", 0.5), 3
            )

            features["instruments"] = self.ensemble.tag_full_track(audio_48k)

            results.append(features)

        return results

    def _load_and_extract_librosa(self, file_path: str, duration_seconds: float = None) -> Optional[Dict]:
        """Load the whole track + extract librosa features (I/O + CPU).
        Thread-safe."""
        try:
            local_path = settings.translate_to_local_path(file_path)
            y_48k = load_full_track_48k(local_path)
            y_librosa = librosa.resample(y_48k, orig_sr=self.clap_sr, target_sr=self.librosa_sr)
            librosa_features = self._librosa_features(y_librosa)
            del y_librosa

            return {"audio_48k": y_48k, "librosa_features": librosa_features}
        except Exception as e:
            logger.error(f"Load+librosa failed for {file_path}: {e}")
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
          - Main thread: collect batch → CLAP GPU inference → save to DB

        Args:
            limit: Max tracks to process.
            force: Re-analyze even if features exist.
            order_by_date: Process newest tracks first.
            librosa_only: Skip CLAP classification (faster, DSP only).
            max_duration_seconds: Maximum duration in seconds.
            track_ids: If provided, only process these track IDs.
            worker_id: Worker index (0-based) for parallel processing.
            worker_count: Total number of workers for parallel processing.

        Returns:
            Statistics dict.
        """
        import time
        from concurrent.futures import ThreadPoolExecutor, as_completed

        stats = {"processed": 0, "success": 0, "failed": 0, "skipped": 0}
        start_time = time.time()
        clap_batch_size = 16
        from hardware_profile import resolve as _hw
        prefetch_workers = _hw().prefetch_workers

        with get_db_context() as db:
            # Query tracks to analyze
            if force:
                query_sql = """
                    SELECT t.id as track_id, mf.id as media_file_id,
                           mf.file_path, mf.bit_depth,
                           mf.sample_rate as mf_sample_rate, mf.is_lossless,
                           mf.duration_seconds
                    FROM media_files mf
                    JOIN tracks t ON t.id = mf.track_id
                    WHERE mf.is_analysis_source = true
                """
            else:
                # Pending = no features yet, OR unlinked provenance (legacy
                # rows and failed fingerprints), OR linked to material other
                # than the current analysis source (source moved to a better
                # rip, or a stream preview awaiting its owned upgrade).
                query_sql = """
                    SELECT t.id as track_id, mf.id as media_file_id,
                           mf.file_path, mf.bit_depth,
                           mf.sample_rate as mf_sample_rate, mf.is_lossless,
                           mf.duration_seconds
                    FROM media_files mf
                    JOIN tracks t ON t.id = mf.track_id
                    LEFT JOIN audio_features af ON af.track_id = t.id
                    LEFT JOIN analysis_sources asrc ON asrc.id = af.analysis_source_id
                    WHERE mf.is_analysis_source = true
                      AND (af.id IS NULL
                           OR af.analysis_source_id IS NULL
                           OR asrc.media_file_id IS DISTINCT FROM mf.id)
                """

            params = {}

            if track_ids is not None:
                query_sql += " AND t.id = ANY(:track_ids)"
                params["track_ids"] = track_ids

            if order_by_date:
                query_sql += " ORDER BY mf.file_modified_at DESC NULLS LAST"
            else:
                query_sql += " ORDER BY t.id"

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
                        f"prefetch={prefetch_workers}, clap_batch={clap_batch_size})")

            # Process in batches: prefetch + librosa in threads, CLAP on GPU.
            # Batches are cut by BOTH row count and total audio duration —
            # whole tracks are held decoded in RAM for the batch, and 16
            # Schulze-length pieces at once would be ~14GB.
            from device import audio_batch_budget_seconds
            batch_budget_seconds = audio_batch_budget_seconds()
            batches = []
            cur, cur_seconds = [], 0.0
            for row in rows:
                dur = float(row.duration_seconds) if row.duration_seconds else 300.0
                if cur and (len(cur) >= clap_batch_size
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

                # Phase 2: batch CLAP on GPU (with single-item fallback)
                if not librosa_only and self.model is not None:
                    audio_arrays = [p[1]["audio_48k"] for p in prepared]
                    try:
                        clap_results = self._extract_clap_features_batch(audio_arrays)
                    except Exception as e:
                        logger.warning(f"CLAP batch failed, falling back to single: {e}")
                        clap_results = []
                        for audio in audio_arrays:
                            try:
                                single = self._extract_clap_features_batch([audio])
                                clap_results.append(single[0])
                            except Exception:
                                clap_results.append({})
                else:
                    clap_results = [{}] * len(prepared)

                # Free audio arrays
                for _, prep in prepared:
                    del prep["audio_48k"]

                # Batch GPU work (CLAP + AST/PaSST) is done — return the
                # allocator's free blocks so the footprint tracks the live
                # per-batch working set, not the run's cumulative peak (MPS
                # unified memory otherwise holds ~28 GB for the whole run).
                from device import empty_cache
                empty_cache(self.device)

                # Phase 3: merge features + save to DB (savepoint per track, commit per batch)
                for (row, prep), clap_feat in zip(prepared, clap_results):
                    try:
                        features = prep["librosa_features"]
                        features.update(clap_feat)

                        savepoint = db.begin_nested()
                        try:
                            # Registered before the results it describes; a
                            # fingerprint failure saves unlinked (src_id NULL)
                            # and the pending predicate retries next run.
                            src_id = provenance.get_or_create_local(
                                db, row.track_id, row.media_file_id,
                                row.file_path, row.mf_sample_rate,
                                row.bit_depth, row.is_lossless,
                                row.duration_seconds)
                            existing = db.query(AudioFeature).filter(
                                AudioFeature.track_id == row.track_id
                            ).first()
                            if existing:
                                for k, v in features.items():
                                    setattr(existing, k, v)
                                existing.analysis_source_id = src_id
                                existing.analysis_version = ANALYSIS_VERSION
                            else:
                                af = AudioFeature(
                                    track_id=row.track_id,
                                    bpm=features.get("bpm"),
                                    key=features.get("key"),
                                    mode=features.get("mode"),
                                    key_confidence=features.get("key_confidence"),
                                    energy=features.get("energy"),
                                    energy_db=features.get("energy_db"),
                                    brightness=features.get("brightness"),
                                    dynamic_range_db=features.get("dynamic_range_db"),
                                    zero_crossing_rate=features.get("zero_crossing_rate"),
                                    instruments=features.get("instruments"),
                                    moods=features.get("moods"),
                                    vocal_instrumental=features.get("vocal_instrumental"),
                                    vocal_score=features.get("vocal_score"),
                                    danceability=features.get("danceability"),
                                    analysis_source_id=src_id,
                                    analysis_version=ANALYSIS_VERSION,
                                )
                                db.add(af)
                            savepoint.commit()
                        except Exception:
                            savepoint.rollback()
                            raise
                        stats["success"] += 1

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
