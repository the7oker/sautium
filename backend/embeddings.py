"""
Audio embedding generation using CLAP model.
Generates 512-dimensional audio embeddings for tracks using laion/clap-htsat-unfused.

Uses the analysis source media file (is_analysis_source=TRUE) for each track.
One embedding per track, not per file.
"""

import logging
from typing import Dict, List, Optional

import librosa
import numpy as np
import torch
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session
from tqdm import tqdm
from transformers import ClapModel, ClapProcessor

from config import settings
from database import get_db_context
from models import Embedding, EmbeddingModel, Track, MediaFile
from uuid_utils import embedding_model_uuid

logger = logging.getLogger(__name__)


class AudioEmbeddingGenerator:
    """Generate audio embeddings using CLAP model on GPU."""

    def __init__(
        self,
        model_name: Optional[str] = None,
        batch_size: Optional[int] = None,
        sample_duration: Optional[int] = None,
        device: Optional[str] = None,
    ):
        self.model_name = model_name or settings.embedding_model
        self.batch_size = batch_size or settings.embedding_batch_size
        self.sample_duration = sample_duration or settings.audio_sample_duration
        self.sample_rate = 48000  # CLAP expects 48kHz

        if device:
            self.device = device
        elif torch.cuda.is_available():
            self.device = "cuda"
        else:
            self.device = "cpu"
            logger.warning("CUDA not available, using CPU (will be slow)")

        self.model = None
        self.processor = None

    def load_model(self):
        """Load CLAP model and processor onto device."""
        if self.model is not None:
            return

        logger.info(f"Loading CLAP model: {self.model_name} on {self.device}")
        self.processor = ClapProcessor.from_pretrained(self.model_name)
        self.model = ClapModel.from_pretrained(self.model_name)
        self.model = self.model.to(self.device)
        self.model.eval()

        if self.device == "cuda":
            mem = torch.cuda.memory_allocated() / 1e9
            logger.info(f"Model loaded, GPU memory used: {mem:.2f} GB")
        else:
            logger.info("Model loaded on CPU")

    def unload_model(self):
        """Free GPU memory by unloading model."""
        if self.model is not None:
            del self.model
            del self.processor
            self.model = None
            self.processor = None
            if self.device == "cuda":
                torch.cuda.empty_cache()
            logger.info("Model unloaded")

    def text_to_embedding(self, text: str) -> np.ndarray:
        """
        Encode text to a 512d embedding using CLAP's text encoder.

        The resulting vector lives in the same space as audio embeddings,
        enabling text-to-audio similarity search.

        Args:
            text: Natural language description (e.g. "slow emotional blues").

        Returns:
            L2-normalized numpy array of shape (512,).
        """
        self.load_model()

        inputs = self.processor(text=[text], return_tensors="pt", padding=True)
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        with torch.no_grad():
            text_features = self.model.get_text_features(**inputs)

        # L2 normalize to match audio embeddings
        text_features = torch.nn.functional.normalize(text_features, p=2, dim=1)

        return text_features[0].cpu().numpy()

    def _load_audio(self, file_path: str) -> Optional[np.ndarray]:
        """
        Load audio file and extract middle segment.

        Loads at 48kHz mono. Extracts the middle `sample_duration` seconds.
        Short tracks (<sample_duration) are used as-is.
        """
        try:
            audio, sr = librosa.load(file_path, sr=self.sample_rate, mono=True)

            total_samples = len(audio)
            target_samples = self.sample_duration * self.sample_rate

            if total_samples > target_samples:
                # Extract middle segment
                start = (total_samples - target_samples) // 2
                audio = audio[start : start + target_samples]

            return audio

        except Exception as e:
            logger.error(f"Failed to load audio {file_path}: {e}")
            return None

    def _generate_batch_embeddings(
        self, audio_arrays: List[np.ndarray]
    ) -> Optional[np.ndarray]:
        """
        Generate embeddings for a batch of audio arrays.

        Returns L2-normalized embeddings as numpy array, or None on failure.
        """
        try:
            inputs = self.processor(
                audios=audio_arrays,
                sampling_rate=self.sample_rate,
                return_tensors="pt",
                padding=True,
            )
            inputs = {k: v.to(self.device) for k, v in inputs.items()}

            with torch.no_grad():
                embeddings = self.model.get_audio_features(**inputs)

            # L2 normalize
            embeddings = torch.nn.functional.normalize(embeddings, p=2, dim=1)

            result = embeddings.cpu().numpy()

            # Check for NaN
            if np.isnan(result).any():
                logger.error("NaN detected in embeddings")
                return None

            return result

        except torch.cuda.OutOfMemoryError:
            logger.error("GPU OOM during batch embedding generation")
            torch.cuda.empty_cache()
            return None
        except Exception as e:
            logger.error(f"Embedding generation failed: {e}")
            return None

    def _get_or_create_embedding_model(self, db: Session) -> EmbeddingModel:
        """Get existing embedding model record or create one (deterministic UUID PK)."""
        mid = embedding_model_uuid(self.model_name)
        em = db.query(EmbeddingModel).filter(EmbeddingModel.id == mid).first()
        if not em:
            em = EmbeddingModel(
                id=mid,
                name=self.model_name,
                description=f"CLAP audio embedding model ({settings.embedding_dimension}d)",
                dimension=settings.embedding_dimension,
            )
            db.add(em)
            db.flush()
            logger.info(f"Created embedding model record: {self.model_name}")
        return em

    def _save_embedding(
        self, db: Session, track_id, vector: np.ndarray, model: EmbeddingModel,
        source_bit_depth: Optional[int] = None,
        source_sample_rate: Optional[int] = None,
        source_is_lossless: Optional[bool] = None,
    ):
        """Create Embedding record linked to track."""
        embedding = Embedding(
            vector=vector.tolist(),
            model_id=model.id,
            track_id=track_id,
            source_bit_depth=source_bit_depth,
            source_sample_rate=source_sample_rate,
            source_is_lossless=source_is_lossless,
        )
        db.add(embedding)
        db.flush()

    def generate_embeddings(self, limit: Optional[int] = None, order_by_date: bool = False, max_duration_seconds: Optional[int] = None, track_ids: Optional[list] = None, worker_id: Optional[int] = None, worker_count: Optional[int] = None) -> Dict[str, int]:
        """
        Generate embeddings for tracks that don't have them yet.

        Queries tracks without embeddings, picks the analysis source media file
        (is_analysis_source=TRUE) for audio loading.

        Args:
            limit: Maximum number of tracks to process.
            order_by_date: If True, process newest tracks first.
            max_duration_seconds: Maximum duration in seconds.
            track_ids: If provided, only process these track IDs.
            worker_id: Worker index (0-based) for parallel processing.
            worker_count: Total number of workers for parallel processing.

        Returns:
            Statistics dict with keys: processed, success, failed, skipped.
        """
        import time

        stats = {"processed": 0, "success": 0, "failed": 0, "skipped": 0}
        start_time = time.time()

        self.load_model()

        try:
            with get_db_context() as db:
                embedding_model = self._get_or_create_embedding_model(db)

                # Query tracks without embeddings, joining to analysis source media file
                query_sql = """
                    SELECT t.id as track_id, mf.file_path, mf.bit_depth,
                           mf.sample_rate, mf.is_lossless
                    FROM tracks t
                    LEFT JOIN embeddings e ON e.track_id = t.id
                    JOIN media_files mf ON mf.track_id = t.id
                        AND mf.is_analysis_source = true
                    WHERE e.id IS NULL
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
                    logger.info("No tracks pending embedding generation")
                    return stats

                logger.info(
                    f"Processing {total} tracks (batch_size={self.batch_size})"
                )
                if max_duration_seconds:
                    logger.info(f"Time limit: {max_duration_seconds} seconds ({max_duration_seconds/60:.1f} minutes)")

                # Process in batches
                for batch_start in tqdm(
                    range(0, total, self.batch_size),
                    desc="Generating embeddings",
                    unit="batch",
                ):
                    # Check time limit before starting new batch
                    if max_duration_seconds:
                        elapsed = time.time() - start_time
                        if elapsed >= max_duration_seconds:
                            logger.info(f"Time limit reached ({elapsed:.1f}s), stopping gracefully")
                            break

                    batch_rows = rows[batch_start : batch_start + self.batch_size]
                    audio_arrays = []
                    valid_rows = []

                    # Load audio for batch
                    for row in batch_rows:
                        stats["processed"] += 1
                        # DB stores native OS paths; translate back to local for file access in Docker
                        local_path = settings.translate_to_local_path(row.file_path)
                        audio = self._load_audio(local_path)
                        if audio is not None:
                            audio_arrays.append(audio)
                            valid_rows.append(row)
                        else:
                            stats["failed"] += 1
                            logger.warning(
                                f"Skipping track {row.track_id}: audio load failed"
                            )

                    if not audio_arrays:
                        continue

                    # Generate embeddings for batch
                    embeddings = self._generate_batch_embeddings(audio_arrays)

                    if embeddings is None:
                        # OOM or error - try one by one
                        logger.warning(
                            "Batch failed, falling back to single processing"
                        )
                        for i, (audio, row) in enumerate(
                            zip(audio_arrays, valid_rows)
                        ):
                            single = self._generate_batch_embeddings([audio])
                            if single is not None:
                                self._save_embedding(
                                    db, row.track_id, single[0], embedding_model,
                                    source_bit_depth=row.bit_depth,
                                    source_sample_rate=row.sample_rate,
                                    source_is_lossless=row.is_lossless,
                                )
                                stats["success"] += 1
                            else:
                                stats["failed"] += 1
                                logger.error(
                                    f"Failed single embedding for track {row.track_id}"
                                )
                    else:
                        for row, vector in zip(valid_rows, embeddings):
                            self._save_embedding(
                                db, row.track_id, vector, embedding_model,
                                source_bit_depth=row.bit_depth,
                                source_sample_rate=row.sample_rate,
                                source_is_lossless=row.is_lossless,
                            )
                            stats["success"] += 1

                    # Commit after each batch
                    db.commit()

                logger.info(
                    f"Embedding generation complete: "
                    f"{stats['success']} success, {stats['failed']} failed"
                )

        finally:
            self.unload_model()

        return stats


def generate_embeddings(
    limit: Optional[int] = None, batch_size: Optional[int] = None, order_by_date: bool = False, max_duration_seconds: Optional[int] = None, track_ids: Optional[list] = None,
    worker_id: Optional[int] = None, worker_count: Optional[int] = None,
) -> Dict[str, int]:
    """
    Convenience function to generate embeddings.

    Args:
        limit: Maximum number of tracks to process.
        batch_size: Override default batch size.
        order_by_date: If True, process newest tracks first.
        max_duration_seconds: Maximum duration in seconds.
        track_ids: If provided, only process these track IDs.
        worker_id: Worker index (0-based) for parallel processing.
        worker_count: Total number of workers for parallel processing.

    Returns:
        Statistics dictionary.
    """
    generator = AudioEmbeddingGenerator(batch_size=batch_size)
    return generator.generate_embeddings(limit=limit, order_by_date=order_by_date, max_duration_seconds=max_duration_seconds, track_ids=track_ids, worker_id=worker_id, worker_count=worker_count)
