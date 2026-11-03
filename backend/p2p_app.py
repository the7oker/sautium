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
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from db_pool import get_conn, in_chat_lane, in_peer_lane
from routers.sync import (
    SYNC_CAPABILITIES, carry_queries, lb_dump_version, lb_inventory, lb_router,
    mb_dump_version, mb_router, node_pubkey_hex, router, sharing_enabled,
)
from serving import request_target

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
# Slice requests node-wide, per family, on top of the per-address window:
# what the node's upload and its build budget (mb_slice_queries.BuildBudget)
# serve together, whoever asks. Mirrors desktop/p2p/sync_server.py.
SLICE_RATE_GLOBAL = 300
SLICE_PATHS = ("/api/mb/slice", "/api/lb/slice")


app = FastAPI(
    title="Sautium P2P",
    description="Peer sync surface",
    # No interactive docs: this port faces the internet and the schema is
    # not part of the peer contract.
    docs_url=None, redoc_url=None, openapi_url=None,
)

# No compression middleware: a bulk answer is made whole — JSON and gzip —
# on the peer lane with its work (routers/sync._answer, wire.render), and
# the rest are a few hundred bytes.


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
        raw_target = request_target(scope)
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
            except _BodyRefused as refused:
                outcome["status"] = refused.status
                raise
            finally:
                _record()
            return

        chunks, more = [], True
        try:
            while more:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunks.append(message.get("body", b""))
                more = message.get("more_body", False)
        except _BodyRefused as refused:
            outcome["status"] = refused.status
            _record()
            raise
        body = b"".join(chunks)
        own = self._own()

        def _shape_and_signer():
            # Off the loop: a body is up to its route's cap, and both its
            # parse and its hash cost per byte.
            return (contact_log.extract_request_shape(raw_target, body),
                    peer_auth.verify_request(headers, own, scope["method"], raw_target, body)
                    if own else (None, None))

        (items, targets), (pubkey, err) = await in_peer_lane(_shape_and_signer)

        async def replay():
            return {"type": "http.request", "body": body, "more_body": False}

        async def refuse(code, error):
            payload = json.dumps({"error": error}).encode()
            await send_wrapper({"type": "http.response.start", "status": code,
                                "headers": [(b"content-type", b"application/json"),
                                            (b"content-length", str(len(payload)).encode())]})
            await send_wrapper({"type": "http.response.body", "body": payload})
            _record()

        if err is not None:
            return await refuse(403, err)

        lane = peer_auth.LANE_ANONYMOUS
        bundle = None
        if pubkey is not None:
            import birth_authority
            from desktop.p2p import identity_registry
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
                banned, row = await in_peer_lane(_registry)
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
            keys = await in_peer_lane(_client_keys, pubkey, addr) if pubkey else None
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

        if bundle is not None:
            # After the gate's verdict: an unpaid flood must not fill the
            # day's `introduced` ceiling once the gate is armed
            # (desktop/p2p/witness.py). Off the loop, like the registry.
            from config import settings
            from p2p_identity import witness_recorder
            await in_peer_lane(witness_recorder(settings).note,
                               bundle["cert"], "introduced", pubkey)

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
        keys = await in_peer_lane(_client_keys, pubkey, addr)
        lane = await in_peer_lane(_lane_of, pubkey)
        n = _price().price(pubkey, action, lane)
        return JSONResponse(await in_peer_lane(_gate().quote, pubkey, keys, action, n))
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


# What a request body may be, by route prefix — the first match wins. Every
# peer request is a few fields, a list of at most 10 000 ids, or a sealed
# payload — a carry push is packed under a MiB (desktop/p2p/sync_walk
# .PUSH_MAX_BYTES); the largest is a support bundle, and only the master
# takes one (routers/peer_diag). Mirrors the launcher's 1 MiB
# client_max_size.
BODY_CAPS = (
    ("/api/diag/bundle", 32 * 1024 * 1024 + 4096),
    ("/api/", 1024 * 1024),
)
BODY_CAP_DEFAULT = 64 * 1024
# A body keeps arriving — never a pause of BODY_IDLE_S — and is whole within
# BODY_DEADLINE_S and a second for every BODY_RATE bytes of its length, or
# the request ends (408).
BODY_IDLE_S = 10
BODY_DEADLINE_S = 30
BODY_RATE = 32 * 1024
# Bodies read at once, by kind: (in all, from any one address). The chat
# lane's, the bulk lane's and the large ones each have their own, so a flood
# of one never holds another's, and one address holds a few of a kind at
# most. A slot is taken at the body's first read and kept until the answer:
# a request refused before its body — a stranger's support bundle — holds
# none, and a lane's backlog holds its own kind's slots only.
BODY_SLOTS = {"chat": (128, 8), "bulk": (128, 8), "large": (4, 1)}
BODY_SLOT_WAIT_S = 10
CHAT_PREFIXES = ("/api/chat/", "/api/relay/", "/api/diag/")


def body_cap(path: str) -> int:
    return next((cap for prefix, cap in BODY_CAPS if path.startswith(prefix)), BODY_CAP_DEFAULT)


