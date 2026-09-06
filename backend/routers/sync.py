"""
P2P Sync API endpoints.

Provides inventory and data pull endpoints for synchronizing
enrichment data between Sautium nodes.

Protocol:
  1. POST /api/sync/inventory  — what enrichment data is available for given tracks?
  2. POST /api/sync/pull/{category} — retrieve enrichment data by UUIDs (batched)
  3. POST /api/mb/slice — raw mb_* rows for artist names (dump holders only)

Lyrics are deliberately NOT part of the protocol (2026-07-11): the one
category that is verbatim copyrighted text — every node fetches its own from
the public sources. Audio analysis travels as SEGMENTS with their seals
(pull category `segments`); the track-level mean is derived locally by the
importer and the legacy `embeddings` mean pull remains only for peers
without the `segments` capability.
"""

import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import record_sig
from config import settings
from db_pool import db_query as _db_query, db_query_one as _db_query_one, get_conn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sync", tags=["sync"])

# Sync-protocol capabilities advertised on /health. Mirrors the launcher
# server's list (desktop/p2p/sync_queries.py CAPABILITIES) — keep in step.
SYNC_CAPABILITIES = ["segments"]

# A track bundle is K=12..24 vectors, each ~2.7KB base64 + ~1.3KB proof —
# segments pulls get a tighter per-request cap than plain-row categories.
SEGMENTS_MAX_UUIDS = 500

# "P2P sharing" in Settings. Until now nothing read it: the switch was
# written to user_settings and never consulted, so a node kept serving its
# whole catalogue to any caller after its owner had explicitly turned
# sharing off. These endpoints are unauthenticated by design (the peer
# protocol has no login), which makes the switch the ONLY thing standing
# between "off" and a full inventory dump — it has to actually work.
# Cached briefly: a sync run fires many pulls and each would otherwise cost
# a settings query.
_SHARING_TTL = 10.0
_sharing_cache: tuple[float, bool] = (0.0, True)


def sharing_enabled() -> bool:
    import time as _time
    global _sharing_cache
    now = _time.monotonic()
    ts, val = _sharing_cache
    if now - ts < _SHARING_TTL:
        return val
    try:
        from routers.settings import _read
        val = bool(_read("sync.p2p_enabled"))
    except Exception as e:                       # settings unreadable: fail closed
        logger.warning(f"sharing flag unreadable ({e}) — refusing to serve")
        val = False
    _sharing_cache = (now, val)
    return val


def _require_sharing() -> None:
    if not sharing_enabled():
        raise HTTPException(status_code=403, detail="sharing disabled")

