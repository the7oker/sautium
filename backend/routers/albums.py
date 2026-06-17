"""
Album detail endpoint.

Aggregates album-level metadata (title, year, primary artist, format
quality), top genre chips, and the full tracklist with per-track
audio features (key, mode, BPM) into a single roundtrip.

When an album has more than one variant (multiple rips/encodings/
masters of the same logical release), callers can pick a specific
variant via ``?variant_id=<id>``. Without it, the response uses
DISTINCT ON across all variants of the album, falling back to the
"best" media_file per track (analysis-source first, lowest id as
deterministic tiebreaker). The full variant list is always returned
so the UI can render a selector without a second roundtrip.
"""

from fastapi import APIRouter, HTTPException, Query

from db_pool import db_query, db_query_one


router = APIRouter(prefix="/api/albums", tags=["albums"])


@router.get("/{album_id}")
def get_album(
    album_id: str,
    variant_id: int | None = Query(None),
) -> dict:
    if variant_id is not None:
        owner = db_query_one("""
            SELECT 1 AS ok FROM album_variants
            WHERE id = %(vid)s AND album_id = %(aid)s::uuid
        """, {"vid": variant_id, "aid": album_id})
        if not owner:
            raise HTTPException(
                status_code=404,
                detail="variant not found for this album",
            )

    params = {"id": album_id, "vid": variant_id}

    album = db_query_one("""
        SELECT al.id::text AS id,
               al.title,
               al.release_year AS year,
               (SELECT mf.cover_id::text
                FROM media_files mf
                JOIN album_variants av ON av.id = mf.album_variant_id
                WHERE av.album_id = al.id
                  AND (%(vid)s::int IS NULL OR av.id = %(vid)s::int)
                  AND mf.cover_id IS NOT NULL
                LIMIT 1) AS cover_id,
               (SELECT mf2.id
                FROM media_files mf2
                JOIN album_variants av2 ON av2.id = mf2.album_variant_id
                WHERE av2.album_id = al.id
                  AND (%(vid)s::int IS NULL OR av2.id = %(vid)s::int)
                ORDER BY mf2.disc_number, mf2.track_number
                LIMIT 1) AS media_file_id
        FROM albums al
        WHERE al.id = %(id)s::uuid
    """, params)

    if not album:
        raise HTTPException(status_code=404, detail="album not found")

    # Most-frequent primary artist for this album (always album-level,
    # independent of variant — composers don't change per remaster).
    primary = db_query_one("""
        SELECT a.id::text AS id, a.name
        FROM artists a
        JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
        JOIN tracks t ON t.id = ta.track_id
        JOIN media_files mf ON mf.track_id = t.id
        JOIN album_variants av ON av.id = mf.album_variant_id
        WHERE av.album_id = %(id)s::uuid
        GROUP BY a.id, a.name
        ORDER BY COUNT(*) DESC
        LIMIT 1
    """, {"id": album_id})
    album["primary_artist"] = primary

    # Quality + total duration. When no variant_id is requested, the same
    # DISTINCT ON pattern as the tracklist below — pick one media_file per
    # track (analysis-source preferred, lowest id as fallback) so multi-
    # variant albums don't double-count duration. When a specific variant
    # is requested, restrict to that variant before DISTINCT ON; tracks
    # without a media_file in that variant simply don't contribute.
    qrow = db_query_one("""
        SELECT BOOL_OR(is_lossless)          AS lossless,
               MAX(sample_rate)              AS sr_max,
               MAX(bit_depth)                AS bd_max,
               SUM(duration_seconds)         AS total_duration
        FROM (
            SELECT DISTINCT ON (mf.track_id)
                   mf.track_id,
                   mf.is_lossless,
                   mf.sample_rate,
                   mf.bit_depth,
                   mf.duration_seconds
            FROM media_files mf
            JOIN album_variants av ON av.id = mf.album_variant_id
            WHERE av.album_id = %(id)s::uuid
              AND (%(vid)s::int IS NULL OR av.id = %(vid)s::int)
            ORDER BY mf.track_id, mf.is_analysis_source DESC, mf.id
        ) per_track
    """, params)
    sr = qrow["sr_max"] or 0
    bd = qrow["bd_max"] or 0
    if qrow["lossless"] and sr >= 48000 and bd >= 24:
        album["quality"] = "hi-res"
    elif qrow["lossless"]:
        album["quality"] = "lossless"
    else:
        album["quality"] = "lossy"
    album["total_duration"] = float(qrow["total_duration"] or 0)

    # Top 3 genres for the album, deduped across sources (filetag/mb) by
    # genre_id and ranked by count (file occurrences or MB tag votes).
    # Genre is album-grain, so this is a direct read — no track rollup.
    album["genres"] = db_query("""
        SELECT g.id::text AS id, g.name, MAX(ag.count) AS occurrences
        FROM album_genres ag
        JOIN genres g ON g.id = ag.genre_id
        WHERE ag.album_id = %(id)s::uuid
        GROUP BY g.id, g.name
        ORDER BY MAX(ag.count) DESC NULLS LAST, g.name
        LIMIT 3
    """, {"id": album_id})

    # Tracklist ordered by disc / track number. `is_analysis_source`
    # is a *preference* — it marks the media_file we chose for audio
    # analysis when a track has multiple variants — not a "show this
    # in the UI" flag. Filtering on it strictly hides tracks whose
    # variants weren't picked yet (newly imported files, edge cases
    # like Wingbeats where two media_files share disc/track and
    # neither is flagged). Use DISTINCT ON instead: one row per
    # track, picking analysis-source first and the lowest media_file
    # id as the deterministic fallback. When a variant is pinned, the
    # WHERE clause narrows the pool to that variant's media_files only.
    album["tracks"] = db_query("""
        SELECT DISTINCT ON (t.id)
               t.id::text AS track_id,
               mf.id AS media_file_id,
               t.title,
               mf.disc_number,
               mf.track_number,
               mf.duration_seconds AS duration,
               af.bpm,
               af.key,
               af.mode
        FROM media_files mf
        JOIN tracks t ON t.id = mf.track_id
        JOIN album_variants av ON av.id = mf.album_variant_id
        LEFT JOIN audio_features af ON af.track_id = t.id
        WHERE av.album_id = %(id)s::uuid
          AND (%(vid)s::int IS NULL OR av.id = %(vid)s::int)
        ORDER BY t.id,
                 mf.is_analysis_source DESC,
                 mf.id
    """, params)
    album["tracks"].sort(key=lambda r: (
        r.get("disc_number") if r.get("disc_number") is not None else 99,
        r.get("track_number") if r.get("track_number") is not None else 999,
        r.get("title") or "",
    ))

    # Full variant list, always returned (UI hides the selector when
    # len == 1). Ordered "best first" so a UI defaulting to variants[0]
    # picks the lossless/highest-resolution rip without further logic.
    album["variants"] = db_query("""
        SELECT av.id AS variant_id,
               av.sample_rate,
               av.bit_depth,
               av.is_lossless,
               av.directory_path,
               av.file_modified_at,
               (SELECT mf.id FROM media_files mf
                WHERE mf.album_variant_id = av.id
                ORDER BY mf.disc_number, mf.track_number
                LIMIT 1) AS first_media_file_id,
               (SELECT mf.file_format FROM media_files mf
                WHERE mf.album_variant_id = av.id
                LIMIT 1) AS file_format
        FROM album_variants av
        WHERE av.album_id = %(id)s::uuid
        ORDER BY av.is_lossless DESC NULLS LAST,
                 av.sample_rate DESC NULLS LAST,
                 av.bit_depth DESC NULLS LAST,
                 av.id
    """, {"id": album_id})
    album["selected_variant_id"] = variant_id

    return album