def body_kind(path: str) -> str:
    if body_cap(path) > 1024 * 1024:
        return "large"
    return "chat" if path.startswith(CHAT_PREFIXES) else "bulk"


class _BodyRefused(Exception):
    def __init__(self, status: int, error: str):
        self.status, self.error = status, error


class _Slots:
    """The bodies of one kind read at once, in all and per address — taken
    and given back on the serving loop."""

    def __init__(self, total: int, per_address: int):
        self._free = asyncio.Semaphore(total)
        self._per_address = per_address
        self._by: dict[str, int] = {}

    async def take(self, addr: str) -> bool:
        held = self._by.get(addr, 0)
        if held >= self._per_address:
            return False
        self._by[addr] = held + 1             # counted while it waits, too
        try:
            await asyncio.wait_for(self._free.acquire(), BODY_SLOT_WAIT_S)
        except asyncio.TimeoutError:
            self._leave(addr)
            return False
        return True

    def give(self, addr: str) -> None:
        self._free.release()
        self._leave(addr)

    def _leave(self, addr: str) -> None:
        if self._by[addr] == 1:
            del self._by[addr]
        else:
            self._by[addr] -= 1


class PeerBodyGuard:
    """The body bounds, before anything reads a body: a declared length over
    the route's cap is refused unread, a body that grows past it is refused
    at the chunk that crosses it, one that pauses or arrives too slowly
    (BODY_IDLE_S, BODY_DEADLINE_S, BODY_RATE) is refused too, and reading
    one takes a slot of its kind (BODY_SLOTS). Sits inside the per-address
    limiter and outside PeerAuthMiddleware, which buffers identity-bound
    bodies to check their signatures."""

    def __init__(self, app):
        self.app = app
        self._slots = None

    def _kinds(self) -> dict:
        if self._slots is None:          # born on the serving loop
            self._slots = {kind: _Slots(*n) for kind, n in BODY_SLOTS.items()}
        return self._slots

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = _CIHeaders(scope["headers"])
        cap = body_cap(scope["path"])
        declared = headers.get("content-length")
        chunked = "chunked" in (headers.get("transfer-encoding") or "").lower()
        if declared is None and not chunked:
            return await self.app(scope, receive, send)
        try:
            declared_n = int(declared) if declared is not None else None
        except ValueError:
            return await _refuse(send, 400, "invalid content-length")
        if declared_n is not None and (declared_n < 0 or declared_n > cap):
            return await _refuse(send, 413, "body too large")
        if declared_n == 0:
            return await self.app(scope, receive, send)

        slots = self._kinds()[body_kind(scope["path"])]
        addr = (scope.get("client") or ("",))[0]
        length = declared_n if declared_n is not None else cap
        state = {"read": 0, "done": False, "started": False, "slot": False, "deadline": 0.0}

        async def bounded_receive():
            if state["done"]:
                return await receive()          # the body is whole: disconnect only
            if not state["slot"]:
                if not await slots.take(addr):
                    raise _BodyRefused(503, "busy")
                state["slot"] = True
                state["deadline"] = time.monotonic() + BODY_DEADLINE_S + length / BODY_RATE
            left = min(BODY_IDLE_S, state["deadline"] - time.monotonic())
            try:
                message = await asyncio.wait_for(receive(), max(left, 0.001))
            except asyncio.TimeoutError:
                raise _BodyRefused(408, "body not received in time") from None
            if message["type"] == "http.request":
                state["read"] += len(message.get("body", b""))
                if state["read"] > cap:
                    raise _BodyRefused(413, "body too large")
                state["done"] = not message.get("more_body", False)
            return message

        async def tracking_send(message):
            if message["type"] == "http.response.start":
                state["started"] = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, tracking_send)
        except _BodyRefused as refused:
            if not state["started"]:
                await _refuse(send, refused.status, refused.error,
                              retry_after=5 if refused.status == 503 else None)
        finally:
            if state["slot"]:
                slots.give(addr)