# Single source: desktop/p2p/mb_slice_queries.py. In Docker the desktop/p2p
# dir is bind-mounted at /app/desktop_p2p; a native repo run finds it at
# ../desktop/p2p. Loaded by file path (not sys.path) so desktop modules can
# never shadow backend ones. Absent → the endpoint 404s (launcher-mode
# backends serve slices through the launcher's own sync server instead).
def _load_mb_slice_queries():
    import importlib.util
    here = Path(__file__).parent.parent
    for candidate in (here / "desktop_p2p" / "mb_slice_queries.py",
                      here.parent / "desktop" / "p2p" / "mb_slice_queries.py"):
        if candidate.exists():
            spec = importlib.util.spec_from_file_location(
                "mb_slice_queries", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    return None


mb_slice_queries = _load_mb_slice_queries()


# Push-seeding needs two things the pull side never did: the carry SQL and
# the import gate. Both are taken from the launcher tree rather than mirrored
# here — the gate is what verifies a seal before a stranger's row lands in
# our DB, and a second copy of a verification gate is the copy that
# eventually drifts open. The carry SQL comes along for the same reason in
# miniature: if the two surfaces disagreed on what "already held" means, a
# pusher's budget accounting would be a lie. In Docker the tree is
# bind-mounted at /app/desktop; a native repo run finds it one level up.
# Absent → we simply never advertise "carry".
def _load_carry():
    import importlib
    import sys
    here = Path(__file__).parent.parent
    for root in (here, here.parent):
        if (root / "desktop" / "sync_client.py").exists():
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            try:
                return (importlib.import_module("desktop.p2p.sync_queries"),
                        importlib.import_module("desktop.sync_client"))
            except Exception as e:
                logger.warning(f"carry unavailable: {e}")
                break
    return None, None


carry_queries, carry_importer = _load_carry()
if carry_importer is not None:
    SYNC_CAPABILITIES.append("carry")
if carry_queries is not None:
    # The holdings filter lives in the same shared module — same reason:
    # the two surfaces must agree on what "held" means.
    SYNC_CAPABILITIES.append("holdings")


def _carry_budget() -> int:
    """How many foreign artists this node is willing to hold. 0 = don't
    carry."""
    try:
        from routers.settings import _read
        return int(_read("sync.carry_limit") or 0)
    except Exception as e:
        logger.warning(f"carry budget unreadable ({e}) — not carrying")
        return 0


class CarryOffer(BaseModel):
    recordings: list[str] = Field(default_factory=list, max_length=10000)


@router.post("/offer")
async def carry_offer(req: CarryOffer) -> dict:
    """"Here are the recordings I could give you" — we answer with OUR
    track uuids we actually want (carry v4).

    The round trip exists so a pusher never ships what we already hold or
    never cared about: 16 bytes per recording to ask, ~46 KB per track to
    send blind."""
    _require_sharing()
    if carry_queries is None:
        return {"wanted": {}}
    with get_conn() as conn:
        wanted = carry_queries.wanted_tracks(
            conn, req.recordings, _carry_budget())
    return {"wanted": wanted}


@router.post("/push/{category}")
async def carry_push(category: str, payload: dict) -> dict:
    """Accept a pushed payload — byte-for-byte what pull/{category} returns,
    so the peer serialises once and we verify through the ordinary import
    gate. A push is unsolicited, which is exactly why nothing here is
    trusted: every record must carry a seal that checks out, or the importer
    drops it."""
    _require_sharing()
    if carry_importer is None:
        raise HTTPException(status_code=404, detail="carry not supported")
    if _carry_budget() <= 0:
        raise HTTPException(status_code=403, detail="not carrying")

    category = category.replace("-", "_")
    if category not in carry_queries.CARRY_CATEGORIES:
        raise HTTPException(
            status_code=404, detail=f"category not carryable: {category}")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) > 10000:
        raise HTTPException(status_code=400, detail="items must be a bounded list")

    import asyncio
    from functools import partial
    imported = await asyncio.get_event_loop().run_in_executor(
        None, partial(carry_importer.import_pushed,
                      settings.database_url, category, payload))
    if imported:
        logger.info(f"Carrying {imported} {category} record(s) pushed by a peer")
    return {"imported": imported}


mb_router = APIRouter(prefix="/api/mb", tags=["sync"])


def mb_dump_version() -> Optional[str]:
    """Full-dump version this node can serve slices from, or None. Requires
    the VERSION marker (MB_DUMP_DIR) AND mb_artist rows — see
    mb_slice_queries.local_dump_available."""
    if mb_slice_queries is None:
        return None
    try:
        with get_conn() as conn:
            return mb_slice_queries.local_dump_available(conn)
    except Exception as e:
        logger.debug(f"MB dump capability check failed: {e}")
        return None


# The backend's own Ed25519 node identity — a Docker deployment has no
# launcher identity dir, so authorship of served slices is anchored to this
# key instead. Lives beside .api_secret (backend/data/, bind-mounted →
# survives container recreates). Lazy: generated on first use.
_NODE_KEY_PATH = Path(__file__).parent.parent / "data" / ".node_key"


def _node_signing_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if _NODE_KEY_PATH.exists():
        seed = bytes.fromhex(_NODE_KEY_PATH.read_text().strip())
    else:
        key = Ed25519PrivateKey.generate()
        from cryptography.hazmat.primitives import serialization
        seed = key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        _NODE_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _NODE_KEY_PATH.write_text(seed.hex())
        os.chmod(_NODE_KEY_PATH, 0o600)
        logger.info("Generated backend node identity (.node_key)")
    return Ed25519PrivateKey.from_private_bytes(seed)


def node_signing_key():
    """The key this node is KNOWN BY on the peer surface: the account key
    when configured — then /health node_id, receipt authorship, the DHT
    user announce and the chat identity all name the same key — with the
    random .node_key as the bare-backend fallback."""
    try:
        from p2p_identity import load_signing_key
        key = load_signing_key(settings)
        if key is not None:
            return key
    except Exception as e:
        logger.debug(f"Account signing key unavailable: {e}")
    return _node_signing_key()


