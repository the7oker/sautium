"""
Genre detail endpoint.

Aggregates everything the Genre screen needs in a single roundtrip:
identity (id + name), description (preferring summary; falls back to
content), a representative album cover (most-tracks-in-genre as a soft
proxy for "iconic for this genre in *your* library"), and the artist
list ranked by relevance — primary track count in this genre, with
local play count as a tiebreaker.
"""

from fastapi import APIRouter, HTTPException

from db_pool import db_query, db_query_one


router = APIRouter(prefix="/api/genres", tags=["genres"])


@router.get("/{genre_id}")
def get_genre(genre_id: str) -> dict:
    genre = db_query_one("""
        SELECT g.id::text AS id, g.name
        FROM genres g
        WHERE g.id = %(id)s::uuid
    """, {"id": genre_id})
    if not genre:
        raise HTTPException(status_code=404, detail="genre not found")

    desc = db_query_one("""
        SELECT summary, content, source
        FROM genre_descriptions
        WHERE genre_id = %(id)s::uuid
        ORDER BY
            CASE WHEN summary IS NOT NULL THEN 0 ELSE 1 END,
            updated_at DESC
        LIMIT 1
    """, {"id": genre_id})
    genre["description"] = (desc and (desc["summary"] or desc["content"])) or None

    # Banner cover — single representative album cover from the genre's
    # tracks. Pick the cover_id that backs the most tracks in the
    # genre; on ties the row order is stable enough for display.
    # SENTINEL_COVER_ID (all-zeros UUID) marks files where lazy cover
    # resolution failed; it's the most-frequent value on a fresh
    # library and would short-circuit the "most representative cover"
    # heuristic into the empty-image placeholder.
    cover = db_query_one("""
        SELECT mf.cover_id::text AS cover_id
        FROM media_files mf
        JOIN track_genres tg ON tg.track_id = mf.track_id
        WHERE tg.genre_id = %(id)s::uuid
          AND mf.cover_id IS NOT NULL
          AND mf.cover_id <> '00000000-0000-0000-0000-000000000000'::uuid
        GROUP BY mf.cover_id
        ORDER BY COUNT(*) DESC
        LIMIT 1
    """, {"id": genre_id})
    genre["cover_id"] = cover and cover["cover_id"]

    # Track count — quick stat for the screen header.
    counts = db_query_one("""
        SELECT COUNT(DISTINCT t.id)::int AS tracks,
               COUNT(DISTINCT al.id)::int AS albums,
               COUNT(DISTINCT ta.artist_id)
                 FILTER (WHERE ta.role = 'primary')::int AS artists
        FROM tracks t
        JOIN track_genres tg ON tg.track_id = t.id
        JOIN media_files mf ON mf.track_id = t.id
        JOIN album_variants av ON av.id = mf.album_variant_id
        JOIN albums al ON al.id = av.album_id
        JOIN track_artists ta ON ta.track_id = t.id
        WHERE tg.genre_id = %(id)s::uuid
    """, {"id": genre_id})
    genre["track_count"] = (counts and counts["tracks"]) or 0
    genre["album_count"] = (counts and counts["albums"]) or 0
    genre["artist_count"] = (counts and counts["artists"]) or 0

    # Artists in this genre, ranked by primary-track count with local
    # play count as a tiebreaker. Limited to 24 — enough for two rows
    # of avatar tiles, more than that turns the screen into a directory.
    genre["artists"] = db_query("""
        SELECT a.id::text AS id,
               a.name,
               COUNT(DISTINCT t.id)::int AS track_count,
               COALESCE(SUM(lps.play_count), 0)::int AS plays
        FROM artists a
        JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
        JOIN tracks t ON t.id = ta.track_id
        JOIN track_genres tg ON tg.track_id = t.id
        LEFT JOIN local_play_stats lps ON lps.track_id = t.id
        WHERE tg.genre_id = %(id)s::uuid
        GROUP BY a.id, a.name
        ORDER BY track_count DESC, plays DESC, a.name
        LIMIT 24
    """, {"id": genre_id})

    return genre
