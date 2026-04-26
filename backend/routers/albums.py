"""
Album detail endpoint.

Aggregates album-level metadata (title, year, primary artist, format
quality), top genre chips, and the full tracklist with per-track
audio features (key, mode, BPM) into a single roundtrip.
"""

from fastapi import APIRouter, HTTPException

from db_pool import db_query, db_query_one


router = APIRouter(prefix="/api/albums", tags=["albums"])


@router.get("/{album_id}")
def get_album(album_id: str) -> dict:
    album = db_query_one("""
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
        WHERE al.id = %(id)s::uuid
    """, {"id": album_id})

    if not album:
        raise HTTPException(status_code=404, detail="album not found")

    # Most-frequent primary artist for this album
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

    # Quality + total duration
    qrow = db_query_one("""
        SELECT BOOL_OR(mf.is_lossless) AS lossless,
               MAX(mf.sample_rate) AS sr_max,
               MAX(mf.bit_depth)   AS bd_max,
               SUM(mf.duration_seconds) AS total_duration
        FROM media_files mf
        JOIN album_variants av ON av.id = mf.album_variant_id
        WHERE av.album_id = %(id)s::uuid
          AND mf.is_analysis_source = true
    """, {"id": album_id})
    sr = qrow["sr_max"] or 0
    bd = qrow["bd_max"] or 0
    if qrow["lossless"] and sr >= 48000 and bd >= 24:
        album["quality"] = "hi-res"
    elif qrow["lossless"]:
        album["quality"] = "lossless"
    else:
        album["quality"] = "lossy"
    album["total_duration"] = float(qrow["total_duration"] or 0)

    # Top 3 genres by occurrence across the album's tracks
    album["genres"] = db_query("""
        SELECT g.id::text AS id, g.name, COUNT(*) AS occurrences
        FROM albums al
        JOIN album_variants av ON av.album_id = al.id
        JOIN media_files mf ON mf.album_variant_id = av.id
        JOIN track_genres tg ON tg.track_id = mf.track_id
        JOIN genres g ON g.id = tg.genre_id
        WHERE al.id = %(id)s::uuid
        GROUP BY g.id, g.name
        ORDER BY occurrences DESC, g.name
        LIMIT 3
    """, {"id": album_id})

    # Tracklist ordered by disc / track number
    album["tracks"] = db_query("""
        SELECT t.id::text AS track_id,
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
          AND mf.is_analysis_source = true
        ORDER BY mf.disc_number NULLS FIRST,
                 mf.track_number NULLS FIRST,
                 t.title
    """, {"id": album_id})

    return album
