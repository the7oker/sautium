"""Shared genre-chip queries — one source of truth so the artist page, the
owned-album page and the phantom-album page all show genres by the same rule.

An artist's genres = Last.fm artist-tags that map to a genre entity (weight >=
10) UNION genres carried by >= 5 of the artist's primary owned tracks. An
album's chips = its own album_genres, falling back to the primary artist's
genres when the album itself has none (common for phantom albums and
sparsely-tagged releases — keeps owned and phantom pages consistent)."""
from typing import Optional

from db_pool import db_query

# Identical rule to the artist + genre detail pages (keep them in sync). The
# non-alphanumeric strip lets "nu-jazz" resolve to "Nu Jazz".
_ARTIST_GENRES_SQL = """
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
        JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
        JOIN media_files mf ON mf.track_id = t.id
        JOIN album_variants av ON av.id = mf.album_variant_id
        JOIN album_genres ag ON ag.album_id = av.album_id
        JOIN genres g ON g.id = ag.genre_id
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
"""

_ALBUM_GENRES_SQL = """
    SELECT g.id::text AS id, g.name, MAX(ag.count) AS occurrences
    FROM album_genres ag
    JOIN genres g ON g.id = ag.genre_id
    WHERE ag.album_id = %(id)s::uuid
    GROUP BY g.id, g.name
    ORDER BY MAX(ag.count) DESC NULLS LAST, g.name
    LIMIT %(lim)s
"""


def artist_genres(artist_id: str, limit: Optional[int] = None) -> list:
    """An artist's genres as entities (genre_id, name, weight, track_count)."""
    sql = _ARTIST_GENRES_SQL + (f"\n    LIMIT {int(limit)}" if limit else "")
    return db_query(sql, {"id": artist_id})


def album_genre_chips(album_id: str, artist_id: Optional[str] = None,
                      limit: int = 3) -> list:
    """An album's genre chips (id, name): its own album_genres, falling back to
    the primary artist's genres when the album has none. Used by both the
    owned- and phantom-album endpoints so they render identically."""
    rows = db_query(_ALBUM_GENRES_SQL, {"id": album_id, "lim": limit})
    if rows:
        return rows
    if artist_id:
        return [{"id": r["genre_id"], "name": r["name"]}
                for r in artist_genres(artist_id, limit)]
    return []
