"""The P2P peer surface — the only Sautium surface that may face the internet.

Runs as a SECOND uvicorn server (see main.py `_start_p2p_server`) on its own
port, serving nothing but the peer protocol: inventory, enrichment pulls, MB
slices, the chat/relay protocol, and a `/health` peers identify the node by.

Why a separate port at all: the Web UI on 8800 inlines the API secret into
its HTML (window.__SAUTIUM_SECRET), so forwarding that port hands an
attacker the whole API — playback, settings, library, AI assistant. The launcher
has had this split from the start (Web UI on 18000, sync on a random
20000-29999 port); Docker had both on 8800, which is why a Docker master
could never be a reachable node.

Sync data is readable by any peer that connects, which is the P2P
protocol's own model (see the public/friends flag discussion in
docs/design/P2P-SYNC-INTEGRITY.md). This app must never gain a route that
reveals a secret or configuration; writes are permitted ONLY to the P2P
domain tables (friends, p2p_messages, invite-token tables) behind
token/friendship gating with signed, timestamp-bound requests — the
chat/relay protocol is a peer-write protocol by nature (routers/peer_chat).

Rate limiting is per-IP and in-process, mirroring the launcher's aiohttp
server: an internet-facing port meets scanners, and a peer that wants more
than a request a second is not syncing.
"""

import asyncio
import json
import logging
import re
import time
from collections import defaultdict

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from db_pool import get_conn
from routers.sync import (
    SYNC_CAPABILITIES, mb_dump_version, mb_router, node_pubkey_hex, router,
)

logger = logging.getLogger(__name__)

RATE_LIMIT_PER_MINUTE = 60
RATE_LIMIT_WINDOW = 60

# MB search budgets — mirrors desktop/p2p/sync_server.py SEARCH_RATE_*
# rationale (incl. the golden-age posture: per-IP sits high so a CGNAT
# crowd never feels it; the GLOBAL window is the node's real ceiling; the
# escalation is identity-scarcity at birth — memory-hard task / birth
# cert — never tighter IP math).
SEARCH_RATE_PER_IP = 60
SEARCH_RATE_GLOBAL = 120


app = FastAPI(
    title="Sautium P2P",
    description="Peer sync surface",
    # No interactive docs: this port faces the internet and the schema is
    # not part of the peer contract.
    docs_url=None, redoc_url=None, openapi_url=None,
)

app.add_middleware(GZipMiddleware, minimum_size=1000)


