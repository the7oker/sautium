"""
aiohttp-based HTTP sync server for P2P data exchange.

Exposes the same endpoints as backend/routers/sync.py so that other
launchers can sync enrichment data from this node using the standard
SyncClient / BackendAPIClient.

Also serves chat endpoints for encrypted P2P messaging.

Runs in a background asyncio event loop thread alongside the DHT service.
"""

import asyncio
import gzip
import hashlib
import json
import logging
import time
from collections import defaultdict, deque
from functools import partial
from typing import Callable, Optional

import psycopg2

from aiohttp import web

from desktop.p2p import contact_log, mb_slice_queries, peer_auth, sync_queries

logger = logging.getLogger(__name__)


def _response_bytes(response) -> int:
    """Body size before the response is written (aiohttp's body_length is
    only known after write)."""
    body = getattr(response, "body", None)
    return len(body) if isinstance(body, (bytes, bytearray)) else 0


def _conn_factory_for(dsn: str):
    """One short-lived autocommit connection per call (executor-safe)."""
    from desktop.p2p.identity_registry import psycopg2_conn_factory
    return psycopg2_conn_factory(dsn)


# Rate limiting: max requests per IP per minute
RATE_LIMIT_PER_MINUTE = 60
RATE_LIMIT_WINDOW = 60  # seconds

# MB search is limited RESOURCE-first, not identity-first: one IP can be a
# whole CGNAT of different people, so per-IP alone is both too coarse (it
# collectively throttles innocents — and pins them to the shared bucket,
# starving their SYNC) and too weak (an attacker has many IPs). The node's
# actual guarantee is the GLOBAL window — whoever asks, the node never
# serves more than this many searches a minute; the per-IP bucket stays as
# a secondary anti-scraper heuristic, SEPARATE from the main bucket so
# interactive search can never poison the sync protocol's budget. A 429
# just rotates the requester to the next node.
#
# Golden-age posture (Valerii, 2026-08-10): the per-IP cap sits HIGH — a
# CGNAT crowd of good actors must never feel it (per-IP throttling one
# node is simultaneously an attack on everyone sharing that IP); it only
# exists to stop one runaway scraper from eating the whole global window.
# The planned escalation once the network grows: per-node-identity limits
# where identity is made SCARCE at birth — a memory-hard task (Argon2id is
# already in the stack) for anonymous nodes, birth certificates for
# verified ones — never tighter IP math.
SEARCH_RATE_PER_IP = 60
SEARCH_RATE_GLOBAL = 120

# Max UUIDs per request (prevents memory/DB DoS)
MAX_UUIDS_PER_REQUEST = 10_000

# Signed-request replay window + relay caps — mirror
# backend/routers/peer_chat.py, keep in step.
TS_WINDOW = 60
TOKEN_FRIENDS_PER_HOUR = 30
WAKE_MAX_PER_IP = 20
PROBE_COOLDOWN = 60
# Forwarding caps — mirror backend/routers/peer_chat.py, keep in step.
FORWARD_ACK_TIMEOUT = 10
FORWARD_QUEUE_MAX = 100
FORWARD_INFLIGHT_PER_SENDER = 10
# Peer-relay caps (Phase D) — mirror backend/routers/peer_chat.py.
# The cap counts FOREIGN clients (voucher-registered, not friends); it is
# adaptive: a full relay stops announcing Sautium-cap:relay, and if it then
# sees no OTHER relays in the DHT it grows the cap instead of letting the
# network starve (the check runs on the re-announce cadence, the signal is
# binary and the reaction one-sided — no oscillation).
RELAY_CAP_BASE = 20
RELAY_CAP_MAX = 100
RELAY_CAP_STEP = 20
# Voucher lifetime: the client re-issues on every (re)subscribe, so this
# only bounds how long a sender may trust a voucher from a relay whose
# client silently vanished mid-window.
VOUCHER_TTL = 24 * 3600


def delivery_payload(message_uuid: str, ciphertext_sha256: str) -> str:
    """Canonical delivery-receipt string — mirrored in
    backend/routers/peer_chat.py. No timestamp: a receipt is a permanent
    fact the sender must be able to re-verify from what it already has."""
    return f"sautium-delivery:v1:{message_uuid}:{ciphertext_sha256}"


def voucher_payload(client_pubkey: str, relay_pubkey: str, until: int) -> str:
    """Canonical relay-voucher string the CLIENT signs — mirrored in
    backend/routers/peer_chat.py. The relay pubkey inside the payload means
    a voucher issued to one relay cannot be presented by another; `until`
    bounds how long the authority lives without re-issue."""
    return (f"sautium-relay-voucher:v1:{client_pubkey.lower()}"
            f":{relay_pubkey.lower()}:{int(until)}")


class _WakeSub:
    """One live wake-stream subscription (relay protocol)."""
    __slots__ = ("evt", "loop", "kinds", "envelopes", "closed", "ip")

    def __init__(self, evt, loop, ip):
        self.evt = evt
        self.loop = loop
        self.kinds: set = set()
        # Envelopes to push down this stream. A queue, not a set: each one
        # is a distinct message, and order is the sender's.
        self.envelopes: deque = deque()
        self.closed = False
        self.ip = ip


