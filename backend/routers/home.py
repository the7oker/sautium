"""
Home screen feed endpoint.

Aggregates the data the Home tab needs into a single roundtrip:
favourite artists, recently imported albums, and a recommendations
seed. The endpoint returns plain JSON so the static frontend can
render directly without further processing.

Recommendation strategy is intentionally simple for the first
iteration (random unplayed albums); a CLAP-similarity-driven
algorithm lands in a later step once the visual surface is proven.
"""

from typing import Any
from fastapi import APIRouter, Query

from db_pool import db_query


router = APIRouter(prefix="/api/home", tags=["home"])


@router.get("/feed")
def get_home_feed(limit: int = Query(6, ge=1, le=24)) -> dict[str, list[dict[str, Any]]]:
    """Single payload powering the Home screen sections."""

    favourite_artists = db_query("""
        SELECT a.id::text AS id,
               a.name,
               SUM(lps.play_count)::int AS plays
        FROM artists a
        JOIN track_artists ta ON ta.artist_id = a.id
        JOIN local_play_stats lps ON lps.track_id = ta.track_id
        GROUP BY a.id, a.name
        HAVING SUM(lps.play_count) > 0
        ORDER BY plays DESC
        LIMIT %(limit)s
    """, {"limit": limit})

    new_in_library = db_query("""
        SELECT al.id::text AS id,
               al.title,
               al.release_year AS year,
               (SELECT a.name
                FROM artists a
                JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
                JOIN tracks t ON t.id = ta.track_id
                JOIN media_files mf2 ON mf2.track_id = t.id
                JOIN album_variants av2 ON av2.id = mf2.album_variant_id
                WHERE av2.album_id = al.id
                GROUP BY a.id, a.name
                ORDER BY COUNT(*) DESC
                LIMIT 1) AS artist,
               (SELECT mf3.cover_id::text
                FROM media_files mf3
                JOIN album_variants av3 ON av3.id = mf3.album_variant_id
                WHERE av3.album_id = al.id AND mf3.cover_id IS NOT NULL
                LIMIT 1) AS cover_id,
               (SELECT mf4.id
                FROM media_files mf4
                JOIN album_variants av4 ON av4.id = mf4.album_variant_id
                WHERE av4.album_id = al.id
                ORDER BY mf4.disc_number, mf4.track_number
                LIMIT 1) AS media_file_id
        FROM albums al
        JOIN album_variants av ON av.album_id = al.id
        JOIN media_files mf ON mf.album_variant_id = av.id
        GROUP BY al.id, al.title, al.release_year
        ORDER BY MAX(mf.file_modified_at) DESC NULLS LAST
        LIMIT %(limit)s
    """, {"limit": limit})

    # Recommendations v1: randomly picked albums the user hasn't played.
    # CLAP-similarity reranking comes in a later iteration.
    recommendations = db_query("""
        WITH album_plays AS (
            SELECT av.album_id,
                   SUM(COALESCE(lps.play_count, 0)) AS total_plays
            FROM album_variants av
            JOIN media_files mf ON mf.album_variant_id = av.id
            LEFT JOIN local_play_stats lps ON lps.track_id = mf.track_id
            GROUP BY av.album_id
        )
        SELECT al.id::text AS id,
               al.title,
               al.release_year AS year,
               (SELECT a.name
                FROM artists a
                JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
                JOIN tracks t ON t.id = ta.track_id
                JOIN media_files mf2 ON mf2.track_id = t.id
                JOIN album_variants av2 ON av2.id = mf2.album_variant_id
                WHERE av2.album_id = al.id
                GROUP BY a.id, a.name
                ORDER BY COUNT(*) DESC
                LIMIT 1) AS artist,
               (SELECT mf3.cover_id::text
                FROM media_files mf3
                JOIN album_variants av3 ON av3.id = mf3.album_variant_id
                WHERE av3.album_id = al.id AND mf3.cover_id IS NOT NULL
                LIMIT 1) AS cover_id,
               (SELECT mf4.id
                FROM media_files mf4
                JOIN album_variants av4 ON av4.id = mf4.album_variant_id
                WHERE av4.album_id = al.id
                ORDER BY mf4.disc_number, mf4.track_number
                LIMIT 1) AS media_file_id
        FROM albums al
        LEFT JOIN album_plays ap ON ap.album_id = al.id
        WHERE COALESCE(ap.total_plays, 0) = 0
        ORDER BY RANDOM()
        LIMIT %(limit)s
    """, {"limit": limit})

    return {
        "favourite_artists": favourite_artists,
        "new_in_library": new_in_library,
        "recommendations": recommendations,
    }
