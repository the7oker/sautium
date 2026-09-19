"""
Shared sync SQL queries for Sautium.

Framework-agnostic module: takes a psycopg2 connection, returns dicts.
Used by both the aiohttp P2P sync server and the FastAPI backend.
"""

import base64
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import psycopg2.extras

from desktop.p2p import record_sig
from desktop.p2p.bloom import BloomFilter

logger = logging.getLogger(__name__)

# Sync-protocol capabilities advertised on /health. Peers pick pull categories
# by this list: "segments" = this node serves per-track segment bundles
# (pull category `segments`, seals on the wire) and the legacy mean-vector
# `embeddings` pull is deprecated for it. Mirrored by the FastAPI backend
# (backend/routers/sync.py SYNC_CAPABILITIES) — keep in step.
CAPABILITIES = ["segments", "carry", "holdings"]

# A track bundle is K=12..24 vectors, each ~2.7KB base64 + ~1.3KB proof —
# segments pulls get a tighter per-request cap than plain-row categories.
SEGMENTS_MAX_UUIDS = 500

# Push-seeding ("carry"): audio analysis, because it is the one layer with
# no external source — GPU work dies with an unreachable node unless someone
# carries it. The artist layer was carried at first and dropped 2026-08-03
# (reproducible from Last.fm by name, and importing similars minted stub
# artists straight into the carrier's phantom-canon feed).
#
# v4 (2026-08-07, Валерій's design): the offer round speaks RECORDING
# MBIDs, the transfer speaks track UUIDs, and the carrier answers only
# with uuids that ALREADY EXIST in its base — its phantom catalogue (~20×
# owned, grown organically from its own discovery graph) IS the taste
# profile, so nothing lands that the carrier would never care about. The
# double key buys both guarantees at once: an MBID exists only for
# canonized material, and a uuid match means the author's seal (which
# binds the track uuid) survives re-serve intact. The two mismatch
# classes die silently on the right side: same-name-different-recording
# is cut by the MBID round; same-recording-different-name never leaves
# the pusher (it holds no such uuid). The v3 structural transport
# (albums / tracks / album_tracks / artist_mbids categories, the
# identity-recompute functions, their importers) is DELETED, not dormant
# — git history has it. The album/album_track seals stay (the pusher's
# full-snapshot gate reads them); the artist_mbids seal layer is gone
# with its transport — the canon-primary gate reads confidence, not a
# signature.
CARRY_CATEGORIES = ("segments", "audio_features", "track_mbids")
# Recordings per offer request — an offer is 16 bytes per recording
# against ~46 KB to push one track blind.
CARRY_MAX_TRACKS = 500
# Foreign tracks a node holds by default (~92 MB at the measured size).
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

EMPTY_INVENTORY = {"tracks": [], "embeddings": [], "audio_features": []}


# Rows the sync protocol will hand out: sealed ones. The inventory and the
# holdings filter must draw from the same population, or a filter hit would
# be a promise the inventory then breaks.
_SIGNED = "signature IS NOT NULL AND batch_root IS NOT NULL"


def get_inventory(conn, track_uuids: list[str]) -> dict:
    """
    Check what analysis this node can serve for the given track UUIDs.

    Returns category -> list. `tracks` is plain uuids; the versioned
    categories return [uuid, analysis_version, segment_count] triples
    (embeddings) and [uuid, analysis_version] pairs (audio_features), so
    peers can re-pull rows produced by an older analysis methodology —
    existence alone never delivers upgrades. segment_count lets a peer see
    how dense this node's grid is (densification / "deepen analysis"
    planning).

    Lyrics are deliberately NOT part of the protocol (2026-07-11): the one
    category that is verbatim copyrighted text — every node fetches its own
    from the public sources. The Last.fm-fetched layer (bios, tags,
    similars, track stats, genre descriptions) left it 2026-09-19 for the
    same kind of reason: Last.fm's API terms do not allow redistributing
    its answers, so every node fetches its own by name.
    """
    if not track_uuids:
        return dict(EMPTY_INVENTORY)
    q = lambda sql: db_query(conn, sql, [track_uuids])

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
        q(f"""SELECT track_id::text AS track_id, analysis_version
              FROM audio_features WHERE track_id = ANY(%s::uuid[])
                AND {_SIGNED}""")
    ]
    return {"tracks": tracks, "embeddings": embeddings,
            "audio_features": audio_features}


