"""
Lyrics embedding generation using sentence-transformers.
Generates 384-dimensional embeddings from track lyrics for semantic search
by lyrical content ("songs about rain", "love songs", "protest songs").

Pipeline:
  1. Fetch plain_lyrics from track_lyrics (non-instrumental only)
  2. Deduplicate lines (remove repeated choruses/verses)
  3. Remove English stop words
  4. Chunk if text exceeds model token limit (~200 tokens)
  5. Encode each chunk → store in lyrics_embeddings with chunk_index
"""

import logging
import math
from typing import Any, Dict, List, Optional

import numpy as np
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session
from tqdm import tqdm

from config import settings
from database import get_db_context
from models import EmbeddingModel, LyricsEmbedding

logger = logging.getLogger(__name__)

# Standard English stop words (~170 words).
# These carry no semantic meaning for lyrics content search.
STOP_WORDS = frozenset({
    "a", "about", "above", "after", "again", "against", "all", "am", "an",
    "and", "any", "are", "aren't", "as", "at", "be", "because", "been",
    "before", "being", "below", "between", "both", "but", "by", "can",
    "can't", "cannot", "could", "couldn't", "did", "didn't", "do", "does",
    "doesn't", "doing", "don't", "down", "during", "each", "few", "for",
    "from", "further", "get", "got", "had", "hadn't", "has", "hasn't",
    "have", "haven't", "having", "he", "he'd", "he'll", "he's", "her",
    "here", "here's", "hers", "herself", "him", "himself", "his", "how",
    "how's", "i", "i'd", "i'll", "i'm", "i've", "if", "in", "into", "is",
    "isn't", "it", "it's", "its", "itself", "let", "let's", "me", "more",
    "most", "mustn't", "my", "myself", "no", "nor", "not", "of", "off",
    "on", "once", "only", "or", "other", "ought", "our", "ours", "ourselves",
    "out", "over", "own", "same", "shan't", "she", "she'd", "she'll",
    "she's", "should", "shouldn't", "so", "some", "such", "than", "that",
    "that's", "the", "their", "theirs", "them", "themselves", "then",
    "there", "there's", "these", "they", "they'd", "they'll", "they're",
    "they've", "this", "those", "through", "to", "too", "under", "until",
    "up", "very", "was", "wasn't", "we", "we'd", "we'll", "we're", "we've",
    "were", "weren't", "what", "what's", "when", "when's", "where",
    "where's", "which", "while", "who", "who's", "whom", "why", "why's",
    "will", "with", "won't", "would", "wouldn't", "you", "you'd", "you'll",
    "you're", "you've", "your", "yours", "yourself", "yourselves",
})


def prepare_lyrics_text(plain_lyrics: str) -> str:
    """
    Prepare lyrics for embedding: deduplicate lines, remove stop words.

    Args:
        plain_lyrics: Raw lyrics text.

    Returns:
        Cleaned text ready for embedding.
    """
    lines = plain_lyrics.strip().split("\n")

    # 1. Deduplicate: preserve order, remove exact repeated lines
    seen = set()
    unique_lines = []
    for line in lines:
        normalized = line.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique_lines.append(line.strip())

    # 2. Remove stop words
    text = " ".join(unique_lines)
    words = text.split()
    words = [w for w in words if w.lower() not in STOP_WORDS]

    return " ".join(words)


def split_into_balanced_chunks(text: str, max_tokens: int = 200) -> List[str]:
    """
    Split text into approximately equal chunks that fit within max_tokens.

    Uses rough estimate: 1 token ~ 0.75 words for English text.

    Args:
        text: Input text.
        max_tokens: Max tokens per chunk (model limit is 256, use 200 for safety).

    Returns:
        List of text chunks (1 element if fits in single chunk).
    """
    words = text.split()
    if not words:
        return []

    # Rough estimate: 1 token ≈ 0.75 words for English
    estimated_tokens = len(words) / 0.75

    if estimated_tokens <= max_tokens:
        return [text]

    # Calculate number of chunks needed
    n_chunks = math.ceil(estimated_tokens / max_tokens)
    # Split words into n equal parts
    chunk_size = math.ceil(len(words) / n_chunks)

    chunks = []
    for i in range(0, len(words), chunk_size):
        chunk = " ".join(words[i : i + chunk_size])
        if chunk:
            chunks.append(chunk)

    return chunks


