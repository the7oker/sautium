"""
Search service for Sautium.
Provides similarity search (by media file ID or text) and metadata filtering
using pgvector cosine similarity over CLAP embeddings.

Uses the canonical schema: tracks → media_files → album_variants → albums.
Returns media_file.id as the track ID (needed for playback).
"""

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from config import settings
from sql_queries import (
    MEDIA_FILE_SELECT, MEDIA_FILE_FROM,
    EMBEDDING_SIMILARITY_SELECT, EMBEDDING_SIMILARITY_FROM,
)

logger = logging.getLogger(__name__)


def _build_track_result(row) -> Dict[str, Any]:
    """Format a database row into a consistent track result dict."""
    result = {
        "id": row.id,
        "title": row.title,
        "artist": row.artist,
        "album": row.album,
        "genre": row.genre,
        "duration_seconds": float(row.duration_seconds) if row.duration_seconds else None,
        "sample_rate": row.sample_rate if hasattr(row, "sample_rate") else None,
        "bit_depth": row.bit_depth if hasattr(row, "bit_depth") else None,
        "is_lossless": row.is_lossless if hasattr(row, "is_lossless") else None,
        "similarity": round(float(row.similarity), 4) if hasattr(row, "similarity") and row.similarity is not None else None,
    }
    return result


