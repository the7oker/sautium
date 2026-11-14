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
from uuid import UUID

import psycopg2.extras

from desktop.p2p import record_sig, slice_trust
from desktop.p2p.bloom import BloomFilter

logger = logging.getLogger(__name__)

# Sync-protocol capabilities advertised on /health. Peers pick pull categories
# by this list: "segments" = this node serves per-track segment bundles
# (pull category `segments`, seals on the wire) — the one way audio
# analysis travels: the unsealed mean-vector pull is gone (2026-11-03), so a
# peer without the capability is asked for no audio at all. Mirrored by the
# FastAPI backend (backend/routers/sync.py SYNC_CAPABILITIES) — keep in step.
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
CARRY_CATEGORIES = ("segments", "audio_features", "instruments", "track_mbids")
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

EMPTY_INVENTORY = {"tracks": [], "embeddings": [], "audio_features": [], "instruments": []}


# Rows the sync protocol will hand out: sealed ones. The inventory and the
# holdings filter must draw from the same population, or a filter hit would
# be a promise the inventory then breaks.
_SIGNED = "signature IS NOT NULL AND batch_root IS NOT NULL"

# What this node HOLDS of a track's analysis: its own (a first-hand source),
# or what an author sealed. The sync's planner asks for no held record, its
# importers replace none, the carry admits none and the gap sets count none
# as a gap — one rule, so nothing is pulled only to be dropped; the one
# exception, a record of a newer methodology over an imported one of an
# older, is KEPT_RECORD's, read by the planner and the importers alike. An imported
# record whose seal a shed removed (a rename, a merge) is held by nobody: it
# is served to no peer and signed by nobody here, so its author's sealed
# version replaces it as it would fill a gap. A mean without segments under
# no first-hand source is a gap too. A grid linked to no source on a track
# with an elected analysis-source file is held: a legacy grid of its own,
# which the analysis pass re-derives from that file (its pending predicate
# takes an unlinked analysis) — a peer's grid in its place would stay there
# wherever enrichment.reanalyze_imported is off. A copy that is away counts:
# away is never gone (ROOT-1), and its pass reads the file once it is back.
# A track held only at an HQPlayer's library has no file a pass reads. Row-level, over embeddings `e` and
# a record table `a` (audio_features, track_instruments), each LEFT JOINed to analysis_sources `s`.
HELD_EMBEDDING = """((s.id IS NOT NULL AND NOT s.imported)
     OR (EXISTS (SELECT 1 FROM embedding_segments es WHERE es.embedding_id = e.id)
         AND (NOT EXISTS (SELECT 1 FROM embedding_segments es
                           WHERE es.embedding_id = e.id AND es.signature IS NULL)
              OR (s.id IS NULL
                  AND EXISTS (SELECT 1 FROM media_files f
                               WHERE f.track_id = e.track_id AND f.is_analysis_source)))))"""
HELD_RECORD = "(a.signature IS NOT NULL OR (s.id IS NOT NULL AND NOT s.imported))"
# What an import of a record of methodology {v} never replaces: this node's
# own record, or a sealed one of a methodology no older. So an imported
# record of a newer methodology replaces an imported one of an older —
# never a first-hand one — and the planner asks for no record the importer
# would drop. Row-level, as HELD_RECORD.
KEPT_RECORD = ("((s.id IS NOT NULL AND NOT s.imported)"
               " OR (a.signature IS NOT NULL AND a.analysis_version >= {v}))")
# The same per track, the track's uuid expression formatted in as {t}.
HOLDS_SEGMENTS = f"""EXISTS (SELECT 1 FROM embeddings e
                        LEFT JOIN analysis_sources s ON s.id = e.analysis_source_id
                        WHERE e.track_id = {{t}} AND {HELD_EMBEDDING})"""
HOLDS_FEATURES = f"""EXISTS (SELECT 1 FROM audio_features a
                        LEFT JOIN analysis_sources s ON s.id = a.analysis_source_id
                        WHERE a.track_id = {{t}} AND {HELD_RECORD})"""
HOLDS_INSTRUMENTS = f"""EXISTS (SELECT 1 FROM track_instruments a
                        LEFT JOIN analysis_sources s ON s.id = a.analysis_source_id
                        WHERE a.track_id = {{t}} AND {HELD_RECORD})"""


