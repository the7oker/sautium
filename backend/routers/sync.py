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
the public sources. Neither is anything Last.fm answered — bios, tags,
similars, track stats, genre descriptions (since 2026-09-19): its API terms
do not allow redistribution, so that layer is node-local and every node
fetches its own by name. Audio analysis travels as SEGMENTS with their seals
(pull category `segments`), the only way it travels; the track-level mean is
derived locally by the importer. The inventory and the pulls are the shared
handlers of desktop/p2p/sync_queries.py, which the launcher's sync server
serves too — one copy of what the two surfaces answer.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from config import settings
from db_pool import get_conn, in_peer_lane
from desktop.p2p import wire

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sync", tags=["sync"])

# Sync-protocol capabilities advertised on /health. Mirrors the launcher
# server's list (desktop/p2p/sync_queries.py CAPABILITIES) — keep in step.
SYNC_CAPABILITIES = ["segments"]

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
# Moved on by every change of the switch: a read begun before one is not kept
_sharing_generation = 0


def sharing_enabled() -> bool:
    import time as _time
    global _sharing_cache
    now = _time.monotonic()
    ts, val = _sharing_cache
    if now - ts < _SHARING_TTL:
        return val
    generation = _sharing_generation
    try:
        from routers.settings import _read
        val = bool(_read("sync.p2p_enabled"))
    except Exception as e:                       # settings unreadable: fail closed
        logger.warning(f"sharing flag unreadable ({e}) — refusing to serve")
        val = False
    if generation == _sharing_generation:
        _sharing_cache = (now, val)
    return val


def sharing_changed() -> None:
    """The owner moved the switch (routers/settings.put_sync_prefs): serving,
    and what /health says of it, follow at once — not at the cache's expiry."""
    global _sharing_cache, _sharing_generation
    _sharing_generation += 1
    _sharing_cache = (0.0, True)


def _require_sharing() -> None:
    if not sharing_enabled():
        raise HTTPException(status_code=403, detail="sharing disabled")

# Single source: desktop/p2p/mb_slice_queries.py. In Docker the desktop/p2p
# dir is bind-mounted at /app/desktop_p2p; a native repo run finds it at
# ../desktop/p2p. Loaded by file path (not sys.path) so desktop modules can
# never shadow backend ones. Absent → the endpoint 404s (launcher-mode
# backends serve slices through the launcher's own sync server instead).
def _requester(request: Request) -> Optional[str]:
    """Whose share of a dump node's build budget a request spends: its
    address — None for a shared one (serving.SHARED_ADDRESSES), which only
    the budget's total bounds."""
    from serving import is_shared
    addr = request.client.host if request.client else ""
    return None if is_shared(addr) else addr


# What the peer connections may hold for their peers together before a new
# bulk answer waits (serving.owed_bytes): each reader above the floor keeps
# its whole answer buffered for as long as it reads, and the sum had no
# bound. A ceiling on starting, not a cap: answers already being made count
# once written, so ones begun together can pass it.
OWED_MAX = 256 * 1024 * 1024


async def _answer(request: Request, run) -> Response:
    """`run()`'s answer, made on the peer lane with the work that produced
    it (wire.render): a bulk answer is tens of MB, and its JSON and gzip on
    the loop held every other request meanwhile, the Web UI's too. Not made
    while the peer connections owe their peers OWED_MAX: a 503 that says
    when to come back (requesters wait on it)."""
    from serving import owed_bytes
    if owed_bytes() > OWED_MAX:
        return JSONResponse({"detail": "busy"}, status_code=503, headers={"Retry-After": "30"})
    gzip_ok = "gzip" in request.headers.get("accept-encoding", "")
    body, encoding = await in_peer_lane(lambda: wire.render(run(), gzip_ok))
    return Response(body, media_type="application/json",
                    headers={"Content-Encoding": encoding} if encoding else None)


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


