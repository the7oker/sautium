"""
Shared sync SQL queries for Sautium.

Framework-agnostic module: takes a psycopg2 connection, returns dicts.
Used by both the aiohttp P2P sync server and the FastAPI backend.
"""

import base64
import json
import logging
from datetime import datetime, timezone

import psycopg2.extras

from desktop.p2p import record_sig

logger = logging.getLogger(__name__)

# Sync-protocol capabilities advertised on /health. Peers pick pull categories
# by this list: "segments" = this node serves per-track segment bundles
# (pull category `segments`, seals on the wire) and the legacy mean-vector
# `embeddings` pull is deprecated for it. Mirrored by the FastAPI backend
# (backend/routers/sync.py SYNC_CAPABILITIES) — keep in step.
CAPABILITIES = ["segments", "carry"]

# A track bundle is K=12..24 vectors, each ~2.7KB base64 + ~1.3KB proof —
# segments pulls get a tighter per-request cap than plain-row categories.
SEGMENTS_MAX_UUIDS = 500

# Push-seeding ("carry"): the artist layer only, for now. Measured on the
# reference library it costs ~21 KB per artist on the wire against ~46 KB
# per track for segments, and unlike segments it is useful to every node
# regardless of what it owns.
CARRY_CATEGORIES = ("artist_bios", "artist_tags", "similar_artists")
# Artists per offer/push request — an offer is 16 bytes each, a push is
# ~21 KB each.
CARRY_MAX_ARTISTS = 200
# Foreign artists a node holds by default (~42 MB at the measured size).
# Mirrors "sync.carry_limit" in backend/routers/settings.py _DEFAULTS — used
# when the row has never been written, so a node that nobody configured still
# carries. Push-seeding that were opt-in would simply never happen.
CARRY_DEFAULT_BUDGET = 2000

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
    "tracks": [], "embeddings": [],
    "audio_features": [], "track_stats": [],
    "artists": [], "artist_bios": [],
    "artist_tags": [], "similar_artists": [],
    "genres": [], "genre_descriptions": [],
}


