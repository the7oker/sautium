"""
Enrichment embedding generation for artist bios, album info, and genre descriptions.

Generates 1024-dimensional text embeddings at entity level (not track level).
Uses chunking to handle texts longer than the model's 128-token limit.

Pipeline:
  1. Fetch text from artist_bios / album_info / genre_descriptions
  2. Strip HTML tags, clean whitespace
  3. Prepend entity context ("Artist: Name. ...")
  4. Chunk if text exceeds model token limit
  5. Encode each chunk → store in corresponding embedding table
"""

import logging
import math
import re
import time
from typing import Any, Dict, List, Optional

import numpy as np
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session
from tqdm import tqdm

from config import settings
from database import get_db_context
from models import (
    AlbumInfoEmbedding,
    ArtistBioEmbedding,
    EmbeddingModel,
    GenreDescEmbedding,
)
from uuid_utils import embedding_model_uuid

logger = logging.getLogger(__name__)

# Max tokens per chunk. BGE-M3 supports 8192 tokens, use 2000 for good
# retrieval granularity (enough context per chunk, minimal chunking).
MAX_TOKENS_PER_CHUNK = 2000


def strip_html(s: str) -> str:
    """Strip HTML tags and decode common entities."""
    if not s:
        return ""
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', '', s)
    # Decode common HTML entities
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def split_into_chunks(text: str, max_tokens: int = MAX_TOKENS_PER_CHUNK) -> List[str]:
    """
    Split text into balanced chunks fitting within max_tokens.

    Uses rough estimate: 1 token ~ 0.75 words for multilingual text.
    """
    words = text.split()
    if not words:
        return []

    estimated_tokens = len(words) / 0.75
    if estimated_tokens <= max_tokens:
        return [text]

    n_chunks = math.ceil(estimated_tokens / max_tokens)
    chunk_size = math.ceil(len(words) / n_chunks)

    chunks = []
    for i in range(0, len(words), chunk_size):
        chunk = " ".join(words[i:i + chunk_size])
        if chunk:
            chunks.append(chunk)
    return chunks


class _BaseEnrichmentGenerator:
    """Base class for enrichment embedding generators."""

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
        if self.model is not None:
            return
        from sentence_transformers import SentenceTransformer
        logger.info(f"Loading text embedding model: {self.model_name} on {self.device}")
        self.model = SentenceTransformer(self.model_name, device=self.device)
        logger.info("Text embedding model loaded")

    def unload_model(self):
        if self.model is not None:
            del self.model
            self.model = None
            import torch
            if self.device == "cuda":
                torch.cuda.empty_cache()
            logger.info("Text embedding model unloaded")

    def encode(self, texts: List[str]) -> np.ndarray:
        self.load_model()
        return self.model.encode(
            texts,
            batch_size=self.batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,
        )

    def _get_or_create_model_record(self, db: Session) -> EmbeddingModel:
        mid = embedding_model_uuid(self.model_name)
        em = db.query(EmbeddingModel).filter(EmbeddingModel.id == mid).first()
        if not em:
            em = EmbeddingModel(
                id=mid,
                name=self.model_name,
                description=f"Sentence-transformers text embedding model ({self.dimension}d)",
                dimension=self.dimension,
            )
            db.add(em)
            db.flush()
            logger.info(f"Created embedding model record: {self.model_name}")
        return em


