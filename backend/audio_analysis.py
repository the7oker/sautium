"""
Audio feature extraction using librosa (DSP) and CLAP zero-shot classification.

Extracts:
- librosa: BPM, key/mode, energy, brightness, dynamic range, ZCR
- CLAP zero-shot: instruments, moods, vocal/instrumental, danceability

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
from transformers import ClapModel, ClapProcessor

from config import settings
from database import get_db_context
from models import AudioFeature, Track, MediaFile

logger = logging.getLogger(__name__)


# --- CLAP zero-shot label sets ---

INSTRUMENT_LABELS = [
    "acoustic guitar", "electric guitar", "bass guitar",
    "piano", "keyboards and synthesizer", "organ",
    "drums and percussion", "violin and strings", "cello",
    "trumpet", "saxophone", "flute", "harmonica",
    "harp", "clarinet", "trombone", "accordion",
]

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

        if device:
            self.device = device
        elif torch.cuda.is_available():
            self.device = "cuda"
        else:
            self.device = "cpu"
            logger.warning("CUDA not available, CLAP zero-shot will be slow on CPU")

        self.model = None
        self.processor = None
        self._text_embeddings_cache = {}

    # --- Model management ---

    def load_model(self):
        """Load CLAP model for zero-shot classification."""
        if self.model is not None:
            return

        logger.info(f"Loading CLAP model: {self.model_name} on {self.device}")
        self.processor = ClapProcessor.from_pretrained(self.model_name)
        self.model = ClapModel.from_pretrained(self.model_name)
        self.model = self.model.to(self.device)
        self.model.eval()

        # Pre-encode all text label sets
        self._encode_text_labels()

        if self.device == "cuda":
            mem = torch.cuda.memory_allocated() / 1e9
            logger.info(f"CLAP model loaded, GPU memory: {mem:.2f} GB")

    def unload_model(self):
        """Free GPU memory."""
        if self.model is not None:
            del self.model
            del self.processor
            self.model = None
            self.processor = None
            self._text_embeddings_cache = {}
            if self.device == "cuda":
                torch.cuda.empty_cache()
            logger.info("CLAP model unloaded")

    def _encode_text_labels(self):
        """Pre-encode all zero-shot label sets. Called once, reused for all tracks."""
        label_sets = {
            "instruments": [f"This is a sound of {l}" for l in INSTRUMENT_LABELS],
            "moods": [f"This is {l} music" for l in MOOD_LABELS],
            "vocal": [f"This is {l}" for l in VOCAL_LABELS],
            "dance": [f"This is {l}" for l in DANCE_LABELS],
        }

        for key, prompts in label_sets.items():
            inputs = self.processor(text=prompts, return_tensors="pt", padding=True)
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
            with torch.no_grad():
                text_features = self.model.get_text_features(**inputs)
                text_features = torch.nn.functional.normalize(text_features, p=2, dim=1)
            self._text_embeddings_cache[key] = text_features

        logger.info(f"Pre-encoded {sum(len(v) for v in label_sets.values())} text labels")

    # --- Audio loading ---

    def _load_middle_segment(self, file_path: str, sr: int) -> Optional[np.ndarray]:
        """Load the middle N seconds of an audio file at given sample rate."""
        try:
            audio, _ = librosa.load(file_path, sr=sr, mono=True)
            total_samples = len(audio)
            target_samples = self.duration * sr

            if total_samples > target_samples:
                start = (total_samples - target_samples) // 2
                audio = audio[start:start + target_samples]

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
        features["bpm"] = round(float(tempo), 2)

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
        """Run CLAP zero-shot classification for instruments, moods, vocal, dance."""
        # Encode audio
        inputs = self.processor(
            audio=[audio_48k],
            sampling_rate=self.clap_sr,
            return_tensors="pt",
            padding=True,
        )
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        with torch.no_grad():
            audio_features = self.model.get_audio_features(**inputs)
            audio_features = torch.nn.functional.normalize(audio_features, p=2, dim=1)

        features = {}

        # Instruments: store all with score > 0.05
        inst_probs = self._classify_zero_shot(audio_features, "instruments", INSTRUMENT_LABELS)
        features["instruments"] = {
            k: v for k, v in sorted(inst_probs.items(), key=lambda x: -x[1]) if v > 0.05
        }

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

        return features

    # --- Main analysis pipeline ---

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
    ) -> Dict[str, int]:
        """
        Batch analyze tracks and store results in audio_features table.

        Uses the analysis source media file (is_analysis_source=TRUE) for each track.

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

        stats = {"processed": 0, "success": 0, "failed": 0, "skipped": 0}
        start_time = time.time()

        if not librosa_only:
            self.load_model()

        try:
            with get_db_context() as db:
                # Query tracks to analyze, joining to analysis source media file
                if force:
                    query_sql = """
                        SELECT t.id as track_id, mf.file_path, mf.bit_depth,
                               mf.sample_rate as mf_sample_rate, mf.is_lossless
                        FROM tracks t
                        JOIN media_files mf ON mf.track_id = t.id
                            AND mf.is_analysis_source = true
                    """
                else:
                    query_sql = """
                        SELECT t.id as track_id, mf.file_path, mf.bit_depth,
                               mf.sample_rate as mf_sample_rate, mf.is_lossless
                        FROM tracks t
                        LEFT JOIN audio_features af ON af.track_id = t.id
                        JOIN media_files mf ON mf.track_id = t.id
                            AND mf.is_analysis_source = true
                        WHERE af.id IS NULL
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

                logger.info(f"Analyzing {total} tracks (librosa_only={librosa_only})")
                if max_duration_seconds:
                    logger.info(f"Time limit: {max_duration_seconds} seconds ({max_duration_seconds/60:.1f} minutes)")

                for row in tqdm(rows, desc="Analyzing audio", unit="track"):
                    # Check time limit before starting new track
                    if max_duration_seconds:
                        elapsed = time.time() - start_time
                        if elapsed >= max_duration_seconds:
                            logger.info(f"Time limit reached ({elapsed:.1f}s), stopping gracefully")
                            break

                    stats["processed"] += 1

                    try:
                        # DB stores native OS paths; translate back to local for file access in Docker
                        local_path = settings.translate_to_local_path(row.file_path)
                        features = self.analyze_track(local_path)
                        if features is None:
                            stats["failed"] += 1
                            continue

                        # Upsert into audio_features
                        if force:
                            existing = db.query(AudioFeature).filter(
                                AudioFeature.track_id == row.track_id
                            ).first()
                            if existing:
                                for k, v in features.items():
                                    setattr(existing, k, v)
                                existing.source_bit_depth = row.bit_depth
                                existing.source_sample_rate = row.mf_sample_rate
                                existing.source_is_lossless = row.is_lossless
                                db.commit()
                                stats["success"] += 1
                                continue

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
                            source_bit_depth=row.bit_depth,
                            source_sample_rate=row.mf_sample_rate,
                            source_is_lossless=row.is_lossless,
                        )
                        db.add(af)
                        db.commit()
                        stats["success"] += 1

                    except Exception as e:
                        db.rollback()
                        stats["failed"] += 1
                        logger.error(f"Failed to analyze track {row.track_id}: {e}")

                logger.info(
                    f"Audio analysis complete: {stats['success']} success, {stats['failed']} failed"
                )

        finally:
            if not librosa_only:
                self.unload_model()

        return stats