def get_inventory(conn, track_uuids: list[str],
                  artist_uuids: list[str] | None = None) -> dict:
    """
    Check what enrichment data is available for the given track UUIDs.

    Returns category -> uuid_list dict. The versioned categories return
    [uuid, analysis_version, segment_count] triples (embeddings) and
    [uuid, analysis_version] pairs (audio_features) instead, so peers can
    re-pull rows produced by an older analysis methodology — existence
    alone never delivers upgrades. segment_count lets a peer see how dense
    this node's grid is (densification / "deepen analysis" planning).

    `artist_uuids` names artists the requester wants the artist layer for,
    independent of any track. Deriving artists from the requested tracks (as
    this did alone) only ever finds artists WE own music by — which makes
    every carried record invisible, since carrying means holding an artist's
    data without owning a note of it. Artist UUIDs are v5 over the name, so
    the requester can name them without us sharing a single track. Absent →
    track-derived only, which is what an older peer sends.

    Lyrics are deliberately NOT part of the protocol (2026-07-11): the one
    category that is verbatim copyrighted text — every node fetches its own
    from the public sources.
    """
    if not track_uuids and not artist_uuids:
        return dict(EMPTY_INVENTORY)

    uuids = track_uuids
    artists_asked = artist_uuids or []
    q = lambda sql: db_query(conn, sql, [uuids])

    def artist_layer(table: str, alias: str) -> list[str]:
        rows = db_query(
            conn,
            f"""SELECT DISTINCT {alias}.artist_id FROM {table} {alias}
                 WHERE {alias}.signature IS NOT NULL
                   AND {alias}.batch_root IS NOT NULL
                   AND ({alias}.artist_id = ANY(%s::uuid[])
                        OR EXISTS (SELECT 1 FROM track_artists ta
                                    WHERE ta.artist_id = {alias}.artist_id
                                      AND ta.track_id = ANY(%s::uuid[])))""",
            [artists_asked, uuids],
        )
        return _uuid_list(rows, "artist_id")

    # -- Track-level data --
    tracks = _uuid_list(
        q("SELECT id FROM tracks WHERE id = ANY(%s::uuid[])"), "id"
    )
    embeddings = [
        [r["track_id"], r["v"], r["segs"]] for r in
        q("""SELECT e.track_id::text AS track_id,
                    MAX(e.analysis_version) AS v, COUNT(es.id) AS segs
             FROM embeddings e
             LEFT JOIN embedding_segments es ON es.embedding_id = e.id
                   AND es.signature IS NOT NULL AND es.batch_root IS NOT NULL
             WHERE e.track_id = ANY(%s::uuid[])
             GROUP BY e.track_id""")
    ]
    audio_features = [
        [r["track_id"], r["analysis_version"]] for r in
        q("""SELECT track_id::text AS track_id, analysis_version
             FROM audio_features WHERE track_id = ANY(%s::uuid[])
               AND signature IS NOT NULL AND batch_root IS NOT NULL""")
    ]
    track_stats = _uuid_list(
        q("""SELECT DISTINCT track_id FROM track_stats
             WHERE track_id = ANY(%s::uuid[])
               AND signature IS NOT NULL AND batch_root IS NOT NULL"""),
        "track_id",
    )

    # -- Related artists (via track_artists) --
    artists = _uuid_list(
        q("SELECT DISTINCT artist_id FROM track_artists WHERE track_id = ANY(%s::uuid[])"),
        "artist_id",
    )
    artist_bios = artist_layer("artist_bios", "ab")
    artist_tags = artist_layer("artist_tags", "at2")
    similar_artists = artist_layer("similar_artists", "sa")
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
             WHERE mf.track_id = ANY(%s::uuid[])
               AND gd.signature IS NOT NULL AND gd.batch_root IS NOT NULL"""),
        "genre_id",
    )

    return {
        "tracks": tracks,
        "embeddings": embeddings,
        "audio_features": audio_features,
        "track_stats": track_stats,
        "artists": artists,
        "artist_bios": artist_bios,
        "artist_tags": artist_tags,
        "similar_artists": similar_artists,
        "genres": genres,
        "genre_descriptions": genre_descriptions,
    }


# ---------------------------------------------------------------------------
# Pull handlers
# ---------------------------------------------------------------------------

def _pull_simple(conn, category: str, sql: str, uuids: list[str],
                 post_process=None) -> dict:
    """Common handler for the sealed enrichment categories.

    Every SELECT here MUST carry the four seal columns plus fetched_at, and
    every row MUST arrive with the signing_batches rows its root names —
    a seal the importer cannot check is a seal it has to drop, and a record
    that arrives unsealed is a record nobody can be held to. The launcher
    used to serve these categories payload-only, which silently stripped
    authorship from everything a peer pulled from it.

    Rows without a seal are not served at all (see _SEALED_ONLY)."""
    if not uuids:
        return {"category": category, "items": [], "batches": {}}
    rows = db_query(conn, sql, [uuids])
    items = [_serialize_row(r) for r in rows]
    if post_process:
        items = [post_process(item) for item in items]
    roots = {i["batch_root"] for i in items if i.get("batch_root")}
    return {"category": category, "items": items,
            "batches": _batches_map(conn, roots)}


# Appended to every enrichment SELECT: unsigned rows never leave this node.
# Distributing a claim nobody signed makes the network unable to attribute —
# and the seal columns are NULLed by the seal-guard trigger the moment a
# payload column changes, so "has a signature" also means "unmodified".
_SEALED_ONLY = " AND {t}.signature IS NOT NULL AND {t}.batch_root IS NOT NULL"

_SEAL_COLS = """{t}.fetched_at, {t}.author_pubkey, {t}.signature,
                {t}.batch_root, {t}.merkle_proof"""


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


def _provenance_item(r: dict):
    """Nested provenance payload from p_-prefixed LEFT JOIN columns; None for
    rows not linked to an analysis_sources row (legacy / failed fingerprints).

    Carries the material declaration the signature commits to, and nothing
    that describes the author's copy of it — see the same function in
    backend/routers/sync.py for why origin/sample_rate/bit_depth are held
    back. None of the three is part of the signed payload."""
    if r.get("p_pcm_hash") is None:
        return None
    return {
        "pcm_hash": r["p_pcm_hash"],
        "chromaprint": r["p_chromaprint"],
        "duration_seconds": r["p_duration_seconds"],
        "grid_version": r["p_grid_version"],
        "is_lossless": r["p_is_lossless"],
    }


_PROVENANCE_COLS = """s.pcm_hash AS p_pcm_hash,
                      s.chromaprint AS p_chromaprint,
                      s.duration_seconds AS p_duration_seconds,
                      s.grid_version AS p_grid_version,
                      s.is_lossless AS p_is_lossless"""


def _batches_map(conn, roots: set) -> dict:
    """signing_batches rows for the referenced Merkle roots, serialized so the
    importer can verify the Worker timestamp: worker_date is re-rendered as the
    exact seconds-precision UTC string the Worker signed."""
    if not roots:
        return {}
    rows = db_query(
        conn,
        """SELECT batch_root, author_pubkey, worker_date, ip_hash::text AS ip_hash,
                  worker_sig, authority
           FROM signing_batches WHERE batch_root = ANY(%s)""",
        [list(roots)],
    )
    return {
        r["batch_root"]: {
            "author_pubkey": r["author_pubkey"],
            "worker_date": r["worker_date"].astimezone(timezone.utc)
                                           .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ip_hash": r["ip_hash"],
            "worker_sig": r["worker_sig"],
            "authority": r["authority"],
        }
        for r in rows
    }


def pull_segments(conn, uuids: list[str]) -> dict:
    """Per-track CLAP segment bundles with their seals — the signed, synced
    unit (the importer derives the mean locally; see record_sig.py). Vectors
    travel as base64 of the canonical float32-LE bytes so vector_hash verifies
    over the received bytes with no float re-derivation. The response-level
    `batches` map carries each seal's Worker timestamp so the full chain
    travels with the records and survives relay."""
    if not uuids:
        return {"category": "segments", "items": [], "batches": {}}
    if len(uuids) > SEGMENTS_MAX_UUIDS:
        raise ValueError(
            f"segments pull is capped at {SEGMENTS_MAX_UUIDS} tracks per request")

    rows = db_query(
        conn,
        f"""SELECT e.track_id::text AS track_uuid,
                   em.id::text AS model_uuid, em.name AS model_name,
                   e.analysis_version,
                   es.segment_index, es.vector::text AS vec,
                   es.author_pubkey, es.signature, es.batch_root, es.merkle_proof,
                   {_PROVENANCE_COLS}
            FROM embeddings e
            INNER JOIN embedding_models em ON em.id = e.model_id
            INNER JOIN embedding_segments es ON es.embedding_id = e.id
            LEFT JOIN analysis_sources s ON s.id = e.analysis_source_id
            WHERE e.track_id = ANY(%s::uuid[])
              AND es.signature IS NOT NULL AND es.batch_root IS NOT NULL
            ORDER BY e.track_id, es.segment_index""",
        [uuids],
    )

    items_by_track: dict[str, dict] = {}
    roots: set = set()
    for r in rows:
        bundle = items_by_track.get(r["track_uuid"])
        if bundle is None:
            bundle = items_by_track[r["track_uuid"]] = {
                "track_uuid": r["track_uuid"],
                "model_uuid": r["model_uuid"],
                "model_name": r["model_name"],
                "analysis_version": r["analysis_version"],
                "provenance": _provenance_item(r),
                "segments": [],
            }
        seg = {
            "i": r["segment_index"],
            "v": base64.b64encode(
                record_sig.vector_to_bytes(_parse_vector(r["vec"]))
            ).decode("ascii"),
        }
        if r["signature"]:
            seg["author_pubkey"] = r["author_pubkey"]
            seg["signature"] = r["signature"]
            seg["batch_root"] = r["batch_root"]
            seg["proof"] = r["merkle_proof"]
            roots.add(r["batch_root"])
        bundle["segments"].append(seg)

    return {"category": "segments", "items": list(items_by_track.values()),
            "batches": _batches_map(conn, roots)}


def pull_embeddings(conn, uuids: list[str]) -> dict:
    """Legacy mean-vector pull — kept for peers without the `segments`
    capability. Capable peers pull `segments` and derive the mean locally."""
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
    """Feature rows travel WITH their seals (author sig + Merkle proof) and a
    `batches` map for the Worker timestamps — imported rows stay verifiable
    and re-servable with authorship intact."""
    if not uuids:
        return {"category": "audio_features", "items": [], "batches": {}}

    rows = db_query(
        conn,
        f"""SELECT a.track_id::text AS track_uuid,
                   a.bpm, a.key, a.mode, a.key_confidence,
                   a.energy, a.energy_db, a.brightness, a.dynamic_range_db,
                   a.zero_crossing_rate, a.instruments, a.moods,
                   a.vocal_instrumental, a.vocal_score, a.danceability,
                   a.analysis_version,
                   a.author_pubkey, a.signature, a.batch_root, a.merkle_proof,
                   {_PROVENANCE_COLS}
            FROM audio_features a
            LEFT JOIN analysis_sources s ON s.id = a.analysis_source_id
            WHERE a.track_id = ANY(%s::uuid[])
              AND a.signature IS NOT NULL AND a.batch_root IS NOT NULL""",
        [uuids],
    )
    items, roots = [], set()
    for r in rows:
        item = {k: v for k, v in r.items() if not k.startswith("p_")}
        item["provenance"] = _provenance_item(r)
        roots.add(item["batch_root"])
        items.append(item)
    return {"category": "audio_features", "items": items,
            "batches": _batches_map(conn, roots)}


def pull_track_stats(conn, uuids: list[str]) -> dict:
    return _pull_simple(
        conn, "track_stats",
        f"""SELECT ts.track_id::text AS track_uuid, ts.source,
                   ts.listeners, ts.playcount,
                   {_SEAL_COLS.format(t='ts')}
            FROM track_stats ts
            WHERE ts.track_id = ANY(%s::uuid[])
            {_SEALED_ONLY.format(t='ts')}""",
        uuids,
    )


def pull_artist_bios(conn, uuids: list[str]) -> dict:
    return _pull_simple(
        conn, "artist_bios",
        f"""SELECT ab.artist_id::text AS artist_uuid, a.name AS artist_name,
                   ab.source, ab.summary, ab.content, ab.url,
                   ab.listeners, ab.playcount,
                   {_SEAL_COLS.format(t='ab')}
            FROM artist_bios ab
            INNER JOIN artists a ON a.id = ab.artist_id
            WHERE ab.artist_id = ANY(%s::uuid[])
            {_SEALED_ONLY.format(t='ab')}""",
        uuids,
    )


def pull_artist_tags(conn, uuids: list[str]) -> dict:
    return _pull_simple(
        conn, "artist_tags",
        f"""SELECT at2.artist_id::text AS artist_uuid,
                   t.id::text AS tag_uuid, t.name AS tag_name,
                   at2.weight, at2.source,
                   {_SEAL_COLS.format(t='at2')}
            FROM artist_tags at2
            INNER JOIN tags t ON t.id = at2.tag_id
            WHERE at2.artist_id = ANY(%s::uuid[])
            {_SEALED_ONLY.format(t='at2')}""",
        uuids,
    )


def pull_similar_artists(conn, uuids: list[str]) -> dict:
    return _pull_simple(
        conn, "similar_artists",
        f"""SELECT sa.artist_id::text AS artist_uuid,
                   sa.similar_artist_id::text AS similar_artist_uuid,
                   a.name AS similar_artist_name,
                   sa.match_score::float, sa.source,
                   {_SEAL_COLS.format(t='sa')}
            FROM similar_artists sa
            INNER JOIN artists a ON a.id = sa.similar_artist_id
            WHERE sa.artist_id = ANY(%s::uuid[])
            {_SEALED_ONLY.format(t='sa')}""",
        uuids,
    )


def pull_genre_descriptions(conn, uuids: list[str]) -> dict:
    return _pull_simple(
        conn, "genre_descriptions",
        f"""SELECT gd.genre_id::text AS genre_uuid, g.name AS genre_name,
                   gd.source, gd.summary, gd.content, gd.url,
                   {_SEAL_COLS.format(t='gd')}
            FROM genre_descriptions gd
            INNER JOIN genres g ON g.id = gd.genre_id
            WHERE gd.genre_id = ANY(%s::uuid[])
            {_SEALED_ONLY.format(t='gd')}""",
        uuids,
    )


# Map category name -> pull function
PULL_HANDLERS = {
    "tracks": pull_tracks,
    "segments": pull_segments,
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
    "genre-descriptions": pull_genre_descriptions,
    "genre_descriptions": pull_genre_descriptions,
}


# ---------------------------------------------------------------------------
# DHT-related queries
# ---------------------------------------------------------------------------

def get_enriched_artist_uuids(conn) -> list[str]:
    """
    Return artist UUIDs that have at least 1 track with embedding or audio_features.
    Used for the LAN-discovery enriched counter, NOT for DHT announcing —
    announcing scales with the node key + a rare tail (see dht_service).
    """
    rows = db_query(
        conn,
        """SELECT DISTINCT ta.artist_id::text AS artist_uuid
           FROM track_artists ta
           WHERE EXISTS (SELECT 1 FROM embeddings e WHERE e.track_id = ta.track_id)
              OR EXISTS (SELECT 1 FROM audio_features af WHERE af.track_id = ta.track_id)""",
    )
    return [r["artist_uuid"] for r in rows]


# ---------------------------------------------------------------------------
# Push-seeding (carry)
#
# Sync is pull-only, so a node that accepts no inbound connections can take
# from the network but never give: nobody can reach it to pull. Its own
# analysis of the rare tail — the part no one else has — dies with it.
# Carrying fixes the direction, not the trust: the pusher hands over rows
# that are already signed, the carrier verifies them through the ordinary
# import gate, and from then on serves them like any other row (the pull
# handlers have no owner filter, deliberately — authorship travels with the
# record).
# ---------------------------------------------------------------------------

def get_pushable_artists(conn, limit: int) -> list[str]:
    """Artists whose FIRST-HAND, sealed artist-layer data this node can
    contribute, rarest first.

    Three conditions, each load-bearing:
      * sealed — unsigned material must not travel at all;
      * NOT imported — first-hand only. Re-pushing what we pulled from
        someone else spreads nothing new and burns a carrier's budget;
      * canon — an MB anchor that is not phantom-confidence. Phantom rows
        are name+genre guesses for artists with no owned tracks to verify
        against, and the schema says so outright: re-verify before trusting
        over P2P. Pushing residue is how carriers would multiply noise.

    Rarest first (Last.fm listeners, unknown counts rarest) for the same
    reason the DHT tail is: anything popular reaches the network anyway.
    """
    if limit <= 0:
        return []
    rows = db_query(
        conn,
        """WITH mine AS (
               SELECT artist_id FROM artist_bios
                WHERE signature IS NOT NULL AND NOT imported
               UNION
               SELECT artist_id FROM artist_tags
                WHERE signature IS NOT NULL AND NOT imported
               UNION
               SELECT artist_id FROM similar_artists
                WHERE signature IS NOT NULL AND NOT imported
           )
           SELECT m.artist_id::text AS artist_uuid
             FROM mine m
             LEFT JOIN artist_bios ab ON ab.artist_id = m.artist_id
            WHERE EXISTS (SELECT 1 FROM artist_mbids am
                           WHERE am.artist_id = m.artist_id
                             AND am.confidence <> 'phantom')
            GROUP BY m.artist_id
            ORDER BY MAX(ab.listeners) ASC NULLS FIRST
            LIMIT %s""",
        [limit],
    )
    return [r["artist_uuid"] for r in rows]


def count_carried_artists(conn) -> int:
    """How many artists this node holds enrichment for while owning none of
    their music — the honest meter for "disk spent on someone else's
    behalf". Material about artists we DO own is not carrying, it is our own
    library, however it arrived."""
    rows = db_query(
        conn,
        """WITH held AS (
               SELECT artist_id FROM artist_bios WHERE imported
               UNION SELECT artist_id FROM artist_tags WHERE imported
               UNION SELECT artist_id FROM similar_artists WHERE imported
           )
           SELECT count(*) AS n FROM held h
            WHERE NOT EXISTS (
                SELECT 1 FROM track_artists ta
                  JOIN media_files mf ON mf.track_id = ta.track_id
                 WHERE ta.artist_id = h.artist_id)""",
    )
    return int(rows[0]["n"]) if rows else 0


def wanted_artists(conn, artist_uuids: list[str], budget: int) -> dict:
    """Answer an offer: per category, which of these artists we hold nothing
    for — capped by whatever is left of the carry budget.

    "Nothing for" rather than "nothing fresher": a freshness upgrade is what
    the ordinary pull is for, and asking for one here would mean shipping
    fetched_at in the offer to no purpose."""
    empty = {c: [] for c in CARRY_CATEGORIES}
    if not artist_uuids or budget <= 0:
        return empty
    room = budget - count_carried_artists(conn)
    if room <= 0:
        return empty

    uuids = artist_uuids[:CARRY_MAX_ARTISTS]
    out = {}
    for category, table in (("artist_bios", "artist_bios"),
                            ("artist_tags", "artist_tags"),
                            ("similar_artists", "similar_artists")):
        rows = db_query(
            conn,
            f"""SELECT u.id::text AS artist_uuid
                  FROM unnest(%s::uuid[]) AS u(id)
                 WHERE NOT EXISTS (SELECT 1 FROM {table} t
                                    WHERE t.artist_id = u.id)
                 LIMIT %s""",
            [uuids, room],
        )
        out[category] = [r["artist_uuid"] for r in rows]
    return out


ANNOUNCE_TAIL_SQL = """
    WITH announceable AS (
        SELECT ta.artist_id
          FROM track_artists ta
          JOIN media_files mf ON mf.track_id = ta.track_id
         WHERE EXISTS (SELECT 1 FROM embeddings e WHERE e.track_id = ta.track_id)
            OR EXISTS (SELECT 1 FROM audio_features af WHERE af.track_id = ta.track_id)
        UNION
        SELECT artist_id FROM artist_bios      WHERE imported
        UNION
        SELECT artist_id FROM artist_tags      WHERE imported
        UNION
        SELECT artist_id FROM similar_artists  WHERE imported
    )
    SELECT x.artist_id::text AS artist_uuid
      FROM announceable x
      LEFT JOIN artist_bios ab ON ab.artist_id = x.artist_id
     GROUP BY x.artist_id
     ORDER BY MAX(ab.listeners) ASC NULLS FIRST
     LIMIT %s