class ArtistBioEmbeddingGenerator(_BaseEnrichmentGenerator):
    """Generate embeddings from artist biographies."""

    def generate_all(
        self,
        db: Session,
        limit: Optional[int] = None,
        force: bool = False,
    ) -> Dict[str, int]:
        stats = {"processed": 0, "success": 0, "skipped": 0, "failed": 0, "chunks": 0}
        start = time.time()

        # Find artists with bios that need embeddings
        where_parts = ["(ab.content IS NOT NULL OR ab.summary IS NOT NULL)"]
        if not force:
            where_parts.append(
                "a.id NOT IN (SELECT DISTINCT abe.artist_id FROM artist_bio_embeddings abe)"
            )
        where_clause = "WHERE " + " AND ".join(where_parts)

        query_sql = f"""
            SELECT a.id as artist_id, a.name,
                   ab.content, ab.summary
            FROM artists a
            JOIN artist_bios ab ON ab.artist_id = a.id AND ab.source = 'lastfm'
            {where_clause}
            ORDER BY a.id
        """
        if limit:
            query_sql += f" LIMIT {limit}"

        rows = db.execute(sa_text(query_sql)).fetchall()
        if not rows:
            logger.info("No artists pending bio embedding generation")
            return stats

        logger.info(f"Processing {len(rows)} artist bios for embeddings")
        self.load_model()
        model_record = self._get_or_create_model_record(db)

        if force:
            artist_ids = [r.artist_id for r in rows]
            db.execute(
                sa_text("DELETE FROM artist_bio_embeddings WHERE artist_id = ANY(:ids)"),
                {"ids": artist_ids},
            )
            db.flush()

        # Process in batches
        for batch_start in tqdm(
            range(0, len(rows), self.batch_size),
            desc="Artist bio embeddings",
            unit="batch",
        ):
            batch_rows = rows[batch_start:batch_start + self.batch_size]
            all_chunks = []  # (artist_id, chunk_index, chunk_text)

            for row in batch_rows:
                # Prefer content (full text), fall back to summary
                raw = row.content or row.summary or ""
                cleaned = strip_html(raw)
                if not cleaned:
                    stats["skipped"] += 1
                    continue

                # Prepend context
                text_with_context = f"Artist: {row.name}. {cleaned}"
                chunks = split_into_chunks(text_with_context)
                for ci, chunk in enumerate(chunks):
                    all_chunks.append((row.artist_id, ci, chunk))

            if not all_chunks:
                stats["processed"] += len(batch_rows)
                continue

            try:
                embeddings = self.encode([c[2] for c in all_chunks])
            except Exception as e:
                logger.error(f"Encoding failed: {e}")
                stats["failed"] += len(batch_rows)
                continue

            batch_failed = set()
            for (artist_id, chunk_index, chunk_text), vector in zip(all_chunks, embeddings):
                if artist_id in batch_failed:
                    continue
                try:
                    db.add(ArtistBioEmbedding(
                        artist_id=artist_id,
                        model_id=model_record.id,
                        vector=vector.tolist(),
                        chunk_index=chunk_index,
                        chunk_text=chunk_text[:500],
                    ))
                    db.flush()
                    stats["chunks"] += 1
                except Exception as e:
                    logger.error(f"Failed to save artist bio embedding for {artist_id}: {e}")
                    db.rollback()
                    batch_failed.add(artist_id)

            batch_artists = {c[0] for c in all_chunks}
            stats["failed"] += len(batch_failed)
            stats["success"] += len(batch_artists) - len(batch_failed)
            stats["processed"] += len(batch_rows)
            db.commit()

        elapsed = time.time() - start
        logger.info(
            f"Artist bio embeddings done: {stats['success']} artists, "
            f"{stats['chunks']} chunks ({elapsed:.1f}s)"
        )
        return stats


