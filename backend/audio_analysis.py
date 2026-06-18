"""
Audio feature extraction using librosa (DSP) + CLAP zero-shot
+ AST/PaSST ensemble.

Extracts:
- librosa: BPM, key/mode, energy, brightness, dynamic range, ZCR
- CLAP zero-shot: moods, vocal/instrumental, danceability
- AST + PaSST ensemble: instrument multi-label tags (AudioSet)

Operates on tracks (one analysis per track), using the analysis source media file.
"""

import logging
from typing import Any, Dict, List, Optional

import librosa
import numpy as np
import torch
from scipy.stats import pearsonr
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session
from tqdm import tqdm

from config import settings
from database import get_db_context
from models import AudioFeature, Track, MediaFile

logger = logging.getLogger(__name__)


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

    # --- Audio loading ---

    def _load_middle_segment(self, file_path: str, sr: int, total_duration: float = None) -> Optional[np.ndarray]:
        """Load the middle N seconds of an audio file at given sample rate.

        When total_duration is known (from DB), uses librosa's offset/duration
        to decode only the needed segment — avoids loading entire hi-res files
        (DSD/192kHz can be 200+ MB per file).
        """
        try:
            offset = 0.0
            load_duration = None

            if total_duration and total_duration > self.duration:
                offset = (total_duration - self.duration) / 2.0
                load_duration = float(self.duration)

            audio, _ = librosa.load(file_path, sr=sr, mono=True,
                                    offset=offset, duration=load_duration)
            return audio
        except Exception as e:
            logger.error(f"Failed to load audio {file_path} at {sr}Hz: {e}")
            return None

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

    def _extract_librosa_features(self, y: np.ndarray, sr: int) -> Dict[str, Any]:
        """Extract all librosa DSP features from audio."""
        features = {}

        # BPM
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        # librosa may return an array; extract scalar
        if hasattr(tempo, '__len__'):
            tempo = float(tempo[0]) if len(tempo) > 0 else 0.0
        features["bpm"] = round(float(tempo), 2) if tempo > 0 else None

        # Key detection
        key_info = self._detect_key(y, sr)
        features.update(key_info)

        # Energy (RMS)
        rms = librosa.feature.rms(y=y)[0]
        features["energy"] = round(float(np.mean(rms)), 6)
        rms_db = librosa.amplitude_to_db(rms)
        features["energy_db"] = round(float(np.mean(rms_db)), 2)

        # Dynamic range (95th - 5th percentile in dB)
        if len(rms_db) > 1:
            features["dynamic_range_db"] = round(
                float(np.percentile(rms_db, 95) - np.percentile(rms_db, 5)), 2
            )
        else:
            features["dynamic_range_db"] = 0.0

        # Brightness (spectral centroid normalized to 0-1)
        spectral_centroid = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
        nyquist = sr / 2.0
        features["brightness"] = round(float(np.mean(spectral_centroid) / nyquist), 4)

        # Zero-crossing rate
        zcr = librosa.feature.zero_crossing_rate(y)[0]
        features["zero_crossing_rate"] = round(float(np.mean(zcr)), 6)

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

    def _extract_clap_features(self, audio_48k: np.ndarray) -> Dict[str, Any]:
        """CLAP moods/vocal/dance + AST+PaSST ensemble instruments."""
        from device import autocast, cast_inputs
        # Encode audio
        inputs = self.processor(
            audio=[audio_48k],
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
        features["instruments"] = self.ensemble.tag(audio_48k)

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
        features.update(self._extract_librosa_features(y_librosa, self.librosa_sr))
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
        Phase 1: librosa DSP at 22kHz (CPU)
        Phase 2: CLAP zero-shot at 48kHz (GPU)
        """
        features = {}

        # Phase 1: librosa
        y_librosa = self._load_middle_segment(file_path, self.librosa_sr)
        if y_librosa is None:
            return None

        features.update(self._extract_librosa_features(y_librosa, self.librosa_sr))
        del y_librosa  # free memory before loading at 48kHz

        # Phase 2: CLAP zero-shot (only if model is loaded)
        if self.model is not None:
            y_clap = self._load_middle_segment(file_path, self.clap_sr)
            if y_clap is not None:
                clap_features = self._extract_clap_features(y_clap)
                features.update(clap_features)
                del y_clap
            else:
                logger.warning(f"CLAP audio load failed for {file_path}, librosa features only")

        return features

    def _extract_clap_features_batch(
        self, audio_arrays: List[np.ndarray]
    ) -> List[Dict[str, Any]]:
        """CLAP moods/vocal/dance for a batch + AST+PaSST per-track ensemble.

        CLAP runs batched (single GPU pass for N tracks); the ensemble is
        invoked per-track since AST/PaSST are cheap enough (~200 ms each)
        and keeping the batch loader simple matters more than micro-gains.
        """
        from device import autocast, cast_inputs
        inputs = self.processor(
            audio=audio_arrays,
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

            features["instruments"] = self.ensemble.tag(audio_48k)

            results.append(features)

        return results

    def _load_and_extract_librosa(self, file_path: str, duration_seconds: float = None) -> Optional[Dict]:
        """Load audio + extract librosa features (I/O + CPU). Thread-safe."""
        try:
            local_path = settings.translate_to_local_path(file_path)
            y_48k = self._load_middle_segment(local_path, self.clap_sr, total_duration=duration_seconds)
            if y_48k is None:
                return None

            y_librosa = librosa.resample(y_48k, orig_sr=self.clap_sr, target_sr=self.librosa_sr)
            librosa_features = self._extract_librosa_features(y_librosa, self.librosa_sr)
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
        prefetch_workers = 4

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
                query_sql = """
                    SELECT t.id as track_id, mf.id as media_file_id,
                           mf.file_path, mf.bit_depth,
                           mf.sample_rate as mf_sample_rate, mf.is_lossless,
                           mf.duration_seconds
                    FROM media_files mf
                    JOIN tracks t ON t.id = mf.track_id
                    LEFT JOIN audio_features af ON af.track_id = t.id
                    WHERE mf.is_analysis_source = true
                      AND (af.id IS NULL
                           OR (af.source_media_file_id IS NOT NULL
                               AND af.source_media_file_id != mf.id))
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

            # Process in batches: prefetch + librosa in threads, CLAP on GPU
            pbar = tqdm(total=total, desc="Analyzing audio", unit="track")
            # Single thread pool for all batches (avoid 2000+ pool creations)
            _io_pool = ThreadPoolExecutor(max_workers=prefetch_workers)

            for batch_start in range(0, total, clap_batch_size):
                if cancel_flag and cancel_flag():
                    logger.info("Audio analysis cancelled by user")
                    break

                if max_duration_seconds:
                    elapsed = time.time() - start_time
                    if elapsed >= max_duration_seconds:
                        logger.info(f"Time limit reached ({elapsed:.1f}s), stopping")
                        break

                batch_rows = rows[batch_start:batch_start + clap_batch_size]

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

                # Phase 3: merge features + save to DB (savepoint per track, commit per batch)
                for (row, prep), clap_feat in zip(prepared, clap_results):
                    try:
                        features = prep["librosa_features"]
                        features.update(clap_feat)

                        savepoint = db.begin_nested()
                        try:
                            existing = db.query(AudioFeature).filter(
                                AudioFeature.track_id == row.track_id
                            ).first()
                            if existing:
                                for k, v in features.items():
                                    setattr(existing, k, v)
                                existing.source_media_file_id = row.media_file_id
                                existing.source_bit_depth = row.bit_depth
                                existing.source_sample_rate = row.mf_sample_rate
                                existing.source_is_lossless = row.is_lossless
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
                                    source_media_file_id=row.media_file_id,
                                    source_bit_depth=row.bit_depth,
                                    source_sample_rate=row.mf_sample_rate,
                                    source_is_lossless=row.is_lossless,
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