"""


def get_announce_tail_uuids(conn, limit: int) -> list[str]:
    """The rare-artist tail to announce by exact key (see dht_service).

    Two things get announced. Artists whose OWNED music this node has
    analyzed — first-hand material nobody else may hold. And artists whose
    layer arrived from a peer, because whoever authored it may be unable to
    announce at all: a node behind CGNAT suppresses its own announces, so
    for anything it push-seeded the carrier is the only address in the DHT.
    An unannounced carry is disk spent on data no one can find.

    Phantoms never enter either set — an owned track or an imported record
    is the whole test, and neither is something a name-guess produces.

    Ranked by Last.fm listeners as the rarity proxy: a peer's random node
    sample will surface anything popular anyway, so an exact key is only
    worth spending on artists nobody else is likely to have. NULL listeners
    (unknown to Last.fm) rank rarest.
    """
    if limit <= 0:
        return []
    return [r["artist_uuid"] for r in db_query(conn, ANNOUNCE_TAIL_SQL, [limit])]


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
    track_stats. (Lyrics are out of the sync protocol — never a trigger.)
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
              OR NOT EXISTS (SELECT 1 FROM track_stats ts WHERE ts.track_id = ta.track_id)
              OR NOT EXISTS (SELECT 1 FROM artist_bios ab WHERE ab.artist_id = ta.artist_id)
              OR NOT EXISTS (SELECT 1 FROM artist_tags atg WHERE atg.artist_id = ta.artist_id)
              OR NOT EXISTS (SELECT 1 FROM similar_artists sa WHERE sa.artist_id = ta.artist_id)""",
    )
    return [r["artist_uuid"] for r in rows]
