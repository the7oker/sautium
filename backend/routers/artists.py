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

    # Genres for this artist — same evidence rule as the genre detail
    # page (so artist-X-on-genre-Y and genre-Y-on-artist-X stay in
    # sync). An artist is "in" a genre if either the Last.fm artist-
    # tag for it carries weight >= 10 OR the artist has at least 5
    # primary tracks tagged with that genre via track_genres.
    # Last.fm weight wins on sort; track count is the supplementary
    # signal that surfaces niche artists whose Last.fm tagging is
    # weak but whose tracks are clearly genre-tagged in the library.
    # The non-alphanumeric-strip lets "nu-jazz" resolve to "Nu Jazz".
    artist["tags"] = db_query("""
        WITH via_tag AS (
            SELECT g.id::text AS genre_id, g.name,
                   COALESCE(at.weight, 0)::int AS lastfm_weight,
                   0::int AS track_count
            FROM artist_tags at
            JOIN tags tg ON tg.id = at.tag_id
            JOIN genres g
              ON regexp_replace(LOWER(g.name), '[^a-z0-9]', '', 'g')
               = regexp_replace(LOWER(tg.name), '[^a-z0-9]', '', 'g')
            WHERE at.artist_id = %(id)s::uuid
              AND COALESCE(at.weight, 0) >= 10
        ),
        via_track AS (
            SELECT g.id::text AS genre_id, g.name,
                   0::int AS lastfm_weight,
                   COUNT(DISTINCT t.id)::int AS track_count
            FROM tracks t
            JOIN track_artists ta
              ON ta.track_id = t.id AND ta.role = 'primary'
            JOIN track_genres tgr ON tgr.track_id = t.id
            JOIN genres g ON g.id = tgr.genre_id
            WHERE ta.artist_id = %(id)s::uuid
            GROUP BY g.id, g.name
            HAVING COUNT(DISTINCT t.id) >= 5
        )
        SELECT genre_id, name,
               MAX(lastfm_weight)::int AS weight,
               MAX(track_count)::int  AS track_count
        FROM (SELECT * FROM via_tag UNION ALL SELECT * FROM via_track) u
        GROUP BY genre_id, name
        ORDER BY weight DESC, track_count DESC, name
    """, {"id": artist_id})

    # Discography: every album where the artist appears in any role.
    # role_priority sorts the list so genuine solo records lead, true
    # collabs (multiple primary artists) follow, and featured-only
    # appearances close out the grid. is_primary feeds a small
    # "feat." badge on tiles where the artist isn't the headline.
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
                LIMIT 1) AS media_file_id,
               BOOL_OR(ta.role = 'primary' AND ta.artist_id = %(id)s::uuid)
                   AS is_primary,
               CASE
                 WHEN BOOL_OR(ta.role = 'primary' AND ta.artist_id = %(id)s::uuid)
                      AND NOT BOOL_OR(ta.role = 'primary' AND ta.artist_id <> %(id)s::uuid)
                   THEN 1
                 WHEN BOOL_OR(ta.role = 'primary' AND ta.artist_id = %(id)s::uuid)
                   THEN 2
                 ELSE 3
               END AS role_priority
        FROM albums al
        JOIN album_variants av ON av.album_id = al.id
        JOIN media_files mf ON mf.album_variant_id = av.id
        JOIN tracks t ON t.id = mf.track_id
        JOIN track_artists ta ON ta.track_id = t.id
        WHERE al.id IN (
            SELECT DISTINCT av2.album_id
            FROM album_variants av2
            JOIN media_files mf2 ON mf2.album_variant_id = av2.id
            JOIN tracks t2 ON t2.id = mf2.track_id
            JOIN track_artists ta2 ON ta2.track_id = t2.id
            WHERE ta2.artist_id = %(id)s::uuid
        )
        GROUP BY al.id, al.title, al.release_year
        ORDER BY role_priority, al.release_year DESC NULLS LAST, al.title
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
        JOIN track_artists ta ON ta.track_id = t.id
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