class AlbumInfoEmbeddingGenerator(_BaseEnrichmentGenerator):
    """Generate embeddings from album information."""

    def generate_all(
        self,
        db: Session,
        limit: Optional[int] = None,
        force: bool = False,
    ) -> Dict[str, int]:
        stats = {"processed": 0, "success": 0, "skipped": 0, "failed": 0, "chunks": 0}
        start = time.time()

        where_parts = ["(ai.content IS NOT NULL OR ai.summary IS NOT NULL)"]
        if not force:
            where_parts.append(
                "al.id NOT IN (SELECT DISTINCT aie.album_id FROM album_info_embeddings aie)"
            )
        where_clause = "WHERE " + " AND ".join(where_parts)

        query_sql = f"""
            SELECT al.id as album_id, al.title, al.release_year,
                   (SELECT a.name FROM album_artists aa
                    JOIN artists a ON aa.artist_id = a.id
                    WHERE aa.album_id = al.id AND aa.role = 'primary'
                    LIMIT 1) as artist_name,
                   ai.content, ai.summary
            FROM albums al
            JOIN album_info ai ON ai.album_id = al.id AND ai.source = 'lastfm'
            {where_clause}
            ORDER BY al.id
        """
        if limit:
            query_sql += f" LIMIT {limit}"

        rows = db.execute(sa_text(query_sql)).fetchall()
        if not rows:
            logger.info("No albums pending info embedding generation")
            return stats

        logger.info(f"Processing {len(rows)} album infos for embeddings")
        self.load_model()
        model_record = self._get_or_create_model_record(db)

        if force:
            album_ids = [r.album_id for r in rows]
            db.execute(
                sa_text("DELETE FROM album_info_embeddings WHERE album_id = ANY(:ids)"),
                {"ids": album_ids},
            )
            db.flush()

        for batch_start in tqdm(
            range(0, len(rows), self.batch_size),
            desc="Album info embeddings",
            unit="batch",
        ):
            batch_rows = rows[batch_start:batch_start + self.batch_size]
            all_chunks = []

            for row in batch_rows:
                raw = row.content or row.summary or ""
                cleaned = strip_html(raw)
                if not cleaned:
                    stats["skipped"] += 1
                    continue

                # Build context prefix
                prefix = f"Album: {row.title}"
                if row.artist_name:
                    prefix += f" by {row.artist_name}"
                if row.release_year:
                    prefix += f" ({row.release_year})"
                text_with_context = f"{prefix}. {cleaned}"

                chunks = split_into_chunks(text_with_context)
                for ci, chunk in enumerate(chunks):
                    all_chunks.append((row.album_id, ci, chunk))

            if not all_chunks:
                stats["processed"] += len(batch_rows)
                continue

            try:
                embeddings = self.encode([c[2] for c in all_chunks])
            except Exception as e:
                logger.error(f"Encoding failed: {e}")
                stats["failed"] += len(batch_rows)
                continue

            batch_failed = set()
            for (album_id, chunk_index, chunk_text), vector in zip(all_chunks, embeddings):
                if album_id in batch_failed:
                    continue
                try:
                    db.add(AlbumInfoEmbedding(
                        album_id=album_id,
                        model_id=model_record.id,
                        vector=vector.tolist(),
                        chunk_index=chunk_index,
                        chunk_text=chunk_text[:500],
                    ))
                    db.flush()
                    stats["chunks"] += 1
                except Exception as e:
                    logger.error(f"Failed to save album info embedding for {album_id}: {e}")
                    db.rollback()
                    batch_failed.add(album_id)

            batch_albums = {c[0] for c in all_chunks}
            stats["failed"] += len(batch_failed)
            stats["success"] += len(batch_albums) - len(batch_failed)
            stats["processed"] += len(batch_rows)
            db.commit()

        elapsed = time.time() - start
        logger.info(
            f"Album info embeddings done: {stats['success']} albums, "
            f"{stats['chunks']} chunks ({elapsed:.1f}s)"
        )
        return stats