# The ListenBrainz slice protocol is imported as a package module (the walk
# already is, since 2026-09-08 — /app is on sys.path via _load_carry): one
# module object per process, never a second file-path loader. Absent → the
# endpoint 404s and /health advertises no lb fields.
def _load_lb_slice_queries():
    try:
        import importlib
        return importlib.import_module("desktop.p2p.lb_slice_queries")
    except Exception as e:
        logger.warning(f"LB slices unavailable: {e}")
        return None


lb_slice_queries = _load_lb_slice_queries()
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
async def carry_offer(req: CarryOffer, request: Request) -> Response:
    """"Here are the recordings I could give you" — we answer with OUR
    track uuids we actually want (carry v4).

    The round trip exists so a pusher never ships what we already hold or
    never cared about: 16 bytes per recording to ask, ~46 KB per track to
    send blind."""
    _require_sharing()
    if carry_queries is None:
        return {"wanted": {}}

    def _run():
        with get_conn() as conn:
            return carry_queries.wanted_tracks(conn, req.recordings, _carry_budget())

    return await _answer(request, lambda: {"wanted": _run()})


def _carried(cert: dict, author: str, via: str) -> None:
    """An author's certificate a peer's push carried: a `carried` sighting,
    delivered by the key that signed the push (desktop/p2p/witness.py)."""
    from p2p_identity import witness_recorder
    witness_recorder(settings).note(cert, "carried", author, via)


@router.post("/push/{category}")
async def carry_push(category: str, request: Request) -> dict:
    """Accept a pushed payload — byte-for-byte what pull/{category} returns,
    so the peer serialises once and we verify through the ordinary import
    gate. A push is unsolicited, which is exactly why nothing here is
    trusted: every record must carry a seal that checks out, or the importer
    drops it. The body — the largest a peer sends — is parsed on the peer
    lane, never on the loop."""
    _require_sharing()
    if carry_importer is None:
        raise HTTPException(status_code=404, detail="carry not supported")
    budget = _carry_budget()
    if budget <= 0:
        raise HTTPException(status_code=403, detail="not carrying")

    category = category.replace("-", "_")
    if category not in carry_queries.CARRY_CATEGORIES:
        raise HTTPException(
            status_code=404, detail=f"category not carryable: {category}")
    raw = await request.body()
    pusher = getattr(request.state, "peer_pubkey", None)

    def _run():
        try:
            payload = wire.loads(raw)
        except ValueError:
            return None
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or len(items) > 10000:
            return None
        return carry_importer.import_pushed(settings.database_url, category, payload, budget,
                                            deliverer=pusher, carried=_carried)

    try:
        imported = await in_peer_lane(_run)
    except Exception as e:
        logger.error(f"Carry push {category} failed: {e}")
        raise HTTPException(status_code=500, detail="internal error")
    if imported is None:
        raise HTTPException(status_code=400, detail="items must be a bounded list")
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


def _own_slice_cert() -> Optional[tuple]:
    """This node's certificate and, for `pow`, its proof — what an entry of
    its own build carries."""
    from desktop.p2p.identity_proof import served_proof
    from p2p_identity import proof_path, served_certificate
    cert = served_certificate(settings)
    if cert is None or cert["pubkey"].lower() != (node_pubkey_hex() or "").lower():
        return None
    path = proof_path(settings) if cert.get("method") == "pow" else None
    return cert, (served_proof(path, cert) if path is not None else None)


async def _slice_refusal(request: Request) -> Optional[Response]:
    """A signed slice request from a key that carries a penalty on the slice
    lanes is refused with the evidence, so its owner can read why in the
    node's own diagnostics (desktop/p2p/slice_trust.refusal)."""
    from desktop.p2p import slice_trust
    pubkey = getattr(request.state, "peer_pubkey", None)
    if not pubkey:
        return None

    def _read():
        with get_conn() as conn:
            return slice_trust.refusal(conn, pubkey)
    refusal = await in_peer_lane(_read)
    return JSONResponse(refusal, status_code=403) if refusal else None


lb_router = APIRouter(prefix="/api/lb", tags=["sync"])