class SyncServer:
    """HTTP server that serves sync endpoints for peer-to-peer data exchange."""

    def __init__(self, db_dsn: str, port: int = 19000, node_id: str = "",
                 account_info: Optional[dict] = None,
                 backend_port: int = 0,
                 mb_dump_version: Optional[str] = None):
        self.db_dsn = db_dsn
        self.port = port
        self.node_id = node_id
        self.account_info = account_info  # {username, public_key_hex, invite_code}
        self._backend_port = backend_port
        self.mb_dump_version = mb_dump_version  # full-dump version served via /api/mb/slice
        self._chat_service = None  # set via set_chat_service()
        self._on_message_cb: Optional[Callable] = None
        self._delivery_trigger_cb: Optional[Callable] = None
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        # Rate limiting state
        self._request_counts: dict[str, list[float]] = defaultdict(list)
        self._sharing = True
        self._sharing_checked = 0.0
        self._gate = None          # identity_registry.IdentityGate, lazy
        self._contact_log = contact_log.ContactLog(
            partial(_conn_factory_for, self.db_dsn))
        # Relay wake registry: subscriber pubkey -> _WakeSub
        self._wake_subs: dict[str, _WakeSub] = {}
        # Peer-relay clients (Phase D): pubkey -> voucher record
        # {invite_code, until, signature}. In-memory on purpose — a relay
        # restart drops the registry and every client re-issues on
        # reconnect (their SSE died with us).
        self._relay_clients: dict[str, dict] = {}
        self._relay_cap = RELAY_CAP_BASE
        # Called with the invite code when a foreign client (un)subscribes —
        # p2p_manager wires these to DHT announce_user_for / withdraw.
        self._client_announce_cb: Optional[Callable] = None
        self._client_withdraw_cb: Optional[Callable] = None
        self._probe_last: dict[str, float] = {}
        # In-flight forwards awaiting a receipt: uuid -> (recipient, Future).
        # The ONLY state a relay holds for a forwarded message, and only
        # until the receipt arrives or the timeout fires.
        self._pending_acks: dict[str, tuple] = {}
        # Reachability: called with no args on any inbound request from a
        # non-LAN, non-overlay address — definitive "we are reachable".
        self._inbound_cb: Optional[Callable] = None

    def set_inbound_cb(self, cb: Callable):
        """Attach the passive-reachability callback (p2p_manager)."""
        self._inbound_cb = cb

    def set_client_announce_cbs(self, announce: Callable, withdraw: Callable):
        """Attach DHT announce-on-behalf hooks (p2p_manager wires these to
        dht_service.announce_user_for / withdraw_user_for)."""
        self._client_announce_cb = announce
        self._client_withdraw_cb = withdraw

    def relay_client_count(self) -> int:
        """Foreign (voucher-registered) clients currently subscribed."""
        return sum(1 for k in self._wake_subs if k in self._relay_clients)

    def relay_has_room(self) -> bool:
        return self.relay_client_count() < self._relay_cap

    def adapt_relay_cap(self, other_relays_visible: bool) -> bool:
        """One adaptive-cap step (Валерій's design): a full relay that sees
        no OTHER relay in the DHT grows its cap instead of letting the
        network starve; a relay with room again, in a network that has
        relays, decays back toward the base. Existing clients are never
        shed by a cap change. Returns True when the cap changed."""
        clients = self.relay_client_count()
        if clients >= self._relay_cap and not other_relays_visible \
                and self._relay_cap < RELAY_CAP_MAX:
            self._relay_cap = min(self._relay_cap + RELAY_CAP_STEP,
                                  RELAY_CAP_MAX)
            logger.info("relay cap raised to %d (no other relays visible)",
                        self._relay_cap)
            return True
        if clients < RELAY_CAP_BASE and other_relays_visible \
                and self._relay_cap > RELAY_CAP_BASE:
            self._relay_cap = max(RELAY_CAP_BASE, clients)
            logger.info("relay cap decayed to %d", self._relay_cap)
            return True
        return False

    def _note_inbound(self, ip: Optional[str]) -> None:
        if not self._inbound_cb or not ip:
            return
        try:
            import ipaddress
            addr = ipaddress.ip_address(ip)
            # LAN/loopback/link-local prove nothing about internet
            # reachability; neither does 100.64/10 — a CGNAT/Tailscale
            # overlay peer reaches us over its tunnel, not our inbound port.
            if addr.is_private or addr.is_loopback or addr.is_link_local:
                return
            if (addr.version == 4
                    and addr in ipaddress.ip_network("100.64.0.0/10")):
                return
        except ValueError:
            return
        try:
            self._inbound_cb()
        except Exception as e:
            logger.debug(f"inbound reachability callback failed: {e}")

    @web.middleware
    async def _inbound_middleware(self, request: web.Request, handler):
        self._note_inbound(request.remote)
        timer = contact_log.RequestTimer()
        peer_pubkey = lane = None
        items = targets = None
        response = None
        try:
            if not peer_auth.is_identity_bound(request.raw_path):
                response = await handler(request)
                return response
            peer_pubkey, status, lane, refusal = await self._authenticate_peer(request)
            items, targets = contact_log.extract_request_shape(
                request.raw_path, await request.read())
            if refusal is not None:
                response = refusal
                return response
            request["peer_pubkey"] = peer_pubkey
            request["lane"] = lane
            response = await handler(request)
            if peer_pubkey is not None:
                response.headers[peer_auth.HDR_IDENTITY] = status
                response.headers[peer_auth.HDR_LANE] = lane
            return response
        except web.HTTPException as e:
            response = e
            raise
        finally:
            wall_ms, cpu_ms = timer.elapsed_ms()
            self._contact_log.record(
                endpoint=contact_log.endpoint_family(request.raw_path),
                status=getattr(response, "status", 500),
                wall_ms=wall_ms, cpu_ms=cpu_ms, pubkey=peer_pubkey,
                addr=request.remote, lane=lane,
                bytes_in=request.content_length or 0,
                bytes_out=_response_bytes(response),
                items=items, targets=targets)

    async def _authenticate_peer(self, request: web.Request):
        """Wire format v1 (peer_auth.py): an unsigned request is the
        anonymous lane; a signed one is checked against OUR pubkey, then
        the identity registry decides stranger vs identity lane; the
        introduction header is recorded (never evaluated here — lazy).
        Returns (pubkey|None, status|None, lane, refusal_response|None)."""
        own = (self.account_info or {}).get("public_key_hex", "")
        body = await request.read()          # cached by aiohttp; b"" for GET
        if not own:
            return None, None, peer_auth.LANE_ANONYMOUS, None
        pubkey, err = peer_auth.verify_request(
            request.headers, own, request.method, request.raw_path, body)
        if err is not None:
            return None, None, None, self._json_response(
                request, {"error": err}, status=403)
        if pubkey is None:
            return None, None, peer_auth.LANE_ANONYMOUS, None

        bundle = None
        raw_bundle = request.headers.get(peer_auth.HDR_CERT)
        if raw_bundle:
            from desktop.p2p.birth_cert import verify_certificate
            bundle = peer_auth.decode_cert_bundle(raw_bundle)
            if (bundle is None or not verify_certificate(bundle["cert"])
                    or bundle["cert"].get("pubkey", "").lower() != pubkey):
                return None, None, None, self._json_response(
                    request, {"error": "identity certificate invalid"}, status=400)

        from desktop.p2p import identity_registry
        addr = request.remote

        def _registry():
            with identity_registry.psycopg2_conn_factory(self.db_dsn) as conn:
                if identity_registry.is_banned(conn, pubkey):
                    return "banned", None
                if bundle is not None:
                    row = identity_registry.observe(
                        conn, bundle["cert"], proof=bundle["proof"], addr=addr)
                else:
                    row = identity_registry.touch(conn, pubkey, addr)
                return None, row

        try:
            banned, row = await asyncio.get_event_loop().run_in_executor(None, _registry)
        except Exception as e:
            logger.warning(f"identity registry unavailable: {e}")
            return pubkey, "unknown", peer_auth.LANE_STRANGER, None
        if banned or (row is not None and row["status"] == "failed"):
            return None, None, None, self._json_response(
                request, {"error": "identity banned"}, status=403)
        status = row["status"] if row is not None else "unknown"
        return pubkey, status, identity_registry.lane_for(row, signed=True), None

    def set_chat_service(self, chat_service, on_message_cb: Callable = None):
        """Attach chat service for handling chat endpoints."""
        self._chat_service = chat_service
        self._on_message_cb = on_message_cb

    def set_delivery_trigger(self, cb: Callable):
        """Set callback to trigger immediate P2P message delivery."""
        self._delivery_trigger_cb = cb

    def _identity_gate(self):
        """Lazy: the asyncio semaphore inside must be born on the serving loop."""
        if self._gate is None:
            from functools import partial
            from desktop.p2p import identity_registry
            from desktop.p2p.birth_cert import verify_certificate
            self._gate = identity_registry.IdentityGate(
                partial(identity_registry.psycopg2_conn_factory, self.db_dsn),
                verify_certificate)
        return self._gate

    def _new_db(self) -> psycopg2.extensions.connection:
        """Create a new DB connection (caller must close it)."""
        conn = psycopg2.connect(self.db_dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET timezone = 'UTC'")
        return conn

    def _sharing_enabled(self) -> bool:
        """The "P2P sharing" switch from Settings. Peer endpoints have no
        login by design, so this flag is the only thing between "off" and a
        full inventory dump — a launcher must honour it exactly like the
        backend does. Cached briefly: one sync run fires many pulls.
        Unreadable settings fail closed."""
        now = time.time()
        if now - self._sharing_checked < 10.0:
            return self._sharing
        self._sharing_checked = now
        try:
            conn = self._new_db()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT value FROM user_settings "
                                "WHERE key = 'sync.p2p_enabled'")
                    row = cur.fetchone()
                # Absent row means the default, which is on.
                self._sharing = True if row is None else bool(row[0])
            finally:
                conn.close()
        except Exception as e:
            logger.warning("sharing flag unreadable (%s) — refusing to serve", e)
            self._sharing = False
        return self._sharing

    def _check_search_limit(self, ip: str) -> bool:
        """MB-search budget: global window first (the node's hard spend
        ceiling), then a per-IP bucket separate from the main one."""
        now = time.time()
        glob = [t for t in getattr(self, "_search_global", [])
                if now - t < RATE_LIMIT_WINDOW]
        if len(glob) >= SEARCH_RATE_GLOBAL:
            self._search_global = glob
            return False
        per_ip = getattr(self, "_search_counts", {})
        stamps = [t for t in per_ip.get(ip, []) if now - t < RATE_LIMIT_WINDOW]
        if len(stamps) >= SEARCH_RATE_PER_IP:
            per_ip[ip] = stamps
            self._search_counts = per_ip
            self._search_global = glob
            return False
        glob.append(now)
        stamps.append(now)
        per_ip[ip] = stamps
        if not stamps:
            per_ip.pop(ip, None)
        self._search_counts = per_ip
        self._search_global = glob
        return True

    def _check_rate_limit(self, ip: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = time.time()
        # Clean old entries
        timestamps = [
            t for t in self._request_counts.get(ip, [])
            if now - t < RATE_LIMIT_WINDOW
        ]
        if not timestamps:
            # Remove empty entries to prevent unbounded dict growth
            self._request_counts.pop(ip, None)
        if len(timestamps) >= RATE_LIMIT_PER_MINUTE:
            self._request_counts[ip] = timestamps
            return False
        timestamps.append(now)
        self._request_counts[ip] = timestamps
        return True

    def _json_response(self, request: web.Request, data: dict,
                       status: int = 200,
                       headers: Optional[dict] = None) -> web.Response:
        """Create a JSON response, with gzip if client accepts it."""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", **(headers or {})}

        accept_enc = request.headers.get("Accept-Encoding", "")
        if "gzip" in accept_enc and len(body) > 1024:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"

        return web.Response(body=body, status=status, headers=headers)

    async def _run_query(self, func, *args):
        """Run a blocking sync_queries function in the thread pool executor."""
        loop = asyncio.get_event_loop()

        def _with_conn():
            conn = self._new_db()
            try:
                return func(conn, *args)
            finally:
                conn.close()

        return await loop.run_in_executor(None, _with_conn)

    # -----------------------------------------------------------------------
    # Route handlers
    # -----------------------------------------------------------------------

    async def handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint. `mb_slices` is the replica inventory size —
        a dump-less node with verified blobs is a partial slice source, and
        requesters try those BEFORE dump nodes to spread the load."""
        return self._json_response(request, {
            "status": "ok",
            "node_id": self.node_id,
            "type": "sautium-peer",
            "mb_dump": self.mb_dump_version,
            "mb_slices": self._slice_blob_total(),
            "capabilities": sync_queries.CAPABILITIES,
        })

    def _slice_blob_total(self) -> int:
        """Replica inventory size, cached 60 s — /health is probed often and
        the number only needs to say "worth asking"."""
        now = time.time()
        if now - getattr(self, "_slice_count_at", 0) > 60:
            try:
                conn = self._new_db()
                try:
                    self._slice_count = \
                        mb_slice_queries.count_slice_blobs(conn)
                finally:
                    conn.close()
            except Exception:
                self._slice_count = 0
            self._slice_count_at = now
        return getattr(self, "_slice_count", 0)

    async def handle_inventory(self, request: web.Request) -> web.Response:
        """POST /api/sync/inventory — check available enrichment data."""
        ip = request.remote or "unknown"
        if not self._check_rate_limit(ip):
            return self._json_response(
                request, {"error": "rate limited"}, status=429
            )

        if not self._sharing_enabled():
            return self._json_response(
                request, {"error": "sharing disabled"}, status=403)

        try:
            body = await request.json()
            track_uuids = body.get("track_uuids", [])
        except (json.JSONDecodeError, Exception):
            return self._json_response(
                request, {"error": "invalid JSON"}, status=400
            )

        if not isinstance(track_uuids, list) or len(track_uuids) > MAX_UUIDS_PER_REQUEST:
            return self._json_response(
                request, {"error": f"track_uuids must be a list of at most {MAX_UUIDS_PER_REQUEST} items"},
                status=400,
            )

        try:
            result = await self._run_query(
                sync_queries.get_inventory, track_uuids
            )
            return self._json_response(request, result)
        except Exception as e:
            logger.error(f"Inventory query failed: {e}")
            return self._json_response(
                request, {"error": "internal error"}, status=500
            )

    async def handle_pull(self, request: web.Request) -> web.Response:
        """POST /api/sync/pull/{category} — pull enrichment data."""
        ip = request.remote or "unknown"
        if not self._check_rate_limit(ip):
            return self._json_response(
                request, {"error": "rate limited"}, status=429
            )

        if not self._sharing_enabled():
            return self._json_response(
                request, {"error": "sharing disabled"}, status=403)

        category = request.match_info.get("category", "")
        handler = sync_queries.PULL_HANDLERS.get(category)
        if handler is None:
            return self._json_response(
                request, {"error": f"unknown category: {category}"}, status=404
            )

        try:
            body = await request.json()
            uuids = body.get("uuids", [])
        except (json.JSONDecodeError, Exception):
            return self._json_response(
                request, {"error": "invalid JSON"}, status=400
            )

        limit = (sync_queries.SEGMENTS_MAX_UUIDS if category == "segments"
                 else MAX_UUIDS_PER_REQUEST)
        if not isinstance(uuids, list) or len(uuids) > limit:
            return self._json_response(
                request, {"error": f"uuids must be a list of at most {limit} items"},
                status=400,
            )

        try:
            result = await self._run_query(handler, uuids)
            return self._json_response(request, result)
        except Exception as e:
            logger.error(f"Pull {category} failed: {e}")
            return self._json_response(
                request, {"error": "internal error"}, status=500
            )

    # -----------------------------------------------------------------------
    # Push-seeding (carry): the inverse direction of the pull protocol
    # -----------------------------------------------------------------------

    def _carry_budget(self) -> int:
        """How many foreign artists this node is willing to hold. An absent
        row means nobody has touched the setting, not "no" — the default
        applies, same as it does when the backend reads it."""
        try:
            conn = self._new_db()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT value FROM user_settings "
                                "WHERE key = 'sync.carry_limit'")
                    row = cur.fetchone()
                if row is None:
                    return sync_queries.CARRY_DEFAULT_BUDGET
                return int(row[0]) if row[0] is not None else 0
            finally:
                conn.close()
        except Exception as e:
            logger.warning("carry budget unreadable (%s) — not carrying", e)
            return 0

    async def handle_carry_offer(self, request: web.Request) -> web.Response:
        """POST /api/sync/offer — "here are the recordings I could give
        you"; we answer with OUR track uuids we actually want (carry v4).

        The round trip exists so a pusher never ships what we already hold
        or never cared about: 16 bytes per recording to ask, ~46 KB per
        track to send blind."""
        ip = request.remote or "unknown"
        if not self._check_rate_limit(ip):
            return self._json_response(
                request, {"error": "rate limited"}, status=429)
        if not self._sharing_enabled():
            return self._json_response(
                request, {"error": "sharing disabled"}, status=403)
        try:
            body = await request.json()
            recordings = body.get("recordings", [])
        except (json.JSONDecodeError, Exception):
            return self._json_response(
                request, {"error": "invalid JSON"}, status=400)
        if not isinstance(recordings, list):
            return self._json_response(
                request, {"error": "recordings must be a list"}, status=400)

        budget = self._carry_budget()
        wanted = await self._run_query(
            sync_queries.wanted_tracks, recordings, budget)
        return self._json_response(request, {"wanted": wanted})

    async def handle_carry_push(self, request: web.Request) -> web.Response:
        """POST /api/sync/push/{category} — accept a pushed payload.

        Body is byte-for-byte what pull/{category} returns, so the peer
        serialises once and we verify through the ordinary import gate. A
        push is unsolicited, which is exactly why nothing here is trusted:
        every record must carry a seal that checks out, or the importer
        drops it."""
        ip = request.remote or "unknown"
        if not self._check_rate_limit(ip):
            return self._json_response(
                request, {"error": "rate limited"}, status=429)
        if not self._sharing_enabled():
            return self._json_response(
                request, {"error": "sharing disabled"}, status=403)
        if self._carry_budget() <= 0:
            return self._json_response(
                request, {"error": "not carrying"}, status=403)

        category = request.match_info.get("category", "").replace("-", "_")
        if category not in sync_queries.CARRY_CATEGORIES:
            return self._json_response(
                request, {"error": f"category not carryable: {category}"},
                status=404)
        try:
            body = await request.json()
            items = body.get("items", [])
        except (json.JSONDecodeError, Exception):
            return self._json_response(
                request, {"error": "invalid JSON"}, status=400)
        if not isinstance(items, list) or len(items) > MAX_UUIDS_PER_REQUEST:
            return self._json_response(
                request, {"error": "items must be a bounded list"}, status=400)

        from desktop.sync_client import import_pushed
        try:
            imported = await asyncio.get_event_loop().run_in_executor(
                None, partial(import_pushed, self.db_dsn, category, body))
        except Exception as e:
            logger.error(f"Carry push {category} failed: {e}")
            return self._json_response(
                request, {"error": "internal error"}, status=500)
        if imported:
            logger.info("Carrying %d %s record(s) pushed by %s",
                        imported, category, ip)
        return self._json_response(request, {"imported": imported})

    async def handle_mb_search(self, request: web.Request) -> web.Response:
        """GET /api/mb/search?q= — artist candidates from the FULL local dump.

        Serve capability is full-dump only: a replica holds mb_* rows just
        for already-fetched names, and searching that partial world would
        answer "not found" for names it simply never saw. Indexed-only
        matching (see mb_slice_queries.search_artists) keeps one query per
        human search cheap for the volunteer node. Budgeted separately
        from the main per-IP bucket — see SEARCH_RATE_* rationale."""
        ip = request.remote or "unknown"
        if not self._check_search_limit(ip):
            return self._json_response(
                request, {"error": "rate limited"}, status=429
            )
        if not self._sharing_enabled():
            return self._json_response(
                request, {"error": "sharing disabled"}, status=403)
        if not self.mb_dump_version:
            return self._json_response(
                request, {"error": "no full dump"}, status=404)

        q = (request.query.get("q") or "").strip()
        if len(q) < 2 or len(q) > 255:
            return self._json_response(
                request, {"error": "q must be 2..255 chars"}, status=400)

        try:
            artists = await self._run_query(
                mb_slice_queries.search_artists, q)
            return self._json_response(request, {"artists": artists})
        except Exception as e:
            logger.error(f"MB search failed: {e}")
            return self._json_response(
                request, {"error": "internal error"}, status=500
            )

    async def handle_mb_slice(self, request: web.Request) -> web.Response:
        """POST /api/mb/slice — per-name signed blobs (v2).

        A dump holder computes+signs+caches misses; a REPLICA (no dump,
        mb_slice_blobs only) answers what it holds and lists the rest in
        `missing` — no 404: partially useful is useful, and the requester
        takes misses to the next candidate. Blobs carry the ORIGINAL
        author's signature either way."""
        ip = request.remote or "unknown"
        if not self._check_rate_limit(ip):
            return self._json_response(
                request, {"error": "rate limited"}, status=429
            )

        if not self._sharing_enabled():
            return self._json_response(
                request, {"error": "sharing disabled"}, status=403)

        try:
            body = await request.json()
            names = body.get("names", [])
        except (json.JSONDecodeError, Exception):
            return self._json_response(
                request, {"error": "invalid JSON"}, status=400
            )

        sign_fn, author = None, ""
        if self.mb_dump_version:
            from desktop.node_identity import get_node_id, sign_message
            try:
                author = get_node_id()
                sign_fn = sign_message
            except Exception as e:
                logger.warning(f"MB slice signing unavailable: {e}")

        try:
            result = await self._run_query(
                mb_slice_queries.serve_slices, names, sign_fn, author)
            return self._json_response(request, result)
        except ValueError as e:
            return self._json_response(request, {"error": str(e)}, status=400)
        except mb_slice_queries.DumpBusy:
            return self._json_response(
                request, {"error": "dump_reloading"}, status=503
            )
        except Exception as e:
            logger.error(f"MB slice query failed: {e}")
            return self._json_response(
                request, {"error": "internal error"}, status=500
            )

    # -----------------------------------------------------------------------
    # Chat handlers
    # -----------------------------------------------------------------------

    @staticmethod
    def _ts_ok(ts) -> bool:
        try:
            return abs(time.time() - int(ts)) <= TS_WINDOW
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _verify_peer_sig(pubkey_hex: str, message: str, sig_hex: str) -> bool:
        from desktop.node_identity import verify_signature
        try:
            return verify_signature(message.encode("utf-8"),
                                    bytes.fromhex(sig_hex), pubkey_hex)
        except (ValueError, TypeError):
            return False

    def _mint_grant(self, token_id: str, rights: list, peer_pubkey: str) -> dict:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        from desktop.node_identity import get_private_key_raw
        from desktop.p2p import invite_tokens
        key = Ed25519PrivateKey.from_private_bytes(get_private_key_raw())
        return invite_tokens.sign_grant(
            key, token_id, rights, peer_pubkey,
            self.account_info.get("public_key_hex", ""))

    async def handle_chat_handshake(self, request: web.Request) -> web.Response:
        """POST /api/chat/handshake — exchange public keys for friend request.

        Three accept paths — MIRRORS backend/routers/peer_chat.chat_handshake,
        keep in step:
        - classic: mutual-add consent (we already added this peer);
        - token:   a live invite token auto-accepts; guest signature required
                   because this path bypasses the consent check;
        - grant:   recovery — our own past signature over their grant
                   replaces the friends row this device lost.
        """
        ip = request.remote or "unknown"
        if not self._check_rate_limit(ip):
            return self._json_response(
                request, {"error": "rate limited"}, status=429
            )

        if not self.account_info or not self._chat_service:
            return self._json_response(
                request, {"error": "no account configured"}, status=503
            )

        try:
            body = await request.json()
            peer_pubkey = body.get("public_key_hex", "")
            peer_username = body.get("username", "")
            peer_invite = body.get("invite_code", "")
        except (json.JSONDecodeError, Exception):
            return self._json_response(
                request, {"error": "invalid JSON"}, status=400
            )

        if not peer_pubkey or not peer_invite:
            return self._json_response(
                request, {"error": "missing public_key_hex or invite_code"},
                status=400,
            )

        # Verify invite code matches public key
        from desktop.node_identity import verify_invite_code
        if not verify_invite_code(peer_invite, peer_pubkey):
            return self._json_response(
                request, {"error": "invite code mismatch"}, status=403
            )

        from desktop.p2p import invite_tokens

        token_id = body.get("token_id") or None
        grant_in = body.get("grant") or None
        own_invite = self.account_info.get("invite_code", "")
        own_pubkey = self.account_info.get("public_key_hex", "")

        # Token/grant paths bypass mutual-add — require a fresh signature.
        if token_id or grant_in:
            ts = body.get("ts")
            if not self._ts_ok(ts):
                return self._json_response(
                    request, {"error": "stale timestamp"}, status=403)
            label = "token_handshake" if token_id else "grant_handshake"
            tid = token_id or (grant_in or {}).get("token_id", "")
            signed = f"{label}:{int(ts)}:{tid}:{own_invite}"
            if not self._verify_peer_sig(peer_pubkey, signed,
                                         body.get("signature", "")):
                return self._json_response(
                    request, {"error": "invalid signature"}, status=403)

        friends = self._chat_service.get_friends()
        match = next(
            (f for f in friends
             if f["invite_code"] == peer_invite
             or f["public_key_hex"] == peer_pubkey), None)
        if match and match.get("is_blocked"):
            return self._json_response(
                request, {"error": "blocked"}, status=403)
        resolved = (match if match
                    and match["public_key_hex"] == peer_pubkey else None)

        def _accept(grant_out=None):
            payload = {
                "accepted": True,
                "public_key_hex": own_pubkey,
                "username": self.account_info.get("username", ""),
                "invite_code": own_invite,
            }
            if grant_out:
                payload["grant"] = grant_out
            return self._json_response(request, payload)

        conn = self._chat_service.db_conn()

        # -- idempotent re-accept: friendship already established -----------
        if resolved is not None and (token_id or grant_in):
            tid = token_id or grant_in["token_id"]
            rights = invite_tokens.friend_rights(conn, resolved["id"])
            if not rights and resolved.get("source") == "token":
                # First accept was interrupted between add_friend and the
                # snapshot — the guest's 15s retry loop lands here to heal
                # it. The use was already burned, so read rights directly.
                rights = (invite_tokens.token_rights(conn, token_id)
                          if token_id else grant_in.get("rights", []))
                invite_tokens.snapshot_rights(conn, resolved["id"], rights)
            self._chat_service.update_friend_last_seen(peer_pubkey)
            return _accept(self._mint_grant(tid, rights, peer_pubkey))

        if token_id:
            requires_cert = invite_tokens.token_requires_cert(conn, token_id)
            if requires_cert is None:
                return self._json_response(
                    request, {"error": "token invalid"}, status=403)
            if requires_cert:
                # Identity gate BEFORE the use is burned: certificate v2 +
                # (for method:pow) the mined proof, verified once and cached
                # in p2p_identities. "busy" is a 503 the guest's 15 s retry
                # loop absorbs; a forged proof bans the key.
                admission = await self._identity_gate().admit(
                    peer_pubkey, body.get("birth_cert") or {},
                    body.get("identity_proof"), request.remote)
                if admission.status != "verified":
                    headers = ({"Retry-After": str(admission.retry_after)}
                               if admission.retry_after else None)
                    return self._json_response(
                        request, {"error": admission.detail},
                        status=admission.http_status, headers=headers)
            tok = invite_tokens.consume_token(conn, token_id)
            if tok is None:
                return self._json_response(
                    request, {"error": "token invalid"}, status=403)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM friends WHERE source = 'token'"
                    " AND added_at > NOW() - INTERVAL '1 hour'")
                if cur.fetchone()[0] >= TOKEN_FRIENDS_PER_HOUR:
                    return self._json_response(
                        request, {"error": "token accept rate exceeded"},
                        status=429)

            fid = self._chat_service.add_friend(
                public_key_hex=peer_pubkey, invite_code=peer_invite,
                username=peer_username, source="token",
                source_token_id=token_id)
            invite_tokens.snapshot_rights(conn, fid, tok["rights"])
            if tok["welcome_message"]:
                self._chat_service.store_message(
                    fid, "out", tok["welcome_message"], delivered=True)
            self._chat_service.update_friend_last_seen(peer_pubkey)
            logger.info(f"Token handshake accepted from {peer_username} "
                        f"({peer_pubkey[:16]}...)")
            return _accept(self._mint_grant(token_id, tok["rights"],
                                            peer_pubkey))

        if grant_in:
            # Recovery: our own signature over their grant replaces the
            # friends row this device never had (or lost with the previous
            # device).
            if not invite_tokens.verify_grant(grant_in, own_pubkey):
                return self._json_response(
                    request, {"error": "invalid grant"}, status=403)
            if grant_in.get("guest_pubkey", "").lower() != peer_pubkey.lower():
                return self._json_response(
                    request, {"error": "grant subject mismatch"}, status=403)
            revoked = invite_tokens.token_revoked(conn, grant_in["token_id"])
            if revoked is True:
                return self._json_response(
                    request, {"error": "token revoked"}, status=403)
            # FK-safe: reference the token row only when it exists locally.
            src_tid = grant_in["token_id"] if revoked is not None else None
            fid = self._chat_service.add_friend(
                public_key_hex=peer_pubkey, invite_code=peer_invite,
                username=peer_username, source="token",
                source_token_id=src_tid)
            invite_tokens.snapshot_rights(conn, fid,
                                          grant_in.get("rights", []))
            self._chat_service.update_friend_last_seen(peer_pubkey)
            logger.info(f"Grant recovery accepted from {peer_username} "
                        f"({peer_pubkey[:16]}...)")
            return _accept(self._mint_grant(grant_in["token_id"],
                                            grant_in.get("rights", []),
                                            peer_pubkey))

        # -- classic path: mutual-add consent -------------------------------
        if match is None:
            logger.info(
                f"Handshake rejected from {peer_username} "
                f"({peer_pubkey[:16]}...) — not in our friends list"
            )
            return self._json_response(
                request,
                {"accepted": False, "error": "not in friends list"},
                status=403,
            )

        self._chat_service.add_friend(
            public_key_hex=peer_pubkey,
            invite_code=peer_invite,
            username=peer_username,
        )
        # A successful handshake means the peer's launcher is up
        # and reachable right now — it's the cleanest passive
        # presence signal we have. Without this update the
        # friend stays "offline" in the UI until they actually
        # send a chat message, which mismatched the "is the
        # peer online?" question users actually want answered.
        self._chat_service.update_friend_last_seen(peer_pubkey)
        logger.info(
            f"Handshake accepted from {peer_username} "
            f"({peer_pubkey[:16]}...)"
        )
        return _accept()

    async def handle_chat_message(self, request: web.Request) -> web.Response:
        """POST /api/chat/message — receive an encrypted message."""
        ip = request.remote or "unknown"
        if not self._check_rate_limit(ip):
            return self._json_response(
                request, {"error": "rate limited"}, status=429
            )

        if not self._chat_service:
            return self._json_response(
                request, {"error": "chat not available"}, status=503
            )

        try:
            body = await request.json()
            sender_pubkey = body.get("from_public_key", "")
            encrypted = body.get("encrypted", "")
            timestamp = body.get("timestamp", "")
            message_uuid = body.get("message_uuid", "")
        except (json.JSONDecodeError, Exception):
            return self._json_response(
                request, {"error": "invalid JSON"}, status=400
            )

        if not sender_pubkey or not encrypted or not timestamp:
            return self._json_response(
                request, {"error": "missing fields"}, status=400
            )

        from desktop.p2p.chat_service import MAX_ENCRYPTED_CHARS
        if len(encrypted) > MAX_ENCRYPTED_CHARS:
            return self._json_response(
                request, {"error": "message too large"}, status=413
            )

        from desktop.p2p import invite_tokens
        if not invite_tokens.friend_has_right(
                self._chat_service.db_conn(), sender_pubkey, "can_message"):
            return self._json_response(
                request, {"error": "not permitted"}, status=403
            )

        result = self._chat_service.handle_incoming(
            sender_pubkey, encrypted, timestamp,
            message_uuid=message_uuid or None,
        )

        if result is None:
            return self._json_response(
                request, {"error": "rejected"}, status=403
            )

        # Notify UI of new message
        if self._on_message_cb:
            try:
                self._on_message_cb(result)
            except Exception as e:
                logger.debug(f"Message callback error: {e}")

        # Wake backend SSE clients so receiver's UI updates instantly
        asyncio.ensure_future(self._wake_backend_sse())

        return self._json_response(request, {"status": "delivered"})

    async def handle_chat_history(self, request: web.Request) -> web.Response:
        """POST /api/chat/history — return conversation history for a friend.

        The requester sends their public key + Ed25519 signature to prove
        key ownership (prevents metadata leakage to third parties).

        Request:  {public_key_hex, signature, ts, nonce, since (optional ISO)}
          signature = Ed25519_sign("history_request:{ts}:{nonce}")
        Response: {messages: [{message_uuid, direction, encrypted, timestamp}]}

        The timestamp bound (±TS_WINDOW) replaced the unbound nonce scheme —
        a captured signature used to replay forever. Hard cutover on both
        surfaces (backend/routers/peer_chat mirrors this).
        """
        ip = request.remote or "unknown"
        if not self._check_rate_limit(ip):
            return self._json_response(
                request, {"error": "rate limited"}, status=429
            )

        if not self._chat_service:
            return self._json_response(
                request, {"error": "chat not available"}, status=503
            )

        try:
            body = await request.json()
            requester_pubkey = body.get("public_key_hex", "")
            since_iso = body.get("since")
            signature_hex = body.get("signature", "")
            nonce = body.get("nonce", "")
            ts = body.get("ts")
        except (json.JSONDecodeError, Exception):
            return self._json_response(
                request, {"error": "invalid JSON"}, status=400
            )

        if not requester_pubkey:
            return self._json_response(
                request, {"error": "missing public_key_hex"}, status=400
            )

        # Verify proof-of-possession: timestamp-bound signature
        if not signature_hex or not nonce:
            return self._json_response(
                request, {"error": "missing signature or nonce"}, status=400
            )
        if not self._ts_ok(ts):
            return self._json_response(
                request, {"error": "stale timestamp"}, status=403
            )
        if not self._verify_peer_sig(
                requester_pubkey, f"history_request:{int(ts)}:{nonce}",
                signature_hex):
            return self._json_response(
                request, {"error": "invalid signature"}, status=403
            )

        # Verify requester is a known friend
        friend = self._chat_service.get_friend_by_public_key(requester_pubkey)
        if not friend:
            return self._json_response(
                request, {"error": "not a friend"}, status=403
            )
        if friend.get("is_blocked"):
            return self._json_response(
                request, {"error": "blocked"}, status=403
            )

        from desktop.p2p import invite_tokens
        if not invite_tokens.friend_has_right(
                self._chat_service.db_conn(), requester_pubkey, "can_message"):
            return self._json_response(
                request, {"error": "not permitted"}, status=403
            )

        # Parse optional since timestamp
        from datetime import datetime
        since = None
        if since_iso:
            try:
                since = datetime.fromisoformat(since_iso)
            except (ValueError, TypeError):
                pass

        # Get messages for this friend from our DB
        messages = self._chat_service.get_history_for_export(
            friend["id"], since=since
        )

        # Encrypt each message's content with requester's public key
        result_messages = []
        max_out_id = 0
        for msg in messages:
            if msg["direction"] == "out":
                max_out_id = max(max_out_id, msg["id"])
            encrypted = self._chat_service.encrypt_message(
                msg["content"], requester_pubkey
            )
            msg_ts = msg["timestamp"]
            ts_str = (
                msg_ts.isoformat() if hasattr(msg_ts, "isoformat")
                else str(msg_ts)
            )
            if "+" not in ts_str and not ts_str.endswith("Z"):
                ts_str += "+00:00"
            result_messages.append({
                # NULL-safe: str(None) would ship the literal string
                # "None" and crash the importer's uuid lookup.
                "message_uuid": (str(msg["message_uuid"])
                                 if msg["message_uuid"] else None),
                "direction": msg["direction"],
                "encrypted": encrypted,
                "timestamp": ts_str,
            })
        if max_out_id:
            # The requester proved key ownership and has the export now —
            # export IS delivery.
            self._chat_service.mark_exported_delivered(friend["id"],
                                                       max_out_id)

        logger.info(
            f"History export for {friend.get('username', '?')}: "
            f"{len(result_messages)} messages"
        )
        return self._json_response(request, {"messages": result_messages})

    async def handle_chat_key_rotation(
        self, request: web.Request
    ) -> web.Response:
        """POST /api/chat/key-rotation — receive key rotation notification."""
        ip = request.remote or "unknown"
        if not self._check_rate_limit(ip):
            return self._json_response(
                request, {"error": "rate limited"}, status=429
            )

        if not self._chat_service:
            return self._json_response(
                request, {"error": "chat not available"}, status=503
            )

        try:
            body = await request.json()
            old_pubkey = body.get("old_public_key", "")
            new_pubkey = body.get("new_public_key", "")
            new_invite = body.get("new_invite_code", "")
            rotation_msg_b64 = body.get("rotation_message", "")
            signature_b64 = body.get("signature", "")
        except (json.JSONDecodeError, Exception):
            return self._json_response(
                request, {"error": "invalid JSON"}, status=400
            )

        # Verify the rotation is signed by the old key
        import base64
        from desktop.node_identity import verify_signature
        rotation_msg = base64.b64decode(rotation_msg_b64)
        signature = base64.b64decode(signature_b64)

        if not verify_signature(rotation_msg, signature, old_pubkey):
            return self._json_response(
                request, {"error": "invalid signature"}, status=403
            )

        # Find friend by old public key
        friend = self._chat_service.get_friend_by_public_key(old_pubkey)
        if not friend:
            return self._json_response(
                request, {"error": "unknown sender"}, status=404
            )

        self._chat_service.store_key_rotation(
            friend_id=friend["id"],
            old_public_key_hex=old_pubkey,
            new_public_key_hex=new_pubkey,
            new_invite_code=new_invite,
            rotation_message=rotation_msg,
            signature=signature,
        )

        # Auto-apply key rotation
        self._chat_service.apply_key_rotation(friend["id"])
        logger.info(
            f"Key rotation applied for {friend.get('username', '?')}"
        )

        return self._json_response(request, {"status": "accepted"})

    async def handle_trigger_send(self, request: web.Request) -> web.Response:
        """POST /api/chat/trigger-send — trigger immediate P2P delivery.

        Called by the backend after a new outgoing message is saved.
        Returns immediately; delivery runs in the background.
        """
        if self._delivery_trigger_cb:
            asyncio.ensure_future(self._run_delivery_trigger())
        # Relay contract: a stored outgoing message may be for a wake
        # subscriber — ping them all (they pull history; a no-op pull is
        # cheap). Phase D refines this to per-friend pings off the
        # NOTIFY payload, like the backend mirror already does.
        self.ping_wake()
        return self._json_response(request, {"ok": True})

    # -----------------------------------------------------------------------
    # Relay protocol: wake stream + reachability probe
    # (MIRRORS backend/routers/peer_chat.py — keep contracts in step)
    # -----------------------------------------------------------------------

    def ping_wake(self, pubkey: Optional[str] = None,
                  kind: str = "message") -> None:
        """Signal one subscriber (or all, when pubkey is None)."""
        subs = ([self._wake_subs[pubkey]]
                if pubkey and pubkey in self._wake_subs
                else list(self._wake_subs.values()) if pubkey is None else [])
        for sub in subs:
            sub.kinds.add(kind)
            sub.loop.call_soon_threadsafe(sub.evt.set)

    async def handle_wake_stream(self, request: web.Request) -> web.Response:
        """GET /api/relay/wake-stream?pubkey=&ts=&sig= — SSE wake channel."""
        ip = request.remote or "unknown"
        if not self._check_rate_limit(ip):
            return self._json_response(
                request, {"error": "rate limited"}, status=429)
        if not self.account_info or not self._chat_service:
            return self._json_response(
                request, {"error": "relay not available"}, status=503)

        pubkey = request.query.get("pubkey", "")
        ts = request.query.get("ts", "")
        sig = request.query.get("sig", "")
        if not self._ts_ok(ts):
            return self._json_response(
                request, {"error": "stale timestamp"}, status=403)
        own_pubkey = self.account_info.get("public_key_hex", "")
        signed = f"wake_subscribe:{int(ts)}:{own_pubkey}:{pubkey}"
        if not self._verify_peer_sig(pubkey, signed, sig):
            return self._json_response(
                request, {"error": "invalid signature"}, status=403)

        # Admission (Phase D). A presented voucher is ALWAYS processed —
        # its signature authorizes the subscription and is the material
        # this relay hands to senders as proof of announce authority.
        # Friendship only waives the voucher's NECESSITY (the master-path
        # legacy): a friend-client that wants to be announced still sends
        # one. Gating on friendship first silently dropped exactly that —
        # two friendly launchers could never relay for each other.
        friend = self._chat_service.get_friend_by_public_key(pubkey)
        if friend and friend.get("is_blocked"):
            return self._json_response(
                request, {"error": "not a friend"}, status=403)
        voucher = None
        invite = request.query.get("invite", "")
        v_sig = request.query.get("voucher_sig", "")
        if invite and v_sig:
            try:
                v_until = int(request.query.get("voucher_until", ""))
            except ValueError:
                v_until = 0
            from desktop.node_identity import verify_invite_code
            if v_until <= int(time.time()):
                return self._json_response(
                    request, {"error": "voucher required"}, status=403)
            if not verify_invite_code(invite, pubkey):
                return self._json_response(
                    request, {"error": "invite does not match key"},
                    status=403)
            if not self._verify_peer_sig(
                    pubkey, voucher_payload(pubkey, own_pubkey, v_until),
                    v_sig):
                return self._json_response(
                    request, {"error": "invalid voucher"}, status=403)
            foreign = sum(1 for k in self._wake_subs
                          if k in self._relay_clients)
            if pubkey not in self._relay_clients \
                    and foreign >= self._relay_cap:
                return self._json_response(
                    request, {"error": "relay full"}, status=429)
            voucher = {"invite_code": invite, "until": v_until,
                       "signature": v_sig}
        elif not friend:
            return self._json_response(
                request, {"error": "voucher required"}, status=403)

        ip_count = sum(1 for s in self._wake_subs.values() if s.ip == ip)
        old = self._wake_subs.get(pubkey)
        if old is None and ip_count >= WAKE_MAX_PER_IP:
            return self._json_response(
                request, {"error": "too many subscriptions"}, status=429)
        if old is not None:
            old.closed = True
            old.loop.call_soon_threadsafe(old.evt.set)
        sub = _WakeSub(asyncio.Event(), asyncio.get_event_loop(), ip)
        self._wake_subs[pubkey] = sub
        if voucher is not None:
            self._relay_clients[pubkey] = voucher
            if self._client_announce_cb:
                try:
                    self._client_announce_cb(voucher["invite_code"])
                except Exception as e:
                    logger.warning("client announce failed: %s", e)

        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        is_client = voucher is not None
        # Presence bumps follow the FRIEND row, not the client flag — a
        # friend that also sent a voucher is still a friend in the UI.
        bump_presence = friend is not None
        await resp.prepare(request)
        try:
            await resp.write(b": connected\n\n")
            if bump_presence:
                self._chat_service.update_friend_last_seen(pubkey)
            cycles = 0
            while not sub.closed:
                try:
                    await asyncio.wait_for(sub.evt.wait(), timeout=15.0)
                    sub.evt.clear()
                    if sub.closed:
                        break
                    kinds, sub.kinds = sub.kinds, set()
                    envelopes = list(sub.envelopes)
                    sub.envelopes.clear()
                    # Envelopes first: a forwarded message is the payload,
                    # a wake is only a hint to go looking.
                    for envelope in envelopes:
                        frame = json.dumps(
                            {"type": "deliver", "envelope": envelope},
                            ensure_ascii=False)
                        await resp.write(
                            b"data: %s\n\n" % frame.encode("utf-8"))
                    for kind in sorted(kinds):
                        await resp.write(
                            b'data: {"type": "wake", "kind": "%s"}\n\n'
                            % kind.encode("ascii"))
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
                cycles += 1
                if cycles >= 8:          # ~2 min — passive presence bump
                    if bump_presence:
                        self._chat_service.update_friend_last_seen(pubkey)
                    cycles = 0
        except (ConnectionResetError, ConnectionError):
            pass  # subscriber went away — normal churn, not an error
        finally:
            if self._wake_subs.get(pubkey) is sub:
                del self._wake_subs[pubkey]
                # The announce lives exactly as long as the subscription:
                # a client we can no longer reach must not stay findable
                # through us. Only when THIS sub is the registered one — a
                # re-subscribe supersession must not withdraw the new life.
                if is_client:
                    rec = self._relay_clients.pop(pubkey, None)
                    if rec and self._client_withdraw_cb:
                        try:
                            self._client_withdraw_cb(rec["invite_code"])
                        except Exception as e:
                            logger.warning("client withdraw failed: %s", e)
        return resp

    async def handle_probe_connect(self, request: web.Request) -> web.Response:
        """POST /api/relay/probe-connect — connectability check, BT-tracker
        style: connect BACK to the request's source address (never a
        caller-supplied IP — no reflector) and confirm /health identity."""
        ip = request.remote or "unknown"
        if not self._check_rate_limit(ip):
            return self._json_response(
                request, {"error": "rate limited"}, status=429)
        if not self.account_info or not self._chat_service:
            return self._json_response(
                request, {"error": "relay not available"}, status=503)
        try:
            body = await request.json()
            pubkey = body.get("public_key_hex", "")
            port = int(body.get("port", 0))
            ts = body.get("ts")
            sig = body.get("signature", "")
        except (json.JSONDecodeError, ValueError, Exception):
            return self._json_response(
                request, {"error": "invalid JSON"}, status=400)
        if not pubkey or not (0 < port < 65536):
            return self._json_response(
                request, {"error": "missing fields"}, status=400)
        if not self._ts_ok(ts):
            return self._json_response(
                request, {"error": "stale timestamp"}, status=403)
        own_pubkey = self.account_info.get("public_key_hex", "")
        signed = f"probe_request:{int(ts)}:{own_pubkey}:{port}"
        if not self._verify_peer_sig(pubkey, signed, sig):
            return self._json_response(
                request, {"error": "invalid signature"}, status=403)
        friend = self._chat_service.get_friend_by_public_key(pubkey)
        if not friend or friend.get("is_blocked"):
            return self._json_response(
                request, {"error": "not a friend"}, status=403)

        now = time.monotonic()
        if now - self._probe_last.get(pubkey, 0) < PROBE_COOLDOWN:
            return self._json_response(
                request, {"error": "probe cooldown"}, status=429)
        self._probe_last[pubkey] = now
        if len(self._probe_last) > 10_000:   # bound the table under a flood
            for k in [k for k, v in self._probe_last.items()
                      if now - v > PROBE_COOLDOWN]:
                self._probe_last.pop(k, None)

        source_ip = request.remote or ""
        host = f"[{source_ip}]" if ":" in source_ip else source_ip
        reachable, error = False, None
        import aiohttp as _aiohttp
        try:
            async with _aiohttp.ClientSession(
                connector=_aiohttp.TCPConnector(ssl=False),
                timeout=_aiohttp.ClientTimeout(total=3),
            ) as session:
                async with session.get(
                        f"https://{host}:{port}/health") as r:
                    data = await r.json()
                    reachable = (r.status == 200
                                 and data.get("node_id") == pubkey)
                    if not reachable:
                        error = "identity mismatch"
        except Exception as e:
            error = type(e).__name__

        payload = {"reachable": reachable, "observed_ip": source_ip,
                   "tested_port": port}
        if error and not reachable:
            payload["error"] = error
        return self._json_response(request, payload)

    # -----------------------------------------------------------------------
    # Relay protocol: forwarding
    #
    # The relay is a pure forwarder — it stores NOTHING. A sender that cannot
    # reach the recipient directly hands us the E2E envelope; we push it down
    # the recipient's already-open wake stream and hold the sender's request
    # until the recipient signs a receipt. No ack, no delivery: the message
    # stays queued at the sender and is retried.
    #
    # The receipt is the recipient's Ed25519 signature over the message uuid
    # and the ciphertext hash, so the SENDER verifies delivery — a relay
    # cannot forge it, and a relay that fabricates an envelope gets no
    # receipt because the forgery will not decrypt.
    #
    # MIRRORS backend/routers/peer_chat.py — keep the contracts in step.
    # -----------------------------------------------------------------------

    async def handle_relay_forward(self, request: web.Request) -> web.Response:
        """POST /api/relay/forward — forward one envelope to a connected
        recipient and return their signed receipt."""
        ip = request.remote or "unknown"
        if not self._check_rate_limit(ip):
            return self._json_response(
                request, {"error": "rate limited"}, status=429)
        if not self.account_info or not self._chat_service:
            return self._json_response(
                request, {"error": "relay not available"}, status=503)
        try:
            body = await request.json()
            sender = body.get("public_key_hex", "")
            recipient = body.get("to_public_key", "")
            envelope = body.get("envelope") or {}
            ts = body.get("ts")
            sig = body.get("signature", "")
            message_uuid = envelope.get("message_uuid", "")
            encrypted = envelope.get("encrypted", "")
        except (json.JSONDecodeError, Exception):
            return self._json_response(
                request, {"error": "invalid JSON"}, status=400)
        if not (sender and recipient and message_uuid and encrypted
                and envelope.get("timestamp")):
            return self._json_response(
                request, {"error": "missing fields"}, status=400)

        from desktop.p2p.chat_service import MAX_ENCRYPTED_CHARS
        if len(encrypted) > MAX_ENCRYPTED_CHARS:
            return self._json_response(
                request, {"error": "message too large"}, status=413)
        if not self._ts_ok(ts):
            return self._json_response(
                request, {"error": "stale timestamp"}, status=403)
        ct_hash = hashlib.sha256(encrypted.encode("utf-8")).hexdigest()
        own_pubkey = self.account_info.get("public_key_hex", "")
        signed = (f"relay_forward:{int(ts)}:{own_pubkey}"
                  f":{message_uuid}:{ct_hash}")
        if not self._verify_peer_sig(sender, signed, sig):
            return self._json_response(
                request, {"error": "invalid signature"}, status=403)
        # Admission (Phase D): a friend may forward to anyone subscribed
        # here; a STRANGER may forward only to a voucher-registered client —
        # that is the whole point of being someone's relay, and the client's
        # E2E decrypt is what actually rejects mail from non-friends. A
        # blocked friend stays blocked on both paths.
        friend = self._chat_service.get_friend_by_public_key(sender)
        if friend and friend.get("is_blocked"):
            return self._json_response(
                request, {"error": "not a friend"}, status=403)
        if not friend and recipient not in self._relay_clients:
            return self._json_response(
                request, {"error": "not a friend"}, status=403)
        # The relay stamps the sender from the signature — a forwarded
        # envelope must never claim an authorship the signature does not back.
        envelope = dict(envelope, from_public_key=sender)

        sub = self._wake_subs.get(recipient)
        if sub is None:
            # Tell the sender immediately rather than burning the timeout:
            # "not connected" is an answer, and it keeps the message queued.
            return self._json_response(
                request, {"error": "recipient not connected"}, status=409)
        if len(sub.envelopes) >= FORWARD_QUEUE_MAX:
            return self._json_response(
                request, {"error": "recipient busy"}, status=429)
        inflight = sum(1 for r, _ in self._pending_acks.values() if r == sender)
        if inflight >= FORWARD_INFLIGHT_PER_SENDER:
            return self._json_response(
                request, {"error": "too many forwards in flight"}, status=429)

        future = asyncio.get_event_loop().create_future()
        self._pending_acks[message_uuid] = (recipient, future)
        sub.envelopes.append(envelope)
        sub.loop.call_soon_threadsafe(sub.evt.set)
        try:
            ack = await asyncio.wait_for(future, timeout=FORWARD_ACK_TIMEOUT)
        except asyncio.TimeoutError:
            return self._json_response(
                request, {"delivered": False, "reason": "no ack"})
        finally:
            if self._pending_acks.get(message_uuid, (None, None))[1] is future:
                del self._pending_acks[message_uuid]

        logger.info("Relayed %s… from %s… to %s…", message_uuid[:8],
                    sender[:8], recipient[:8])
        return self._json_response(request, {"delivered": True, "ack": ack})

    async def handle_relay_voucher(self, request: web.Request) -> web.Response:
        """GET /api/relay/voucher?invite= — prove our authority to relay for
        a client. The sender verifies the CLIENT's signature over
        {client_pubkey, our_pubkey, until}; we cannot forge it, so a
        black-hole impostor announcing someone else's invite has nothing to
        answer with here — and the sender drops the candidate."""
        ip = request.remote or "unknown"
        if not self._check_rate_limit(ip):
            return self._json_response(
                request, {"error": "rate limited"}, status=429)
        invite = request.query.get("invite", "")
        for pubkey, rec in self._relay_clients.items():
            if rec["invite_code"] == invite:
                return self._json_response(request, {
                    "client_pubkey": pubkey,
                    "invite_code": rec["invite_code"],
                    "relay_pubkey":
                        (self.account_info or {}).get("public_key_hex", ""),
                    "until": rec["until"],
                    "signature": rec["signature"],
                })
        return self._json_response(
            request, {"error": "no such client"}, status=404)

    async def handle_relay_ack(self, request: web.Request) -> web.Response:
        """POST /api/relay/ack — the recipient's signed receipt."""
        ip = request.remote or "unknown"
        if not self._check_rate_limit(ip):
            return self._json_response(
                request, {"error": "rate limited"}, status=429)
        if not self._chat_service:
            return self._json_response(
                request, {"error": "relay not available"}, status=503)
        try:
            body = await request.json()
            pubkey = body.get("public_key_hex", "")
            message_uuid = body.get("message_uuid", "")
            ct_hash = body.get("ciphertext_sha256", "")
            sig = body.get("signature", "")
        except (json.JSONDecodeError, Exception):
            return self._json_response(
                request, {"error": "invalid JSON"}, status=400)
        if not (pubkey and message_uuid and ct_hash and sig):
            return self._json_response(
                request, {"error": "missing fields"}, status=400)
        if not self._verify_peer_sig(
                pubkey, delivery_payload(message_uuid, ct_hash), sig):
            return self._json_response(
                request, {"error": "invalid signature"}, status=403)
        # A receipt may come from a friend OR a voucher-registered client
        # (Phase D) — the real gate is below: the uuid must match a pending
        # forward addressed to exactly this pubkey.
        friend = self._chat_service.get_friend_by_public_key(pubkey)
        if friend and friend.get("is_blocked"):
            return self._json_response(
                request, {"error": "not a friend"}, status=403)
        if not friend and pubkey not in self._relay_clients:
            return self._json_response(
                request, {"error": "not a friend"}, status=403)

        entry = self._pending_acks.get(message_uuid)
        # The uuid must be one WE are waiting on, for THIS recipient — so a
        # receipt cannot be planted for someone else's forward.
        if entry is None or entry[0] != pubkey:
            return self._json_response(
                request, {"error": "no such forward"}, status=404)
        future = entry[1]
        if not future.done():
            future.set_result({
                "public_key_hex": pubkey, "message_uuid": message_uuid,
                "ciphertext_sha256": ct_hash, "signature": sig})
        return self._json_response(request, {"ok": True})

    async def _run_delivery_trigger(self):
        """Run the delivery trigger callback with error handling."""
        try:
            await self._delivery_trigger_cb()
        except Exception as e:
            logger.debug(f"Delivery trigger error: {e}")

    async def handle_invite_accepted(self, request: web.Request) -> web.Response:
        """POST /api/chat/invite-accepted — peer nudge after accepting our invite.

        When Sasha accepts Masha's invite (via Worker), Sasha's P2P manager
        finds Masha online and sends this nudge. Masha's server checks the
        Worker for pending accepts, auto-adds Sasha, and returns handshake
        data so Sasha can resolve the friend immediately.

        Request:  {public_key_hex, username, invite_code}
        Response: {accepted: true/false, public_key_hex, username, invite_code}
        """
        ip = request.remote or "unknown"
        if not self._check_rate_limit(ip):
            return self._json_response(
                request, {"error": "rate limited"}, status=429
            )

        if not self.account_info:
            return self._json_response(
                request, {"error": "no account configured"}, status=503
            )

        try:
            body = await request.json()
            peer_pubkey = body.get("public_key_hex", "")
            peer_username = body.get("username", "")
            peer_invite = body.get("invite_code", "")
        except (json.JSONDecodeError, Exception):
            return self._json_response(
                request, {"error": "invalid JSON"}, status=400
            )

        if not peer_pubkey or not peer_invite:
            return self._json_response(
                request, {"error": "missing fields"}, status=400
            )

        # Verify invite code matches public key
        from desktop.node_identity import verify_invite_code
        if not verify_invite_code(peer_invite, peer_pubkey):
            return self._json_response(
                request, {"error": "invite code mismatch"}, status=403
            )

        # Check DB first — skip Worker call if no pending invites
        loop = asyncio.get_event_loop()
        has_pending = await loop.run_in_executor(
            None, self._has_pending_invites_db
        )
        if not has_pending:
            return self._json_response(
                request, {"accepted": False, "error": "no pending invites"},
                status=403,
            )

        # Check Worker for pending accepts (blocking call → run in executor)
        accepts = await loop.run_in_executor(None, self._fetch_pending_accepts)

        # Look for this peer among the accepts
        found = any(a.get("invite_code") == peer_invite for a in accepts)

        if not found:
            return self._json_response(
                request, {"accepted": False, "error": "no pending accept found"},
                status=403,
            )

        # Auto-add the peer as friend
        if self._chat_service:
            self._chat_service.add_friend(
                public_key_hex=peer_pubkey,
                invite_code=peer_invite,
                username=peer_username,
            )
            # Same passive-presence rule as for ordinary handshake:
            # a successful nudge means the peer's launcher is up
            # right now, so mark them online. Without this the
            # auto-added friend stays offline in the UI until the
            # next chat message or 15-min handshake refresh.
            self._chat_service.update_friend_last_seen(peer_pubkey)
            logger.info(
                f"Invite-accepted nudge: auto-added {peer_username} "
                f"({peer_pubkey[:16]}...)"
            )
            # Wake Web UI so the new friend appears immediately
            asyncio.ensure_future(self._wake_backend_sse())

        return self._json_response(request, {
            "accepted": True,
            "public_key_hex": self.account_info.get("public_key_hex", ""),
            "username": self.account_info.get("username", ""),
            "invite_code": self.account_info.get("invite_code", ""),
        })

    def _has_pending_invites_db(self) -> bool:
        """Blocking: check DB for unreciprocated sent invites."""
        try:
            conn = self._new_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM sent_invites "
                        "WHERE sent_at > NOW() - INTERVAL '30 days' LIMIT 1"
                    )
                    return cur.fetchone() is not None
            finally:
                conn.close()
        except Exception:
            return False

    @staticmethod
    def _fetch_pending_accepts() -> list[dict]:
        """Blocking: check Worker for pending accepts."""
        try:
            from desktop.p2p.email_verify import check_pending_accepts
            return check_pending_accepts()
        except Exception as e:
            logger.error(f"Failed to fetch pending accepts: {e}")
            return []

    async def _wake_backend_sse(self):
        """Tell the local backend to wake SSE clients (incoming message)."""
        if not self._backend_port:
            return
        import aiohttp as _aiohttp
        try:
            async with _aiohttp.ClientSession(
                connector=_aiohttp.TCPConnector(ssl=False),
            ) as session:
                async with session.post(
                    f"http://127.0.0.1:{self._backend_port}/api/p2p/chat/wake",
                    timeout=_aiohttp.ClientTimeout(total=2),
                ):
                    pass
        except Exception:
            pass  # Fallback: DB NOTIFY will wake SSE eventually

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def start(self):
        """Build aiohttp app, bind to port, start serving."""
        # Replica inventory table (idempotent DDL) — a node updated in
        # place gains it without a manual migration.
        try:
            def _prep():
                conn = self._new_db()
                try:
                    mb_slice_queries.ensure_blob_table(conn)
                    from desktop.p2p import identity_registry
                    identity_registry.ensure_schema(conn)
                    contact_log.ensure_schema(conn)
                    conn.commit()
                finally:
                    conn.close()
            await asyncio.get_event_loop().run_in_executor(None, _prep)
        except Exception as e:
            logger.warning(f"slice blob table init failed: {e}")
        self._contact_log.start()
        self._app = web.Application(middlewares=[self._inbound_middleware])
        self._app.router.add_get("/health", self.handle_health)
        self._app.router.add_post("/api/sync/inventory", self.handle_inventory)
        self._app.router.add_post(
            "/api/sync/pull/{category}", self.handle_pull
        )
        self._app.router.add_post("/api/sync/offer", self.handle_carry_offer)
        self._app.router.add_post(
            "/api/sync/push/{category}", self.handle_carry_push)
        self._app.router.add_post("/api/mb/slice", self.handle_mb_slice)
        self._app.router.add_get("/api/mb/search", self.handle_mb_search)
        # Chat endpoints
        self._app.router.add_post(
            "/api/chat/handshake", self.handle_chat_handshake
        )
        self._app.router.add_post(
            "/api/chat/message", self.handle_chat_message
        )
        self._app.router.add_post(
            "/api/chat/history", self.handle_chat_history
        )
        self._app.router.add_post(
            "/api/chat/key-rotation", self.handle_chat_key_rotation
        )
        self._app.router.add_post(
            "/api/chat/trigger-send", self.handle_trigger_send
        )
        self._app.router.add_post(
            "/api/chat/invite-accepted", self.handle_invite_accepted
        )
        # Relay protocol (proxy-agnostic contract; this node = a relay
        # candidate once Phase D lands, the master serves it today)
        self._app.router.add_get(
            "/api/relay/wake-stream", self.handle_wake_stream
        )
        self._app.router.add_post(
            "/api/relay/probe-connect", self.handle_probe_connect
        )
        self._app.router.add_post(
            "/api/relay/forward", self.handle_relay_forward
        )
        self._app.router.add_post(
            "/api/relay/ack", self.handle_relay_ack
        )
        self._app.router.add_get(
            "/api/relay/voucher", self.handle_relay_voucher
        )

        self._runner = web.AppRunner(
            self._app,
            access_log=None,
            keepalive_timeout=30,    # close idle keep-alive after 30s
        )
        await self._runner.setup()

        # Enable TLS with self-signed certificate
        ssl_ctx = None
        try:
            from desktop.node_identity import get_server_ssl_context
            ssl_ctx = get_server_ssl_context()
        except Exception as e:
            logger.warning(f"TLS not available, falling back to HTTP: {e}")

        self._site = web.TCPSite(
            self._runner, "0.0.0.0", self.port, ssl_context=ssl_ctx,
            reuse_address=True,
            shutdown_timeout=5.0,    # fast restart on health-check failure
        )
        try:
            await self._site.start()
            proto = "HTTPS" if ssl_ctx else "HTTP"
            logger.info(f"Sync server listening on {proto} port {self.port}")
        except OSError as e:
            logger.error(f"Failed to bind port {self.port}: {e}")
            raise

    async def stop(self):
        """Graceful shutdown."""
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        await asyncio.get_event_loop().run_in_executor(None, self._contact_log.stop)
        logger.info("Sync server stopped")
