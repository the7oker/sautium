"""
Text embedding generation using sentence-transformers.
Generates 384-dimensional text embeddings from track metadata for semantic search.

Operates on tracks (one embedding per track). Uses track_artists, track_genres,
and enrichment data (artist_bios, album_info, tags) to compose descriptive text.
"""

import logging
import re
from typing import Any, Dict, List, Optional

import numpy as np
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session
from tqdm import tqdm

from config import settings
from database import get_db_context
from models import EmbeddingModel, TextEmbedding, Track

logger = logging.getLogger(__name__)


def _strip_html(s: str) -> str:
    """Strip HTML tags (especially Last.fm <a href> links) from text."""
    if not s:
        return ""
    return re.sub(r'<[^>]+>', '', s).strip()


class TextEmbeddingGenerator:
    """Generate text embeddings from track metadata using sentence-transformers."""

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
        """
        Encode a list of texts into embeddings.

        Args:
            texts: List of text strings.

        Returns:
            numpy array of shape (len(texts), 384).
        """
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

    def compose_tracks_text_batch(
        self, db: Session, track_ids: list
    ) -> Dict:
        """
        Build descriptive text for multiple tracks in a single efficient query.

        Returns dict mapping track_id -> composed text string.
        """
        if not track_ids:
            return {}

        query = sa_text("""
            SELECT
                t.id as track_id,
                t.title as track_title,
                a.name as artist_name,
                mf_rep.album_title,
                mf_rep.release_year,
                mf_rep.is_lossless,
                g_agg.genres,
                at_agg.artist_tags,
                alt_agg.album_tags,
                ab.summary as artist_bio,
                ai.summary as album_info,
                gd_agg.genre_descs
            FROM tracks t
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a ON ta.artist_id = a.id
            -- Representative media file for album info
            JOIN LATERAL (
                SELECT al.title as album_title, al.release_year, al.id as album_id,
                       mf.is_lossless
                FROM media_files mf
                JOIN album_variants av ON mf.album_variant_id = av.id
                JOIN albums al ON av.album_id = al.id
                WHERE mf.track_id = t.id
                ORDER BY mf.is_analysis_source DESC, mf.id
                LIMIT 1
            ) mf_rep ON true
            -- Aggregated genres
            LEFT JOIN LATERAL (
                SELECT STRING_AGG(g.name, ', ' ORDER BY g.name) as genres
                FROM track_genres tg
                JOIN genres g ON tg.genre_id = g.id
                WHERE tg.track_id = t.id
            ) g_agg ON true
            -- Aggregated artist tags (top 10 by weight)
            LEFT JOIN LATERAL (
                SELECT STRING_AGG(tg.name, ', ' ORDER BY at2.weight DESC) as artist_tags
                FROM (
                    SELECT tag_id, weight FROM artist_tags
                    WHERE artist_id = a.id
                    ORDER BY weight DESC LIMIT 10
                ) at2
                JOIN tags tg ON at2.tag_id = tg.id
            ) at_agg ON true
            -- Aggregated album tags (top 10 by weight)
            LEFT JOIN LATERAL (
                SELECT STRING_AGG(tg.name, ', ' ORDER BY alt2.weight DESC) as album_tags
                FROM (
                    SELECT tag_id, weight FROM album_tags
                    WHERE album_id = mf_rep.album_id
                    ORDER BY weight DESC LIMIT 10
                ) alt2
                JOIN tags tg ON alt2.tag_id = tg.id
            ) alt_agg ON true
            -- Artist bio (Last.fm, first match)
            LEFT JOIN LATERAL (
                SELECT summary FROM artist_bios
                WHERE artist_id = a.id AND source = 'lastfm'
                LIMIT 1
            ) ab ON true
            -- Album info (Last.fm, first match)
            LEFT JOIN LATERAL (
                SELECT summary FROM album_info
                WHERE album_id = mf_rep.album_id AND source = 'lastfm'
                LIMIT 1
            ) ai ON true
            -- Genre descriptions aggregated
            LEFT JOIN LATERAL (
                SELECT STRING_AGG(
                    g.name || ': ' || LEFT(gd.summary, 200),
                    '; '
                ) as genre_descs
                FROM track_genres tg
                JOIN genres g ON tg.genre_id = g.id
                LEFT JOIN genre_descriptions gd ON g.id = gd.genre_id AND gd.source = 'lastfm'
                WHERE tg.track_id = t.id AND gd.summary IS NOT NULL
            ) gd_agg ON true
            WHERE t.id = ANY(:track_ids)
        """)

        rows = db.execute(query, {"track_ids": track_ids}).fetchall()

        result = {}
        for row in rows:
            parts = []

            # Core metadata
            parts.append(f'Track: "{row.track_title}" by {row.artist_name}')

            album_str = f"Album: {row.album_title}"
            if row.release_year:
                album_str += f" ({row.release_year})"
            parts.append(album_str)

            if row.genres:
                parts.append(f"Genre: {row.genres}")

            if row.is_lossless is not None:
                parts.append(f"Quality: {'Lossless' if row.is_lossless else 'Lossy'}")

            # Tags
            if row.artist_tags:
                parts.append(f"Artist style: {row.artist_tags}")

            if row.album_tags:
                parts.append(f"Album style: {row.album_tags}")

            # Enriched data (strip HTML from Last.fm)
            if row.artist_bio:
                bio = _strip_html(row.artist_bio)[:300]
                if bio:
                    parts.append(f"About artist: {bio}")

            if row.album_info:
                info = _strip_html(row.album_info)[:300]
                if info:
                    parts.append(f"About album: {info}")

            if row.genre_descs:
                descs = _strip_html(row.genre_descs)[:400]
                if descs:
                    parts.append(f"Genre description: {descs}")

            result[row.track_id] = "\n".join(parts)

        return result

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
        order_by_date: bool = False,
        max_duration_seconds: Optional[int] = None,
        track_ids: Optional[list] = None,
    ) -> Dict[str, int]:
        """
        Generate text embeddings for all tracks (or those missing them).

        Args:
            db: Database session.
            limit: Max tracks to process.
            force: If True, regenerate even if embedding exists.
            order_by_date: If True, process newest tracks first.
            max_duration_seconds: Maximum duration in seconds.
            track_ids: If provided, only process these track IDs.

        Returns:
            Stats dict with processed, success, failed counts.
        """
        import time

        stats = {"processed": 0, "success": 0, "failed": 0}
        start_time = time.time()

        # Query tracks to process
        where_parts = []
        params: Dict[str, Any] = {}

        if not force:
            where_parts.append("""
                t.id NOT IN (SELECT te.track_id FROM text_embeddings te)
            """)

        if track_ids is not None:
            where_parts.append("t.id = ANY(:filter_track_ids)")
            params["filter_track_ids"] = track_ids

        where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

        if order_by_date:
            order_clause = """
                ORDER BY (
                    SELECT MAX(mf.file_modified_at)
                    FROM media_files mf WHERE mf.track_id = t.id
                ) DESC NULLS LAST
            """
        else:
            order_clause = "ORDER BY t.id"

        query_sql = f"SELECT t.id FROM tracks t {where_clause} {order_clause}"
        if limit:
            query_sql += f" LIMIT {limit}"

        rows = db.execute(sa_text(query_sql), params).fetchall()
        pending_track_ids = [r[0] for r in rows]

        if not pending_track_ids:
            logger.info("No tracks pending text embedding generation")
            return stats

        logger.info(f"Processing {len(pending_track_ids)} tracks for text embeddings")
        if max_duration_seconds:
            logger.info(f"Time limit: {max_duration_seconds} seconds ({max_duration_seconds/60:.1f} minutes)")

        self.load_model()
        model_record = self._get_or_create_model_record(db)

        # Process in batches
        batch_size = self.batch_size
        for batch_start in tqdm(
            range(0, len(pending_track_ids), batch_size),
            desc="Generating text embeddings",
            unit="batch",
        ):
            # Check time limit before starting new batch
            if max_duration_seconds:
                elapsed = time.time() - start_time
                if elapsed >= max_duration_seconds:
                    logger.info(f"Time limit reached ({elapsed:.1f}s), stopping gracefully")
                    break

            batch_ids = pending_track_ids[batch_start:batch_start + batch_size]

            # Compose text for batch
            texts_map = self.compose_tracks_text_batch(db, batch_ids)

            if not texts_map:
                stats["failed"] += len(batch_ids)
                continue

            # Prepare ordered lists
            ordered_ids = []
            ordered_texts = []
            for sid in batch_ids:
                if sid in texts_map:
                    ordered_ids.append(sid)
                    ordered_texts.append(texts_map[sid])
                else:
                    stats["failed"] += 1

            if not ordered_texts:
                continue

            # Encode batch
            try:
                embeddings = self.encode(ordered_texts)
            except Exception as e:
                logger.error(f"Encoding failed for batch starting at {batch_start}: {e}")
                stats["failed"] += len(ordered_ids)
                continue

            # Create text embeddings linked to tracks
            for sid, vector in zip(ordered_ids, embeddings):
                try:
                    text_embedding = TextEmbedding(
                        vector=vector.tolist(),
                        model_id=model_record.id,
                        track_id=sid,
                    )
                    db.add(text_embedding)
                    db.flush()
                    stats["success"] += 1
                except Exception as e:
                    logger.error(f"Failed to save embedding for track {sid}: {e}")
                    stats["failed"] += 1

            stats["processed"] += len(batch_ids)

            # Commit after each batch
            db.commit()

        logger.info(
            f"Text embedding generation complete: "
            f"{stats['success']} success, {stats['failed']} failed"
        )

        return stats


def generate_text_embeddings(
    limit: Optional[int] = None,
    batch_size: Optional[int] = None,
    force: bool = False,
    order_by_date: bool = False,
    max_duration_seconds: Optional[int] = None,
    track_ids: Optional[list] = None,
) -> Dict[str, int]:
    """
    Convenience function to generate text embeddings.

    Args:
        limit: Max tracks to process.
        batch_size: Override default batch size.
        force: Regenerate even if already exists.
        order_by_date: If True, process newest tracks first.
        max_duration_seconds: Maximum duration in seconds.
        track_ids: If provided, only process these track IDs.

    Returns:
        Statistics dictionary.
    """
    generator = TextEmbeddingGenerator(batch_size=batch_size)
    try:
        with get_db_context() as db:
            return generator.generate_all(db, limit=limit, force=force, order_by_date=order_by_date, max_duration_seconds=max_duration_seconds, track_ids=track_ids)
    finally:
        generator.unload_model()