def get_inventory(conn, track_uuids: list[str]) -> dict:
    """
    What analysis this node serves for the given track UUIDs, and nothing
    else: the asker uses an inventory to plan its pulls, so it learns only
    what a pull would hand it — never which of its tracks exist here as rows
    without analysis (a probe could otherwise map this node's catalogue, the
    phantom tracks its history and its browsing minted among them).

    Returns category -> list, every list drawn from sealed records: the
    versioned categories as [uuid, analysis_version, sealed segment count]
    triples (embeddings, only those with a sealed segment) and [uuid,
    analysis_version] pairs (audio_features, instruments), so peers can
    re-pull rows produced by an older analysis methodology — existence alone
    never delivers upgrades; `tracks` is the asked uuids any of them names.
    The segment count lets a peer see how dense this node's grid is
    (densification / "deepen analysis" planning).

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

    embeddings = [
        [r["track_id"], r["v"], r["segs"]] for r in
        q("""SELECT e.track_id::text AS track_id,
                    MAX(e.analysis_version) AS v, COUNT(es.id) AS segs
             FROM embeddings e
             JOIN embedding_segments es ON es.embedding_id = e.id
                   AND es.signature IS NOT NULL AND es.batch_root IS NOT NULL
             WHERE e.track_id = ANY(%s::uuid[])
             GROUP BY e.track_id""")
    ]
    records = {}
    for category, table in (("audio_features", "audio_features"),
                            ("instruments", "track_instruments")):
        records[category] = [
            [r["track_id"], r["analysis_version"]] for r in
            q(f"""SELECT track_id::text AS track_id, analysis_version
                  FROM {table} WHERE track_id = ANY(%s::uuid[])
                    AND {_SIGNED}""")
        ]
    served = {row[0] for rows in (embeddings, *records.values()) for row in rows}
    return {"tracks": sorted(served), "embeddings": embeddings, **records}


# ---------------------------------------------------------------------------
# Holdings filter — the compressed inventory (P2P_NETWORK.md § Holdings filter)
# ---------------------------------------------------------------------------
# A node with millions of phantom gaps cannot ask every peer about all of
# them (306 inventory requests per peer per run at the master's size). The
# peer publishes instead: Bloom filters over the track uuids it HOLDS
# analysis for — `tracks` its segments or sealed features, `instruments` its
# sealed instruments, what its inventory serves — sized by its holdings,
# never by anyone's gaps. The asker
# tests its gaps locally, each against the filter of what it wants, and asks
# the exact inventory only about the hits: a track whose one gap is its
# instruments, which most peers holding its segments never made, costs a
# test of the second filter, not an inventory request every run. Built in
# memory when the holdings version moved (checked at most every
# _HOLDINGS_RECHECK_S), served from memory otherwise; a caller that already
# has the current version gets a stub, no bits. (Until 2026-09-19 a second
# filter named the artists with sealed bios/tags/similars — that layer is
# node-local now.)

HOLDINGS_FPR = 0.01
_HOLDINGS_RECHECK_S = 300

_holdings_lock = threading.Lock()
_holdings: dict = {"checked_at": 0.0, "version": None, "payload": None,
                   "tracks_n": 0, "instruments_n": 0}

_HOLDINGS_TRACKS_SQL = f"""
    SELECT e.track_id::text FROM embeddings e
      LEFT JOIN analysis_sources s ON s.id = e.analysis_source_id
     WHERE {HELD_EMBEDDING}
    UNION SELECT track_id::text FROM audio_features WHERE {_SIGNED}"""
_HOLDINGS_INSTRUMENTS_SQL = f"""
    SELECT track_id::text FROM track_instruments WHERE {_SIGNED}"""


def holdings_version(conn) -> str:
    """Moves whenever a contributing table's row count or latest updated_at
    moves, or the seals do — a grid held only while it is sealed (a shed
    unseals segments and touches no embeddings row): the unsealed segments
    are counted from their partial index; a shed of a record's seal moves
    its row's updated_at. One scan per table, so it is checked on a timer,
    not per request."""
    row = db_query(conn, f"""
        SELECT (SELECT count(*) FROM embeddings) AS e,
               (SELECT count(*) FROM audio_features WHERE {_SIGNED}) AS af,
               (SELECT count(*) FROM embedding_segments WHERE signature IS NULL) AS unsealed,
               (SELECT count(*) FROM track_instruments WHERE {_SIGNED}) AS ti,
               GREATEST((SELECT max(updated_at) FROM embeddings),
                        (SELECT max(updated_at) FROM audio_features),
                        (SELECT max(updated_at) FROM track_instruments)) AS latest
    """)[0]
    counts = [int(row[k]) for k in ("e", "af", "unsealed", "ti")]
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
    """The holdings payload: {"version", "tracks": filter, "instruments":
    filter}. `have` is the version the caller already holds — an unchanged
    one answers {"version", "unchanged": true}."""
    with _holdings_lock:
        now = time.monotonic()
        if (_holdings["payload"] is None
                or now - _holdings["checked_at"] > _HOLDINGS_RECHECK_S):
            version = holdings_version(conn)
            if version != _holdings["version"]:
                t0 = time.monotonic()
                tracks = _build_filter(conn, _HOLDINGS_TRACKS_SQL)
                instruments = _build_filter(conn, _HOLDINGS_INSTRUMENTS_SQL)
                _holdings.update(
                    version=version, tracks_n=tracks.n, instruments_n=instruments.n,
                    payload={"version": version, "tracks": tracks.to_dict(),
                             "instruments": instruments.to_dict()},
                )
                logger.info(
                    f"Holdings filter rebuilt: {tracks.n} tracks + "
                    f"{instruments.n} with instruments, "
                    f"{(len(tracks.bits) + len(instruments.bits)) // 1024} KB, "
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
    return {"version": _holdings["version"], "tracks": _holdings["tracks_n"],
            "instruments": _holdings["instruments_n"]}


def split_engaged(conn, artist_uuids: list[str]) -> tuple[list[str], list[str]]:
    """Partition artists into the ENGAGED core — an owned file (here, or in
    the library of the HQPlayer the node drives: owned_files) or a
    completed, unskipped listen — asked of every peer in full, and the
    phantom bulk, asked through the holdings filter."""
    if not artist_uuids:
        return [], []
    rows = db_query(conn, """
        SELECT DISTINCT ta.artist_id::text AS artist_uuid
          FROM track_artists ta
         WHERE ta.artist_id = ANY(%s::uuid[])
           AND (EXISTS (SELECT 1 FROM owned_files f
                         WHERE f.track_id = ta.track_id)
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

# Beside `batches`, every sealed payload carries an `authors` map (BIRTH-SYNC
# slice 2): for each author its seals name, the author's birth certificate in
# a slice entry's form (slice_trust.author_entries) — what lets a node that
# never meets the author witness it as a `carried` sighting
# (desktop/p2p/witness.py). `own` is this node's own (certificate, proof),
# passed by a server for the records it authored itself. Older peers ignore
# the field.

def _pull_simple(conn, category: str, sql: str, uuids: list[str],
                 post_process=None, own=None) -> dict:
    """Common handler for the sealed plain-row categories.

    Every SELECT here MUST carry the four seal columns plus fetched_at, and
    every row MUST arrive with the signing_batches rows its root names —
    a seal the importer cannot check is a seal it has to drop, and a record
    that arrives unsealed is a record nobody can be held to. The launcher
    used to serve these categories payload-only, which silently stripped
    authorship from everything a peer pulled from it.

    Rows without a seal are not served at all (see _SEALED_ONLY)."""
    if not uuids:
        return {"category": category, "items": [], "batches": {}, "authors": {}}
    rows = db_query(conn, sql, [uuids])
    items = [_serialize_row(r) for r in rows]
    if post_process:
        items = [post_process(item) for item in items]
    roots = {i["batch_root"] for i in items if i.get("batch_root")}
    return {"category": category, "items": items,
            "batches": _batches_map(conn, roots),
            "authors": slice_trust.author_entries(
                conn, {i.get("author_pubkey") for i in items}, own)}


# Appended to every enrichment SELECT: unsigned rows never leave this node.
# Distributing a claim nobody signed makes the network unable to attribute —
# and the seal columns are NULLed by the seal-guard trigger the moment a
# payload column changes, so "has a signature" also means "unmodified".
_SEALED_ONLY = " AND {t}.signature IS NOT NULL AND {t}.batch_root IS NOT NULL"


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


def pull_segments(conn, uuids: list[str], own=None) -> dict:
    """Per-track CLAP segment bundles with their seals — the signed, synced
    unit (the importer derives the mean locally; see record_sig.py). Vectors
    travel as base64 of the canonical float32-LE bytes so vector_hash verifies
    over the received bytes with no float re-derivation. The response-level
    `batches` map carries each seal's Worker timestamp so the full chain
    travels with the records and survives relay; `authors`, each author's
    certificate."""
    if not uuids:
        return {"category": "segments", "items": [], "batches": {}, "authors": {}}
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
    authors: set = set()
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
            authors.add(r["author_pubkey"])
        bundle["segments"].append(seg)

    return {"category": "segments", "items": list(items_by_track.values()),
            "batches": _batches_map(conn, roots),
            "authors": slice_trust.author_entries(conn, authors, own)}


def _pull_records(conn, category: str, table: str, fields: list, uuids: list[str],
                  own) -> dict:
    """A per-track record kind (audio_features, track_instruments): its
    record's fields, its methodology version and its seal, the material
    declaration the seal binds as `provenance`, a `batches` map for the
    Worker timestamps and `authors` for each author's certificate — rows
    travel sealed or not at all, and an imported row is re-served with its
    authorship intact."""
    if not uuids:
        return {"category": category, "items": [], "batches": {}, "authors": {}}
    rows = db_query(
        conn,
        f"""SELECT a.track_id::text AS track_uuid,
                   {", ".join(f"a.{c}" for c in fields)},
                   a.analysis_version,
                   a.author_pubkey, a.signature, a.batch_root, a.merkle_proof,
                   {_PROVENANCE_COLS}
            FROM {table} a
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
    return {"category": category, "items": items,
            "batches": _batches_map(conn, roots),
            "authors": slice_trust.author_entries(
                conn, {i["author_pubkey"] for i in items}, own)}


def pull_audio_features(conn, uuids: list[str], own=None) -> dict:
    """The features records (record_sig.features_payload)."""
    return _pull_records(conn, "audio_features", "audio_features",
                         record_sig.FEATURE_ORDER, uuids, own)


def pull_instruments(conn, uuids: list[str], own=None) -> dict:
    """The instruments records (record_sig.instruments_payload)."""
    return _pull_records(conn, "instruments", "track_instruments", ["instruments"],
                         uuids, own)


def pull_track_mbids(conn, track_uuids: list[str], own=None) -> dict:
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
        track_uuids, own=own,
    )


# Map category name -> pull function
PULL_HANDLERS = {
    "segments": pull_segments,
    "audio-features": pull_audio_features,
    "audio_features": pull_audio_features,
    "instruments": pull_instruments,
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


# The tables of per-track analysis records, each row linked to its source.
_ANALYSIS_TABLES = ("embeddings", "audio_features", "track_instruments")

def _analysis_of(imported: bool) -> str:
    """A track holds analysis of any kind under an imported source, or
    under one of this node's own; the track's uuid expression as {t}."""
    return "(" + " OR ".join(
        f"""EXISTS (SELECT 1 FROM {table} c_x
                      JOIN analysis_sources c_s ON c_s.id = c_x.analysis_source_id
                     WHERE c_x.track_id = {{t}} AND {"" if imported else "NOT "}c_s.imported)"""
        for table in _ANALYSIS_TABLES) + ")"


# A track this node CARRIES: it holds another author's analysis of it, of any
# kind — a phantom it streamed itself and was pushed the features or the
# instruments of included. What follows for it adds nothing to carry.
_CARRIED = _analysis_of(imported=True)
# Of those, the ones that spend the carry room: no analysis of this node's
# own. A phantom it streamed itself takes others' features or instruments
# without spending it (an instruments map is a kilobyte of a unit budgeted at
# ~46 KB) — as many as the owner streamed.
_OWN_ANALYSIS = _analysis_of(imported=False)


def count_carried_tracks(conn) -> int:
    """How many tracks this node holds another author's analysis for, of any
    kind, while owning no file and holding no analysis of its own — the
    honest meter for "disk spent on someone else's behalf". Analysis of
    tracks we DO own is not carrying, it is our own library, however it
    arrived; nor is what joins a phantom's own analysis."""
    rows = db_query(
        conn,
        "SELECT count(DISTINCT x.track_id) AS n FROM ("
        + " UNION ALL ".join(f"""SELECT r.track_id FROM {table} r
                                   JOIN analysis_sources s ON s.id = r.analysis_source_id
                                  WHERE s.imported""" for table in _ANALYSIS_TABLES)
        + f""") x WHERE NOT EXISTS (SELECT 1 FROM owned_files f WHERE f.track_id = x.track_id)
                 AND NOT {_OWN_ANALYSIS.format(t="x.track_id")}""",
    )
    return int(rows[0]["n"]) if rows else 0


def _is_uuid(value) -> bool:
    """A uuid as the database writes it, lower-case and hyphenated — not
    urn:, braces or bare hex, which UUID() takes and ::uuid does not, nor
    another spelling of one that would miss it in a lookup."""
    try:
        return str(UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False


def phantom_layer_on(conn) -> bool:
    """The owner's switch over the phantom layer (discovery.phantom_layer):
    on unless explicitly off — a missing row is the default
    (backend/routers/settings._DEFAULTS)."""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM user_settings WHERE key = 'discovery.phantom_layer'")
        row = cur.fetchone()
    return not (row is not None and row[0] is False)


# What a track lacks, per carry category — the test both the offer's answer
# and a push's admission make: no held record of it (HOLDS_*).
_LACKS = {
    "segments": "NOT " + HOLDS_SEGMENTS,
    "audio_features": "NOT " + HOLDS_FEATURES,
    "instruments": "NOT " + HOLDS_INSTRUMENTS,
    "track_mbids": "NOT EXISTS (SELECT 1 FROM track_mbids tm WHERE tm.track_id = {t})",
}


def wanted_for_push(conn, category: str, items: list, budget: int) -> list:
    """Of the items a peer PUSHED, the ones this node takes — what its own
    answer to an offer (wanted_tracks) would have named: a track on its
    tracklist with a recording MBID — for a binding, that very recording —,
    lacking the category, owned or, with the phantom layer on, phantom: one
    carried here already (_CARRIED: another author's analysis of it, of any
    kind, is here — what follows adds nothing to carry) or holding analysis
    of this node's own (_OWN_ANALYSIS: what joins it spends no room), or one
    more within the room the carry limit leaves (an owned track's gap-fill is
    never carrying either: count_carried_tracks counts neither).
    One item per track (per track and recording for a binding), but for a
    record category, whose every item of a track taken goes to the importer
    to choose from. No offer state is kept: the
    push is judged by the same rules as the offer. The room is read per
    push, and pushes in flight at once may each spend it: the limit is
    exceeded by at most what they carry together, a part each (900 KiB) —
    a soft ceiling, where serialising them would hold every thread that
    serves peers behind one import."""
    binding = category == "track_mbids"
    # A record category keeps every item of a track it takes: which one lands
    # is the importer's choice — its newest methodology among those whose
    # seal verifies, as for a pull of the same payload.
    records = category in ("audio_features", "instruments")
    keyed: dict = {}
    for it in items:
        key = (it.get("track_uuid"), it.get("recording_mbid") if binding else None)
        if _is_uuid(key[0]) and (not binding or _is_uuid(key[1])):
            keyed.setdefault(key, []).append(it)
    if not keyed or budget <= 0:
        return []
    room = budget - count_carried_tracks(conn)
    rows = db_query(conn, f"""
        WITH cand AS (
            SELECT p.id, p.rec,
                   EXISTS (SELECT 1 FROM owned_files f WHERE f.track_id = p.id) AS owned,
                   {_CARRIED.format(t="p.id")} AS carried,
                   {_OWN_ANALYSIS.format(t="p.id")} AS own_analysis
              FROM unnest(%(ids)s::uuid[], %(recs)s::uuid[]) AS p(id, rec)
             WHERE EXISTS (SELECT 1 FROM album_tracks at2
                            WHERE at2.track_id = p.id AND at2.recording_mbid IS NOT NULL
                              AND (p.rec IS NULL OR at2.recording_mbid = p.rec))
               AND {_LACKS[category].format(t="p.id")})
        SELECT id::text AS track_uuid, rec::text AS rec FROM cand
         WHERE owned OR ((carried OR own_analysis) AND %(phantoms)s)
        UNION ALL
        (SELECT id::text, rec::text FROM cand
          WHERE NOT owned AND NOT carried AND NOT own_analysis AND %(phantoms)s
          ORDER BY id LIMIT %(room)s)""",
        {"ids": [k[0] for k in keyed], "recs": [k[1] for k in keyed],
         "phantoms": phantom_layer_on(conn), "room": max(room, 0)})
    return [it for r in rows
            for it in keyed[(r["track_uuid"], r["rec"])][:None if records else 1]]


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

    mbids = recording_mbids[:CARRY_MAX_TRACKS]
    # The phantom layer is the owner's switch: off, only owned tracks are
    # wanted — their gap-fill is the node's own library. As a push is judged
    # (wanted_for_push): a phantom carried here already, or holding analysis
    # of this node's own, is wanted whatever the room — what it lacks adds
    # nothing to carry —, and the room is for the other phantoms.
    matched = db_query(
        conn,
        f"""WITH m AS (SELECT DISTINCT at2.track_id
                        FROM unnest(%(m)s::uuid[]) AS o(mbid)
                        JOIN album_tracks at2 ON at2.recording_mbid = o.mbid),
                c AS (SELECT m.track_id,
                             EXISTS (SELECT 1 FROM owned_files f
                                      WHERE f.track_id = m.track_id) AS owned,
                             {_CARRIED.format(t="m.track_id")} AS carried,
                             {_OWN_ANALYSIS.format(t="m.track_id")} AS own_analysis
                        FROM m)
           SELECT track_id::text AS track_uuid FROM c
            WHERE owned OR ((carried OR own_analysis) AND %(p)s)
           UNION ALL
           (SELECT track_id::text FROM c
             WHERE NOT owned AND NOT carried AND NOT own_analysis AND %(p)s
             ORDER BY track_id LIMIT %(room)s)""",
        {"m": mbids, "p": phantom_layer_on(conn), "room": max(room, 0)},
    )
    uuids = [r["track_uuid"] for r in matched]
    if not uuids:
        return empty

    out = dict(empty)
    for category in CARRY_CATEGORIES:
        out[category] = [r["track_uuid"] for r in db_query(
            conn,
            f"""SELECT u.id::text AS track_uuid
                  FROM unnest(%s::uuid[]) AS u(id)
                 WHERE {_LACKS[category].format(t="u.id")}""",
            [uuids])]
    return out


ANNOUNCE_TAIL_SQL = f"""
    WITH analyzed AS (
        SELECT e.track_id FROM embeddings e
          LEFT JOIN analysis_sources s ON s.id = e.analysis_source_id
         WHERE {HELD_EMBEDDING}
        UNION
        SELECT a.track_id FROM audio_features a
          LEFT JOIN analysis_sources s ON s.id = a.analysis_source_id
         WHERE {HELD_RECORD}
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


# A track's audio analysis, as the gap sets count it: what this node holds
# (HOLDS_SEGMENTS, HOLDS_FEATURES, HOLDS_INSTRUMENTS). A mean without sealed
# segments — what the unsealed mean pull once left behind, or an imported grid
# a shed unsealed — is a gap: sealed segments replace it at the next pull.
_ANALYSED = HOLDS_SEGMENTS.format(t="ta.track_id")
_FEATURED = HOLDS_FEATURES.format(t="ta.track_id")
_INSTRUMENTED = HOLDS_INSTRUMENTS.format(t="ta.track_id")


RARE_SEARCH_SQL = f"""
    WITH engaged AS (
        SELECT DISTINCT ta.artist_id
          FROM track_artists ta
         WHERE EXISTS (SELECT 1 FROM owned_files f
                        WHERE f.track_id = ta.track_id)
            OR EXISTS (SELECT 1 FROM listening_history lh
                        WHERE lh.track_id = ta.track_id
                          AND lh.completed AND NOT lh.skipped)
    )
    SELECT ta.artist_id::text AS artist_uuid
      FROM engaged e
      JOIN track_artists ta ON ta.artist_id = e.artist_id
      LEFT JOIN artist_bios ab ON ab.artist_id = ta.artist_id
     WHERE NOT {_ANALYSED}
       AND NOT {_FEATURED}
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
    Artist UUIDs with at least one track missing analysis in ANY category —
    segments, features, instruments — the trigger set for a sync run, and
    what every peer of it is asked about (since 2026-11-10 the internet
    tier's drain and the master hint too, not only the manual and LAN
    peers): partial states as well — embeddings landed but features didn't
    (a transient pull failure, an imported track whose features the records'
    cutover deleted), or an author's instrument pass came days after its
    sonic one. Only the DHT's exact keys keep to a track holding neither
    (get_rare_search_uuids): a partial gap costs no DHT traffic. Inventory +
    _compute_needed at the peer/category level filter out anything we
    already have, the peers' instruments filters keep an instruments gap no
    peer can fill a local test, and a peer that had nothing for us is left
    alone for the walk's EMPTY_PEER_TTL, so even a wide set here is cheap
    when there's nothing new to pull.

    Until 2026-09-19 a missing bio, tag list, similars or track stats was a
    trigger too; that layer is node-local now (Last.fm's API terms) and no
    peer can fill it.
    """
    rows = db_query(
        conn,
        f"""SELECT DISTINCT ta.artist_id::text AS artist_uuid
           FROM track_artists ta
           WHERE NOT {_ANALYSED} OR NOT {_FEATURED} OR NOT {_INSTRUMENTED}""",
    )
    return [r["artist_uuid"] for r in rows]