class PeerAuthMiddleware:
    """Wire format v1 on the Docker peer surface (mirror of the launcher's
    sync_server._authenticate_peer, same shared desktop/p2p/peer_auth.py):
    identity-bound paths get their body buffered and their signature checked
    against THIS node's pubkey; a signed request is looked up / introduced
    in the identity registry (never evaluated here — lazy) and answered
    with X-Sautium-Peer-Identity / X-Sautium-Peer-Lane. Pure ASGI so the
    buffered body can be replayed to the endpoint."""

    def __init__(self, app):
        self.app = app
        self._own_pubkey = None

    def _own(self):
        if self._own_pubkey is None:
            from config import settings
            from p2p_identity import resolve_identity
            ident = resolve_identity(settings)
            self._own_pubkey = (ident or {}).get("public_key_hex", "").lower() or ""
        return self._own_pubkey

    async def __call__(self, scope, receive, send):
        from desktop.p2p import contact_log, peer_auth
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        timer = contact_log.RequestTimer()
        client = scope.get("client")
        addr = client[0] if client else None
        raw_target = scope.get("raw_path", scope["path"].encode()).decode("latin-1")
        if scope.get("query_string"):
            raw_target += "?" + scope["query_string"].decode("latin-1")
        headers = _CIHeaders(scope["headers"])
        outcome = {"status": 500, "bytes_out": 0}
        pubkey, status, lane = None, None, None
        gate_result = None
        gate = {"price": None, "status": None}
        items = targets = None
        body = b""

        def _record():
            wall_ms, cpu_ms = timer.elapsed_ms()
            _contact_log().record(
                endpoint=contact_log.endpoint_family(scope["path"]),
                status=outcome["status"], wall_ms=wall_ms, cpu_ms=cpu_ms,
                pubkey=pubkey, addr=addr, lane=lane,
                bytes_in=len(body) or int(headers.get("content-length") or 0),
                bytes_out=outcome["bytes_out"], items=items, targets=targets,
                gate_price=gate["price"], gate_status=gate["status"])

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                outcome["status"] = message["status"]
                extra = []
                if pubkey is not None:
                    extra += [(peer_auth.HDR_IDENTITY.lower().encode(), status.encode()),
                              (peer_auth.HDR_LANE.lower().encode(), lane.encode())]
                if gate_result:
                    extra.append((HDR_GATE_RESULT.lower().encode(), gate_result.encode()))
                if extra:
                    message = {**message, "headers": list(message.get("headers", [])) + extra}
            elif message["type"] == "http.response.body":
                outcome["bytes_out"] += len(message.get("body", b""))
            await send(message)

        if not peer_auth.is_identity_bound(scope["path"]):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                _record()
            return

        chunks, more = [], True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(chunks)
        items, targets = contact_log.extract_request_shape(raw_target, body)

        async def replay():
            return {"type": "http.request", "body": body, "more_body": False}

        async def refuse(code, error):
            payload = json.dumps({"error": error}).encode()
            await send_wrapper({"type": "http.response.start", "status": code,
                                "headers": [(b"content-type", b"application/json"),
                                            (b"content-length", str(len(payload)).encode())]})
            await send_wrapper({"type": "http.response.body", "body": payload})
            _record()

        own = self._own()
        pubkey, err = (peer_auth.verify_request(headers, own, scope["method"], raw_target, body)
                       if own else (None, None))
        if err is not None:
            return await refuse(403, err)

        lane = peer_auth.LANE_ANONYMOUS
        if pubkey is not None:
            import birth_authority
            from desktop.p2p import identity_registry
            bundle = None
            raw_bundle = headers.get(peer_auth.HDR_CERT)
            if raw_bundle:
                bundle = peer_auth.decode_cert_bundle(raw_bundle)
                if (bundle is None or not birth_authority.verify_certificate(bundle["cert"])
                        or bundle["cert"].get("pubkey", "").lower() != pubkey):
                    return await refuse(400, "identity certificate invalid")
            def _registry():
                with get_conn() as conn:
                    if identity_registry.is_banned(conn, pubkey):
                        return "banned", None
                    if bundle is not None:
                        return None, identity_registry.observe(
                            conn, bundle["cert"], proof=bundle["proof"], addr=addr)
                    return None, identity_registry.touch(conn, pubkey, addr)

            try:
                banned, row = await asyncio.to_thread(_registry)
            except Exception as e:
                logger.warning(f"identity registry unavailable: {e}")
                banned, row = None, None
                status = "unknown"
            else:
                if banned or (row is not None and row["status"] == "failed"):
                    return await refuse(403, "identity banned")
                status = row["status"] if row is not None else "unknown"
            lane = identity_registry.lane_for(row, signed=True)

        action = contact_log.endpoint_family(scope["path"])
        pricer = _price()
        gate["price"] = pricer.would_be(pubkey or "", action, lane)
        payment = headers.get(HDR_GATE)
        if payment:
            keys = await asyncio.to_thread(_client_keys, pubkey, addr) if pubkey else None
            verdict = await _gate().check_payment(payment, pubkey, keys, action)
            gate["status"] = verdict.status
            if verdict.status not in ("ok", "none"):
                await send_wrapper({"type": "http.response.start", "status": verdict.http_status,
                                    "headers": [(b"content-type", b"application/json")]
                                    + ([(b"retry-after", str(verdict.retry_after).encode())]
                                       if verdict.retry_after else [])})
                await send_wrapper({"type": "http.response.body",
                                    "body": json.dumps({"error": verdict.error, "detail": verdict.detail}).encode()})
                _record()
                return
            gate_result = verdict.status
        elif pricer.mode() == "enforce" and gate["price"] > 0:
            gate["status"] = "required"
            payload = json.dumps({"error": "gate_required", "price": gate["price"],
                                  "quote_url": f"/api/gate/quote?pubkey={pubkey or ''}&action={action}"}).encode()
            await send_wrapper({"type": "http.response.start", "status": 402,
                                "headers": [(b"content-type", b"application/json"),
                                            (b"content-length", str(len(payload)).encode())]})
            await send_wrapper({"type": "http.response.body", "body": payload})
            _record()
            return

        state = scope.setdefault("state", {})
        state["peer_pubkey"] = pubkey
        state["lane"] = lane
        state["gate"] = gate_result
        try:
            await self.app(scope, replay, send_wrapper)
        finally:
            _record()