def node_pubkey_hex() -> Optional[str]:
    from cryptography.hazmat.primitives import serialization
    try:
        return node_signing_key().public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()
    except Exception as e:
        logger.warning(f"Backend node key unavailable: {e}")
        return None




class MBSliceRequest(BaseModel):
    names: list[str] = Field(default_factory=list, max_length=50)


@mb_router.get("/search")
def mb_search(q: str = Query(..., min_length=2, max_length=255)) -> dict:
    """Artist candidates from the FULL local dump — mirrors
    desktop/p2p/sync_server.handle_mb_search. Full-dump only: a replica's
    partial mb_* world would answer "not found" for names it never saw.
    Indexed-only matching keeps the volunteer cost of one human search
    at a few index probes."""
    _require_sharing()
    if not mb_dump_version():
        raise HTTPException(status_code=404, detail="no full dump")
    with get_conn() as conn:
        return {"artists": mb_slice_queries.search_artists(conn, q)}


@mb_router.post("/slice")
def mb_slice(req: MBSliceRequest) -> dict:
    """Per-name signed blobs (v2) — mirrors
    desktop/p2p/sync_server.handle_mb_slice. Dump holders compute+sign+
    cache misses; a replica answers what it holds, misses land in
    `missing`. Blobs carry the ORIGINAL author's signature either way."""
    _require_sharing()
    sign_fn, author = None, ""
    if mb_dump_version():
        try:
            key = node_signing_key()
            author = node_pubkey_hex() or ""
            sign_fn = key.sign
        except Exception as e:
            logger.warning(f"MB slice signing unavailable: {e}")
    try:
        with get_conn() as conn:
            mb_slice_queries.ensure_blob_table(conn)
            return mb_slice_queries.serve_slices(
                conn, req.names, sign_fn, author)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except mb_slice_queries.DumpBusy:
        raise HTTPException(status_code=503, detail="dump_reloading")



# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class InventoryRequest(BaseModel):
    # Same ceiling as the launcher sync server (MAX_UUIDS_PER_REQUEST) — the
    # client chunks its library into ≤10k slices and merges the responses.
    track_uuids: list[str] = Field(default_factory=list, max_length=10000)
    # Artists to answer the artist-level categories for directly — the
    # holdings-filter path names phantom artists whose tracks all missed
    # the track filter but who may still have a bio, tags or similars here.
    artist_uuids: list[str] = Field(default_factory=list, max_length=10000)


class PullRequest(BaseModel):
    uuids: list[str] = Field(default_factory=list, max_length=10000)


# ---------------------------------------------------------------------------
# Inventory endpoint
# ---------------------------------------------------------------------------

_EMPTY_INVENTORY = {
    "tracks": [], "embeddings": [],
    "audio_features": [], "track_stats": [],
    "artists": [], "artist_bios": [],
    "artist_tags": [], "similar_artists": [],
    "genres": [], "genre_descriptions": [],
}