def _apply_filters(filters: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    """
    Build SQL WHERE clauses and params from filter dict.

    Supported keys: artist, album, genre, is_lossless, year_from, year_to,
                    bpm_min, bpm_max, key, mode, instrument, vocal, danceable, energy_min.
    Returns (sql_fragment, params_dict).
    """
    clauses = []
    params = {}

    if filters.get("artist"):
        clauses.append("a.name ILIKE :f_artist")
        params["f_artist"] = f"%{filters['artist']}%"

    if filters.get("album"):
        clauses.append("al.title ILIKE :f_album")
        params["f_album"] = f"%{filters['album']}%"

    if filters.get("genre"):
        clauses.append("EXISTS (SELECT 1 FROM track_genres tg JOIN genres g ON tg.genre_id = g.id WHERE tg.track_id = t.id AND g.name ILIKE :f_genre)")
        params["f_genre"] = f"%{filters['genre']}%"

    if filters.get("is_lossless") is not None:
        clauses.append("mf.is_lossless = :f_lossless")
        params["f_lossless"] = filters["is_lossless"]

    # Legacy quality_source filter → map to is_lossless
    if filters.get("quality_source"):
        qs = filters["quality_source"]
        if qs in ("CD", "Vinyl", "Hi-Res"):
            clauses.append("mf.is_lossless = true")
        elif qs == "MP3":
            clauses.append("mf.is_lossless = false")

    if filters.get("year_from"):
        clauses.append("al.release_year >= :f_year_from")
        params["f_year_from"] = filters["year_from"]

    if filters.get("year_to"):
        clauses.append("al.release_year <= :f_year_to")
        params["f_year_to"] = filters["year_to"]

    # Audio feature filters (require af alias in query)
    if filters.get("bpm_min"):
        clauses.append("af.bpm >= :f_bpm_min")
        params["f_bpm_min"] = filters["bpm_min"]

    if filters.get("bpm_max"):
        clauses.append("af.bpm <= :f_bpm_max")
        params["f_bpm_max"] = filters["bpm_max"]

    if filters.get("key"):
        clauses.append("af.key = :f_key")
        params["f_key"] = filters["key"]

    if filters.get("mode"):
        clauses.append("af.mode = :f_mode")
        params["f_mode"] = filters["mode"]

    if filters.get("instrument"):
        clauses.append("af.instruments ? :f_instrument")
        params["f_instrument"] = filters["instrument"]

    if filters.get("vocal"):
        clauses.append("af.vocal_instrumental = :f_vocal")
        params["f_vocal"] = filters["vocal"]

    if filters.get("danceable"):
        clauses.append("af.danceability >= 0.5")

    if filters.get("energy_min"):
        clauses.append("af.energy_db >= :f_energy_min")
        params["f_energy_min"] = filters["energy_min"]

    sql = (" AND " + " AND ".join(clauses)) if clauses else ""
    return sql, params


def _needs_audio_features_join(filters: Dict[str, Any]) -> bool:
    """Check if any audio feature filters are present."""
    af_keys = {"bpm_min", "bpm_max", "key", "mode", "instrument", "vocal", "danceable", "energy_min"}
    return bool(af_keys & set(filters.keys()))


def search_similar_tracks(
    db: Session,
    track_id: int,
    limit: int = None,
    min_similarity: float = None,
    filters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Find tracks similar to a given media file using cosine similarity.

    Args:
        db: Database session.
        track_id: Source media_file ID.
        limit: Max results to return.
        min_similarity: Minimum similarity score (0-1).
        filters: Optional metadata filters.

    Returns:
        Dict with results list, count, and query_track info.
    """
    limit = limit or settings.default_search_limit
    min_similarity = min_similarity if min_similarity is not None else settings.min_similarity_threshold
    filters = filters or {}

    # Get the source track info
    source_sql = text("""
        SELECT mf.id, t.title, a.name as artist, al.title as album,
               (SELECT g.name FROM track_genres tg JOIN genres g ON tg.genre_id = g.id
                WHERE tg.track_id = t.id LIMIT 1) as genre,
               mf.duration_seconds, mf.sample_rate,
               mf.bit_depth, mf.is_lossless, t.id as track_id
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN album_variants av ON mf.album_variant_id = av.id
        JOIN albums al ON av.album_id = al.id
        WHERE mf.id = :track_id
    """)
    source_row = db.execute(source_sql, {"track_id": track_id}).fetchone()

    if not source_row:
        return {"error": f"Track {track_id} not found", "results": [], "count": 0}

    # Check the track has an embedding
    emb_check = db.execute(
        text("SELECT id FROM embeddings WHERE track_id = :track_id"),
        {"track_id": source_row.track_id},
    ).fetchone()

    if not emb_check:
        return {"error": f"Track {track_id} has no embedding", "results": [], "count": 0}

    # Build filter clauses
    filter_sql, filter_params = _apply_filters(filters)

    af_join = "LEFT JOIN audio_features af ON t.id = af.track_id" if _needs_audio_features_join(filters) else ""

    # Use embedding similarity via tracks, return representative media_file
    similarity_sql = text(f"""
        WITH target AS (
            SELECT e.vector
            FROM embeddings e
            WHERE e.track_id = :track_id
        )
        {EMBEDDING_SIMILARITY_SELECT},
               1 - (e.vector <=> (SELECT vector FROM target)) as similarity
        {EMBEDDING_SIMILARITY_FROM}
        {af_join}
        WHERE t.id != :track_id
          AND 1 - (e.vector <=> (SELECT vector FROM target)) >= :min_similarity
          {filter_sql}
        ORDER BY e.vector <=> (SELECT vector FROM target)
        LIMIT :limit
    """)

    params = {"track_id": source_row.track_id, "min_similarity": min_similarity, "limit": limit}
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

    # Format vector as pgvector literal
    vector_str = "'" + "[" + ",".join(str(float(x)) for x in text_vector) + "]" + "'::vector"

    filter_sql, filter_params = _apply_filters(filters)
    af_join = "LEFT JOIN audio_features af ON t.id = af.track_id" if _needs_audio_features_join(filters) else ""

    similarity_sql = text(f"""
        {EMBEDDING_SIMILARITY_SELECT},
               1 - (e.vector <=> {vector_str}) as similarity
        {EMBEDDING_SIMILARITY_FROM}
        {af_join}
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
        filters: Metadata filters (artist, album, genre, is_lossless, year_from, year_to).
        limit: Max results to return.
        offset: Pagination offset.

    Returns:
        Dict with results list and count.
    """
    limit = limit or settings.default_search_limit
    filters = filters or {}

    filter_sql, filter_params = _apply_filters(filters)
    af_join = "LEFT JOIN audio_features af ON t.id = af.track_id" if _needs_audio_features_join(filters) else ""

    # Remove leading " AND " for WHERE clause
    where_clause = "WHERE " + filter_sql.lstrip(" AND ") if filter_sql else ""

    count_sql = text(f"""
        SELECT COUNT(DISTINCT mf.id)
        {MEDIA_FILE_FROM}
        {af_join}
        {where_clause}
    """)

    total = db.execute(count_sql, filter_params).scalar()

    query_sql = text(f"""
        SELECT DISTINCT ON (mf.id)
            {MEDIA_FILE_SELECT.lstrip('    SELECT ')},
               NULL::float as similarity,
               mf.track_number
        {MEDIA_FILE_FROM}
        {af_join}
        {where_clause}
        ORDER BY mf.id, a.name, al.title, mf.track_number
        LIMIT :limit OFFSET :offset
    """)

    params = {"limit": limit, "offset": offset}
    params.update(filter_params)

    rows = db.execute(query_sql, params).fetchall()

    results = [_build_track_result(row) for row in rows]

    return {"results": results, "count": len(results), "total": total}


def _build_feature_result(row) -> Dict[str, Any]:
    """Format a database row from feature search into a result dict."""
    return {
        "id": row.id,
        "title": row.title,
        "artist": row.artist,
        "album": row.album,
        "genre": row.genre,
        "is_lossless": row.is_lossless if hasattr(row, "is_lossless") else None,
        "duration_seconds": float(row.duration_seconds) if row.duration_seconds else None,
        "bpm": float(row.bpm) if row.bpm else None,
        "key": row.key,
        "mode": row.mode,
        "vocal_instrumental": row.vocal_instrumental,
        "danceability": float(row.danceability) if row.danceability else None,
        "instruments": row.instruments if hasattr(row, "instruments") else None,
    }


def search_by_features(
    db: Session,
    filters: Dict[str, Any],
    limit: int = None,
) -> Dict[str, Any]:
    """
    Search tracks by audio features (BPM, key, instruments, vocal, danceability).

    Args:
        db: Database session.
        filters: Feature filters (bpm_min, bpm_max, key, mode, instrument, vocal, danceable,
                                  plus standard: artist, genre, is_lossless).
        limit: Max results.

    Returns:
        Dict with results list and count.
    """
    limit = limit or settings.default_search_limit

    filter_sql, filter_params = _apply_filters(filters)

    # Remove leading " AND " for WHERE clause
    where_clause = "WHERE " + filter_sql.lstrip(" AND ") if filter_sql else ""

    query_sql = text(f"""
        SELECT DISTINCT mf.id, t.title, a.name as artist, al.title as album,
               (SELECT g.name FROM track_genres tg JOIN genres g ON tg.genre_id = g.id
                WHERE tg.track_id = t.id LIMIT 1) as genre,
               mf.is_lossless,
               mf.duration_seconds,
               af.bpm, af.key, af.mode, af.vocal_instrumental,
               af.danceability, af.instruments
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        JOIN audio_features af ON t.id = af.track_id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN album_variants av ON mf.album_variant_id = av.id
        JOIN albums al ON av.album_id = al.id
        {where_clause}
        ORDER BY af.bpm, a.name, t.title
        LIMIT :limit
    """)

    params = {"limit": limit}
    params.update(filter_params)

    rows = db.execute(query_sql, params).fetchall()
    results = [_build_feature_result(row) for row in rows]

    return {"results": results, "count": len(results)}


def search_by_lyrics(
    db: Session,
    query_text: str,
    limit: int = None,
    min_similarity: float = None,
) -> Dict[str, Any]:
    """
    Search tracks by lyrics content similarity.

    Uses sentence-transformer embeddings of lyrics text.
    For tracks with multiple chunks, takes the MAX similarity across chunks.

    Args:
        db: Database session.
        query_text: Natural language description of lyrical content.
        limit: Max results to return.
        min_similarity: Minimum similarity score (0-1).

    Returns:
        Dict with results list, count, and query_text.
    """
    from lyrics_embeddings import LyricsEmbeddingGenerator

    limit = limit or settings.default_search_limit
    min_similarity = min_similarity if min_similarity is not None else 0.3

    # Get the model_id for the text embedding model
    model_row = db.execute(
        text("SELECT id FROM embedding_models WHERE name = :name"),
        {"name": settings.text_embedding_model},
    ).fetchone()

    if not model_row:
        return {"error": "Text embedding model not found in DB", "results": [], "count": 0}

    model_id = model_row.id

    # Generate query embedding
    generator = LyricsEmbeddingGenerator()
    query_vector = generator.query_to_embedding(query_text)
    generator.unload_model()

    # Format vector as pgvector literal
    vector_str = "'" + "[" + ",".join(str(float(x)) for x in query_vector) + "]" + "'::vector"

    # Search: GROUP BY track_id, take MAX similarity across chunks
    # Use subquery with DISTINCT ON to avoid duplicates from genre joins
    similarity_sql = text(f"""
        SELECT * FROM (
            SELECT DISTINCT ON (matches.track_id)
                   mf_rep.id, t.title, a.name as artist,
                   mf_rep.album_title as album,
                   (SELECT g.name FROM track_genres tg JOIN genres g ON tg.genre_id = g.id
                    WHERE tg.track_id = t.id LIMIT 1) as genre,
                   mf_rep.duration_seconds,
                   mf_rep.sample_rate, mf_rep.bit_depth, mf_rep.is_lossless,
                   matches.similarity
            FROM (
                SELECT le.track_id,
                       MAX(1 - (le.vector <=> {vector_str})) as similarity
                FROM lyrics_embeddings le
                WHERE le.model_id = :model_id
                GROUP BY le.track_id
                HAVING MAX(1 - (le.vector <=> {vector_str})) >= :min_similarity
            ) matches
            JOIN tracks t ON matches.track_id = t.id
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a ON ta.artist_id = a.id
            JOIN LATERAL (
                SELECT mf.id, mf.duration_seconds,
                       mf.sample_rate, mf.bit_depth, mf.is_lossless,
                       al.title as album_title
                FROM media_files mf
                JOIN album_variants av ON mf.album_variant_id = av.id
                JOIN albums al ON av.album_id = al.id
                WHERE mf.track_id = t.id
                ORDER BY mf.is_analysis_source DESC, mf.id
                LIMIT 1
            ) mf_rep ON true
            ORDER BY matches.track_id
        ) deduped
        ORDER BY deduped.similarity DESC
        LIMIT :limit
    """)

    params = {"model_id": model_id, "min_similarity": min_similarity, "limit": limit}
    rows = db.execute(similarity_sql, params).fetchall()

    results = [_build_track_result(row) for row in rows]

    return {"results": results, "count": len(results), "query_text": query_text}


def _encode_enrichment_query(query_text: str) -> "np.ndarray":
    """Encode a text query using the enrichment embedding model (BGE-M3)."""
    from enrichment_embeddings import _BaseEnrichmentGenerator
    gen = _BaseEnrichmentGenerator()
    gen.load_model()
    vector = gen.model.encode(query_text, normalize_embeddings=True)
    gen.unload_model()
    return vector


def _vector_literal(vector) -> str:
    """Format a numpy vector as a pgvector SQL literal."""
    return "'" + "[" + ",".join(str(float(x)) for x in vector) + "]" + "'::vector"


def _get_text_model_id(db: Session):
    """Get embedding model ID from DB. Returns None if not found."""
    row = db.execute(
        text("SELECT id FROM embedding_models WHERE name = :name"),
        {"name": settings.text_embedding_model},
    ).fetchone()
    return row.id if row else None


def search_artists_by_bio(
    db: Session,
    query_text: str,
    limit: int = 10,
    min_similarity: float = 0.3,
) -> Dict[str, Any]:
    """
    Search artists by biography similarity.

    Returns artists ranked by how well their Last.fm bio matches the query.
    Each artist includes track_count and sample tracks for context.

    Use for: "British rock band from the 70s", "female jazz vocalist",
    "German electronic music producer", "blind blues musician".
    """
    model_id = _get_text_model_id(db)
    if not model_id:
        return {"error": "Text embedding model not found", "results": [], "count": 0}

    query_vector = _encode_enrichment_query(query_text)
    vector_str = _vector_literal(query_vector)

    sql = text(f"""
        SELECT
            a.id as artist_id,
            a.name as artist_name,
            matches.similarity,
            (SELECT COUNT(*) FROM track_artists ta2
             WHERE ta2.artist_id = a.id AND ta2.role = 'primary') as track_count,
            (SELECT STRING_AGG(sub.info, ' | ' ORDER BY sub.info)
             FROM (
                SELECT t.title || ' (' || al.title || ')' as info
                FROM track_artists ta3
                JOIN tracks t ON ta3.track_id = t.id
                JOIN media_files mf ON mf.track_id = t.id AND mf.is_analysis_source = TRUE
                JOIN album_variants av ON mf.album_variant_id = av.id
                JOIN albums al ON av.album_id = al.id
                WHERE ta3.artist_id = a.id AND ta3.role = 'primary'
                LIMIT 3
             ) sub
            ) as sample_tracks
        FROM (
            SELECT abe.artist_id,
                   MAX(1 - (abe.vector <=> {vector_str})) as similarity
            FROM artist_bio_embeddings abe
            WHERE abe.model_id = :model_id
            GROUP BY abe.artist_id
            HAVING MAX(1 - (abe.vector <=> {vector_str})) >= :min_similarity
        ) matches
        JOIN artists a ON matches.artist_id = a.id
        ORDER BY matches.similarity DESC
        LIMIT :limit
    """)

    rows = db.execute(sql, {
        "model_id": model_id, "min_similarity": min_similarity, "limit": limit,
    }).fetchall()

    results = [
        {
            "artist_id": str(r.artist_id),
            "artist": r.artist_name,
            "similarity": round(float(r.similarity), 4),
            "track_count": r.track_count,
            "sample_tracks": r.sample_tracks,
        }
        for r in rows
    ]

    return {"results": results, "count": len(results), "query_text": query_text}


def search_albums_by_info(
    db: Session,
    query_text: str,
    limit: int = 10,
    min_similarity: float = 0.3,
) -> Dict[str, Any]:
    """
    Search albums by description similarity.

    Returns albums ranked by how well their Last.fm description matches the query.
    Each album includes artist name, year, and track count.

    Use for: "concept album about war", "live recording from the 80s",
    "debut album", "double album with orchestral arrangements".
    """
    model_id = _get_text_model_id(db)
    if not model_id:
        return {"error": "Text embedding model not found", "results": [], "count": 0}

    query_vector = _encode_enrichment_query(query_text)
    vector_str = _vector_literal(query_vector)

    sql = text(f"""
        SELECT
            al.id as album_id,
            al.title as album_title,
            a.name as artist_name,
            al.release_year,
            matches.similarity,
            (SELECT COUNT(DISTINCT mf2.track_id)
             FROM album_variants av2
             JOIN media_files mf2 ON mf2.album_variant_id = av2.id
             WHERE av2.album_id = al.id) as track_count
        FROM (
            SELECT aie.album_id,
                   MAX(1 - (aie.vector <=> {vector_str})) as similarity
            FROM album_info_embeddings aie
            WHERE aie.model_id = :model_id
            GROUP BY aie.album_id
            HAVING MAX(1 - (aie.vector <=> {vector_str})) >= :min_similarity
        ) matches
        JOIN albums al ON matches.album_id = al.id
        -- Get primary artist via first track on this album
        JOIN LATERAL (
            SELECT a2.name, a2.id
            FROM album_variants av3
            JOIN media_files mf3 ON mf3.album_variant_id = av3.id
            JOIN track_artists ta ON mf3.track_id = ta.track_id AND ta.role = 'primary'
            JOIN artists a2 ON ta.artist_id = a2.id
            WHERE av3.album_id = al.id
            LIMIT 1
        ) a ON true
        ORDER BY matches.similarity DESC
        LIMIT :limit
    """)

    rows = db.execute(sql, {
        "model_id": model_id, "min_similarity": min_similarity, "limit": limit,
    }).fetchall()

    results = [
        {
            "album_id": str(r.album_id),
            "album": r.album_title,
            "artist": r.artist_name,
            "year": r.release_year,
            "similarity": round(float(r.similarity), 4),
            "track_count": r.track_count,
        }
        for r in rows
    ]

    return {"results": results, "count": len(results), "query_text": query_text}


def search_genres_by_description(
    db: Session,
    query_text: str,
    limit: int = 10,
    min_similarity: float = 0.3,
) -> Dict[str, Any]:
    """
    Search genres by description similarity.

    Returns genres ranked by how well their description matches the query.
    Each genre includes track count and sample artists.

    Use for: "music with heavy distorted guitars", "slow romantic music",
    "African rhythms", "repetitive minimalist compositions".
    """
    model_id = _get_text_model_id(db)
    if not model_id:
        return {"error": "Text embedding model not found", "results": [], "count": 0}

    query_vector = _encode_enrichment_query(query_text)
    vector_str = _vector_literal(query_vector)

    sql = text(f"""
        SELECT
            g.id as genre_id,
            g.name as genre_name,
            matches.similarity,
            (SELECT COUNT(*) FROM track_genres tg2 WHERE tg2.genre_id = g.id) as track_count,
            (SELECT STRING_AGG(DISTINCT sub.name, ', ' ORDER BY sub.name)
             FROM (
                SELECT a.name
                FROM track_genres tg3
                JOIN track_artists ta ON tg3.track_id = ta.track_id AND ta.role = 'primary'
                JOIN artists a ON ta.artist_id = a.id
                WHERE tg3.genre_id = g.id
                LIMIT 5
             ) sub
            ) as sample_artists
        FROM (
            SELECT gde.genre_id,
                   MAX(1 - (gde.vector <=> {vector_str})) as similarity
            FROM genre_desc_embeddings gde
            WHERE gde.model_id = :model_id
            GROUP BY gde.genre_id
            HAVING MAX(1 - (gde.vector <=> {vector_str})) >= :min_similarity
        ) matches
        JOIN genres g ON matches.genre_id = g.id
        ORDER BY matches.similarity DESC
        LIMIT :limit
    """)

    rows = db.execute(sql, {
        "model_id": model_id, "min_similarity": min_similarity, "limit": limit,
    }).fetchall()

    results = [
        {
            "genre_id": str(r.genre_id),
            "genre": r.genre_name,
            "similarity": round(float(r.similarity), 4),
            "track_count": r.track_count,
            "sample_artists": r.sample_artists,
        }
        for r in rows
    ]

    return {"results": results, "count": len(results), "query_text": query_text}
