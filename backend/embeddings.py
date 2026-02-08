"""
Audio embedding generation using CLAP model.
Generates 512-dimensional audio embeddings for tracks using laion/clap-htsat-unfused.
"""

import logging
from typing import Dict, List, Optional

import librosa
import numpy as np
import torch
from sqlalchemy.orm import Session
from tqdm import tqdm
from transformers import ClapModel, ClapProcessor

from config import settings
from database import get_db_context
from models import Embedding, EmbeddingModel, Track

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
        """Get existing embedding model record or create one."""
        em = (
            db.query(EmbeddingModel)
            .filter(EmbeddingModel.name == self.model_name)
            .first()
        )
        if not em:
            em = EmbeddingModel(
                name=self.model_name,
                description=f"CLAP audio embedding model ({settings.embedding_dimension}d)",
                dimension=settings.embedding_dimension,
            )
            db.add(em)
            db.flush()
            logger.info(f"Created embedding model record: {self.model_name}")
        return em

    def _save_embedding(
        self, db: Session, track: Track, vector: np.ndarray, model: EmbeddingModel
    ):
        """Create Embedding record and link to track."""
        embedding = Embedding(
            vector=vector.tolist(),
            model_id=model.id,
        )
        db.add(embedding)
        db.flush()

        track.embedding_id = embedding.id

    def generate_embeddings(self, limit: Optional[int] = None) -> Dict[str, int]:
        """
        Generate embeddings for tracks that don't have them yet.

        Args:
            limit: Maximum number of tracks to process.

        Returns:
            Statistics dict with keys: processed, success, failed, skipped.
        """
        stats = {"processed": 0, "success": 0, "failed": 0, "skipped": 0}

        self.load_model()

        try:
            with get_db_context() as db:
                embedding_model = self._get_or_create_embedding_model(db)

                # Query tracks without embeddings
                query = db.query(Track).filter(Track.embedding_id.is_(None))
                if limit:
                    query = query.limit(limit)

                tracks = query.all()
                total = len(tracks)

                if total == 0:
                    logger.info("No tracks pending embedding generation")
                    return stats

                logger.info(
                    f"Processing {total} tracks (batch_size={self.batch_size})"
                )

                # Process in batches
                for batch_start in tqdm(
                    range(0, total, self.batch_size),
                    desc="Generating embeddings",
                    unit="batch",
                ):
                    batch_tracks = tracks[
                        batch_start : batch_start + self.batch_size
                    ]
                    audio_arrays = []
                    valid_tracks = []

                    # Load audio for batch
                    for track in batch_tracks:
                        stats["processed"] += 1
                        audio = self._load_audio(track.file_path)
                        if audio is not None:
                            audio_arrays.append(audio)
                            valid_tracks.append(track)
                        else:
                            stats["failed"] += 1
                            logger.warning(
                                f"Skipping track {track.id}: audio load failed"
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
                        for i, (audio, track) in enumerate(
                            zip(audio_arrays, valid_tracks)
                        ):
                            single = self._generate_batch_embeddings([audio])
                            if single is not None:
                                self._save_embedding(
                                    db, track, single[0], embedding_model
                                )
                                stats["success"] += 1
                            else:
                                stats["failed"] += 1
                                logger.error(
                                    f"Failed single embedding for track {track.id}"
                                )
                    else:
                        for track, vector in zip(valid_tracks, embeddings):
                            self._save_embedding(
                                db, track, vector, embedding_model
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
    limit: Optional[int] = None, batch_size: Optional[int] = None
) -> Dict[str, int]:
    """
    Convenience function to generate embeddings.

    Args:
        limit: Maximum number of tracks to process.
        batch_size: Override default batch size.

    Returns:
        Statistics dictionary.
    """
    generator = AudioEmbeddingGenerator(batch_size=batch_size)
    return generator.generate_embeddings(limit=limit)
