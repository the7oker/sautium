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


@router.get("")
def top_genres(limit: int = 14) -> dict:
    """Top genres by owned-album coverage — the Discovery filter panel's quick
    chips (the full 500+ owned-genre list is reachable via typeahead search)."""
    rows = db_query("""
        SELECT g.id::text AS genre_id, g.name AS genre,
               COUNT(DISTINCT ag.album_id) AS album_count
        FROM genres g
        JOIN album_genres ag ON ag.genre_id = g.id
        JOIN album_variants av ON av.album_id = ag.album_id
        GROUP BY g.id, g.name
        ORDER BY COUNT(DISTINCT ag.album_id) DESC
        LIMIT %(limit)s
    """, {"limit": min(int(limit), 40)})
    return {"genres": rows}


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
    #   * the artist has at least 5 primary tracks on albums tagged
    #     with the genre (album_genres) ("library-derived" signal —
    #     picks up niche artists with weak Last.fm tagging but heavy
    #     genre coverage in this library).
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
            LEFT JOIN local_play_stats lps ON lps.track_id = t.id
            WHERE EXISTS (
                SELECT 1 FROM media_files mf
                JOIN album_variants av ON av.id = mf.album_variant_id
                JOIN album_genres ag ON ag.album_id = av.album_id
                WHERE mf.track_id = t.id AND ag.genre_id = (SELECT id FROM g)
            )
            GROUP BY a.id, a.name
            HAVING COUNT(DISTINCT t.id) >= 5
        )
        SELECT id, name,
               MAX(lastfm_weight)::int AS weight,
               MAX(track_count)::int   AS track_count,
               MAX(plays)::int         AS plays,
               EXISTS (SELECT 1 FROM media_files mf
                       JOIN track_artists ta ON ta.track_id = mf.track_id
                       WHERE ta.artist_id = u.id::uuid) AS is_owned
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

    # Popular tracks for this genre — hybrid rank weighted by how
    # strongly each track belongs to the genre.
    #
    #   relevance = max(
    #       direct  : 1.0 if track is tagged with the genre,
    #       indirect: artist_tags.weight / 100,
    #   )
    #
    # Tracks with relevance = 0 are dropped (artist isn't in the
    # cohort). Surviving tracks rank in two tiers, mirroring the
    # artist-page rule:
    #   tier 0 — track is known to Last.fm; order by
    #            playcount * relevance descending
    #   tier 1 — Last.fm knows nothing but the user has played it
    #            locally; order by play_count * relevance
    # Tracks with no popularity signal at all fall out — keeps the
    # block honest on a freshly-imported library.
    #
    # Tier ordering, weighting and the 5-row cap all happen in SQL.
    # `candidates` dedups media_file variants per track (one row per
    # track id, picking the variant with the strongest signal); the
    # outer SELECT applies the two-tier order and LIMITs to top 5,
    # so we never haul tens of thousands of rows into Python for a
    # genre with a large catalogue.
    genre["popular_tracks"] = db_query("""
        WITH g AS (
            SELECT id,
                   regexp_replace(LOWER(name), '[^a-z0-9]', '', 'g') AS norm
            FROM genres WHERE id = %(id)s::uuid
        ),
        artist_weights AS (
            SELECT a.id AS artist_id,
                   MAX(at.weight)::int AS weight
            FROM artists a
            JOIN artist_tags at ON at.artist_id = a.id
            JOIN tags tg ON tg.id = at.tag_id
            WHERE regexp_replace(LOWER(tg.name), '[^a-z0-9]', '', 'g')
                = (SELECT norm FROM g)
            GROUP BY a.id
        ),
        candidates AS (
            SELECT DISTINCT ON (t.id)
                   t.id::text AS track_id,
                   mf.id AS media_file_id,
                   t.title,
                   al.title AS album,
                   a.name AS artist,
                   mf.duration_seconds AS duration,
                   GREATEST(
                       CASE WHEN ag.album_id IS NOT NULL THEN 100 ELSE 0 END,
                       COALESCE(aw.weight, 0)
                   )::int AS relevance_pct,
                   COALESCE(lps.play_count, 0)::int AS local_plays,
                   COALESCE(ts.playcount, 0)::bigint AS lastfm_playcount
            FROM tracks t
            JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
            JOIN artists a ON a.id = ta.artist_id
            JOIN media_files mf ON mf.track_id = t.id AND mf.is_analysis_source = true
            JOIN album_variants av ON av.id = mf.album_variant_id
            JOIN albums al ON al.id = av.album_id
            LEFT JOIN album_genres ag
                   ON ag.album_id = av.album_id AND ag.genre_id = (SELECT id FROM g)
            LEFT JOIN artist_weights aw ON aw.artist_id = ta.artist_id
            LEFT JOIN local_play_stats lps ON lps.track_id = t.id
            LEFT JOIN track_stats ts
                   ON ts.track_id = t.id AND ts.source = 'lastfm'
            WHERE GREATEST(
                      CASE WHEN ag.album_id IS NOT NULL THEN 100 ELSE 0 END,
                      COALESCE(aw.weight, 0)
                  ) > 0
            ORDER BY t.id, COALESCE(ts.playcount, 0) DESC,
                           COALESCE(lps.play_count, 0) DESC
        )
        SELECT track_id, media_file_id, title, album, artist, duration
        FROM candidates
        WHERE lastfm_playcount > 0 OR local_plays > 0
        ORDER BY
            CASE WHEN lastfm_playcount > 0 THEN 0 ELSE 1 END,
            lastfm_playcount * relevance_pct DESC,
            local_plays * relevance_pct DESC,
            title
        LIMIT 5
    """, {"id": genre_id})

    return genre