def lb_dump_version() -> Optional[str]:
    """ListenBrainz dump version this node serves slices from, or None —
    the loader's in-DB marker plus rows (lb_slice_queries.local_dump_available)."""
    if lb_slice_queries is None:
        return None
    try:
        with get_conn() as conn:
            return lb_slice_queries.local_dump_available(conn)
    except Exception as e:
        logger.debug(f"LB dump capability check failed: {e}")
        return None


def lb_inventory() -> tuple:
    """(re-serve inventory size, newest version in it) for /health."""
    if lb_slice_queries is None:
        return 0, None
    try:
        with get_conn() as conn:
            return (lb_slice_queries.count_slice_blobs(conn, node_pubkey_hex()),
                    lb_slice_queries.max_blob_version(conn))
    except Exception:
        return 0, None


class LBSliceRequest(BaseModel):
    artist_mbids: list[str] = Field(default_factory=list, max_length=50)
    min_version: Optional[str] = Field(default=None, max_length=32)


@lb_router.post("/slice")
async def lb_slice(req: LBSliceRequest, request: Request) -> Response:
    """Per-artist-MBID signed ListenBrainz statistics blobs (v2) — mirrors
    desktop/p2p/sync_server.handle_lb_slice. Dump holders compute+sign+
    cache misses; a replica answers only the blobs it checked, misses land
    in `missing`; ``min_version`` keeps a stale cache from answering. A
    signer this node penalized is refused with the evidence."""
    _require_sharing()
    if lb_slice_queries is None:
        raise HTTPException(status_code=404, detail="lb slices unavailable")
    refused = await _slice_refusal(request)
    if refused is not None:
        return refused

    def _run():
        sign_fn, author = None, ""
        if lb_dump_version():
            try:
                key = node_signing_key()
                author = node_pubkey_hex() or ""
                sign_fn = key.sign
            except Exception as e:
                logger.warning(f"LB slice signing unavailable: {e}")
        with get_conn() as conn:
            return lb_slice_queries.serve_slices(
                conn, req.artist_mbids, req.min_version, sign_fn, author,
                _requester(request), _own_slice_cert(), node_pubkey_hex())

    try:
        return await _answer(request, _run)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except lb_slice_queries.DumpBusy:
        raise HTTPException(status_code=503, detail="dump_reloading")
    except Exception as e:
        logger.error(f"LB slice query failed: {e}")
        raise HTTPException(status_code=500, detail="internal error")


@mb_router.get("/search")
async def mb_search(q: str = Query(..., min_length=2, max_length=255)) -> dict:
    """Artist candidates from the FULL local dump — mirrors
    desktop/p2p/sync_server.handle_mb_search. Full-dump only: a replica's
    partial mb_* world would answer "not found" for names it never saw.
    Indexed-only matching keeps the volunteer cost of one human search
    at a few index probes."""
    _require_sharing()

    def _run():
        if not mb_dump_version():
            return None
        with get_conn() as conn:
            return {"artists": mb_slice_queries.search_artists(conn, q)}

    try:
        found = await in_peer_lane(_run)
    except Exception as e:
        logger.error(f"MB search failed: {e}")
        raise HTTPException(status_code=500, detail="internal error")
    if found is None:
        raise HTTPException(status_code=404, detail="no full dump")
    return found


@mb_router.post("/slice")
async def mb_slice(req: MBSliceRequest, request: Request) -> Response:
    """Per-name signed blobs (v4) — mirrors
    desktop/p2p/sync_server.handle_mb_slice. Dump holders compute+sign+
    cache misses; a replica answers only the blobs it checked, misses land
    in `missing`. Blobs carry the ORIGINAL author's signature either way. A
    signer this node penalized is refused with the evidence."""
    _require_sharing()
    refused = await _slice_refusal(request)
    if refused is not None:
        return refused

    def _run():
        sign_fn, author = None, ""
        if mb_dump_version():
            try:
                key = node_signing_key()
                author = node_pubkey_hex() or ""
                sign_fn = key.sign
            except Exception as e:
                logger.warning(f"MB slice signing unavailable: {e}")
        with get_conn() as conn:
            return mb_slice_queries.serve_slices(
                conn, req.names, sign_fn, author,
                _requester(request), _own_slice_cert(), node_pubkey_hex())

    try:
        return await _answer(request, _run)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except mb_slice_queries.DumpBusy:
        raise HTTPException(status_code=503, detail="dump_reloading")
    except Exception as e:
        logger.error(f"MB slice query failed: {e}")
        raise HTTPException(status_code=500, detail="internal error")



# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class InventoryRequest(BaseModel):
    # Same ceiling as the launcher sync server (MAX_UUIDS_PER_REQUEST) — the
    # client chunks its library into ≤10k slices and merges the responses.
    track_uuids: list[str] = Field(default_factory=list, max_length=10000)


class PullRequest(BaseModel):
    uuids: list[str] = Field(default_factory=list, max_length=10000)


# ---------------------------------------------------------------------------
# Inventory endpoint
# ---------------------------------------------------------------------------

@router.post("/inventory")
async def get_inventory(req: InventoryRequest, request: Request) -> Response:
    """
    Check what analysis this node can serve for the given track UUIDs.

    The requester sends track UUIDs from their library; the response
    carries, per category, what this node holds sealed for them — one
    round trip. desktop/p2p/sync_queries.get_inventory, as the launcher
    answers it.
    """
    _require_sharing()
    if carry_queries is None:
        raise HTTPException(status_code=404, detail="sync unavailable")

    def _run():
        with get_conn() as conn:
            return carry_queries.get_inventory(conn, req.track_uuids)

    try:
        return await _answer(request, _run)
    except Exception as e:
        logger.error(f"Inventory query failed: {e}")
        raise HTTPException(status_code=500, detail="internal error")


@router.get("/holdings")
async def get_holdings(request: Request, have: Optional[str] = None) -> Response:
    """The holdings filters — what this node holds, compressed (Bloom
    filters over the tracks it holds segments or sealed features for, and
    those it holds sealed instruments for), so a peer with millions of gaps
    tests them locally and asks /inventory only about the hits.
    `have` = the version the caller already holds; unchanged answers with
    a stub. Logic in desktop/p2p/sync_queries.get_holdings, shared with
    the launcher surface."""
    _require_sharing()
    if carry_queries is None:
        raise HTTPException(status_code=404, detail="holdings unavailable")

    def _run():
        with get_conn() as conn:
            return carry_queries.get_holdings(conn, have)

    try:
        return await _answer(request, _run)
    except Exception as e:
        logger.error(f"Holdings query failed: {e}")
        raise HTTPException(status_code=500, detail="internal error")


# ---------------------------------------------------------------------------
# Pull endpoints
# ---------------------------------------------------------------------------

@router.post("/pull/{category}")
async def pull(category: str, req: PullRequest, request: Request) -> Response:
    """A pull category's records — desktop/p2p/sync_queries.PULL_HANDLERS,
    as the launcher's sync server serves them: the sealed analysis (segment
    bundles, features, instruments, recording bindings) with the `batches`
    map of their Worker timestamps and the `authors` map of their authors'
    certificates, this node's own for the records it authored."""
    _require_sharing()
    if carry_queries is None:
        raise HTTPException(status_code=404, detail="sync unavailable")
    handler = carry_queries.PULL_HANDLERS.get(category)
    if handler is None:
        raise HTTPException(status_code=404, detail=f"unknown category: {category}")
    if category == "segments" and len(req.uuids) > carry_queries.SEGMENTS_MAX_UUIDS:
        raise HTTPException(
            status_code=400,
            detail=f"segments pull is capped at {carry_queries.SEGMENTS_MAX_UUIDS} tracks per request")

    def _run():
        # Its `authors` map carries this node's own certificate for the
        # records it authored.
        with get_conn() as conn:
            return handler(conn, req.uuids, own=_own_slice_cert())

    try:
        return await _answer(request, _run)
    except Exception as e:
        logger.error(f"Pull {category} failed: {e}")
        raise HTTPException(status_code=500, detail="internal error")