@router.post("/inventory")
async def get_inventory(req: InventoryRequest) -> dict:
    """
    Check what enrichment data is available for the given track UUIDs.

    The requester sends track UUIDs from their library.
    The response contains categorized UUID lists indicating which
    enrichment data this node can provide.

    Uses 3 consolidated CTE queries instead of 15 separate ones.
    """
    _require_sharing()
    if not req.track_uuids and not req.artist_uuids:
        return dict(_EMPTY_INVENTORY)

    uuids = req.track_uuids

    try:
        # Query 1: Track-level + genre data (7 categories, 1 round-trip)
        track_row = _db_query_one("""
            WITH uuids AS (SELECT unnest(%(u)s::uuid[]) AS id)
            SELECT
                ARRAY(SELECT t.id::text FROM tracks t
                      WHERE t.id IN (SELECT id FROM uuids)) AS tracks,
                (SELECT COALESCE(json_agg(json_build_array(x.tid, x.v, x.segs)), '[]'::json)
                 FROM (SELECT e.track_id::text AS tid, MAX(e.analysis_version) AS v,
                              COUNT(es.id) AS segs
                       FROM embeddings e
                       LEFT JOIN embedding_segments es ON es.embedding_id = e.id
                             AND es.signature IS NOT NULL
                             AND es.batch_root IS NOT NULL
                       WHERE e.track_id IN (SELECT id FROM uuids)
                       GROUP BY e.track_id) x) AS embeddings,
                (SELECT COALESCE(json_agg(json_build_array(af.track_id::text, af.analysis_version)), '[]'::json)
                 FROM audio_features af
                 WHERE af.track_id IN (SELECT id FROM uuids)
                   AND af.signature IS NOT NULL
                   AND af.batch_root IS NOT NULL) AS audio_features,
                ARRAY(SELECT DISTINCT ts.track_id::text FROM track_stats ts
                      WHERE ts.track_id IN (SELECT id FROM uuids)
                        AND ts.signature IS NOT NULL
                        AND ts.batch_root IS NOT NULL) AS track_stats,
                ARRAY(SELECT DISTINCT ag.genre_id::text FROM album_genres ag
                      JOIN album_variants av ON av.album_id = ag.album_id
                      JOIN media_files mf ON mf.album_variant_id = av.id
                      WHERE mf.track_id IN (SELECT id FROM uuids)) AS genres,
                ARRAY(SELECT DISTINCT gd.genre_id::text FROM genre_descriptions gd
                      JOIN album_genres ag ON ag.genre_id = gd.genre_id
                      JOIN album_variants av ON av.album_id = ag.album_id
                      JOIN media_files mf ON mf.album_variant_id = av.id
                      WHERE mf.track_id IN (SELECT id FROM uuids)
                        AND gd.signature IS NOT NULL
                        AND gd.batch_root IS NOT NULL) AS genre_descriptions
        """, {"u": uuids})

        # Query 2: Artist data (5 categories, 1 round-trip)
        artist_row = _db_query_one("""
            WITH uuids AS (SELECT unnest(%(u)s::uuid[]) AS id),
                 rel AS (SELECT DISTINCT ta.artist_id FROM track_artists ta
                         WHERE ta.track_id IN (SELECT id FROM uuids)
                         UNION SELECT unnest(%(a)s::uuid[]))
            SELECT
                ARRAY(SELECT artist_id::text FROM rel) AS artists,
                ARRAY(SELECT DISTINCT ab.artist_id::text FROM artist_bios ab
                      WHERE ab.artist_id IN (SELECT artist_id FROM rel)
                        AND ab.signature IS NOT NULL
                        AND ab.batch_root IS NOT NULL) AS artist_bios,
                ARRAY(SELECT DISTINCT at2.artist_id::text FROM artist_tags at2
                      WHERE at2.artist_id IN (SELECT artist_id FROM rel)
                        AND at2.signature IS NOT NULL
                        AND at2.batch_root IS NOT NULL) AS artist_tags,
                ARRAY(SELECT DISTINCT sa.artist_id::text FROM similar_artists sa
                      WHERE sa.artist_id IN (SELECT artist_id FROM rel)
                        AND sa.signature IS NOT NULL
                        AND sa.batch_root IS NOT NULL) AS similar_artists
        """, {"u": uuids, "a": req.artist_uuids})

        return {
            "tracks": track_row["tracks"],
            "embeddings": track_row["embeddings"],
            "audio_features": track_row["audio_features"],
            "track_stats": track_row["track_stats"],
            "artists": artist_row["artists"],
            "artist_bios": artist_row["artist_bios"],
            "artist_tags": artist_row["artist_tags"],
            "similar_artists": artist_row["similar_artists"],
            "genres": track_row["genres"],
            "genre_descriptions": track_row["genre_descriptions"],
        }

    except Exception as e:
        logger.error(f"Inventory query failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/holdings")