_log = None
_gate_svc = None
_pricer = None
_gate_mode = ("shadow", 0.0)
HDR_GATE = "X-Sautium-Gate"
HDR_GATE_RESULT = "X-Sautium-Gate-Result"


def _read_gate_mode() -> str:
    """user_settings['p2p.gate_mode'] cached 10 s; unreadable → shadow."""
    global _gate_mode
    value, at = _gate_mode
    now = time.time()
    if now - at < 10.0:
        return value
    mode = "shadow"
    try:
        from routers.settings import _read
        m = _read("p2p.gate_mode")
        if isinstance(m, str):
            mode = m
    except Exception as e:
        logger.debug(f"gate mode unreadable ({e}) — shadow")
    _gate_mode = (mode, now)
    return mode


def _price():
    """This surface's pricer (base × load × siege), installed as the process
    singleton so /api/settings/p2p can show it."""
    global _pricer
    if _pricer is None:
        from desktop.p2p import load_meter, pricing, similarity
        index = similarity.current() or similarity.install(similarity.SimilarityIndex(get_conn))
        _pricer = pricing.install(pricing.Pricer(
            load_meter.current(), costs=_contact_log().costs, mode=_read_gate_mode,
            sim_mult=index.sim_mult))
    return _pricer


def _lane_of(pubkey: str) -> str:
    from desktop.p2p import identity_registry, peer_auth
    try:
        with get_conn() as conn:
            return identity_registry.lane_for(identity_registry.get(conn, pubkey), signed=True)
    except Exception:
        return peer_auth.LANE_STRANGER


def _gate():
    """This surface's admission gate (dormant: price 0), lazy on the loop."""
    global _gate_svc
    if _gate_svc is None:
        from config import settings
        from p2p_identity import load_signing_key, resolve_identity
        from desktop.p2p import admission, gate_service, identity_registry
        key = load_signing_key(settings)
        ident = resolve_identity(settings)

        def evidence(pubkey: str, reason: str) -> None:
            with get_conn() as conn:
                identity_registry.mark_failed(conn, pubkey, None, reason)

        from desktop.p2p import load_meter
        meter = load_meter.current()
        _gate_svc = gate_service.GateService(
            ident["public_key_hex"], key.sign,
            admission.derive_gate_secret(key.private_bytes_raw()),
            conn_factory=get_conn, on_evidence=evidence, meter=meter,
            verify_concurrency=1 if (meter is not None and meter.profile == "lite") else 2)
    return _gate_svc


def _client_keys(pubkey: str, addr):
    """Exclusion keys for the pool: pubkey, subnet, registry email domain."""
    from desktop.p2p import gate_service, identity_registry
    domain = None
    try:
        with get_conn() as conn:
            row = identity_registry.get(conn, pubkey)
            domain = row["email_domain_token"] if row else None
    except Exception as e:
        logger.debug(f"registry lookup for gate keys failed: {e}")
    return gate_service.client_keys(pubkey, addr, domain)


