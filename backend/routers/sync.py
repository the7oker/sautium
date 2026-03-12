"""
P2P Sync API endpoints.

Provides inventory and data pull endpoints for synchronizing
enrichment data between Music AI DJ nodes.

Protocol:
  1. POST /api/sync/inventory  — what enrichment data is available for given tracks?
  2. POST /api/sync/pull/{category} — retrieve enrichment data by UUIDs (batched)
"""

import json
import logging
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sync", tags=["sync"])

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_db_conn: Optional[psycopg2.extensions.connection] = None


def _get_db() -> psycopg2.extensions.connection:
    global _db_conn
    if _db_conn is None or _db_conn.closed:
        _db_conn = psycopg2.connect(settings.database_url)
        _db_conn.autocommit = True
    return _db_conn


def _db_query(sql: str, params=None) -> list[dict]:
    conn = _get_db()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def _uuid_list(rows: list[dict], column: str) -> list[str]:
    """Extract a column from query rows as a list of UUID strings."""
    return [str(row[column]) for row in rows]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class InventoryRequest(BaseModel):
    track_uuids: list[str]


class PullRequest(BaseModel):
    uuids: list[str]


# ---------------------------------------------------------------------------
# Inventory endpoint
# ---------------------------------------------------------------------------

_EMPTY_INVENTORY = {
    "tracks": [], "lyrics": [], "embeddings": [],
    "audio_features": [], "track_stats": [],
    "artists": [], "artist_bios": [],
    "artist_tags": [], "similar_artists": [],
    "artist_members": [],
    "albums": [], "album_info": [], "album_tags": [],
    "genres": [], "genre_descriptions": [],
}


