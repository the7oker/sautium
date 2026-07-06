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
    "genres": [], "genre_descriptions": [],
}


def get_inventory(conn, track_uuids: list[str]) -> dict:
    """
    Check what enrichment data is available for the given track UUIDs.

    Returns category -> uuid_list dict. The versioned categories
    (embeddings, audio_features) return [uuid, analysis_version] pairs
    instead, so peers can re-pull rows produced by an older analysis
    methodology — existence alone never delivers upgrades.
    """
    if not track_uuids:
        return dict(EMPTY_INVENTORY)

    uuids = track_uuids
    q = lambda sql: db_query(conn, sql, [uuids])

    # -- Track-level data --
    tracks = _uuid_list(
        q("SELECT id FROM tracks WHERE id = ANY(%s::uuid[])"), "id"
    )
    embeddings = [
        [r["track_id"], r["v"]] for r in
        q("""SELECT track_id::text AS track_id, MAX(analysis_version) AS v
             FROM embeddings WHERE track_id = ANY(%s::uuid[])
             GROUP BY track_id""")
    ]
    audio_features = [
        [r["track_id"], r["analysis_version"]] for r in
        q("""SELECT track_id::text AS track_id, analysis_version
             FROM audio_features WHERE track_id = ANY(%s::uuid[])""")
    ]
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

    # -- Related genres (album-grain: genres of the albums containing these tracks).
    #    album_genres is local-only (albums don't sync), so we share only the genre
    #    entities + their descriptions, derived via the sender's own albums. --
    genres = _uuid_list(
        q("""SELECT DISTINCT ag.genre_id FROM album_genres ag
             INNER JOIN album_variants av ON av.album_id = ag.album_id
             INNER JOIN media_files mf ON mf.album_variant_id = av.id
             WHERE mf.track_id = ANY(%s::uuid[])"""),
        "genre_id",
    )
    genre_descriptions = _uuid_list(
        q("""SELECT DISTINCT gd.genre_id FROM genre_descriptions gd
             INNER JOIN album_genres ag ON ag.genre_id = gd.genre_id
             INNER JOIN album_variants av ON av.album_id = ag.album_id
             INNER JOIN media_files mf ON mf.album_variant_id = av.id
             WHERE mf.track_id = ANY(%s::uuid[])"""),
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
    """Pull track metadata with associated artists."""
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
    artists_by_track: dict[str, list] = {}
    for a in artists:
        tid = str(a["track_id"])
        artists_by_track.setdefault(tid, []).append({
            "artist_uuid": a["artist_uuid"],
            "name": a["artist_name"],
            "role": a["role"],
        })

    items = []
    for t in tracks:
        uuid = t["track_uuid"]
        items.append({
            "track_uuid": uuid,
            "title": t["title"],
            "artists": artists_by_track.get(uuid, []),
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


def _provenance_item(r: dict):
    """Nested provenance payload from p_-prefixed LEFT JOIN columns; None for
    rows not linked to an analysis_sources row (legacy / failed fingerprints)."""
    if r.get("p_pcm_hash") is None:
        return None
    return {
        "origin": r["p_origin"],
        "pcm_hash": r["p_pcm_hash"],
        "chromaprint": r["p_chromaprint"],
        "duration_seconds": r["p_duration_seconds"],
        "grid_version": r["p_grid_version"],
        "sample_rate": r["p_sample_rate"],
        "bit_depth": r["p_bit_depth"],
        "is_lossless": r["p_is_lossless"],
    }


_PROVENANCE_COLS = """s.origin::text AS p_origin, s.pcm_hash AS p_pcm_hash,
                      s.chromaprint AS p_chromaprint,
                      s.duration_seconds AS p_duration_seconds,
                      s.grid_version AS p_grid_version,
                      s.sample_rate AS p_sample_rate, s.bit_depth AS p_bit_depth,
                      s.is_lossless AS p_is_lossless"""


def pull_embeddings(conn, uuids: list[str]) -> dict:
    if not uuids:
        return {"category": "embeddings", "items": []}

    rows = db_query(
        conn,
        f"""SELECT e.track_id::text AS track_uuid,
                   em.id::text AS model_uuid, em.name AS model_name,
                   e.vector::text AS vector, e.analysis_version,
                   {_PROVENANCE_COLS}
            FROM embeddings e
            INNER JOIN embedding_models em ON em.id = e.model_id
            LEFT JOIN analysis_sources s ON s.id = e.analysis_source_id
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
            "analysis_version": r["analysis_version"],
            "provenance": _provenance_item(r),
        })

    return {"category": "embeddings", "items": items}


def pull_audio_features(conn, uuids: list[str]) -> dict:
    if not uuids:
        return {"category": "audio_features", "items": []}

    rows = db_query(
        conn,
        f"""SELECT a.track_id::text AS track_uuid,
                   a.bpm, a.key, a.mode, a.key_confidence,
                   a.energy, a.energy_db, a.brightness, a.dynamic_range_db,
                   a.zero_crossing_rate, a.instruments, a.moods,
                   a.vocal_instrumental, a.vocal_score, a.danceability,
                   a.analysis_version,
                   {_PROVENANCE_COLS}
            FROM audio_features a
            LEFT JOIN analysis_sources s ON s.id = a.analysis_source_id
            WHERE a.track_id = ANY(%s::uuid[])""",
        [uuids],
    )
    items = []
    for r in rows:
        item = {k: v for k, v in r.items() if not k.startswith("p_")}
        item["provenance"] = _provenance_item(r)
        items.append(item)
    return {"category": "audio_features", "items": items}


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
    Return artist UUIDs that have tracks but NO audio enrichment data
    (no embeddings and no audio_features for any of their tracks).
    These are the artists we should look for in DHT — DHT lookup is
    expensive and only worth it when we don't have anything for the
    artist; partial-enrichment gaps are filled via the manual/LAN
    sync flow which uses get_incomplete_artist_uuids instead.
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


def get_incomplete_artist_uuids(conn) -> list[str]:
    """
    Return artist UUIDs whose data is missing in at least one sync
    category — used as the trigger set for manual/LAN peer sync.

    Track-level (any track missing → trigger): embeddings, audio_features,
    track_lyrics, track_stats.
    Artist-level (artist itself missing → trigger): artist_bios,
    artist_tags, similar_artists.

    Why broader than get_unenriched_artist_uuids: that one is for DHT
    lookup and uses AND-of-audio to keep DHT traffic down. Inside the
    sync flow we want to catch partial states — e.g. embeddings landed
    but features didn't (transient pull failure), or audio is full but
    Last.fm bios never came through because the artist was already
    "audio-enriched" and skipped. Inventory + _compute_needed at the
    peer/category level filter out anything we already have, so even
    a wide trigger here is cheap when there's nothing new to pull.

    Not included: artist_members (compound-artist-only — would trigger
    every solo artist forever), genre_descriptions (per-genre).
    """
    rows = db_query(
        conn,
        """SELECT DISTINCT ta.artist_id::text AS artist_uuid
           FROM track_artists ta
           WHERE NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.track_id = ta.track_id)
              OR NOT EXISTS (SELECT 1 FROM audio_features af WHERE af.track_id = ta.track_id)
              OR NOT EXISTS (SELECT 1 FROM track_lyrics tl WHERE tl.track_id = ta.track_id)
              OR NOT EXISTS (SELECT 1 FROM track_stats ts WHERE ts.track_id = ta.track_id)
              OR NOT EXISTS (SELECT 1 FROM artist_bios ab WHERE ab.artist_id = ta.artist_id)
              OR NOT EXISTS (SELECT 1 FROM artist_tags atg WHERE atg.artist_id = ta.artist_id)
              OR NOT EXISTS (SELECT 1 FROM similar_artists sa WHERE sa.artist_id = ta.artist_id)""",
    )
    return [r["artist_uuid"] for r in rows]
