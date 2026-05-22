"""
Audio embedding generation using CLAP model.
Generates 512-dimensional audio embeddings for tracks using laion/clap-htsat-unfused.

Uses the analysis source media file (is_analysis_source=TRUE) for each track.
One embedding per track, not per file.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import librosa
import numpy as np
import torch
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session
from tqdm import tqdm

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

        from device import get_device
        self.device = device or get_device()
        if self.device == "cpu":
            logger.warning("No GPU accelerator detected, using CPU (will be slow)")

        self.model = None
        self.processor = None

    def load_model(self):
        """Acquire process-wide singleton CLAP processor + model."""
        if self.model is not None:
            return
        from clap_model import get_clap_model
        self.processor, self.model = get_clap_model(self.model_name, self.device)

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
        from device import autocast, cast_inputs
        self.load_model()

        inputs = self.processor(text=[text], return_tensors="pt", padding=True)
        inputs = cast_inputs(inputs, self.model.dtype)
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        with torch.no_grad(), autocast(self.device):
            text_features = self.model.get_text_features(**inputs)

        # L2 normalize in fp32 — normalization on fp16 saturates near zero
        text_features = torch.nn.functional.normalize(text_features.float(), p=2, dim=1)

        return text_features[0].cpu().numpy()

    def _load_audio(self, file_path: str, duration_seconds: float = None) -> Optional[np.ndarray]:
        """
        Load audio file and extract middle segment.

        Loads at 48kHz mono. Only decodes the middle `sample_duration` seconds
        using librosa's offset/duration params to avoid reading the entire file.
        Short tracks (<sample_duration) are used as-is.

        Args:
            file_path: Path to audio file.
            duration_seconds: Total track duration (from DB) to calculate offset
                without reading the full file. If None, loads entire file.
        """
        try:
            offset = 0.0
            load_duration = None

            if duration_seconds and duration_seconds > self.sample_duration:
                # Only decode the middle segment — massive I/O savings
                offset = (duration_seconds - self.sample_duration) / 2.0
                load_duration = float(self.sample_duration)

            audio, sr = librosa.load(
                file_path, sr=self.sample_rate, mono=True,
                offset=offset, duration=load_duration,
            )

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
            from device import autocast, cast_inputs
            inputs = self.processor(
                audio=audio_arrays,
                sampling_rate=self.sample_rate,
                return_tensors="pt",
                padding=True,
            )
            inputs = cast_inputs(inputs, self.model.dtype)
            inputs = {k: v.to(self.device) for k, v in inputs.items()}

            with torch.no_grad(), autocast(self.device):
                embeddings = self.model.get_audio_features(**inputs)

            # Diagnostic: report dtype + pre-normalize stats on first NaN to
            # distinguish fp16 attention overflow (post-encoder inf/NaN) from
            # actual model output failure.
            pre_nan = torch.isnan(embeddings).any().item()
            pre_inf = torch.isinf(embeddings).any().item()

            # L2 normalize in fp32 — fp16 norm saturates and loses precision
            embeddings = torch.nn.functional.normalize(embeddings.float(), p=2, dim=1)

            result = embeddings.cpu().numpy()

            if np.isnan(result).any():
                logger.error(
                    "NaN detected in embeddings (model_dtype=%s, pre_norm_nan=%s, pre_norm_inf=%s, batch=%d)",
                    self.model.dtype, pre_nan, pre_inf, len(audio_arrays),
                )
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

    @staticmethod
    def _quality_score(bit_depth, is_lossless) -> int:
        """Quality priority: CD (16bit lossless) = 100 > other lossless = 50 > lossy = 10."""
        if is_lossless and bit_depth == 16:
            return 100  # CD — ideal for analysis
        if is_lossless:
            return 50   # Hi-Res / DSD / Vinyl
        return 10       # MP3, OGG, M4A

    def _save_embedding(
        self, db: Session, track_id, vector: np.ndarray, model: EmbeddingModel,
        source_media_file_id: Optional[int] = None,
        source_bit_depth: Optional[int] = None,
        source_sample_rate: Optional[int] = None,
        source_is_lossless: Optional[bool] = None,
    ):
        """Create or update Embedding record. Won't overwrite higher quality source."""
        existing = db.query(Embedding).filter(
            Embedding.track_id == track_id,
            Embedding.model_id == model.id,
        ).first()

        if existing:
            old_score = self._quality_score(existing.source_bit_depth, existing.source_is_lossless)
            new_score = self._quality_score(source_bit_depth, source_is_lossless)
            if new_score < old_score:
                return  # Don't overwrite better quality embedding
            existing.vector = vector.tolist()
            existing.source_media_file_id = source_media_file_id
            existing.source_bit_depth = source_bit_depth
            existing.source_sample_rate = source_sample_rate
            existing.source_is_lossless = source_is_lossless
        else:
            embedding = Embedding(
                vector=vector.tolist(),
                model_id=model.id,
                track_id=track_id,
                source_media_file_id=source_media_file_id,
                source_bit_depth=source_bit_depth,
                source_sample_rate=source_sample_rate,
                source_is_lossless=source_is_lossless,
            )
            db.add(embedding)
        db.flush()

    def generate_embeddings(self, limit: Optional[int] = None, order_by_date: bool = False, max_duration_seconds: Optional[int] = None, track_ids: Optional[list] = None, worker_id: Optional[int] = None, worker_count: Optional[int] = None, cancel_flag=None) -> Dict[str, int]:
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

        with get_db_context() as db:
            embedding_model = self._get_or_create_embedding_model(db)

            # Query tracks without embeddings OR with stale embeddings
            # (analysis source changed since embedding was generated)
            query_sql = """
                SELECT t.id as track_id, mf.id as media_file_id,
                       mf.file_path, mf.bit_depth,
                       mf.sample_rate, mf.is_lossless,
                       mf.duration_seconds
                FROM tracks t
                LEFT JOIN embeddings e ON e.track_id = t.id
                JOIN LATERAL (
                    SELECT * FROM media_files
                    WHERE track_id = t.id AND is_analysis_source = true
                    ORDER BY id LIMIT 1
                ) mf ON true
                WHERE e.id IS NULL
                   OR (e.source_media_file_id IS NOT NULL
                       AND e.source_media_file_id != mf.id)
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

            self.load_model()

            logger.info(
                f"Processing {total} tracks (batch_size={self.batch_size})"
            )
            if max_duration_seconds:
                logger.info(f"Time limit: {max_duration_seconds} seconds ({max_duration_seconds/60:.1f} minutes)")

            # --- Pipelined batch processing ---
            # CPU threads decode batch N+1 while GPU processes batch N
            def _load_one(row):
                local_path = settings.translate_to_local_path(row.file_path)
                dur = float(row.duration_seconds) if row.duration_seconds else None
                return row, self._load_audio(local_path, duration_seconds=dur)

            def _load_batch(batch_rows):
                """Load audio for a batch using thread pool. Returns (valid_rows, audio_arrays, failed_count)."""
                audio_arrays = []
                valid_rows = []
                failed = 0
                futures = [io_pool.submit(_load_one, row) for row in batch_rows]
                for future in as_completed(futures):
                    row, audio = future.result()
                    if audio is not None:
                        audio_arrays.append(audio)
                        valid_rows.append(row)
                    else:
                        failed += 1
                        logger.warning(f"Skipping track {row.track_id}: audio load failed")
                return valid_rows, audio_arrays, failed

            # Split into batch slices
            batch_slices = [rows[i:i + self.batch_size] for i in range(0, total, self.batch_size)]
            num_batches = len(batch_slices)

            # Persistent I/O thread pool (16 workers for i9-14900HX)
            io_pool = ThreadPoolExecutor(max_workers=16)
            # Prefetch pool runs _load_batch in background
            prefetch_pool = ThreadPoolExecutor(max_workers=1)

            try:
                # Pre-submit first batch loading
                prefetch_future = prefetch_pool.submit(_load_batch, batch_slices[0])

                for batch_idx in tqdm(range(num_batches), desc="Generating embeddings", unit="batch"):
                    # Check cancellation
                    if cancel_flag and cancel_flag():
                        logger.info("Embedding generation cancelled by user")
                        break

                    # Check time limit
                    if max_duration_seconds:
                        elapsed = time.time() - start_time
                        if elapsed >= max_duration_seconds:
                            logger.info(f"Time limit reached ({elapsed:.1f}s), stopping gracefully")
                            break

                    # Wait for current batch audio (should already be ready or nearly ready)
                    valid_rows, audio_arrays, batch_failed = prefetch_future.result()
                    stats["processed"] += len(batch_slices[batch_idx])
                    stats["failed"] += batch_failed

                    # Start loading NEXT batch immediately (runs while GPU works)
                    if batch_idx + 1 < num_batches:
                        prefetch_future = prefetch_pool.submit(_load_batch, batch_slices[batch_idx + 1])

                    if not audio_arrays:
                        continue

                    # Generate embeddings on GPU (next batch loads in parallel!)
                    embeddings = self._generate_batch_embeddings(audio_arrays)

                    if embeddings is None:
                        logger.warning("Batch failed, falling back to single processing")
                        for audio, row in zip(audio_arrays, valid_rows):
                            single = self._generate_batch_embeddings([audio])
                            if single is not None:
                                self._save_embedding(
                                    db, row.track_id, single[0], embedding_model,
                                    source_media_file_id=row.media_file_id,
                                    source_bit_depth=row.bit_depth,
                                    source_sample_rate=row.sample_rate,
                                    source_is_lossless=row.is_lossless,
                                )
                                stats["success"] += 1
                            else:
                                stats["failed"] += 1
                                logger.error(f"Failed single embedding for track {row.track_id}")
                    else:
                        for row, vector in zip(valid_rows, embeddings):
                            self._save_embedding(
                                db, row.track_id, vector, embedding_model,
                                source_media_file_id=row.media_file_id,
                                source_bit_depth=row.bit_depth,
                                source_sample_rate=row.sample_rate,
                                source_is_lossless=row.is_lossless,
                            )
                            stats["success"] += 1

                    db.commit()
            finally:
                io_pool.shutdown(wait=True, cancel_futures=True)
                prefetch_pool.shutdown(wait=True, cancel_futures=True)

            logger.info(
                f"Embedding generation complete: "
                f"{stats['success']} success, {stats['failed']} failed"
            )


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