@router.post("/inventory")
async def get_inventory(req: InventoryRequest) -> dict:
    """
    Check what enrichment data is available for the given track UUIDs.

    The requester sends track UUIDs from their library.
    The response contains categorized UUID lists indicating which
    enrichment data this node can provide.
    """
    if not req.track_uuids:
        return dict(_EMPTY_INVENTORY)

    uuids = req.track_uuids

    try:
        # -- Track-level data --
        tracks = _uuid_list(
            _db_query("SELECT id FROM tracks WHERE id = ANY(%s::uuid[])", [uuids]),
            "id",
        )
        embeddings = _uuid_list(
            _db_query(
                "SELECT DISTINCT track_id FROM embeddings WHERE track_id = ANY(%s::uuid[])",
                [uuids],
            ),
            "track_id",
        )
        audio_features = _uuid_list(
            _db_query(
                "SELECT DISTINCT track_id FROM audio_features WHERE track_id = ANY(%s::uuid[])",
                [uuids],
            ),
            "track_id",
        )
        lyrics = _uuid_list(
            _db_query(
                "SELECT DISTINCT track_id FROM track_lyrics WHERE track_id = ANY(%s::uuid[])",
                [uuids],
            ),
            "track_id",
        )
        track_stats = _uuid_list(
            _db_query(
                "SELECT DISTINCT track_id FROM track_stats WHERE track_id = ANY(%s::uuid[])",
                [uuids],
            ),
            "track_id",
        )

        # -- Related artists (via track_artists) --
        artists = _uuid_list(
            _db_query(
                "SELECT DISTINCT artist_id FROM track_artists WHERE track_id = ANY(%s::uuid[])",
                [uuids],
            ),
            "artist_id",
        )
        artist_bios = _uuid_list(
            _db_query(
                """SELECT DISTINCT ab.artist_id FROM artist_bios ab
                   INNER JOIN track_artists ta ON ta.artist_id = ab.artist_id
                   WHERE ta.track_id = ANY(%s::uuid[])""",
                [uuids],
            ),
            "artist_id",
        )
        artist_tags = _uuid_list(
            _db_query(
                """SELECT DISTINCT at2.artist_id FROM artist_tags at2
                   INNER JOIN track_artists ta ON ta.artist_id = at2.artist_id
                   WHERE ta.track_id = ANY(%s::uuid[])""",
                [uuids],
            ),
            "artist_id",
        )
        similar_artists = _uuid_list(
            _db_query(
                """SELECT DISTINCT sa.artist_id FROM similar_artists sa
                   INNER JOIN track_artists ta ON ta.artist_id = sa.artist_id
                   WHERE ta.track_id = ANY(%s::uuid[])""",
                [uuids],
            ),
            "artist_id",
        )

        # -- Artist members (compound → member) --
        artist_members = _uuid_list(
            _db_query(
                """SELECT DISTINCT am.compound_artist_id AS artist_id
                   FROM artist_members am
                   INNER JOIN track_artists ta ON ta.artist_id = am.compound_artist_id
                   WHERE ta.track_id = ANY(%s::uuid[])""",
                [uuids],
            ),
            "artist_id",
        )

        # -- Related albums (track → media_files → album_variants → album) --
        albums = _uuid_list(
            _db_query(
                """SELECT DISTINCT av.album_id FROM album_variants av
                   INNER JOIN media_files mf ON mf.album_variant_id = av.id
                   WHERE mf.track_id = ANY(%s::uuid[])""",
                [uuids],
            ),
            "album_id",
        )
        album_info = _uuid_list(
            _db_query(
                """SELECT DISTINCT ai.album_id FROM album_info ai
                   INNER JOIN album_variants av ON av.album_id = ai.album_id
                   INNER JOIN media_files mf ON mf.album_variant_id = av.id
                   WHERE mf.track_id = ANY(%s::uuid[])""",
                [uuids],
            ),
            "album_id",
        )
        album_tags = _uuid_list(
            _db_query(
                """SELECT DISTINCT at2.album_id FROM album_tags at2
                   INNER JOIN album_variants av ON av.album_id = at2.album_id
                   INNER JOIN media_files mf ON mf.album_variant_id = av.id
                   WHERE mf.track_id = ANY(%s::uuid[])""",
                [uuids],
            ),
            "album_id",
        )

        # -- Related genres (via track_genres) --
        genres = _uuid_list(
            _db_query(
                "SELECT DISTINCT genre_id FROM track_genres WHERE track_id = ANY(%s::uuid[])",
                [uuids],
            ),
            "genre_id",
        )
        genre_descriptions = _uuid_list(
            _db_query(
                """SELECT DISTINCT gd.genre_id FROM genre_descriptions gd
                   INNER JOIN track_genres tg ON tg.genre_id = gd.genre_id
                   WHERE tg.track_id = ANY(%s::uuid[])""",
                [uuids],
            ),
            "genre_id",
        )

        return {
            "tracks": tracks,
            "lyrics": lyrics,
            "embeddings": embeddings,
            "audio_features": audio_features,
            "track_stats": track_stats,
            "artists": artists,
            "artist_bios": artist_bios,
            "artist_tags": artist_tags,
            "similar_artists": similar_artists,
            "artist_members": artist_members,
            "albums": albums,
            "album_info": album_info,
            "album_tags": album_tags,
            "genres": genres,
            "genre_descriptions": genre_descriptions,
        }

    except Exception as e:
        logger.error(f"Inventory query failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Helpers for pull endpoints
# ---------------------------------------------------------------------------

def _serialize_row(row: dict) -> dict:
    """Convert non-JSON-serializable types in a row dict."""
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif hasattr(v, "__str__") and not isinstance(v, (str, int, float, bool, list, dict, type(None))):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def _parse_vector(vec_text: str) -> list[float]:
    """Parse pgvector text representation '[0.1,0.2,...]' to list of floats."""
    return json.loads(vec_text)


def _pull_handler(category: str, sql: str, uuids: list[str], post_process=None) -> dict:
    """Common handler for pull endpoints."""
    if not uuids:
        return {"category": category, "items": []}
    try:
        rows = _db_query(sql, [uuids])
        items = [_serialize_row(r) for r in rows]
        if post_process:
            items = [post_process(item) for item in items]
        return {"category": category, "items": items}
    except Exception as e:
        logger.error(f"Pull {category} failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Pull endpoints
# ---------------------------------------------------------------------------

@router.post("/pull/tracks")
async def pull_tracks(req: PullRequest) -> dict:
    """Pull track metadata with associated artists and genres."""
    if not req.uuids:
        return {"category": "tracks", "items": []}

    try:
        # Get tracks
        tracks = _db_query(
            "SELECT id::text AS track_uuid, title FROM tracks WHERE id = ANY(%s::uuid[])",
            [req.uuids],
        )

        # Get artists for these tracks
        artists = _db_query(
            """SELECT ta.track_id::text, ta.role,
                      a.id::text AS artist_uuid, a.name AS artist_name
               FROM track_artists ta
               INNER JOIN artists a ON a.id = ta.artist_id
               WHERE ta.track_id = ANY(%s::uuid[])
               ORDER BY ta.track_id, ta.role""",
            [req.uuids],
        )

        # Get genres for these tracks
        genres = _db_query(
            """SELECT tg.track_id::text, g.id::text AS genre_uuid, g.name AS genre_name
               FROM track_genres tg
               INNER JOIN genres g ON g.id = tg.genre_id
               WHERE tg.track_id = ANY(%s::uuid[])""",
            [req.uuids],
        )

        # Group artists and genres by track
        artists_by_track: dict[str, list] = {}
        for a in artists:
            tid = str(a["track_id"])
            artists_by_track.setdefault(tid, []).append({
                "artist_uuid": a["artist_uuid"],
                "name": a["artist_name"],
                "role": a["role"],
            })

        genres_by_track: dict[str, list] = {}
        for g in genres:
            tid = str(g["track_id"])
            genres_by_track.setdefault(tid, []).append({
                "genre_uuid": g["genre_uuid"],
                "name": g["genre_name"],
            })

        items = []
        for t in tracks:
            uuid = t["track_uuid"]
            items.append({
                "track_uuid": uuid,
                "title": t["title"],
                "artists": artists_by_track.get(uuid, []),
                "genres": genres_by_track.get(uuid, []),
            })

        return {"category": "tracks", "items": items}

    except Exception as e:
        logger.error(f"Pull tracks failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/pull/lyrics")
async def pull_lyrics(req: PullRequest) -> dict:
    """Pull track lyrics."""
    return _pull_handler(
        "lyrics",
        """SELECT track_id::text AS track_uuid, source,
                  plain_lyrics, synced_lyrics, instrumental
           FROM track_lyrics
           WHERE track_id = ANY(%s::uuid[])""",
        req.uuids,
    )


@router.post("/pull/embeddings")
async def pull_embeddings(req: PullRequest) -> dict:
    """Pull audio embeddings (CLAP 512d vectors)."""
    if not req.uuids:
        return {"category": "embeddings", "items": []}

    try:
        rows = _db_query(
            """SELECT e.track_id::text AS track_uuid,
                      em.id::text AS model_uuid, em.name AS model_name,
                      e.vector::text AS vector,
                      e.source_bit_depth, e.source_sample_rate, e.source_is_lossless
               FROM embeddings e
               INNER JOIN embedding_models em ON em.id = e.model_id
               WHERE e.track_id = ANY(%s::uuid[])""",
            [req.uuids],
        )

        items = []
        for r in rows:
            items.append({
                "track_uuid": r["track_uuid"],
                "model_uuid": r["model_uuid"],
                "model_name": r["model_name"],
                "vector": _parse_vector(r["vector"]),
                "source_bit_depth": r["source_bit_depth"],
                "source_sample_rate": r["source_sample_rate"],
                "source_is_lossless": r["source_is_lossless"],
            })

        return {"category": "embeddings", "items": items}

    except Exception as e:
        logger.error(f"Pull embeddings failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/pull/audio-features")
async def pull_audio_features(req: PullRequest) -> dict:
    """Pull audio analysis features."""
    return _pull_handler(
        "audio_features",
        """SELECT track_id::text AS track_uuid,
                  bpm, key, mode, key_confidence,
                  energy, energy_db, brightness, dynamic_range_db,
                  zero_crossing_rate, instruments, moods,
                  vocal_instrumental, vocal_score, danceability,
                  source_bit_depth, source_sample_rate, source_is_lossless
           FROM audio_features
           WHERE track_id = ANY(%s::uuid[])""",
        req.uuids,
    )


@router.post("/pull/track-stats")
async def pull_track_stats(req: PullRequest) -> dict:
    """Pull track statistics (listeners, playcount)."""
    return _pull_handler(
        "track_stats",
        """SELECT track_id::text AS track_uuid, source, listeners, playcount
           FROM track_stats
           WHERE track_id = ANY(%s::uuid[])""",
        req.uuids,
    )


@router.post("/pull/artist-bios")
async def pull_artist_bios(req: PullRequest) -> dict:
    """Pull artist biographies."""
    return _pull_handler(
        "artist_bios",
        """SELECT ab.artist_id::text AS artist_uuid, a.name AS artist_name,
                  ab.source, ab.summary, ab.content, ab.url,
                  ab.listeners, ab.playcount
           FROM artist_bios ab
           INNER JOIN artists a ON a.id = ab.artist_id
           WHERE ab.artist_id = ANY(%s::uuid[])""",
        req.uuids,
    )


@router.post("/pull/artist-tags")
async def pull_artist_tags(req: PullRequest) -> dict:
    """Pull artist tags with tag names and weights."""
    return _pull_handler(
        "artist_tags",
        """SELECT at2.artist_id::text AS artist_uuid,
                  t.id::text AS tag_uuid, t.name AS tag_name,
                  at2.weight, at2.source
           FROM artist_tags at2
           INNER JOIN tags t ON t.id = at2.tag_id
           WHERE at2.artist_id = ANY(%s::uuid[])""",
        req.uuids,
    )


@router.post("/pull/similar-artists")
async def pull_similar_artists(req: PullRequest) -> dict:
    """Pull similar artist relationships."""
    return _pull_handler(
        "similar_artists",
        """SELECT sa.artist_id::text AS artist_uuid,
                  sa.similar_artist_id::text AS similar_artist_uuid,
                  a.name AS similar_artist_name,
                  sa.match_score::float, sa.source
           FROM similar_artists sa
           INNER JOIN artists a ON a.id = sa.similar_artist_id
           WHERE sa.artist_id = ANY(%s::uuid[])""",
        req.uuids,
    )


@router.post("/pull/artist-members")
async def pull_artist_members(req: PullRequest) -> dict:
    """Pull artist member relationships (compound → individual artists)."""
    return _pull_handler(
        "artist_members",
        """SELECT am.compound_artist_id::text AS compound_artist_uuid,
                  c.name AS compound_artist_name,
                  c.artist_type, c.verification_status,
                  am.member_artist_id::text AS member_artist_uuid,
                  m.name AS member_artist_name,
                  am.role
           FROM artist_members am
           INNER JOIN artists c ON c.id = am.compound_artist_id
           INNER JOIN artists m ON m.id = am.member_artist_id
           WHERE am.compound_artist_id = ANY(%s::uuid[])""",
        req.uuids,
    )


@router.post("/pull/album-info")
async def pull_album_info(req: PullRequest) -> dict:
    """Pull album information."""
    return _pull_handler(
        "album_info",
        """SELECT ai.album_id::text AS album_uuid, al.title AS album_title,
                  ai.source, ai.summary, ai.content, ai.url,
                  ai.listeners, ai.playcount
           FROM album_info ai
           INNER JOIN albums al ON al.id = ai.album_id
           WHERE ai.album_id = ANY(%s::uuid[])""",
        req.uuids,
    )


@router.post("/pull/album-tags")
async def pull_album_tags(req: PullRequest) -> dict:
    """Pull album tags with tag names and weights."""
    return _pull_handler(
        "album_tags",
        """SELECT at2.album_id::text AS album_uuid, al.title AS album_title,
                  t.id::text AS tag_uuid, t.name AS tag_name,
                  at2.weight, at2.source
           FROM album_tags at2
           INNER JOIN tags t ON t.id = at2.tag_id
           INNER JOIN albums al ON al.id = at2.album_id
           WHERE at2.album_id = ANY(%s::uuid[])""",
        req.uuids,
    )


@router.post("/pull/genre-descriptions")
async def pull_genre_descriptions(req: PullRequest) -> dict:
    """Pull genre descriptions."""
    return _pull_handler(
        "genre_descriptions",
        """SELECT gd.genre_id::text AS genre_uuid, g.name AS genre_name,
                  gd.source, gd.summary, gd.content, gd.url
           FROM genre_descriptions gd
           INNER JOIN genres g ON g.id = gd.genre_id
           WHERE gd.genre_id = ANY(%s::uuid[])""",
        req.uuids,
    )
