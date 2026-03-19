"""
Shared sync SQL queries for Sautium.

Framework-agnostic module: takes a psycopg2 connection, returns dicts.
Used by both the aiohttp P2P sync server and the FastAPI backend.
"""

import json
import logging
from datetime import datetime

import psycopg2.extras

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def db_query(conn, sql: str, params=None) -> list[dict]:
    """Execute SQL and return list of dicts using RealDictCursor."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def _uuid_list(rows: list[dict], column: str) -> list[str]:
    """Extract a column from query rows as a list of UUID strings."""
    return [str(row[column]) for row in rows]


def _serialize_row(row: dict) -> dict:
    """Convert non-JSON-serializable types in a row dict."""
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif hasattr(v, "__str__") and not isinstance(
            v, (str, int, float, bool, list, dict, type(None))
        ):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def _parse_vector(vec_text: str) -> list[float]:
    """Parse pgvector text representation '[0.1,0.2,...]' to list of floats."""
    return json.loads(vec_text)


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

EMPTY_INVENTORY = {
    "tracks": [], "lyrics": [], "embeddings": [],
    "audio_features": [], "track_stats": [],
    "artists": [], "artist_bios": [],
    "artist_tags": [], "similar_artists": [],
    "artist_members": [],
    "albums": [], "album_info": [], "album_tags": [],
    "genres": [], "genre_descriptions": [],
}


def get_inventory(conn, track_uuids: list[str]) -> dict:
    """
    Check what enrichment data is available for the given track UUIDs.

    Returns category -> uuid_list dict.
    """
    if not track_uuids:
        return dict(EMPTY_INVENTORY)

    uuids = track_uuids
    q = lambda sql: db_query(conn, sql, [uuids])

    # -- Track-level data --
    tracks = _uuid_list(
        q("SELECT id FROM tracks WHERE id = ANY(%s::uuid[])"), "id"
    )
    embeddings = _uuid_list(
        q("SELECT DISTINCT track_id FROM embeddings WHERE track_id = ANY(%s::uuid[])"),
        "track_id",
    )
    audio_features = _uuid_list(
        q("SELECT DISTINCT track_id FROM audio_features WHERE track_id = ANY(%s::uuid[])"),
        "track_id",
    )
    lyrics = _uuid_list(
        q("SELECT DISTINCT track_id FROM track_lyrics WHERE track_id = ANY(%s::uuid[])"),
        "track_id",
    )
    track_stats = _uuid_list(
        q("SELECT DISTINCT track_id FROM track_stats WHERE track_id = ANY(%s::uuid[])"),
        "track_id",
    )

    # -- Related artists (via track_artists) --
    artists = _uuid_list(
        q("SELECT DISTINCT artist_id FROM track_artists WHERE track_id = ANY(%s::uuid[])"),
        "artist_id",
    )
    artist_bios = _uuid_list(
        q("""SELECT DISTINCT ab.artist_id FROM artist_bios ab
             INNER JOIN track_artists ta ON ta.artist_id = ab.artist_id
             WHERE ta.track_id = ANY(%s::uuid[])"""),
        "artist_id",
    )
    artist_tags = _uuid_list(
        q("""SELECT DISTINCT at2.artist_id FROM artist_tags at2
             INNER JOIN track_artists ta ON ta.artist_id = at2.artist_id
             WHERE ta.track_id = ANY(%s::uuid[])"""),
        "artist_id",
    )
    similar_artists = _uuid_list(
        q("""SELECT DISTINCT sa.artist_id FROM similar_artists sa
             INNER JOIN track_artists ta ON ta.artist_id = sa.artist_id
             WHERE ta.track_id = ANY(%s::uuid[])"""),
        "artist_id",
    )
    artist_members = _uuid_list(
        q("""SELECT DISTINCT am.compound_artist_id AS artist_id
             FROM artist_members am
             INNER JOIN track_artists ta ON ta.artist_id = am.compound_artist_id
             WHERE ta.track_id = ANY(%s::uuid[])"""),
        "artist_id",
    )

    # -- Related albums (track -> media_files -> album_variants -> album) --
    albums = _uuid_list(
        q("""SELECT DISTINCT av.album_id FROM album_variants av
             INNER JOIN media_files mf ON mf.album_variant_id = av.id
             WHERE mf.track_id = ANY(%s::uuid[])"""),
        "album_id",
    )
    album_info = _uuid_list(
        q("""SELECT DISTINCT ai.album_id FROM album_info ai
             INNER JOIN album_variants av ON av.album_id = ai.album_id
             INNER JOIN media_files mf ON mf.album_variant_id = av.id
             WHERE mf.track_id = ANY(%s::uuid[])"""),
        "album_id",
    )
    album_tags = _uuid_list(
        q("""SELECT DISTINCT at2.album_id FROM album_tags at2
             INNER JOIN album_variants av ON av.album_id = at2.album_id
             INNER JOIN media_files mf ON mf.album_variant_id = av.id
             WHERE mf.track_id = ANY(%s::uuid[])"""),
        "album_id",
    )

    # -- Related genres (via track_genres) --
    genres = _uuid_list(
        q("SELECT DISTINCT genre_id FROM track_genres WHERE track_id = ANY(%s::uuid[])"),
        "genre_id",
    )
    genre_descriptions = _uuid_list(
        q("""SELECT DISTINCT gd.genre_id FROM genre_descriptions gd
             INNER JOIN track_genres tg ON tg.genre_id = gd.genre_id
             WHERE tg.track_id = ANY(%s::uuid[])"""),
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


# ---------------------------------------------------------------------------
# Pull handlers
# ---------------------------------------------------------------------------

def _pull_simple(conn, category: str, sql: str, uuids: list[str],
                 post_process=None) -> dict:
    """Common handler for simple pull categories."""
    if not uuids:
        return {"category": category, "items": []}
    rows = db_query(conn, sql, [uuids])
    items = [_serialize_row(r) for r in rows]
    if post_process:
        items = [post_process(item) for item in items]
    return {"category": category, "items": items}


def pull_tracks(conn, uuids: list[str]) -> dict:
    """Pull track metadata with associated artists and genres."""
    if not uuids:
        return {"category": "tracks", "items": []}

    tracks = db_query(
        conn,
        "SELECT id::text AS track_uuid, title FROM tracks WHERE id = ANY(%s::uuid[])",
        [uuids],
    )
    artists = db_query(
        conn,
        """SELECT ta.track_id::text, ta.role,
                  a.id::text AS artist_uuid, a.name AS artist_name
           FROM track_artists ta
           INNER JOIN artists a ON a.id = ta.artist_id
           WHERE ta.track_id = ANY(%s::uuid[])
           ORDER BY ta.track_id, ta.role""",
        [uuids],
    )
    genres = db_query(
        conn,
        """SELECT tg.track_id::text, g.id::text AS genre_uuid, g.name AS genre_name
           FROM track_genres tg
           INNER JOIN genres g ON g.id = tg.genre_id
           WHERE tg.track_id = ANY(%s::uuid[])""",
        [uuids],
    )

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


def pull_lyrics(conn, uuids: list[str]) -> dict:
    return _pull_simple(
        conn, "lyrics",
        """SELECT track_id::text AS track_uuid, source,
                  plain_lyrics, synced_lyrics, instrumental
           FROM track_lyrics WHERE track_id = ANY(%s::uuid[])""",
        uuids,
    )


def pull_embeddings(conn, uuids: list[str]) -> dict:
    if not uuids:
        return {"category": "embeddings", "items": []}

    rows = db_query(
        conn,
        """SELECT e.track_id::text AS track_uuid,
                  em.id::text AS model_uuid, em.name AS model_name,
                  e.vector::text AS vector,
                  e.source_bit_depth, e.source_sample_rate, e.source_is_lossless
           FROM embeddings e
           INNER JOIN embedding_models em ON em.id = e.model_id
           WHERE e.track_id = ANY(%s::uuid[])""",
        [uuids],
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


def pull_audio_features(conn, uuids: list[str]) -> dict:
    return _pull_simple(
        conn, "audio_features",
        """SELECT track_id::text AS track_uuid,
                  bpm, key, mode, key_confidence,
                  energy, energy_db, brightness, dynamic_range_db,
                  zero_crossing_rate, instruments, moods,
                  vocal_instrumental, vocal_score, danceability,
                  source_bit_depth, source_sample_rate, source_is_lossless
           FROM audio_features WHERE track_id = ANY(%s::uuid[])""",
        uuids,
    )


def pull_track_stats(conn, uuids: list[str]) -> dict:
    return _pull_simple(
        conn, "track_stats",
        """SELECT track_id::text AS track_uuid, source, listeners, playcount
           FROM track_stats WHERE track_id = ANY(%s::uuid[])""",
        uuids,
    )


def pull_artist_bios(conn, uuids: list[str]) -> dict:
    return _pull_simple(
        conn, "artist_bios",
        """SELECT ab.artist_id::text AS artist_uuid, a.name AS artist_name,
                  ab.source, ab.summary, ab.content, ab.url,
                  ab.listeners, ab.playcount
           FROM artist_bios ab
           INNER JOIN artists a ON a.id = ab.artist_id
           WHERE ab.artist_id = ANY(%s::uuid[])""",
        uuids,
    )


def pull_artist_tags(conn, uuids: list[str]) -> dict:
    return _pull_simple(
        conn, "artist_tags",
        """SELECT at2.artist_id::text AS artist_uuid,
                  t.id::text AS tag_uuid, t.name AS tag_name,
                  at2.weight, at2.source
           FROM artist_tags at2
           INNER JOIN tags t ON t.id = at2.tag_id
           WHERE at2.artist_id = ANY(%s::uuid[])""",
        uuids,
    )


def pull_similar_artists(conn, uuids: list[str]) -> dict:
    return _pull_simple(
        conn, "similar_artists",
        """SELECT sa.artist_id::text AS artist_uuid,
                  sa.similar_artist_id::text AS similar_artist_uuid,
                  a.name AS similar_artist_name,
                  sa.match_score::float, sa.source
           FROM similar_artists sa
           INNER JOIN artists a ON a.id = sa.similar_artist_id
           WHERE sa.artist_id = ANY(%s::uuid[])""",
        uuids,
    )


def pull_artist_members(conn, uuids: list[str]) -> dict:
    return _pull_simple(
        conn, "artist_members",
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
        uuids,
    )


def pull_album_info(conn, uuids: list[str]) -> dict:
    return _pull_simple(
        conn, "album_info",
        """SELECT ai.album_id::text AS album_uuid, al.title AS album_title,
                  ai.source, ai.summary, ai.content, ai.url,
                  ai.listeners, ai.playcount
           FROM album_info ai
           INNER JOIN albums al ON al.id = ai.album_id
           WHERE ai.album_id = ANY(%s::uuid[])""",
        uuids,
    )


def pull_album_tags(conn, uuids: list[str]) -> dict:
    return _pull_simple(
        conn, "album_tags",
        """SELECT at2.album_id::text AS album_uuid, al.title AS album_title,
                  t.id::text AS tag_uuid, t.name AS tag_name,
                  at2.weight, at2.source
           FROM album_tags at2
           INNER JOIN tags t ON t.id = at2.tag_id
           INNER JOIN albums al ON al.id = at2.album_id
           WHERE at2.album_id = ANY(%s::uuid[])""",
        uuids,
    )


def pull_genre_descriptions(conn, uuids: list[str]) -> dict:
    return _pull_simple(
        conn, "genre_descriptions",
        """SELECT gd.genre_id::text AS genre_uuid, g.name AS genre_name,
                  gd.source, gd.summary, gd.content, gd.url
           FROM genre_descriptions gd
           INNER JOIN genres g ON g.id = gd.genre_id
           WHERE gd.genre_id = ANY(%s::uuid[])""",
        uuids,
    )


# Map category name -> pull function
PULL_HANDLERS = {
    "tracks": pull_tracks,
    "lyrics": pull_lyrics,
    "embeddings": pull_embeddings,
    "audio-features": pull_audio_features,
    "audio_features": pull_audio_features,
    "track-stats": pull_track_stats,
    "track_stats": pull_track_stats,
    "artist-bios": pull_artist_bios,
    "artist_bios": pull_artist_bios,
    "artist-tags": pull_artist_tags,
    "artist_tags": pull_artist_tags,
    "similar-artists": pull_similar_artists,
    "similar_artists": pull_similar_artists,
    "artist-members": pull_artist_members,
    "artist_members": pull_artist_members,
    "album-info": pull_album_info,
    "album_info": pull_album_info,
    "album-tags": pull_album_tags,
    "album_tags": pull_album_tags,
    "genre-descriptions": pull_genre_descriptions,
    "genre_descriptions": pull_genre_descriptions,
}


# ---------------------------------------------------------------------------
# DHT-related queries
# ---------------------------------------------------------------------------

def get_enriched_artist_uuids(conn) -> list[str]:
    """
    Return artist UUIDs that have at least 1 track with embedding or audio_features.
    These are the artists worth announcing in DHT.
    """
    rows = db_query(
        conn,
        """SELECT DISTINCT ta.artist_id::text AS artist_uuid
           FROM track_artists ta
           WHERE EXISTS (SELECT 1 FROM embeddings e WHERE e.track_id = ta.track_id)
              OR EXISTS (SELECT 1 FROM audio_features af WHERE af.track_id = ta.track_id)""",
    )
    return [r["artist_uuid"] for r in rows]


def get_track_uuids_for_artist(conn, artist_uuid: str) -> list[str]:
    """Return track UUIDs linked to a specific artist."""
    rows = db_query(
        conn,
        "SELECT track_id::text FROM track_artists WHERE artist_id = %s::uuid",
        [artist_uuid],
    )
    return [r["track_id"] for r in rows]


def get_unenriched_artist_uuids(conn) -> list[str]:
    """
    Return artist UUIDs that have tracks but NO enrichment data
    (no embeddings and no audio_features for any of their tracks).
    These are the artists we should look for in DHT.
    """
    rows = db_query(
        conn,
        """SELECT DISTINCT ta.artist_id::text AS artist_uuid
           FROM track_artists ta
           WHERE NOT EXISTS (
               SELECT 1 FROM embeddings e WHERE e.track_id = ta.track_id
           )
           AND NOT EXISTS (
               SELECT 1 FROM audio_features af WHERE af.track_id = ta.track_id
           )""",
    )
    return [r["artist_uuid"] for r in rows]