async def _refuse(send, status: int, error: str, retry_after: Optional[int] = None) -> None:
    payload = json.dumps({"error": error}).encode()
    headers = [(b"content-type", b"application/json"),
               (b"content-length", str(len(payload)).encode()),
               (b"connection", b"close")]
    if retry_after:
        headers.append((b"retry-after", str(retry_after).encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


app.add_middleware(PeerAuthMiddleware)
app.add_middleware(PeerBodyGuard)

_hits: dict[str, list[float]] = defaultdict(list)
_health_hits: dict[str, list[float]] = defaultdict(list)
_search_hits: dict[str, list[float]] = defaultdict(list)
_search_global: list[float] = []
_slice_global: dict[str, list[float]] = {p: [] for p in SLICE_PATHS}


def _slice_allowed(path: str, now: float) -> bool:
    stamps = [t for t in _slice_global[path] if now - t < RATE_LIMIT_WINDOW]
    _slice_global[path] = stamps
    if len(stamps) >= SLICE_RATE_GLOBAL:
        return False
    stamps.append(now)
    return True


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


def _rate_limited(stamps: list, now: float) -> JSONResponse:
    """The 429 carries Retry-After = when the oldest stamp leaves the
    window, i.e. the first moment a retry can succeed. A requester that
    reads it waits a minute at most instead of parking its work until its
    own timer (the launcher's slice cycle used to wait six hours)."""
    wait = int(RATE_LIMIT_WINDOW - (now - stamps[0])) + 1 if stamps else RATE_LIMIT_WINDOW
    return JSONResponse({"error": "rate limited", "retry_after": wait},
                        status_code=429,
                        headers={"Retry-After": str(max(1, wait))})


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    # The routed path, never request.url's (serving.request_target)
    path = request.scope["path"]
    if path == "/api/mb/search":
        if not _search_allowed(ip, now):
            return _rate_limited(_search_hits.get(ip, []), now)
        return await call_next(request)
    # /health counts in its own window: every requester probes before it
    # asks, and the nodes behind one router share one address — probes
    # counted with the work spent the window the work needed, and a 429 on
    # a probe read as "no source" (2026-09-24).
    bucket = _health_hits if path == "/health" else _hits
    recent = [t for t in bucket.get(ip, ()) if now - t < RATE_LIMIT_WINDOW]
    if len(recent) >= RATE_LIMIT_PER_MINUTE:
        bucket[ip] = recent
        return _rate_limited(recent, now)
    if request.url.path in _slice_global and not _slice_allowed(request.url.path, now):
        return _rate_limited(_slice_global[request.url.path], now)
    recent.append(now)
    bucket[ip] = recent
    if len(bucket) > 10_000:                   # bound the tables under a flood
        for table in (_hits, _health_hits, _search_hits):
            for k in [k for k, v in table.items() if not v or now - v[-1] > RATE_LIMIT_WINDOW]:
                table.pop(k, None)
    return await call_next(request)


HEALTH_TTL_S = 60.0
_health_counts = {"at": float("-inf"), "value": None, "refresh": None}


def _dump_and_slice_counts() -> dict:
    """What /health says about the dumps and the slice inventories — five
    queries, so read at most once a HEALTH_TTL_S (every requester probes
    before it asks), like the launcher's."""
    from routers.sync import mb_slice_queries
    mb_slices = 0
    if mb_slice_queries is not None:
        try:
            with get_conn() as conn:
                mb_slices = mb_slice_queries.count_slice_blobs(conn)
        except Exception as e:
            logger.debug(f"slice inventory unreadable ({e}) — 0")   # absent until the first slice lands
    lb_slices, lb_slices_version = lb_inventory()
    return {"mb_dump": mb_dump_version(), "mb_slices": mb_slices,
            "lb_dump": lb_dump_version(), "lb_slices": lb_slices,
            "lb_slices_version": lb_slices_version}


async def _refresh_counts() -> None:
    try:
        _health_counts["value"] = await in_chat_lane(_dump_and_slice_counts)
    except Exception as e:
        logger.warning(f"/health counts unreadable: {e}")   # asked again a HEALTH_TTL_S on
    finally:
        _health_counts["at"] = time.monotonic()
        _health_counts["refresh"] = None


async def _counts() -> dict:
    """The counts /health carries: one refresh at a time, on the chat lane —
    the bulk lane can be a peer's pull busy for longer than a walker waits
    for /health — and the last value answers while it runs. Only the first
    probe after a start waits for one."""
    if (time.monotonic() - _health_counts["at"] >= HEALTH_TTL_S
            and _health_counts["refresh"] is None):
        _health_counts["refresh"] = asyncio.ensure_future(_refresh_counts())
    if _health_counts["value"] is None and _health_counts["refresh"] is not None:
        await asyncio.shield(_health_counts["refresh"])
    return _health_counts["value"] or {}


@app.get("/health")
async def health() -> dict:
    """Peer-facing identity. Same contract as the launcher's sync server —
    node_id + mb_dump + capabilities are what a peer picks sources by, and
    `birth_cert` (the certificate, never the proof) is what a peer that
    dials this node records it by (desktop/p2p/witness.py). `sharing` is the
    owner's P2P sharing switch: a launcher on the same host names this node
    in its LAN beacon only while it is on (desktop/p2p/lan_discovery.py)."""
    from config import settings
    from p2p_identity import served_certificate
    counts = await _counts()
    node_id = node_pubkey_hex()
    cert = served_certificate(settings)
    return {
        "status": "ok",
        "type": "sautium-peer",
        "node_id": node_id,
        "sharing": sharing_enabled(),
        "birth_cert": cert if cert is not None and node_id and cert["pubkey"] == node_id else None,
        **counts,
        "capabilities": SYNC_CAPABILITIES,
        # Version + counts of the holdings filter (None until built): the
        # asker prices fetching it against sending its own gaps.
        "holdings": (carry_queries.holdings_summary()
                     if carry_queries is not None else None),
    }


app.include_router(router)
app.include_router(mb_router)
app.include_router(lb_router)

from routers.peer_chat import chat_router, relay_router  # noqa: E402
from routers.peer_diag import diag_router  # noqa: E402

app.include_router(chat_router)
app.include_router(relay_router)
# Support diagnostics ingress — answers only for warrants this node issued,
# so the router is inert on every node but the master.
app.include_router(diag_router)