@router.get("/{album_id}/similar")
def get_similar_albums(
    album_id: str,
    limit: int = Query(12, ge=1, le=50),
    exclude_same_artist: bool = Query(False),
    min_similarity: float = Query(0.6, ge=0.0, le=1.0),
) -> dict:
    """Audio-similar albums (CLAP, one-to-one assignment scoring).

    Read-through cache: served from `similar_albums` when present, otherwise
    computed on first view and persisted (see backend/album_similarity.py).
    The compute (~1s, sync → FastAPI threadpool) only happens once per album.
    """
    from album_similarity import SOURCE, compute_and_cache

    cached = db_query_one(
        "SELECT 1 AS ok FROM similar_albums WHERE album_id = %(id)s::uuid AND source = %(src)s LIMIT 1",
        {"id": album_id, "src": SOURCE},
    )
    if not cached:
        # Compute only when the album actually has embeddings; otherwise leave
        # the cache empty so it recomputes once the album is analysed.
        has_emb = db_query_one("""
            SELECT 1 AS ok
            FROM embeddings e
            JOIN media_files mf ON mf.track_id = e.track_id
            JOIN album_variants av ON av.id = mf.album_variant_id
            WHERE av.album_id = %(id)s::uuid
            LIMIT 1
        """, {"id": album_id})
        if has_emb:
            compute_and_cache(album_id)

    # Enrich the cached neighbours with display metadata + the most-frequent
    # primary artist (album has no artist_id), excluding same-artist when asked.
    # All ordering / filtering / trimming pushed into SQL.
    results = db_query("""
        WITH src_artist AS (
            SELECT ta.artist_id
            FROM track_artists ta
            JOIN media_files mf ON mf.track_id = ta.track_id
            JOIN album_variants av ON av.id = mf.album_variant_id
            WHERE av.album_id = %(id)s::uuid AND ta.role = 'primary'
            GROUP BY ta.artist_id
            ORDER BY COUNT(*) DESC
            LIMIT 1
        )
        SELECT sa.similar_album_id::text AS album_id,
               al.title AS album,
               al.release_year AS year,
               sa.match_score AS similarity,
               pa.name AS artist,
               pa.artist_id::text AS artist_id,
               (SELECT mf2.cover_id::text
                FROM album_variants av2
                JOIN media_files mf2 ON mf2.album_variant_id = av2.id
                WHERE av2.album_id = sa.similar_album_id AND mf2.cover_id IS NOT NULL
                LIMIT 1) AS cover_id,
               (SELECT mf3.id
                FROM album_variants av3
                JOIN media_files mf3 ON mf3.album_variant_id = av3.id
                WHERE av3.album_id = sa.similar_album_id
                ORDER BY mf3.disc_number, mf3.track_number
                LIMIT 1) AS media_file_id
        FROM similar_albums sa
        JOIN albums al ON al.id = sa.similar_album_id
        JOIN LATERAL (
            SELECT a.id AS artist_id, a.name
            FROM track_artists ta
            JOIN artists a ON a.id = ta.artist_id
            JOIN media_files mf ON mf.track_id = ta.track_id
            JOIN album_variants av ON av.id = mf.album_variant_id
            WHERE av.album_id = sa.similar_album_id AND ta.role = 'primary'
            GROUP BY a.id, a.name
            ORDER BY COUNT(*) DESC
            LIMIT 1
        ) pa ON true
        WHERE sa.album_id = %(id)s::uuid
          AND sa.source = %(src)s
          AND sa.match_score >= %(floor)s
          AND (NOT %(excl)s OR pa.artist_id <> (SELECT artist_id FROM src_artist))
        ORDER BY sa.match_score DESC
        LIMIT %(limit)s
    """, {"id": album_id, "src": SOURCE, "excl": exclude_same_artist,
          "floor": min_similarity, "limit": limit})

    for r in results:
        r["similarity"] = round(float(r["similarity"]), 4) if r["similarity"] is not None else None

    return {"results": results}