# ---------------------------------------------------------------------------
# Holdings filter — the compressed inventory (P2P_NETWORK.md § Holdings filter)
# ---------------------------------------------------------------------------
# A node with millions of phantom gaps cannot ask every peer about all of
# them (306 inventory requests per peer per run at the master's size). The
# peer publishes instead: a Bloom filter over the track uuids it HOLDS
# sealed analysis for — sized by its holdings, never by anyone's gaps. The
# asker tests its gaps locally and asks the exact inventory only about the
# hits. Built in memory when the holdings version moved (checked at most
# every _HOLDINGS_RECHECK_S), served from memory otherwise; a caller that
# already has the current version gets a stub, no bits. (Until 2026-09-19 a
# second filter named the artists with sealed bios/tags/similars — that
# layer is node-local now.)

HOLDINGS_FPR = 0.01
_HOLDINGS_RECHECK_S = 300

_holdings_lock = threading.Lock()
_holdings: dict = {"checked_at": 0.0, "version": None, "payload": None,
                   "tracks_n": 0}

_HOLDINGS_TRACKS_SQL = f"""
    SELECT track_id::text FROM embeddings
    UNION SELECT track_id::text FROM audio_features WHERE {_SIGNED}"""


def holdings_version(conn) -> str:
    """Moves whenever a contributing table's row count or latest updated_at
    moves — one scan per table, so it is checked on a timer, not per
    request."""
    row = db_query(conn, f"""
        SELECT (SELECT count(*) FROM embeddings) AS e,
               (SELECT count(*) FROM audio_features WHERE {_SIGNED}) AS af,
               GREATEST((SELECT max(updated_at) FROM embeddings),
                        (SELECT max(updated_at) FROM audio_features)) AS latest
    """)[0]
    counts = [int(row[k]) for k in ("e", "af")]
    latest = int(row["latest"].timestamp()) if row["latest"] else 0
    return ".".join(str(n) for n in counts) + f".{latest}"


def _stream_uuids(conn, sql: str):
    # A holdable server-side cursor: both peer surfaces run autocommit
    # connections, and a plain cursor would pull millions of rows into the
    # client at once.
    with conn.cursor(name="sautium_holdings", withhold=True) as cur:
        cur.itersize = 50_000
        cur.execute(sql)
        for (uid,) in cur:
            yield uid


def _build_filter(conn, sql: str) -> BloomFilter:
    # Sized by the exact distinct count — the per-table row counts in the
    # version are a poor bound (one track sits in both tables), and an
    # oversized filter is wasted bytes for every peer that fetches it. One
    # extra pass over the union, on rebuild only.
    capacity = db_query(conn, f"SELECT count(*) AS n FROM ({sql}) u")[0]["n"]
    bf = BloomFilter.sized(max(int(capacity), 1000), HOLDINGS_FPR)
    bf.update(_stream_uuids(conn, sql))
    return bf


def get_holdings(conn, have: Optional[str] = None) -> dict:
    """The holdings payload: {"version", "tracks": filter}. `have` is the
    version the caller already holds — an unchanged one answers {"version",
    "unchanged": true}."""
    with _holdings_lock:
        now = time.monotonic()
        if (_holdings["payload"] is None
                or now - _holdings["checked_at"] > _HOLDINGS_RECHECK_S):
            version = holdings_version(conn)
            if version != _holdings["version"]:
                t0 = time.monotonic()
                tracks = _build_filter(conn, _HOLDINGS_TRACKS_SQL)
                _holdings.update(
                    version=version, tracks_n=tracks.n,
                    payload={"version": version, "tracks": tracks.to_dict()},
                )
                logger.info(
                    f"Holdings filter rebuilt: {tracks.n} tracks, "
                    f"{len(tracks.bits) // 1024} KB, "
                    f"{time.monotonic() - t0:.1f}s"
                )
            _holdings["checked_at"] = now
        if have and have == _holdings["version"]:
            return {"version": _holdings["version"], "unchanged": True}
        return _holdings["payload"]


