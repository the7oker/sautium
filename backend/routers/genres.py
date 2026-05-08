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

    # The hero banner shows the photo of the top-ranked artist —
    # picked at render time from genre["artists"][0] on the
    # frontend. No precomputed cover_id needed; /api/covers/by-artist
    # lazy-resolves the photo from Last.fm on first hit.

    # Artists in this genre — same evidence model as the artist
    # detail page (so artist-X-on-genre-Y and genre-Y-on-artist-X
    # stay in sync). An artist is "in" the genre if either:
    #   * Last.fm artist-level tag for the genre carries
    #     weight >= 10 ("authoritative" signal; same source as the
    #     artist hero's chip row), OR
    #   * the artist has at least 5 primary tracks tagged with the
    #     genre via track_genres ("library-derived" signal — picks
    #     up niche artists with weak Last.fm tagging but heavy
    #     track-level genre coverage in this library).
    # Last.fm weight is the primary sort key — track-level evidence
    # is supplementary, surfaced after every weighted artist.
    artist_rows = db_query("""
        WITH g AS (
            SELECT id,
                   regexp_replace(LOWER(name), '[^a-z0-9]', '', 'g') AS norm
            FROM genres WHERE id = %(id)s::uuid
        ),
        via_tag AS (
            SELECT a.id::text AS id, a.name,
                   COALESCE(at.weight, 0)::int AS lastfm_weight,
                   0::int AS track_count,
                   0::int AS plays
            FROM artists a
            JOIN artist_tags at ON at.artist_id = a.id
            JOIN tags ag ON ag.id = at.tag_id
            WHERE regexp_replace(LOWER(ag.name), '[^a-z0-9]', '', 'g')
                = (SELECT norm FROM g)
              AND COALESCE(at.weight, 0) >= 10
        ),
        via_track AS (
            SELECT a.id::text AS id, a.name,
                   0::int AS lastfm_weight,
                   COUNT(DISTINCT t.id)::int AS track_count,
                   COALESCE(SUM(lps.play_count), 0)::int AS plays
            FROM artists a
            JOIN track_artists ta
              ON ta.artist_id = a.id AND ta.role = 'primary'
            JOIN tracks t ON t.id = ta.track_id
            JOIN track_genres tg ON tg.track_id = t.id
            LEFT JOIN local_play_stats lps ON lps.track_id = t.id
            WHERE tg.genre_id = (SELECT id FROM g)
            GROUP BY a.id, a.name
            HAVING COUNT(DISTINCT t.id) >= 5
        )
        SELECT id, name,
               MAX(lastfm_weight)::int AS weight,
               MAX(track_count)::int   AS track_count,
               MAX(plays)::int         AS plays
        FROM (SELECT * FROM via_tag UNION ALL SELECT * FROM via_track) u
        GROUP BY id, name
        ORDER BY weight DESC, track_count DESC, plays DESC, name
    """, {"id": genre_id})

    artist_uuids = [r["id"] for r in artist_rows]
    if artist_uuids:
        # Tracks/albums are scoped to the matched cohort's primary
        # output in the user's library — broader than just tracks
        # tagged with this genre, which understates the picture
        # whenever Last.fm artist-level tagging is stronger signal
        # than per-track tagging (typical for older or niche genres).
        cohort_counts = db_query_one("""
            SELECT COUNT(DISTINCT t.id)::int AS tracks,
                   COUNT(DISTINCT al.id)::int AS albums
            FROM track_artists ta
            JOIN tracks t ON t.id = ta.track_id
            JOIN media_files mf ON mf.track_id = t.id
            JOIN album_variants av ON av.id = mf.album_variant_id
            JOIN albums al ON al.id = av.album_id
            WHERE ta.role = 'primary'
              AND ta.artist_id::text = ANY(%(ids)s)
        """, {"ids": artist_uuids})
        genre["track_count"] = (cohort_counts and cohort_counts["tracks"]) or 0
        genre["album_count"] = (cohort_counts and cohort_counts["albums"]) or 0
    else:
        genre["track_count"] = 0
        genre["album_count"] = 0

    # Return the full cohort — frontend tiles use loading="lazy" so
    # the browser only fetches photos as the user scrolls, and the
    # stat line stays honest with the grid below it (no "X of Y"
    # split between header and tiles).
    genre["artist_count"] = len(artist_rows)
    genre["artists"] = artist_rows

    return genre