class GenreDescEmbeddingGenerator(_BaseEnrichmentGenerator):
    """Generate embeddings from genre descriptions."""

    def generate_all(
        self,
        db: Session,
        limit: Optional[int] = None,
        force: bool = False,
    ) -> Dict[str, int]:
        stats = {"processed": 0, "success": 0, "skipped": 0, "failed": 0, "chunks": 0}
        start = time.time()

        where_parts = ["(gd.content IS NOT NULL OR gd.summary IS NOT NULL)"]
        if not force:
            where_parts.append(
                "g.id NOT IN (SELECT DISTINCT gde.genre_id FROM genre_desc_embeddings gde)"
            )
        where_clause = "WHERE " + " AND ".join(where_parts)

        query_sql = f"""
            SELECT g.id as genre_id, g.name,
                   gd.content, gd.summary
            FROM genres g
            JOIN genre_descriptions gd ON gd.genre_id = g.id AND gd.source = 'lastfm'
            {where_clause}
            ORDER BY g.id
        """
        if limit:
            query_sql += f" LIMIT {limit}"

        rows = db.execute(sa_text(query_sql)).fetchall()
        if not rows:
            logger.info("No genres pending description embedding generation")
            return stats

        logger.info(f"Processing {len(rows)} genre descriptions for embeddings")
        self.load_model()
        model_record = self._get_or_create_model_record(db)

        if force:
            genre_ids = [r.genre_id for r in rows]
            db.execute(
                sa_text("DELETE FROM genre_desc_embeddings WHERE genre_id = ANY(:ids)"),
                {"ids": genre_ids},
            )
            db.flush()

        for batch_start in tqdm(
            range(0, len(rows), self.batch_size),
            desc="Genre desc embeddings",
            unit="batch",
        ):
            batch_rows = rows[batch_start:batch_start + self.batch_size]
            all_chunks = []

            for row in batch_rows:
                raw = row.content or row.summary or ""
                cleaned = strip_html(raw)
                if not cleaned:
                    stats["skipped"] += 1
                    continue

                text_with_context = f"Genre: {row.name}. {cleaned}"
                chunks = split_into_chunks(text_with_context)
                for ci, chunk in enumerate(chunks):
                    all_chunks.append((row.genre_id, ci, chunk))

            if not all_chunks:
                stats["processed"] += len(batch_rows)
                continue

            try:
                embeddings = self.encode([c[2] for c in all_chunks])
            except Exception as e:
                logger.error(f"Encoding failed: {e}")
                stats["failed"] += len(batch_rows)
                continue

            batch_failed = set()
            for (genre_id, chunk_index, chunk_text), vector in zip(all_chunks, embeddings):
                if genre_id in batch_failed:
                    continue
                try:
                    db.add(GenreDescEmbedding(
                        genre_id=genre_id,
                        model_id=model_record.id,
                        vector=vector.tolist(),
                        chunk_index=chunk_index,
                        chunk_text=chunk_text[:500],
                    ))
                    db.flush()
                    stats["chunks"] += 1
                except Exception as e:
                    logger.error(f"Failed to save genre desc embedding for {genre_id}: {e}")
                    db.rollback()
                    batch_failed.add(genre_id)

            batch_genres = {c[0] for c in all_chunks}
            stats["failed"] += len(batch_failed)
            stats["success"] += len(batch_genres) - len(batch_failed)
            stats["processed"] += len(batch_rows)
            db.commit()

        elapsed = time.time() - start
        logger.info(
            f"Genre desc embeddings done: {stats['success']} genres, "
            f"{stats['chunks']} chunks ({elapsed:.1f}s)"
        )
        return stats


# ── Convenience functions ────────────────────────────────────────────────

def generate_artist_bio_embeddings(
    limit: Optional[int] = None, batch_size: Optional[int] = None, force: bool = False,
) -> Dict[str, int]:
    gen = ArtistBioEmbeddingGenerator(batch_size=batch_size)
    try:
        with get_db_context() as db:
            return gen.generate_all(db, limit=limit, force=force)
    finally:
        gen.unload_model()


def generate_album_info_embeddings(
    limit: Optional[int] = None, batch_size: Optional[int] = None, force: bool = False,
) -> Dict[str, int]:
    gen = AlbumInfoEmbeddingGenerator(batch_size=batch_size)
    try:
        with get_db_context() as db:
            return gen.generate_all(db, limit=limit, force=force)
    finally:
        gen.unload_model()


def generate_genre_desc_embeddings(
    limit: Optional[int] = None, batch_size: Optional[int] = None, force: bool = False,
) -> Dict[str, int]:
    gen = GenreDescEmbeddingGenerator(batch_size=batch_size)
    try:
        with get_db_context() as db:
            return gen.generate_all(db, limit=limit, force=force)
    finally:
        gen.unload_model()


def generate_all_enrichment_embeddings(
    limit: Optional[int] = None, batch_size: Optional[int] = None, force: bool = False,
) -> Dict[str, Dict[str, int]]:
    """Generate all three enrichment embedding types. Shares model across all."""
    gen = ArtistBioEmbeddingGenerator(batch_size=batch_size)
    results = {}
    try:
        with get_db_context() as db:
            results["artist_bios"] = gen.generate_all(db, limit=limit, force=force)

        # Reuse loaded model for album info
        album_gen = AlbumInfoEmbeddingGenerator(batch_size=batch_size)
        album_gen.model = gen.model
        album_gen.device = gen.device
        with get_db_context() as db:
            results["album_info"] = album_gen.generate_all(db, limit=limit, force=force)

        # Reuse for genre descriptions
        genre_gen = GenreDescEmbeddingGenerator(batch_size=batch_size)
        genre_gen.model = gen.model
        genre_gen.device = gen.device
        with get_db_context() as db:
            results["genre_descs"] = genre_gen.generate_all(db, limit=limit, force=force)
    finally:
        gen.unload_model()

    return results