def holdings_summary() -> Optional[dict]:
    """Version and count for /health, from memory only — never builds.
    The asker prices "fetch the filter" against "send my gaps" with it;
    None until the first holdings request built the filter."""
    if _holdings["payload"] is None:
        return None
    return {"version": _holdings["version"], "tracks": _holdings["tracks_n"]}


def split_engaged(conn, artist_uuids: list[str]) -> tuple[list[str], list[str]]:
    """Partition artists into the ENGAGED core — an owned file or a
    completed, unskipped listen — asked of every peer in full, and the
    phantom bulk, asked through the holdings filter."""
    if not artist_uuids:
        return [], []
    rows = db_query(conn, """
        SELECT DISTINCT ta.artist_id::text AS artist_uuid
          FROM track_artists ta
         WHERE ta.artist_id = ANY(%s::uuid[])
           AND (EXISTS (SELECT 1 FROM media_files mf
                         WHERE mf.track_id = ta.track_id)
                OR EXISTS (SELECT 1 FROM listening_history lh
                            WHERE lh.track_id = ta.track_id
                              AND lh.completed AND NOT lh.skipped))""",
        [artist_uuids])
    engaged = {r["artist_uuid"] for r in rows}
    return ([a for a in artist_uuids if a in engaged],
            [a for a in artist_uuids if a not in engaged])


# ---------------------------------------------------------------------------
# Pull handlers
# ---------------------------------------------------------------------------

def _pull_simple(conn, category: str, sql: str, uuids: list[str],
                 post_process=None) -> dict:
    """Common handler for the sealed plain-row categories.

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
    rows not linked to an analysis_sources row (unlinked legacy rows).

    Carries the material declaration the signature commits to, and nothing
    that describes the author's copy of it — see the same function in
    backend/routers/sync.py for why provider_id/sample_rate/bit_depth are held
    back. None of the three is part of the signed payload."""
    if r.get("p_chromaprint") is None:
        return None
    return {
        "chromaprint": r["p_chromaprint"],
        "duration_seconds": r["p_duration_seconds"],
        "grid_version": r["p_grid_version"],
        "is_lossless": r["p_is_lossless"],
    }