@app.get("/api/gate/quote")
async def gate_quote(request: Request, pubkey: str = "", action: str = "") -> JSONResponse:
    """A free, O(1), signed, client-bound quote (wire format v1 § 4), priced
    for the action the client is about to perform."""
    pubkey = pubkey.lower()
    try:
        if len(bytes.fromhex(pubkey)) != 32:
            raise ValueError
    except ValueError:
        return JSONResponse({"error": "invalid pubkey"}, status_code=400)
    if not re.match(r"^[a-z]+\.[a-z_]+$", action or ""):
        action = ""
    try:
        addr = request.client.host if request.client else None
        keys = await asyncio.to_thread(_client_keys, pubkey, addr)
        lane = await asyncio.to_thread(_lane_of, pubkey)
        n = _price().price(pubkey, action, lane)
        return JSONResponse(await asyncio.to_thread(_gate().quote, pubkey, keys, action, n))
    except Exception as e:                       # no identity configured → no gate
        logger.warning(f"gate quote unavailable: {e}")
        return JSONResponse({"error": "gate unavailable"}, status_code=503)


def _contact_log():
    global _log
    if _log is None:
        from desktop.p2p import contact_log
        _log = contact_log.ContactLog(get_conn)
        _log.start()
    return _log


class _CIHeaders(dict):
    """ASGI headers (lowercase bytes pairs) with case-insensitive .get()."""

    def __init__(self, pairs):
        super().__init__((k.decode("latin-1").lower(), v.decode("latin-1")) for k, v in pairs)

    def get(self, key, default=None):
        return super().get(key.lower(), default)


app.add_middleware(PeerAuthMiddleware)

_hits: dict[str, list[float]] = defaultdict(list)
_search_hits: dict[str, list[float]] = defaultdict(list)
_search_global: list[float] = []


def _search_allowed(ip: str, now: float) -> bool:
    global _search_global
    _search_global = [t for t in _search_global if now - t < RATE_LIMIT_WINDOW]
    if len(_search_global) >= SEARCH_RATE_GLOBAL:
        return False
    recent = [t for t in _search_hits.get(ip, ()) if now - t < RATE_LIMIT_WINDOW]
    if len(recent) >= SEARCH_RATE_PER_IP:
        _search_hits[ip] = recent
        return False
    recent.append(now)
    _search_hits[ip] = recent
    _search_global.append(now)
    return True


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    if request.url.path == "/api/mb/search":
        if not _search_allowed(ip, now):
            return JSONResponse({"error": "rate limited"}, status_code=429)
        return await call_next(request)
    recent = [t for t in _hits.get(ip, ()) if now - t < RATE_LIMIT_WINDOW]
    if len(recent) >= RATE_LIMIT_PER_MINUTE:
        _hits[ip] = recent
        return JSONResponse({"error": "rate limited"}, status_code=429)
    recent.append(now)
    _hits[ip] = recent
    if len(_hits) > 10_000:                    # bound the table under a flood
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > RATE_LIMIT_WINDOW]:
            _hits.pop(k, None)
        for k in [k for k, v in _search_hits.items() if not v or now - v[-1] > RATE_LIMIT_WINDOW]:
            _search_hits.pop(k, None)
    return await call_next(request)


@app.get("/health")
async def health() -> dict:
    """Peer-facing identity. Same contract as the launcher's sync server —
    node_id + mb_dump + capabilities are what a peer picks sources by."""
    mb_slices = 0
    try:
        from db_pool import get_conn
        from routers.sync import mb_slice_queries
        if mb_slice_queries is not None:
            with get_conn() as conn:
                mb_slices = mb_slice_queries.count_slice_blobs(conn)
    except Exception:
        pass   # table absent until the first slice lands — 0 is the truth
    return {
        "status": "ok",
        "type": "sautium-peer",
        "node_id": node_pubkey_hex(),
        "mb_dump": mb_dump_version(),
        "mb_slices": mb_slices,
        "capabilities": SYNC_CAPABILITIES,
    }


app.include_router(router)
app.include_router(mb_router)

from routers.peer_chat import chat_router, relay_router  # noqa: E402

app.include_router(chat_router)
app.include_router(relay_router)