async def get_holdings(have: Optional[str] = None) -> dict:
    """The holdings filters — what this node holds, compressed (Bloom
    filters over sealed track and artist uuids), so a peer with millions
    of gaps tests them locally and asks /inventory only about the hits.
    `have` = the version the caller already holds; unchanged answers with
    a stub. Logic in desktop/p2p/sync_queries.get_holdings, shared with
    the launcher surface."""
    _require_sharing()
    if carry_queries is None:
        raise HTTPException(status_code=404, detail="holdings unavailable")
    import asyncio

    def _run():
        with get_conn() as conn:
            return carry_queries.get_holdings(conn, have)

    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        logger.error(f"Holdings query failed: {e}")
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
    """Common handler for pull endpoints.

    Any category whose rows carry a batch_root also ships the signing_batches
    rows those roots name — the importer cannot verify a Merkle inclusion or a
    Worker timestamp without them, and a seal it cannot check is a seal it must
    drop."""
    _require_sharing()
    if not uuids:
        return {"category": category, "items": []}
    try:
        rows = _db_query(sql, [uuids])
        items = [_serialize_row(r) for r in rows]
        if post_process:
            items = [post_process(item) for item in items]
        roots = {i["batch_root"] for i in items if i.get("batch_root")}
        if roots:
            return {"category": category, "items": items,
                    "batches": _batches_map(roots)}
        return {"category": category, "items": items}
    except Exception as e:
        logger.error(f"Pull {category} failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Pull endpoints
# ---------------------------------------------------------------------------

@router.post("/pull/tracks")
async def pull_tracks(req: PullRequest) -> dict:
    """Pull track metadata with associated artists."""
    _require_sharing()
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

        # Group artists by track
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

    except Exception as e:
        logger.error(f"Pull tracks failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _provenance_item(r: dict) -> Optional[dict]:
    """Nested provenance payload from p_-prefixed LEFT JOIN columns; None for
    rows not linked to an analysis_sources row (legacy / failed fingerprints).

    Carries the material declaration the signature commits to, and nothing
    that describes the author's copy of it. `origin`, `sample_rate` and
    `bit_depth` are deliberately NOT sent: a signature already says "I had
    this audio", and those three would turn it into "I hold this FILE" —
    origin says so outright, and a 96kHz/24-bit source says it just as
    plainly, since no streaming tier serves hi-res. Withholding them leaves
    a local rip and a lossless stream indistinguishable on the wire, which
    is the same possession-privacy line the three-tier signing policy draws.
    None of the three is part of the signed payload, so seals are unaffected.
    `is_lossless` stays: Deezer FLAC is lossless too, so it grades analysis
    quality without implying a file."""
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


def _batches_map(roots: set) -> dict:
    """signing_batches rows for the referenced Merkle roots, serialized so the
    importer can verify the Worker timestamp: worker_date is re-rendered as the
    exact seconds-precision UTC string the Worker signed."""
    if not roots:
        return {}
    rows = _db_query(
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


@router.post("/pull/segments")
async def pull_segments(req: PullRequest) -> dict:
    """Per-track CLAP segment bundles with their seals — the signed, synced
    unit (the importer derives the mean locally; see record_sig.py). Vectors
    travel as base64 of the canonical float32-LE bytes so vector_hash verifies
    over the received bytes; the `batches` map carries the Worker timestamps.
    Mirrors desktop/p2p/sync_queries.pull_segments."""
    _require_sharing()
    if not req.uuids:
        return {"category": "segments", "items": [], "batches": {}}
    if len(req.uuids) > SEGMENTS_MAX_UUIDS:
        raise HTTPException(
            status_code=400,
            detail=f"segments pull is capped at {SEGMENTS_MAX_UUIDS} tracks per request")

    try:
        rows = _db_query(
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
            [req.uuids],
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
                "batches": _batches_map(roots)}

    except Exception as e:
        logger.error(f"Pull segments failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/pull/embeddings")
async def pull_embeddings(req: PullRequest) -> dict:
    """Legacy mean-vector pull — kept for peers without the `segments`
    capability. Capable peers pull `segments` and derive the mean locally."""
    _require_sharing()
    if not req.uuids:
        return {"category": "embeddings", "items": []}

    try:
        rows = _db_query(
            f"""SELECT e.track_id::text AS track_uuid,
                       em.id::text AS model_uuid, em.name AS model_name,
                       e.vector::text AS vector, e.analysis_version,
                       {_PROVENANCE_COLS}
                FROM embeddings e
                INNER JOIN embedding_models em ON em.id = e.model_id
                LEFT JOIN analysis_sources s ON s.id = e.analysis_source_id
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
                "analysis_version": r["analysis_version"],
                "provenance": _provenance_item(r),
            })

        return {"category": "embeddings", "items": items}

    except Exception as e:
        logger.error(f"Pull embeddings failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/pull/audio-features")
async def pull_audio_features(req: PullRequest) -> dict:
    """Pull audio analysis features with their provenance. Rows travel WITH
    their seals + a `batches` map (Worker timestamps) — imported rows stay
    verifiable and re-servable with authorship intact."""
    _require_sharing()
    if not req.uuids:
        return {"category": "audio_features", "items": [], "batches": {}}

    try:
        rows = _db_query(
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
            [req.uuids],
        )
        items, roots = [], set()
        for r in rows:
            item = {k: v for k, v in r.items() if not k.startswith("p_")}
            item["provenance"] = _provenance_item(r)
            roots.add(item["batch_root"])
            items.append(item)
        return {"category": "audio_features", "items": items,
                "batches": _batches_map(roots)}

    except Exception as e:
        logger.error(f"Pull audio features failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/pull/track-stats")
async def pull_track_stats(req: PullRequest) -> dict:
    """Pull track statistics (listeners, playcount)."""
    return _pull_handler(
        "track_stats",
        """SELECT track_id::text AS track_uuid, source, listeners, playcount,
                  fetched_at, author_pubkey, signature, batch_root, merkle_proof
           FROM track_stats
           WHERE track_id = ANY(%s::uuid[])
             AND signature IS NOT NULL
             AND batch_root IS NOT NULL""",
        req.uuids,
    )


@router.post("/pull/artist-bios")
async def pull_artist_bios(req: PullRequest) -> dict:
    """Pull artist biographies."""
    return _pull_handler(
        "artist_bios",
        """SELECT ab.artist_id::text AS artist_uuid, a.name AS artist_name,
                  ab.source, ab.summary, ab.content, ab.url,
                  ab.listeners, ab.playcount, ab.fetched_at,
                  ab.author_pubkey, ab.signature, ab.batch_root, ab.merkle_proof
           FROM artist_bios ab
           INNER JOIN artists a ON a.id = ab.artist_id
           WHERE ab.artist_id = ANY(%s::uuid[])
             AND ab.signature IS NOT NULL
             AND ab.batch_root IS NOT NULL""",
        req.uuids,
    )


@router.post("/pull/artist-tags")
async def pull_artist_tags(req: PullRequest) -> dict:
    """Pull artist tags with tag names and weights."""
    return _pull_handler(
        "artist_tags",
        """SELECT at2.artist_id::text AS artist_uuid,
                  t.id::text AS tag_uuid, t.name AS tag_name,
                  at2.weight, at2.source, at2.fetched_at,
                  at2.author_pubkey, at2.signature, at2.batch_root, at2.merkle_proof
           FROM artist_tags at2
           INNER JOIN tags t ON t.id = at2.tag_id
           WHERE at2.artist_id = ANY(%s::uuid[])
             AND at2.signature IS NOT NULL
             AND at2.batch_root IS NOT NULL""",
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
                  sa.match_score::float, sa.source, sa.fetched_at,
                  sa.author_pubkey, sa.signature, sa.batch_root, sa.merkle_proof
           FROM similar_artists sa
           INNER JOIN artists a ON a.id = sa.similar_artist_id
           WHERE sa.artist_id = ANY(%s::uuid[])
             AND sa.signature IS NOT NULL
             AND sa.batch_root IS NOT NULL""",
        req.uuids,
    )



@router.post("/pull/genre-descriptions")
async def pull_genre_descriptions(req: PullRequest) -> dict:
    """Pull genre descriptions."""
    return _pull_handler(
        "genre_descriptions",
        """SELECT gd.genre_id::text AS genre_uuid, g.name AS genre_name,
                  gd.source, gd.summary, gd.content, gd.url, gd.fetched_at,
                  gd.author_pubkey, gd.signature, gd.batch_root, gd.merkle_proof
           FROM genre_descriptions gd
           INNER JOIN genres g ON g.id = gd.genre_id
           WHERE gd.genre_id = ANY(%s::uuid[])
             AND gd.signature IS NOT NULL
             AND gd.batch_root IS NOT NULL""",
        req.uuids,
    )