class LyricsEmbeddingGenerator:
    """Generate embeddings from track lyrics using sentence-transformers."""

    def __init__(
        self,
        model_name: Optional[str] = None,
        batch_size: Optional[int] = None,
        device: Optional[str] = None,
    ):
        self.model_name = model_name or settings.text_embedding_model
        self.batch_size = batch_size or settings.text_embedding_batch_size
        self.dimension = settings.text_embedding_dimension

        if device:
            self.device = device
        else:
            import torch
            self.device = "cuda" if torch.cuda.is_available() else "cpu"

        self.model = None

    def load_model(self):
        """Load sentence-transformers model."""
        if self.model is not None:
            return

        from sentence_transformers import SentenceTransformer

        logger.info(f"Loading text embedding model: {self.model_name} on {self.device}")
        self.model = SentenceTransformer(self.model_name, device=self.device)
        logger.info("Text embedding model loaded")

    def unload_model(self):
        """Free memory by unloading model."""
        if self.model is not None:
            del self.model
            self.model = None
            import torch
            if self.device == "cuda":
                torch.cuda.empty_cache()
            logger.info("Text embedding model unloaded")

    def encode(self, texts: List[str]) -> np.ndarray:
        """Encode a list of texts into embeddings."""
        self.load_model()
        return self.model.encode(
            texts,
            batch_size=self.batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,
        )

    def query_to_embedding(self, query: str) -> np.ndarray:
        """Encode a search query into a 384d embedding vector."""
        self.load_model()
        return self.model.encode(
            query,
            normalize_embeddings=True,
        )

    def _get_or_create_model_record(self, db: Session) -> EmbeddingModel:
        """Get or create the embedding model record in DB."""
        em = (
            db.query(EmbeddingModel)
            .filter(EmbeddingModel.name == self.model_name)
            .first()
        )
        if not em:
            em = EmbeddingModel(
                name=self.model_name,
                description=f"Sentence-transformers text embedding model ({self.dimension}d)",
                dimension=self.dimension,
            )
            db.add(em)
            db.flush()
            logger.info(f"Created embedding model record: {self.model_name}")
        return em

    def generate_all(
        self,
        db: Session,
        limit: Optional[int] = None,
        force: bool = False,
        track_ids: Optional[list] = None,
    ) -> Dict[str, int]:
        """
        Generate lyrics embeddings for tracks with lyrics.

        Args:
            db: Database session.
            limit: Max tracks to process.
            force: If True, delete existing and regenerate.
            track_ids: If provided, only process these track IDs.

        Returns:
            Stats dict with processed, success, skipped, failed, chunks counts.
        """
        import time

        stats = {"processed": 0, "success": 0, "skipped": 0, "failed": 0, "chunks": 0}
        start_time = time.time()

        # Find tracks with lyrics that need embedding
        where_parts = [
            "tl.plain_lyrics IS NOT NULL",
            "tl.instrumental = FALSE",
        ]
        params: Dict[str, Any] = {}

        if not force:
            where_parts.append("""
                t.id NOT IN (SELECT DISTINCT le.track_id FROM lyrics_embeddings le)
            """)

        if track_ids is not None:
            where_parts.append("t.id = ANY(:filter_track_ids)")
            params["filter_track_ids"] = track_ids

        where_clause = "WHERE " + " AND ".join(where_parts)

        query_sql = f"""
            SELECT t.id as track_id, tl.plain_lyrics
            FROM tracks t
            JOIN track_lyrics tl ON tl.track_id = t.id
            {where_clause}
            ORDER BY t.id
        """
        if limit:
            query_sql += f" LIMIT {limit}"

        rows = db.execute(sa_text(query_sql), params).fetchall()

        if not rows:
            logger.info("No tracks pending lyrics embedding generation")
            return stats

        logger.info(f"Processing {len(rows)} tracks for lyrics embeddings")

        self.load_model()
        model_record = self._get_or_create_model_record(db)

        # If force, delete existing embeddings for these tracks
        if force:
            track_ids_to_delete = [r.track_id for r in rows]
            db.execute(
                sa_text("DELETE FROM lyrics_embeddings WHERE track_id = ANY(:ids)"),
                {"ids": track_ids_to_delete},
            )
            db.flush()

        # Process in batches
        batch_size = self.batch_size

        for batch_start in tqdm(
            range(0, len(rows), batch_size),
            desc="Generating lyrics embeddings",
            unit="batch",
        ):
            batch_rows = rows[batch_start : batch_start + batch_size]

            # Prepare all chunks for this batch
            all_chunks = []  # list of (track_id, chunk_index, chunk_text)
            for row in batch_rows:
                prepared = prepare_lyrics_text(row.plain_lyrics)
                if not prepared.strip():
                    stats["skipped"] += 1
                    continue

                chunks = split_into_balanced_chunks(prepared)
                for ci, chunk in enumerate(chunks):
                    all_chunks.append((row.track_id, ci, chunk))

            if not all_chunks:
                stats["processed"] += len(batch_rows)
                continue

            # Encode all chunks at once
            chunk_texts = [c[2] for c in all_chunks]
            try:
                embeddings = self.encode(chunk_texts)
            except Exception as e:
                logger.error(f"Encoding failed for batch at {batch_start}: {e}")
                stats["failed"] += len(batch_rows)
                continue

            # Save embeddings
            current_track = None
            track_ok = True
            for (track_id, chunk_index, chunk_text), vector in zip(all_chunks, embeddings):
                if track_id != current_track:
                    current_track = track_id
                    track_ok = True

                if not track_ok:
                    continue

                try:
                    le = LyricsEmbedding(
                        track_id=track_id,
                        model_id=model_record.id,
                        vector=vector.tolist(),
                        chunk_index=chunk_index,
                        chunk_text=chunk_text[:500],  # truncate for debug storage
                    )
                    db.add(le)
                    db.flush()
                    stats["chunks"] += 1
                except Exception as e:
                    logger.error(f"Failed to save lyrics embedding for track {track_id} chunk {chunk_index}: {e}")
                    db.rollback()
                    track_ok = False
                    stats["failed"] += 1

            # Count successful tracks in this batch
            successful_tracks = set()
            for track_id, _, _ in all_chunks:
                successful_tracks.add(track_id)
            stats["success"] += len(successful_tracks) - stats["failed"]

            stats["processed"] += len(batch_rows)

            # Commit after each batch
            db.commit()

        elapsed = time.time() - start_time
        logger.info(
            f"Lyrics embedding generation complete: "
            f"{stats['success']} tracks, {stats['chunks']} chunks, "
            f"{stats['failed']} failed, {stats['skipped']} skipped "
            f"({elapsed:.1f}s)"
        )

        return stats


def generate_lyrics_embeddings(
    limit: Optional[int] = None,
    batch_size: Optional[int] = None,
    force: bool = False,
    track_ids: Optional[list] = None,
) -> Dict[str, int]:
    """
    Convenience function to generate lyrics embeddings.

    Args:
        limit: Max tracks to process.
        batch_size: Override default batch size.
        force: Regenerate even if already exists.
        track_ids: If provided, only process these track IDs.

    Returns:
        Statistics dictionary.
    """
    generator = LyricsEmbeddingGenerator(batch_size=batch_size)
    try:
        with get_db_context() as db:
            return generator.generate_all(db, limit=limit, force=force, track_ids=track_ids)
    finally:
        generator.unload_model()
