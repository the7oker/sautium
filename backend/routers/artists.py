"""
Artist detail endpoint.

Aggregates everything the Artist screen needs in a single roundtrip:
identity, Last.fm bio + tags, the artist's albums in user's library,
popular tracks (by local play count), and similar artists with one
representative cover for each. Photo URL is reserved for future
Last.fm artist-image enrichment — returned as null until Step 1.7.
"""

from fastapi import APIRouter, HTTPException

from db_pool import db_query, db_query_one


router = APIRouter(prefix="/api/artists", tags=["artists"])


@router.get("/{artist_id}")
def get_artist(artist_id: str) -> dict:
    artist = db_query_one("""
        SELECT a.id::text AS id,
               a.name,
               NULL::text AS photo_url
        FROM artists a
        WHERE a.id = %(id)s::uuid
    """, {"id": artist_id})

    if not artist:
        raise HTTPException(status_code=404, detail="artist not found")

    bio_row = db_query_one("""
        SELECT content, summary
        FROM artist_bios
        WHERE artist_id = %(id)s::uuid
    """, {"id": artist_id})
    artist["bio"] = bio_row["content"] if bio_row else None
    artist["bio_summary"] = bio_row["summary"] if bio_row else None

    artist["tags"] = db_query("""
        SELECT t.name
        FROM artist_tags at
        JOIN tags t ON t.id = at.tag_id
        WHERE at.artist_id = %(id)s::uuid
        ORDER BY at.weight DESC NULLS LAST
        LIMIT 4
    """, {"id": artist_id})

    artist["albums"] = db_query("""
        SELECT al.id::text AS id,
               al.title,
               al.release_year AS year,
               (SELECT mf.cover_id::text
                FROM media_files mf
                JOIN album_variants av ON av.id = mf.album_variant_id
                WHERE av.album_id = al.id AND mf.cover_id IS NOT NULL
                LIMIT 1) AS cover_id,
               (SELECT mf2.id
                FROM media_files mf2
                JOIN album_variants av2 ON av2.id = mf2.album_variant_id
                WHERE av2.album_id = al.id
                ORDER BY mf2.disc_number, mf2.track_number
                LIMIT 1) AS media_file_id
        FROM albums al
        WHERE al.id IN (
            SELECT DISTINCT av.album_id
            FROM album_variants av
            JOIN media_files mf ON mf.album_variant_id = av.id
            JOIN tracks t ON t.id = mf.track_id
            JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
            WHERE ta.artist_id = %(id)s::uuid
        )
        ORDER BY al.release_year DESC NULLS LAST, al.title
        LIMIT 12
    """, {"id": artist_id})

    artist["popular_tracks"] = db_query("""
        SELECT DISTINCT ON (t.id)
               t.id::text AS track_id,
               mf.id AS media_file_id,
               t.title,
               al.title AS album,
               mf.duration_seconds AS duration,
               COALESCE(lps.play_count, 0) AS plays
        FROM tracks t
        JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
        JOIN media_files mf ON mf.track_id = t.id AND mf.is_analysis_source = true
        JOIN album_variants av ON av.id = mf.album_variant_id
        JOIN albums al ON al.id = av.album_id
        LEFT JOIN local_play_stats lps ON lps.track_id = t.id
        WHERE ta.artist_id = %(id)s::uuid
        ORDER BY t.id, COALESCE(lps.play_count, 0) DESC
    """, {"id": artist_id})
    # Re-sort by plays after DISTINCT ON, take top 5
    artist["popular_tracks"] = sorted(
        artist["popular_tracks"], key=lambda r: r["plays"], reverse=True
    )[:5]

    artist["similar_artists"] = db_query("""
        SELECT a.id::text AS id,
               a.name,
               (SELECT mf.cover_id::text
                FROM media_files mf
                JOIN tracks t ON t.id = mf.track_id
                JOIN track_artists ta ON ta.track_id = t.id
                WHERE ta.artist_id = a.id AND mf.cover_id IS NOT NULL
                LIMIT 1) AS cover_id,
               (SELECT mf2.id
                FROM media_files mf2
                JOIN tracks t2 ON t2.id = mf2.track_id
                JOIN track_artists ta2 ON ta2.track_id = t2.id AND ta2.role = 'primary'
                WHERE ta2.artist_id = a.id
                LIMIT 1) AS media_file_id
        FROM similar_artists sa
        JOIN artists a ON a.id = sa.similar_artist_id
        WHERE sa.artist_id = %(id)s::uuid
        ORDER BY sa.match_score DESC NULLS LAST
        LIMIT 5
    """, {"id": artist_id})

    return artist
