"""Release-group page endpoint.

A logical album (a MusicBrainz release group) is spread across several `albums`
rows — one per *edition* (a distinct tracklist: Standard / Deluxe / box set /
orchestral re-recording). This serves the intermediate screen that lists those
editions when there is more than one: a cover, the group name + artist, and a
horizontal shelf of editions, each labelled with the part of its title that
distinguishes it and its track count. Grouping + name simplification live in
``backend/release_groups.py``; the group id is the primary edition's album id.
"""

from fastapi import APIRouter, HTTPException

from db_pool import db_query, db_query_one
from release_groups import release_group_cluster, simplify_edition_name

router = APIRouter(prefix="/api/release-groups", tags=["release-groups"])


def _quality(row: dict) -> str:
    if row["lossless"] and (row["sr"] or 0) >= 48000 and (row["bd"] or 0) >= 24:
        return "hi-res"
    return "lossless" if row["lossless"] else "lossy"


@router.get("/{group_id}")
def get_release_group(group_id: str) -> dict:
    ids = release_group_cluster(group_id)
    if not ids:
        raise HTTPException(status_code=404, detail="release group not found")

    # One pass over the cluster's edition rows — quality / track-count / cover
    # subqueries mirror routers/albums.py so an edition reads like a mini album.
    rows = db_query("""
        SELECT al.id::text AS album_id, al.title, al.release_year AS year,
               al.cover_url, al.musicbrainz_id::text AS rg_mbid,
               (SELECT av.edition FROM album_variants av
                WHERE av.album_id = al.id AND av.edition IS NOT NULL LIMIT 1) AS stored_edition,
               (SELECT av.release_mbid::text FROM album_variants av
                WHERE av.album_id = al.id AND av.release_mbid IS NOT NULL LIMIT 1) AS release_mbid,
               (SELECT count(DISTINCT mf.track_id)
                FROM album_variants av JOIN media_files mf ON mf.album_variant_id = av.id
                WHERE av.album_id = al.id) AS track_count,
               (SELECT mf.cover_id::text
                FROM media_files mf JOIN album_variants av ON av.id = mf.album_variant_id
                WHERE av.album_id = al.id AND mf.cover_id IS NOT NULL LIMIT 1) AS cover_id,
               (SELECT mf.id
                FROM media_files mf JOIN album_variants av ON av.id = mf.album_variant_id
                WHERE av.album_id = al.id
                ORDER BY mf.disc_number, mf.track_number LIMIT 1) AS media_file_id,
               (SELECT bool_or(av.is_lossless) FROM album_variants av WHERE av.album_id = al.id) AS lossless,
               (SELECT max(av.sample_rate) FROM album_variants av WHERE av.album_id = al.id) AS sr,
               (SELECT max(av.bit_depth) FROM album_variants av WHERE av.album_id = al.id) AS bd
        FROM albums al
        WHERE al.id = ANY(%(ids)s::uuid[])
    """, {"ids": ids})

    by_id = {r["album_id"]: r for r in rows}
    primary = by_id.get(ids[0]) or rows[0]    # cluster is primary-first

    rg_name = None
    if primary["rg_mbid"]:
        nm = db_query_one("SELECT name FROM mb_release_group WHERE gid = %(g)s::uuid",
                          {"g": primary["rg_mbid"]})
        rg_name = nm["name"] if nm else None
    rg_name = rg_name or primary["title"]

    artist = db_query_one("""
        SELECT a.id::text AS id, a.name
        FROM artists a
        JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
        JOIN media_files mf ON mf.track_id = ta.track_id
        JOIN album_variants av ON av.id = mf.album_variant_id
        WHERE av.album_id = %(id)s::uuid
        GROUP BY a.id, a.name
        ORDER BY COUNT(*) DESC LIMIT 1
    """, {"id": primary["album_id"]})

    editions = []
    for r in rows:
        # stored qualifier (canon-stamped) first; fall back to deriving it from the
        # title — so the page is correct even before the edition migration runs.
        name = r["stored_edition"] or simplify_edition_name(r["title"], rg_name) or rg_name
        editions.append({
            "album_id": r["album_id"],
            "edition_name": name,
            "track_count": int(r["track_count"] or 0),
            "year": r["year"],
            "quality": _quality(r),
            "cover_id": r["cover_id"],
            "media_file_id": r["media_file_id"],
            "cover_url": r["cover_url"],
            "release_mbid": r["release_mbid"],
        })
    editions.sort(key=lambda e: (0 if e["album_id"] == primary["album_id"] else 1,
                                 -e["track_count"], e["year"] or 9999))

    return {
        "id": primary["album_id"],
        "name": rg_name,
        "artist": artist,
        "cover": {"cover_id": primary["cover_id"],
                  "media_file_id": primary["media_file_id"],
                  "cover_url": primary["cover_url"]},
        "editions": editions,
    }
