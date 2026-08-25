"""
Bulk hydration of entity tiles by ID.

Discovery's per-block endpoints couple search + ranking + cover
hydration into one SQL. Here we expose just the hydration step:
given a list of UUIDs / media_file_ids, return tile-shaped dicts
ready for the same `renderArtistRow` / `renderAlbumRow` /
`renderTrackList` frontend renderers Discovery uses.

Used by AI chat when the model produces a `[SAUTIUM_BLOCKS]` payload that
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
                LIMIT 1) AS media_file_id,
               EXISTS (SELECT 1 FROM media_files mf3
                       JOIN track_artists ta3 ON ta3.track_id = mf3.track_id
                       WHERE ta3.artist_id = a.id) AS is_owned
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
               al.cover_url,
               -- primary artist: owned credit via media_files, else (phantom) via album_tracks
               COALESCE(
                 (SELECT a.name FROM artists a
                  JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
                  JOIN tracks t ON t.id = ta.track_id
                  JOIN media_files mf ON mf.track_id = t.id
                  JOIN album_variants av ON av.id = mf.album_variant_id
                  WHERE av.album_id = al.id
                  GROUP BY a.id, a.name ORDER BY COUNT(*) DESC LIMIT 1),
                 (SELECT a.name FROM artists a
                  JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
                  JOIN album_tracks atr ON atr.track_id = ta.track_id
                  WHERE atr.album_id = al.id
                  GROUP BY a.id, a.name ORDER BY COUNT(*) DESC LIMIT 1)
               ) AS artist,
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
               -- track count: owned files, else (phantom) the album_tracks tracklist
               COALESCE(NULLIF(
                 (SELECT COUNT(*)::int FROM media_files mf4
                  JOIN album_variants av4 ON av4.id = mf4.album_variant_id
                  WHERE av4.album_id = al.id), 0),
                 (SELECT COUNT(*)::int FROM album_tracks atr WHERE atr.album_id = al.id)
               ) AS track_count,
               EXISTS (SELECT 1 FROM album_variants av5
                       WHERE av5.album_id = al.id) AS is_owned
        FROM albums al
        WHERE al.id::text = ANY(%(ids)s)
    """, {"ids": list(album_ids)})
    by_id = {r["album_id"]: r for r in rows}
    return [by_id[aid] for aid in album_ids if aid in by_id]


def hydrate_tracks(refs: list) -> list[dict]:
    """Fetch track tiles by canonical track UUID. Output shape mirrors the
    discovery track results: `track_id` (the identity), `id` (media_files.id,
    None for a track with no file), `is_owned`, plus album_id / artist_id for
    navigation and cover_url for the not-owned rows that have no cover_id.

    An integer ref (media_files.id) is accepted and resolved to its track — the
    model reaches that id through any media_files query, and rejecting it would
    drop a perfectly good tile.
    """
    if not refs:
        return []

    legacy = [int(r) for r in refs
              if isinstance(r, int) or (isinstance(r, str) and r.isdigit())]
    by_media = {}
    if legacy:
        by_media = {r["id"]: r["track_id"] for r in db_query(
            "SELECT id, track_id::text AS track_id FROM media_files WHERE id = ANY(%(ids)s)",
            {"ids": legacy})}

    wanted, seen = [], set()
    for r in refs:
        tid = by_media.get(int(r)) if (isinstance(r, int) or
                                       (isinstance(r, str) and r.isdigit())) else str(r)
        if tid and tid not in seen:
            seen.add(tid)
            wanted.append(tid)
    if not wanted:
        return []

    rows = db_query("""
        SELECT t.id::text AS track_id,
               own.id,
               (own.id IS NOT NULL) AS is_owned,
               t.title,
               ta.artist_id::text AS artist_id,
               a.name AS artist,
               COALESCE(own.album_id, ph.album_id)::text AS album_id,
               COALESCE(own.album, ph.album) AS album,
               COALESCE(own.release_year, ph.release_year) AS year,
               COALESCE(own.duration_seconds, ph.length_ms / 1000.0)::float
                   AS duration_seconds,
               own.cover_id::text AS cover_id,
               ph.cover_url
        FROM tracks t
        JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
        JOIN artists a ON a.id = ta.artist_id
        LEFT JOIN LATERAL (
            SELECT mf.id, mf.cover_id, mf.duration_seconds,
                   al.id AS album_id, al.title AS album, al.release_year
            FROM media_files mf
            JOIN album_variants av ON av.id = mf.album_variant_id
            JOIN albums al ON al.id = av.album_id
            WHERE mf.track_id = t.id
            ORDER BY mf.is_analysis_source DESC, mf.id
            LIMIT 1
        ) own ON true
        LEFT JOIN LATERAL (
            SELECT atr.length_ms, al.id AS album_id, al.title AS album,
                   al.release_year, al.cover_url
            FROM album_tracks atr
            JOIN albums al ON al.id = atr.album_id
            WHERE atr.track_id = t.id
            ORDER BY (al.cover_url IS NOT NULL) DESC, al.id
            LIMIT 1
        ) ph ON true
        WHERE t.id::text = ANY(%(ids)s)
    """, {"ids": wanted})
    by_id = {r["track_id"]: r for r in rows}
    return [by_id[tid] for tid in wanted if tid in by_id]