_PROVENANCE_COLS = """s.chromaprint AS p_chromaprint,
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
                  worker_sig, authority, timestamp_version
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
            "timestamp_version": r["timestamp_version"],
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


def pull_track_mbids(conn, track_uuids: list[str]) -> dict:
    """Sealed track↔recording bindings (carry v3)."""
    return _pull_simple(
        conn, "track_mbids",
        f"""SELECT tm.track_id::text AS track_uuid,
                   tm.recording_mbid::text AS recording_mbid,
                   tm.confidence::text AS confidence,
                   tm.created_at AS fetched_at, tm.author_pubkey,
                   tm.signature, tm.batch_root, tm.merkle_proof
            FROM track_mbids tm
            WHERE tm.track_id = ANY(%s::uuid[])
            {_SEALED_ONLY.format(t='tm')}""",
        track_uuids,
    )


# Map category name -> pull function
PULL_HANDLERS = {
    "tracks": pull_tracks,
    "segments": pull_segments,
    "embeddings": pull_embeddings,
    "audio-features": pull_audio_features,
    "audio_features": pull_audio_features,
    "track-mbids": pull_track_mbids,
    "track_mbids": pull_track_mbids,
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

def get_pushable_tracks(conn, limit: int) -> list[dict]:
    """Tracks whose FIRST-HAND, sealed audio analysis this node can
    contribute, rarest first — {track_uuid, recordings} rows; the offer
    round speaks the recordings (v4).

    Full-snapshot gate — every condition load-bearing:
      * sealed segments — unsigned material must not travel at all;
      * first-hand source (analysis_sources NOT imported) — re-pushing what
        we pulled from someone else spreads nothing new and burns a
        carrier's budget;
      * canonized album — a sealed tracklist row under a sealed RG-anchored
        album: the full-snapshot proof that this node's canon matured
        around the track, not just its analysis. sign_audio seals those rows
        for every track with signable first-hand analysis — an owned rip OR
        a stream of a phantom;
      * a recording to name the track by — the offer round speaks MBIDs.
        Two sources, one per identity path: the canon matcher's sealed
        track_mbids binding (owned files), or the sealed tracklist row's own
        recording_mbid (a phantom minted from the MB tracklist — its slot IS
        the binding; track_mbids is the matcher's output and a phantom never
        went through the matcher). Before 2026-08-25 only the first counted,
        and 1659 stream-analyzed phantoms with a recording in album_tracks
        were invisible to the offer;
      * a trustworthy identity: either the primary artist has a non-phantom
        MB anchor (a phantom anchor is a Last.fm-name guess, an unsplit
        "A feat. B" mess has none), or the recording came off the MB-minted
        tracklist of an RG-anchored album — MB's own recording under MB's
        own release-group needs no artist guess to vouch for it, and the
        stream enricher already refused audio whose length disagreed with
        MB's.

    Measured 2026-08-25: 28037 of 38719 sealed first-hand tracks passed the
    old gate (0 of 1659 phantoms); the rest are compilations and residue
    whose canon has not matured — they ride a later push.

    Rarest first (Last.fm listeners of the primary artist, unknown counts
    rarest) for the same reason the DHT tail is: anything popular reaches
    the network anyway."""
    if limit <= 0:
        return []
    rows = db_query(
        conn,
        """WITH mine AS (
               SELECT e.track_id
                 FROM embeddings e
                 JOIN analysis_sources s ON s.id = e.analysis_source_id
                WHERE NOT s.imported
                  AND EXISTS (SELECT 1 FROM embedding_segments es
                               WHERE es.embedding_id = e.id
                                 AND es.signature IS NOT NULL
                                 AND es.batch_root IS NOT NULL)
           ),
           snapshot AS (
               SELECT m.track_id,
                      bool_or(at2.recording_mbid IS NOT NULL
                              AND al.musicbrainz_id IS NOT NULL) AS mb_native,
                      array_remove(array_agg(DISTINCT at2.recording_mbid::text),
                                   NULL) AS slot_recordings
                 FROM mine m
                 JOIN album_tracks at2 ON at2.track_id = m.track_id
                 JOIN albums al        ON al.id = at2.album_id
                WHERE at2.signature IS NOT NULL
                  AND al.signature IS NOT NULL
                GROUP BY m.track_id
           )
           SELECT t.id::text AS track_uuid,
                  (SELECT array_agg(DISTINCT r)
                     FROM (SELECT tm.recording_mbid::text AS r
                             FROM track_mbids tm
                            WHERE tm.track_id = t.id
                              AND tm.signature IS NOT NULL
                            UNION ALL
                           SELECT unnest(sn.slot_recordings)) u) AS recordings
             FROM snapshot sn
             JOIN tracks t         ON t.id = sn.track_id
             JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
             LEFT JOIN artist_bios ab ON ab.artist_id = ta.artist_id
            WHERE (sn.mb_native
                   OR EXISTS (SELECT 1 FROM artist_mbids am
                               WHERE am.artist_id = ta.artist_id
                                 AND am.confidence <> 'phantom'))
              AND (cardinality(sn.slot_recordings) > 0
                   OR EXISTS (SELECT 1 FROM track_mbids tm
                               WHERE tm.track_id = t.id
                                 AND tm.signature IS NOT NULL))
            GROUP BY t.id, sn.slot_recordings
            ORDER BY MAX(ab.listeners) ASC NULLS FIRST
            LIMIT %s""",
        [limit],
    )
    return [dict(r) for r in rows]


def count_carried_tracks(conn) -> int:
    """How many tracks this node holds imported analysis for while owning
    no file — the honest meter for "disk spent on someone else's behalf".
    Analysis of tracks we DO own is not carrying, it is our own library,
    however it arrived."""
    rows = db_query(
        conn,
        """SELECT count(DISTINCT e.track_id) AS n
             FROM embeddings e
             JOIN analysis_sources s ON s.id = e.analysis_source_id
            WHERE s.imported
              AND NOT EXISTS (SELECT 1 FROM media_files mf
                               WHERE mf.track_id = e.track_id)""",
    )
    return int(rows[0]["n"]) if rows else 0


def wanted_tracks(conn, recording_mbids: list[str], budget: int) -> dict:
    """Answer an offer (v4): the offer speaks recording MBIDs, the answer
    speaks OUR track uuids — and only uuids that already exist here.

    The existence rule is the whole taste filter: this node's phantom
    catalogue grew from its own discovery graph, so a recording it has no
    row for is a recording it never cared about, and nothing gets minted
    to hold it. The uuid answer is the integrity gate: the pusher can only
    serve uuids it also holds, which is exactly the set where the author's
    track-uuid-bound seals survive re-serve. Same-name-different-recording
    dies in the MBID join; same-recording-different-name dies because the
    pusher holds no such uuid. Owned tracks with analysis gaps match too —
    that is ordinary gap-fill through the same door.

    Structural categories are never asked for (the skeleton is already
    here, MB-canonical); track_mbids ARE — a phantom's recording binding
    lives in album_tracks, not track_mbids, and the sealed marks are
    cheap."""
    empty = {c: [] for c in CARRY_CATEGORIES}
    if not recording_mbids or budget <= 0:
        return empty
    room = budget - count_carried_tracks(conn)
    if room <= 0:
        return empty

    mbids = recording_mbids[:CARRY_MAX_TRACKS]
    matched = db_query(
        conn,
        """SELECT DISTINCT at2.track_id::text AS track_uuid
             FROM unnest(%s::uuid[]) AS o(mbid)
             JOIN album_tracks at2 ON at2.recording_mbid = o.mbid
            LIMIT %s""",
        [mbids, room],
    )
    uuids = [r["track_uuid"] for r in matched]
    if not uuids:
        return empty

    no_segments = db_query(
        conn,
        """SELECT u.id::text AS track_uuid
             FROM unnest(%s::uuid[]) AS u(id)
            WHERE NOT EXISTS (
                SELECT 1 FROM embeddings e
                  JOIN embedding_segments es ON es.embedding_id = e.id
                 WHERE e.track_id = u.id)""",
        [uuids],
    )
    no_features = db_query(
        conn,
        """SELECT u.id::text AS track_uuid
             FROM unnest(%s::uuid[]) AS u(id)
            WHERE NOT EXISTS (SELECT 1 FROM audio_features af
                               WHERE af.track_id = u.id)""",
        [uuids],
    )
    no_recording = db_query(
        conn,
        """SELECT u.id::text AS track_uuid
             FROM unnest(%s::uuid[]) AS u(id)
            WHERE NOT EXISTS (SELECT 1 FROM track_mbids tm
                               WHERE tm.track_id = u.id)""",
        [uuids],
    )
    out = dict(empty)
    out["segments"] = [r["track_uuid"] for r in no_segments]
    out["audio_features"] = [r["track_uuid"] for r in no_features]
    out["track_mbids"] = [r["track_uuid"] for r in no_recording]
    return out


ANNOUNCE_TAIL_SQL = """
    WITH analyzed AS (
        SELECT track_id FROM embeddings
        UNION
        SELECT track_id FROM audio_features
    )
    SELECT ta.artist_id::text AS artist_uuid
      FROM analyzed x
      JOIN track_artists ta ON ta.track_id = x.track_id
      LEFT JOIN artist_bios ab ON ab.artist_id = ta.artist_id
     WHERE NOT EXISTS (
         SELECT 1 FROM seed_picks sp
         JOIN album_tracks at2 ON at2.album_id = sp.album_id
        WHERE at2.track_id = x.track_id
     )
     GROUP BY ta.artist_id
     ORDER BY MAX(ab.listeners) ASC NULLS FIRST
     LIMIT %s
