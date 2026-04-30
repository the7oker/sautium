"""
Bulk hydration of entity tiles by ID.

Discovery's per-block endpoints couple search + ranking + cover
hydration into one SQL. Here we expose just the hydration step:
given a list of UUIDs / media_file_ids, return tile-shaped dicts
ready for the same `renderArtistRow` / `renderAlbumRow` /
`renderTrackList` frontend renderers Discovery uses.

Used by AI chat when the model produces a `[DJ_BLOCKS]` payload that
references entities by ID — covers, names, years and counts get
filled in here so the chat block list can render with the same
visual contract as Discovery results.

Output order matches the input ID order; rows for unknown IDs are
silently dropped (logged at debug level by the caller if needed).
"""

from db_pool import db_query


def hydrate_artists(artist_ids: list[str]) -> list[dict]:
    """Fetch artist tiles by UUID. Output shape mirrors
    `/api/discovery/artists` results: artist_id, artist, gender,
    is_vocalist, track_count, cover_id, media_file_id.
    """
    if not artist_ids:
        return []
    rows = db_query("""
        SELECT a.id::text AS artist_id,
               a.name AS artist,
               a.gender,
               a.is_vocalist,
               (SELECT COUNT(*)::int FROM track_artists ta
                WHERE ta.artist_id = a.id AND ta.role = 'primary') AS track_count,
               (SELECT mf.cover_id::text
                FROM media_files mf
                JOIN tracks t ON t.id = mf.track_id
                JOIN track_artists ta ON ta.track_id = t.id
                WHERE ta.artist_id = a.id AND mf.cover_id IS NOT NULL
                LIMIT 1) AS cover_id,
               (SELECT mf2.id
                FROM media_files mf2
                JOIN tracks t2 ON t2.id = mf2.track_id
                JOIN track_artists ta2 ON ta2.track_id = t2.id
                                       AND ta2.role = 'primary'
                WHERE ta2.artist_id = a.id
                LIMIT 1) AS media_file_id
        FROM artists a
        WHERE a.id::text = ANY(%(ids)s)
    """, {"ids": list(artist_ids)})
    by_id = {r["artist_id"]: r for r in rows}
    return [by_id[aid] for aid in artist_ids if aid in by_id]


def hydrate_albums(album_ids: list[str]) -> list[dict]:
    """Fetch album tiles by UUID. Output shape mirrors
    `/api/discovery/albums` results: album_id, album, artist, year,
    cover_id, media_file_id, track_count.
    """
    if not album_ids:
        return []
    rows = db_query("""
        SELECT al.id::text AS album_id,
               al.title AS album,
               al.release_year AS year,
               (SELECT a.name
                FROM artists a
                JOIN track_artists ta ON ta.artist_id = a.id
                                      AND ta.role = 'primary'
                JOIN tracks t ON t.id = ta.track_id
                JOIN media_files mf ON mf.track_id = t.id
                JOIN album_variants av ON av.id = mf.album_variant_id
                WHERE av.album_id = al.id
                GROUP BY a.id, a.name
                ORDER BY COUNT(*) DESC
                LIMIT 1) AS artist,
               (SELECT mf2.cover_id::text
                FROM media_files mf2
                JOIN album_variants av2 ON av2.id = mf2.album_variant_id
                WHERE av2.album_id = al.id AND mf2.cover_id IS NOT NULL
                LIMIT 1) AS cover_id,
               (SELECT mf3.id
                FROM media_files mf3
                JOIN album_variants av3 ON av3.id = mf3.album_variant_id
                WHERE av3.album_id = al.id
                ORDER BY mf3.disc_number, mf3.track_number
                LIMIT 1) AS media_file_id,
               (SELECT COUNT(*)::int
                FROM media_files mf4
                JOIN album_variants av4 ON av4.id = mf4.album_variant_id
                WHERE av4.album_id = al.id) AS track_count
        FROM albums al
        WHERE al.id::text = ANY(%(ids)s)
    """, {"ids": list(album_ids)})
    by_id = {r["album_id"]: r for r in rows}
    return [by_id[aid] for aid in album_ids if aid in by_id]


def hydrate_tracks(media_file_ids: list[int]) -> list[dict]:
    """Fetch track rows by media_file_id (integer PK). Output shape
    mirrors `/api/discovery/titles` results: id, title, artist,
    album, year, duration_seconds, cover_id, plus album_id /
    artist_id for in-app navigation.
    """
    if not media_file_ids:
        return []
    rows = db_query("""
        SELECT mf.id,
               t.title,
               a.id::text AS artist_id,
               a.name AS artist,
               al.id::text AS album_id,
               al.title AS album,
               al.release_year AS year,
               mf.duration_seconds::float AS duration_seconds,
               mf.cover_id::text AS cover_id
        FROM media_files mf
        JOIN tracks t ON t.id = mf.track_id
        JOIN track_artists ta ON ta.track_id = t.id
                              AND ta.role = 'primary'
        JOIN artists a ON a.id = ta.artist_id
        JOIN album_variants av ON av.id = mf.album_variant_id
        JOIN albums al ON al.id = av.album_id
        WHERE mf.id = ANY(%(ids)s)
    """, {"ids": list(media_file_ids)})
    by_id = {r["id"]: r for r in rows}
    return [by_id[mid] for mid in media_file_ids if mid in by_id]
