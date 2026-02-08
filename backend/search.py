"""
Search service for Music AI DJ.
Provides similarity search (by track ID or text) and metadata filtering
using pgvector cosine similarity over CLAP embeddings.
"""

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from config import settings

logger = logging.getLogger(__name__)


def _build_track_result(row) -> Dict[str, Any]:
    """Format a database row into a consistent track result dict."""
    return {
        "id": row.id,
        "title": row.title,
        "artist": row.artist,
        "album": row.album,
        "genre": row.genre,
        "quality_source": row.quality_source,
        "duration_seconds": float(row.duration_seconds) if row.duration_seconds else None,
        "sample_rate": row.sample_rate,
        "bit_depth": row.bit_depth,
        "similarity": round(float(row.similarity), 4) if hasattr(row, "similarity") and row.similarity is not None else None,
    }


def _apply_filters(filters: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    """
    Build SQL WHERE clauses and params from filter dict.

    Supported keys: artist, album, genre, quality_source, year_from, year_to.
    Returns (sql_fragment, params_dict).
    """
    clauses = []
    params = {}

    if filters.get("artist"):
        clauses.append("a2.name ILIKE :f_artist")
        params["f_artist"] = f"%{filters['artist']}%"

    if filters.get("album"):
        clauses.append("al.title ILIKE :f_album")
        params["f_album"] = f"%{filters['album']}%"

    if filters.get("genre"):
        clauses.append("g.name ILIKE :f_genre")
        params["f_genre"] = f"%{filters['genre']}%"

    if filters.get("quality_source"):
        clauses.append("al.quality_source = :f_quality")
        params["f_quality"] = filters["quality_source"]

    if filters.get("year_from"):
        clauses.append("al.release_year >= :f_year_from")
        params["f_year_from"] = filters["year_from"]

    if filters.get("year_to"):
        clauses.append("al.release_year <= :f_year_to")
        params["f_year_to"] = filters["year_to"]

    sql = (" AND " + " AND ".join(clauses)) if clauses else ""
    return sql, params


def search_similar_tracks(
    db: Session,
    track_id: int,
    limit: int = None,
    min_similarity: float = None,
    filters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Find tracks similar to a given track using cosine similarity.

    Args:
        db: Database session.
        track_id: Source track ID.
        limit: Max results to return.
        min_similarity: Minimum similarity score (0-1).
        filters: Optional metadata filters.

    Returns:
        Dict with results list, count, and query_track info.
    """
    limit = limit or settings.default_search_limit
    min_similarity = min_similarity if min_similarity is not None else settings.min_similarity_threshold
    filters = filters or {}

    # Get the source track info first
    source_sql = text("""
        SELECT t.id, t.title, a2.name as artist, al.title as album,
               g.name as genre, al.quality_source,
               t.duration_seconds, t.sample_rate, t.bit_depth
        FROM tracks t
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a2 ON ta.artist_id = a2.id
        JOIN albums al ON t.album_id = al.id
        LEFT JOIN track_genres tg ON t.id = tg.track_id
        LEFT JOIN genres g ON tg.genre_id = g.id
        WHERE t.id = :track_id
    """)
    source_row = db.execute(source_sql, {"track_id": track_id}).fetchone()

    if not source_row:
        return {"error": f"Track {track_id} not found", "results": [], "count": 0}

    # Check the track has an embedding
    emb_check = db.execute(
        text("SELECT embedding_id FROM tracks WHERE id = :track_id"),
        {"track_id": track_id},
    ).fetchone()

    if not emb_check or emb_check.embedding_id is None:
        return {"error": f"Track {track_id} has no embedding", "results": [], "count": 0}

    # Build filter clauses
    filter_sql, filter_params = _apply_filters(filters)

    similarity_sql = text(f"""
        WITH target AS (
            SELECT e.vector
            FROM tracks t
            JOIN embeddings e ON t.embedding_id = e.id
            WHERE t.id = :track_id
        )
        SELECT t.id, t.title, a2.name as artist, al.title as album,
               g.name as genre, al.quality_source,
               t.duration_seconds, t.sample_rate, t.bit_depth,
               1 - (e.vector <=> (SELECT vector FROM target)) as similarity
        FROM tracks t
        JOIN embeddings e ON t.embedding_id = e.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a2 ON ta.artist_id = a2.id
        JOIN albums al ON t.album_id = al.id
        LEFT JOIN track_genres tg ON t.id = tg.track_id
        LEFT JOIN genres g ON tg.genre_id = g.id
        WHERE t.id != :track_id
          AND 1 - (e.vector <=> (SELECT vector FROM target)) >= :min_similarity
          {filter_sql}
        ORDER BY e.vector <=> (SELECT vector FROM target)
        LIMIT :limit
    """)

    params = {"track_id": track_id, "min_similarity": min_similarity, "limit": limit}
    params.update(filter_params)

    rows = db.execute(similarity_sql, params).fetchall()

    results = [_build_track_result(row) for row in rows]

    query_track = {
        "id": source_row.id,
        "title": source_row.title,
        "artist": source_row.artist,
        "album": source_row.album,
        "genre": source_row.genre,
    }

    return {"results": results, "count": len(results), "query_track": query_track}


def search_by_text(
    db: Session,
    query_text: str,
    limit: int = None,
    min_similarity: float = None,
    filters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Search tracks by text description using CLAP text-to-audio embeddings.

    Args:
        db: Database session.
        query_text: Natural language description.
        limit: Max results to return.
        min_similarity: Minimum similarity score (0-1).
        filters: Optional metadata filters.

    Returns:
        Dict with results list, count, and query_text.
    """
    from embeddings import AudioEmbeddingGenerator

    limit = limit or settings.default_search_limit
    min_similarity = min_similarity if min_similarity is not None else settings.min_similarity_threshold
    filters = filters or {}

    # Generate text embedding via CLAP
    generator = AudioEmbeddingGenerator()
    text_vector = generator.text_to_embedding(query_text)
    generator.unload_model()

    # Format vector as pgvector literal and embed directly in SQL
    # (avoids SQLAlchemy misinterpreting ::vector cast as a bind param)
    vector_str = "'" + "[" + ",".join(str(float(x)) for x in text_vector) + "]" + "'::vector"

    filter_sql, filter_params = _apply_filters(filters)

    similarity_sql = text(f"""
        SELECT t.id, t.title, a2.name as artist, al.title as album,
               g.name as genre, al.quality_source,
               t.duration_seconds, t.sample_rate, t.bit_depth,
               1 - (e.vector <=> {vector_str}) as similarity
        FROM tracks t
        JOIN embeddings e ON t.embedding_id = e.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a2 ON ta.artist_id = a2.id
        JOIN albums al ON t.album_id = al.id
        LEFT JOIN track_genres tg ON t.id = tg.track_id
        LEFT JOIN genres g ON tg.genre_id = g.id
        WHERE 1 - (e.vector <=> {vector_str}) >= :min_similarity
          {filter_sql}
        ORDER BY e.vector <=> {vector_str}
        LIMIT :limit
    """)

    params = {"min_similarity": min_similarity, "limit": limit}
    params.update(filter_params)

    rows = db.execute(similarity_sql, params).fetchall()

    results = [_build_track_result(row) for row in rows]

    return {"results": results, "count": len(results), "query_text": query_text}


def search_by_metadata(
    db: Session,
    filters: Optional[Dict[str, Any]] = None,
    limit: int = None,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Search tracks by metadata only (no similarity).

    Args:
        db: Database session.
        filters: Metadata filters (artist, album, genre, quality_source, year_from, year_to).
        limit: Max results to return.
        offset: Pagination offset.

    Returns:
        Dict with results list and count.
    """
    limit = limit or settings.default_search_limit
    filters = filters or {}

    filter_sql, filter_params = _apply_filters(filters)

    # Remove leading " AND " for WHERE clause
    where_clause = "WHERE " + filter_sql.lstrip(" AND ") if filter_sql else ""

    count_sql = text(f"""
        SELECT COUNT(DISTINCT t.id)
        FROM tracks t
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a2 ON ta.artist_id = a2.id
        JOIN albums al ON t.album_id = al.id
        LEFT JOIN track_genres tg ON t.id = tg.track_id
        LEFT JOIN genres g ON tg.genre_id = g.id
        {where_clause}
    """)

    total = db.execute(count_sql, filter_params).scalar()

    query_sql = text(f"""
        SELECT DISTINCT t.id, t.title, a2.name as artist, al.title as album,
               g.name as genre, al.quality_source,
               t.duration_seconds, t.sample_rate, t.bit_depth,
               NULL::float as similarity,
               t.track_number
        FROM tracks t
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a2 ON ta.artist_id = a2.id
        JOIN albums al ON t.album_id = al.id
        LEFT JOIN track_genres tg ON t.id = tg.track_id
        LEFT JOIN genres g ON tg.genre_id = g.id
        {where_clause}
        ORDER BY a2.name, al.title, t.track_number
        LIMIT :limit OFFSET :offset
    """)

    params = {"limit": limit, "offset": offset}
    params.update(filter_params)

    rows = db.execute(query_sql, params).fetchall()

    results = [_build_track_result(row) for row in rows]

    return {"results": results, "count": len(results), "total": total}