"""


def get_announce_tail_uuids(conn, limit: int) -> list[str]:
    """The rare-artist tail to announce by exact key (see dht_service).

    One test: does this node HOLD analysis for the artist's tracks.
    Analysis is what a peer can actually pull, so the announce mirrors
    serveability, not file ownership — the announce used to require
    media_files, which silently dropped everything servable-but-fileless:
    carried rows (whose author suppresses its own announces, making the
    carrier their only DHT address), stream-derived analysis, and
    first-hand analysis orphaned by a prune (file deleted, embeddings
    deliberately kept). Meanwhile an owned-but-unanalyzed file advertises
    nothing a peer wants. "Has analysis" covers all of it exactly.

    The MB-minted phantom tracklist layer never enters: millions of
    track_artists rows, none with an embeddings row — which is also why
    the query drives FROM the analyzed set (tens of thousands) instead of
    filtering track_artists.

    Seed-pick tracks are excluded on every node, the master included:
    their analysis ships in every install's seed bundle, and universally
    held is the same as popular — an exact DHT key spent on it is wasted
    and, on a fresh node, would make the tail 100% identical seeds. An
    artist with any non-seed analyzed track still enters via those
    tracks.

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
    Artists with at least one track that has neither an embedding nor
    audio_features — the audio gap set the sync walk asks each peer's
    inventory about between drains. Per track, not per artist: one
    unanalysed track is a reason to ask, a fully analysed discography is
    not. Partial-category gaps (a bio missing on an analysed artist) are
    get_incomplete_artist_uuids' job.
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
           )
           ORDER BY 1""",
    )
    return [r["artist_uuid"] for r in rows]


RARE_SEARCH_SQL = """
    WITH engaged AS (
        SELECT DISTINCT ta.artist_id
          FROM track_artists ta
         WHERE EXISTS (SELECT 1 FROM media_files mf
                        WHERE mf.track_id = ta.track_id)
            OR EXISTS (SELECT 1 FROM listening_history lh
                        WHERE lh.track_id = ta.track_id
                          AND lh.completed AND NOT lh.skipped)
    )
    SELECT ta.artist_id::text AS artist_uuid
      FROM engaged e
      JOIN track_artists ta ON ta.artist_id = e.artist_id
      LEFT JOIN artist_bios ab ON ab.artist_id = ta.artist_id
     WHERE NOT EXISTS (SELECT 1 FROM embeddings em
                        WHERE em.track_id = ta.track_id)
       AND NOT EXISTS (SELECT 1 FROM audio_features af
                        WHERE af.track_id = ta.track_id)
     GROUP BY ta.artist_id
     ORDER BY MAX(ab.listeners) ASC NULLS FIRST, ta.artist_id
