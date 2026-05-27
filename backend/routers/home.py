"""
Home screen endpoints.

Each Home section is its own endpoint so the frontend can render
on-readiness instead of waiting for the slowest block to load, and so
"New in library" can paginate independently via infinite scroll.

Favourite artists rank by total listening time (not play count): a
single 90-minute ambient track should outweigh ten 5-minute pop plays.

Recommendation strategy is intentionally simple for the first
iteration (random unplayed albums); a CLAP-similarity-driven
algorithm lands in a later step once the visual surface is proven.
"""

from datetime import datetime
from typing import Any
from fastapi import APIRouter, HTTPException, Query

from db_pool import db_query


router = APIRouter(prefix="/api/home", tags=["home"])


# Subqueries shared by new-in-library and recommendations to fetch the
# album-tile-row contract: {artist, cover_id, media_file_id}.
_ALBUM_TILE_SUBQUERIES = """
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
"""


@router.get("/favourite-artists")
def get_favourite_artists(
    limit: int = Query(100, ge=1, le=200),
) -> dict[str, list[dict[str, Any]]]:
    """Top primary artists ranked by total listening time."""

    # Count time only against the primary artist of each track — featured /
    # composer / conductor rows in track_artists would otherwise hoist
    # soundtrack composers into Favourites for any film-score listener, but
    # the artist page lists albums only where they are primary, so the tile
    # would lead to an empty detail screen. Tie-break by play_count so two
    # artists with identical (rare) total_seconds order stably.
    artists = db_query("""
        SELECT a.id::text AS id,
               a.name,
               SUM(lps.play_count)::int AS play_count,
               FLOOR(SUM(lps.total_listen_time))::bigint AS total_seconds
        FROM artists a
        JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
        JOIN local_play_stats lps ON lps.track_id = ta.track_id
        GROUP BY a.id, a.name
        HAVING SUM(lps.total_listen_time) > 0
        ORDER BY total_seconds DESC, play_count DESC
        LIMIT %(limit)s
    """, {"limit": limit})

    return {"artists": artists}


@router.get("/new-in-library")
def get_new_in_library(
    limit: int = Query(20, ge=1, le=50),
    before: str | None = None,
    before_id: str | None = None,
) -> dict[str, Any]:
    """Albums by recency of latest file mtime, cursor-paginated."""

    if (before is None) != (before_id is None):
        raise HTTPException(
            status_code=400,
            detail="before and before_id must be provided together",
        )

    cursor_ts: datetime | None = None
    if before is not None:
        try:
            cursor_ts = datetime.fromisoformat(before)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"invalid before timestamp: {exc}"
            )

    # album_variants.file_modified_at is denormalised from media_files via
    # FOR EACH STATEMENT triggers, so GROUP BY here scans ~3.7k variants
    # against an index on (file_modified_at DESC) instead of 30k+ media_files
    # rows. Cursor comparison uses (newest_added, album_id) as a composite
    # key so ties on identical timestamps still paginate without dupes.
    rows = db_query("""
        WITH album_keys AS (
            SELECT al.id AS album_id,
                   al.title,
                   al.release_year AS year,
                   MAX(av.file_modified_at) AS newest_added
            FROM albums al
            JOIN album_variants av ON av.album_id = al.id
            GROUP BY al.id, al.title, al.release_year
        )
        SELECT ak.album_id::text AS id,
               ak.title,
               ak.year,
               ak.newest_added,
               ak.album_id AS _album_uuid,
               (SELECT a.name
                FROM artists a
                JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
                JOIN tracks t ON t.id = ta.track_id
                JOIN media_files mf2 ON mf2.track_id = t.id
                JOIN album_variants av2 ON av2.id = mf2.album_variant_id
                WHERE av2.album_id = ak.album_id
                GROUP BY a.id, a.name
                ORDER BY COUNT(*) DESC
                LIMIT 1) AS artist,
               (SELECT mf3.cover_id::text
                FROM media_files mf3
                JOIN album_variants av3 ON av3.id = mf3.album_variant_id
                WHERE av3.album_id = ak.album_id AND mf3.cover_id IS NOT NULL
                LIMIT 1) AS cover_id,
               (SELECT mf4.id
                FROM media_files mf4
                JOIN album_variants av4 ON av4.id = mf4.album_variant_id
                WHERE av4.album_id = ak.album_id
                ORDER BY mf4.disc_number, mf4.track_number
                LIMIT 1) AS media_file_id
        FROM album_keys ak
        WHERE %(before)s::timestamptz IS NULL
           OR (ak.newest_added, ak.album_id) < (%(before)s::timestamptz, %(before_id)s::uuid)
        ORDER BY ak.newest_added DESC NULLS LAST, ak.album_id DESC
        LIMIT %(limit)s
    """, {
        "limit": limit,
        "before": cursor_ts,
        "before_id": before_id,
    })

    next_cursor: dict[str, str] | None = None
    if len(rows) == limit:
        last = rows[-1]
        next_cursor = {
            "before": last["newest_added"].isoformat(),
            "before_id": str(last["_album_uuid"]),
        }

    albums = [
        {k: v for k, v in row.items() if k not in {"newest_added", "_album_uuid"}}
        for row in rows
    ]

    return {"albums": albums, "next_cursor": next_cursor}


@router.get("/recommendations")
def get_recommendations(
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, list[dict[str, Any]]]:
    """Random unplayed albums — placeholder until CLAP-similarity lands."""

    albums = db_query(f"""
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
               {_ALBUM_TILE_SUBQUERIES}
        FROM albums al
        LEFT JOIN album_plays ap ON ap.album_id = al.id
        WHERE COALESCE(ap.total_plays, 0) = 0
        ORDER BY RANDOM()
        LIMIT %(limit)s
    """, {"limit": limit})

    return {"albums": albums}
