"""
Audio embedding generation using CLAP model.
Generates 512-dimensional audio embeddings for tracks using laion/clap-htsat-unfused.

Uses the analysis source media file (is_analysis_source=TRUE) for each track.
Per track: the track-level embedding (search vector; becomes the materialized
mean of segments after the Stage-2b flip) + windowed segment embeddings on
the canonical 10s grid (embedding_segments).
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

import provenance
from audio_analysis import load_full_track_48k
from config import settings
from database import get_db_context
from models import Embedding, EmbeddingModel, Track, MediaFile
from uuid_utils import embedding_model_uuid

logger = logging.getLogger(__name__)


# ── Canonical segment grid ──────────────────────────────────────────────────
# Window i covers [i*10s, i*10s+10s) from the track start. Every sampling
# density (balanced default, full research grid, "deepen this artist") writes
# a compatible, top-uppable SUBSET of this one grid — an absent index always
# means "not analyzed", which the done-predicates rely on.

WINDOW_SECONDS = 10

# Methodology version stamped on embeddings rows and carried through P2P sync
# (peers re-pull rows whose version is older than the source's):
#   1 — random 10s crop of the middle 30s (CLAP rand_trunc)
#   2 — normalized mean of canonical-grid segment embeddings (2026-07-05)
EMBEDDING_ANALYSIS_VERSION = 2


def balanced_k(duration_seconds: float) -> int:
    """Windows needed for a portrait within 0.99 cosine of the full-grid
    centroid for >=99% of tracks — measured on a 303-track stratified
    experiment 2026-07-04 (constant-concentration and variance-adaptive
    strategies both measured worse at equal cost)."""
    if duration_seconds <= 480:
        return 12
    if duration_seconds <= 1200:
        return 16
    return 24


def segment_grid_indices(duration_seconds: float, full: bool = False) -> list:
    """Evenly spaced canonical-grid indices (first window at the track start,
    last at the end); the whole grid when full=True."""
    n = max(1, int(duration_seconds) // WINDOW_SECONDS)
    if full:
        return list(range(n))
    k = balanced_k(duration_seconds)
    return np.unique(np.linspace(0, n - 1, min(k, n)).round().astype(int)).tolist()


class AudioEmbeddingGenerator:
    """Generate audio embeddings using CLAP model on GPU."""

    def __init__(
        self,
        model_name: Optional[str] = None,
        batch_size: Optional[int] = None,
        device: Optional[str] = None,
    ):
        self.model_name = model_name or settings.embedding_model
        self.batch_size = batch_size or settings.embedding_batch_size
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

    def _load_audio(self, file_path: str) -> Optional[np.ndarray]:
        """Load the WHOLE track at 48kHz mono (shared scanner decode path) —
        segments cover the full canonical grid; the track-level vector's
        middle window is sliced from the same array."""
        try:
            return load_full_track_48k(file_path)
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
            from device import empty_cache
            empty_cache(self.device)
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

    def _compute_segments(self, audio_full: np.ndarray):
        """Balanced-grid segment embeddings for one track from its already
        decoded audio; windows of one track are encoded as one batch. Pure
        compute, no DB. Returns (indices, vectors, normalized mean) — the
        mean is the track-level portrait — or None on failure."""
        duration = len(audio_full) / self.sample_rate
        idxs = segment_grid_indices(duration)
        step = self.sample_rate * WINDOW_SECONDS
        windows = [audio_full[i * step:(i + 1) * step] for i in idxs]
        vecs = self._generate_batch_embeddings(windows)
        if vecs is None:
            return None
        mean = vecs.mean(axis=0)
        return idxs, vecs, mean / np.linalg.norm(mean)

    @staticmethod
    def _quality_score(bit_depth, is_lossless) -> int:
        """Quality priority: CD (16bit lossless) = 100 > other lossless = 50 > lossy = 10."""
        if is_lossless and bit_depth == 16:
            return 100  # CD — ideal for analysis
        if is_lossless:
            return 50   # Hi-Res / DSD / Vinyl
        return 10       # MP3, OGG, M4A

    def _persist_analysis(
        self, db: Session, track_id, model: EmbeddingModel,
        idxs, vecs, portrait: np.ndarray,
        analysis_source_id: Optional[int], origin: str,
        bit_depth: Optional[int] = None, is_lossless: Optional[bool] = None,
    ) -> bool:
        """Upsert the track's embedding row and REPLACE its segments, in one
        transaction unit. The overwrite decision happens before anything is
        written — origin rank first (local > deezer > youtube; unlinked legacy
        rows rank below everything), source quality within the same origin —
        so a lower-ranked pass (e.g. a stream preview racing an owned scan)
        can no longer clobber segments while the mean survives. Replaced
        segments are deleted, not updated: fresh rows are unsigned by
        construction, which is the seal-invalidation model for segments.
        Returns False when the existing row outranks the incoming analysis."""
        existing = db.execute(sa_text("""
            SELECT e.id, s.origin::text AS origin, s.bit_depth, s.is_lossless
            FROM embeddings e
            LEFT JOIN analysis_sources s ON s.id = e.analysis_source_id
            WHERE e.track_id = :tid AND e.model_id = :mid
        """), {"tid": str(track_id), "mid": str(model.id)}).first()
        if existing and existing.origin is not None:
            old_rank = (provenance.ORIGIN_RANK.get(existing.origin, -1),
                        self._quality_score(existing.bit_depth, existing.is_lossless))
            new_rank = (provenance.ORIGIN_RANK.get(origin, -1),
                        self._quality_score(bit_depth, is_lossless))
            if new_rank < old_rank:
                return False

        eid = db.execute(sa_text("""
            INSERT INTO embeddings (track_id, model_id, vector,
                                    analysis_source_id, analysis_version)
            VALUES (:tid, :mid, CAST(:vec AS vector), :sid, :ver)
            ON CONFLICT (track_id, model_id) DO UPDATE
               SET vector = EXCLUDED.vector,
                   analysis_source_id = EXCLUDED.analysis_source_id,
                   analysis_version = EXCLUDED.analysis_version
            RETURNING id
        """), {"tid": str(track_id), "mid": str(model.id),
               "vec": str(portrait.tolist()), "sid": analysis_source_id,
               "ver": EMBEDDING_ANALYSIS_VERSION}).scalar()
        db.execute(sa_text(
            "DELETE FROM embedding_segments WHERE embedding_id = :eid"),
            {"eid": eid})
        for i, v in zip(idxs, vecs):
            db.execute(sa_text("""
                INSERT INTO embedding_segments (embedding_id, segment_index, vector)
                VALUES (:eid, :idx, CAST(:vec AS vector))
            """), {"eid": eid, "idx": int(i), "vec": str(v.tolist())})
        return True

    def embed_track(self, db: Session, track_id, media_file,
                    audio_full: Optional[np.ndarray] = None) -> bool:
        """Full per-track pipeline for a local file: provenance + balanced-grid
        segments + mean portrait. `media_file` is a MediaFile row/namespace
        (id, file_path, sample_rate, bit_depth, is_lossless); audio decodes
        here unless the caller already holds it."""
        if audio_full is None:
            audio_full = self._load_audio(
                settings.translate_to_local_path(media_file.file_path))
            if audio_full is None:
                return False
        model = self._get_or_create_embedding_model(db)
        src_id = provenance.get_or_create_local(
            db, track_id, media_file.id, media_file.file_path,
            media_file.sample_rate, media_file.bit_depth,
            media_file.is_lossless, media_file.duration_seconds)
        computed = self._compute_segments(audio_full)
        if computed is None:
            return False
        idxs, vecs, portrait = computed
        return self._persist_analysis(
            db, track_id, model, idxs, vecs, portrait, src_id,
            origin="local", bit_depth=media_file.bit_depth,
            is_lossless=media_file.is_lossless)

    def generate_embeddings(self, limit: Optional[int] = None, order_by_date: bool = False, max_duration_seconds: Optional[int] = None, track_ids: Optional[list] = None, worker_id: Optional[int] = None, worker_count: Optional[int] = None, cancel_flag=None, force: bool = False) -> Dict[str, int]:
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
            force: Re-embed every analysis-source track regardless of state.

        Returns:
            Statistics dict with keys: processed, success, failed, skipped.
        """
        import time

        stats = {"processed": 0, "success": 0, "failed": 0, "skipped": 0}
        start_time = time.time()

        with get_db_context() as db:
            embedding_model = self._get_or_create_embedding_model(db)

            # Pending = no embedding yet, OR unlinked provenance (legacy rows
            # and failed fingerprints — re-analysis links them), OR linked to
            # material other than the current analysis source (source moved to
            # a better rip, or a stream preview awaiting its owned upgrade).
            # Driven from media_files (is_analysis_source) — the OWNED set.
            # tracks now holds millions of trackless phantom rows; driving from
            # tracks would scan them all to find owned ones needing an embedding.
            query_sql = """
                SELECT t.id as track_id, mf.id as media_file_id,
                       mf.file_path, mf.bit_depth,
                       mf.sample_rate, mf.is_lossless,
                       mf.duration_seconds
                FROM media_files mf
                JOIN tracks t ON t.id = mf.track_id
                LEFT JOIN embeddings e ON e.track_id = t.id
                LEFT JOIN analysis_sources asrc ON asrc.id = e.analysis_source_id
                WHERE mf.is_analysis_source = true
            """
            if not force:
                query_sql += """
                  AND (e.id IS NULL
                       OR e.analysis_source_id IS NULL
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
                return row, self._load_audio(local_path)

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

            # Split into batch slices — bounded by BOTH row count and total
            # audio duration: whole tracks are held decoded in RAM per batch,
            # and a batch of Schulze-length pieces would be gigabytes.
            from device import audio_batch_budget_seconds
            batch_budget_seconds = audio_batch_budget_seconds()
            batch_slices, cur, cur_seconds = [], [], 0.0
            for row in rows:
                dur = float(row.duration_seconds) if row.duration_seconds else 300.0
                if cur and (len(cur) >= self.batch_size
                            or cur_seconds + dur > batch_budget_seconds):
                    batch_slices.append(cur)
                    cur, cur_seconds = [], 0.0
                cur.append(row)
                cur_seconds += dur
            if cur:
                batch_slices.append(cur)
            num_batches = len(batch_slices)

            # Persistent I/O thread pool. Each worker is a whole ffmpeg
            # subprocess — 16 of them saturate a 24-core dev box but thrash
            # a 4-core laptop, so the size comes from the hardware profile.
            from hardware_profile import resolve as _hw
            io_pool = ThreadPoolExecutor(max_workers=_hw().io_workers)
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

                    # Per track: balanced-grid segments (one batched GPU pass
                    # per track) → the track-level vector is their normalized
                    # mean. Next batch decodes in parallel with the GPU work.
                    for row, audio in zip(valid_rows, audio_arrays):
                        src_id = provenance.get_or_create_local(
                            db, row.track_id, row.media_file_id, row.file_path,
                            row.sample_rate, row.bit_depth, row.is_lossless,
                            row.duration_seconds)
                        computed = self._compute_segments(audio)
                        if computed is None:
                            stats["failed"] += 1
                            logger.error(f"Failed embedding for track {row.track_id}")
                            continue
                        idxs, vecs, portrait = computed
                        if self._persist_analysis(
                                db, row.track_id, embedding_model, idxs, vecs,
                                portrait, src_id, origin="local",
                                bit_depth=row.bit_depth,
                                is_lossless=row.is_lossless):
                            stats["success"] += 1
                        else:
                            stats["skipped"] += 1

                    db.commit()
                    # Hand the allocator's free blocks back each batch — else
                    # the caching allocator holds its peak for the whole run
                    # (~28 GB on MPS unified memory; heavy swap on a 16 GB Mac).
                    from device import empty_cache
                    empty_cache(self.device)
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
    force: bool = False,
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
    return generator.generate_embeddings(limit=limit, order_by_date=order_by_date, max_duration_seconds=max_duration_seconds, track_ids=track_ids, worker_id=worker_id, worker_count=worker_count, force=force)