"""


def get_rare_search_uuids(conn) -> list[str]:
    """The artists worth an exact DHT key: ENGAGED (an owned file or a
    completed, unskipped listen — the same gate as split_engaged and the
    similars pull) with a track this node holds no analysis for, rarest
    first (the announce tail's order — the popular ones are found through
    the node key, the rare ones are what a per-artist key is for).

    Engagement is the whole point. The audio gap set alone is, on a node
    with the phantom layer, the phantom layer: thousands of artists nobody
    owns, nobody has listened to and — since a tail announces only HELD
    analysis — nobody announces. Asking the DHT for those was 47 s of dead
    traversals per run for an answer of zero, every run."""
    return [r["artist_uuid"] for r in db_query(conn, RARE_SEARCH_SQL)]


def get_incomplete_artist_uuids(conn) -> list[str]:
    """
    Artist UUIDs with at least one track missing analysis in EITHER
    category — the trigger set for a sync run.

    Why broader than get_unenriched_artist_uuids: that one is the audio gap
    set between drains and uses AND-of-audio to keep DHT traffic down. At
    the start of a run we want to catch partial states — embeddings landed
    but features didn't (a transient pull failure). Inventory +
    _compute_needed at the peer/category level filter out anything we
    already have, so even a wide trigger here is cheap when there's
    nothing new to pull.

    Until 2026-09-19 a missing bio, tag list, similars or track stats was a
    trigger too; that layer is node-local now (Last.fm's API terms) and no
    peer can fill it.
    """
    rows = db_query(
        conn,
        """SELECT DISTINCT ta.artist_id::text AS artist_uuid
           FROM track_artists ta
           WHERE NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.track_id = ta.track_id)
              OR NOT EXISTS (SELECT 1 FROM audio_features af WHERE af.track_id = ta.track_id)""",
    )
    return [r["artist_uuid"] for r in rows]
