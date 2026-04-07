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
import json
import logging
import time
from collections import defaultdict
from functools import partial
from typing import Callable, Optional

import psycopg2

from aiohttp import web

from desktop.p2p import sync_queries

logger = logging.getLogger(__name__)

# Rate limiting: max requests per IP per minute
RATE_LIMIT_PER_MINUTE = 60
RATE_LIMIT_WINDOW = 60  # seconds

# Max UUIDs per request (prevents memory/DB DoS)
MAX_UUIDS_PER_REQUEST = 10_000


class SyncServer:
    """HTTP server that serves sync endpoints for peer-to-peer data exchange."""

    def __init__(self, db_dsn: str, port: int = 19000, node_id: str = "",
                 account_info: Optional[dict] = None,
                 backend_port: int = 0):
        self.db_dsn = db_dsn
        self.port = port
        self.node_id = node_id
        self.account_info = account_info  # {username, public_key_hex, invite_code}
        self._backend_port = backend_port
        self._chat_service = None  # set via set_chat_service()
        self._on_message_cb: Optional[Callable] = None
        self._delivery_trigger_cb: Optional[Callable] = None
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        # Rate limiting state
        self._request_counts: dict[str, list[float]] = defaultdict(list)

    def set_chat_service(self, chat_service, on_message_cb: Callable = None):
        """Attach chat service for handling chat endpoints."""
        self._chat_service = chat_service
        self._on_message_cb = on_message_cb

    def set_delivery_trigger(self, cb: Callable):
        """Set callback to trigger immediate P2P message delivery."""
        self._delivery_trigger_cb = cb

    def _new_db(self) -> psycopg2.extensions.connection:
        """Create a new DB connection (caller must close it)."""
        conn = psycopg2.connect(self.db_dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET timezone = 'UTC'")
        return conn

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
                       status: int = 200) -> web.Response:
        """Create a JSON response, with gzip if client accepts it."""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}

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
        """Health check endpoint."""
        return self._json_response(request, {
            "status": "ok",
            "node_id": self.node_id,
            "type": "sautium-peer",
        })

    async def handle_inventory(self, request: web.Request) -> web.Response:
        """POST /api/sync/inventory — check available enrichment data."""
        ip = request.remote or "unknown"
        if not self._check_rate_limit(ip):
            return self._json_response(
                request, {"error": "rate limited"}, status=429
            )

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

        if not isinstance(uuids, list) or len(uuids) > MAX_UUIDS_PER_REQUEST:
            return self._json_response(
                request, {"error": f"uuids must be a list of at most {MAX_UUIDS_PER_REQUEST} items"},
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
    # Chat handlers
    # -----------------------------------------------------------------------

    async def handle_chat_handshake(self, request: web.Request) -> web.Response:
        """POST /api/chat/handshake — exchange public keys for friend request."""
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
                request, {"error": "missing public_key_hex or invite_code"},
                status=400,
            )

        # Verify invite code matches public key
        from desktop.node_identity import verify_invite_code
        if not verify_invite_code(peer_invite, peer_pubkey):
            return self._json_response(
                request, {"error": "invite code mismatch"}, status=403
            )

        # Mutual invite check: only accept if we already added this peer
        if self._chat_service:
            friends = self._chat_service.get_friends()
            already_added = any(
                f["invite_code"] == peer_invite
                or f["public_key_hex"] == peer_pubkey
                for f in friends
            )
            if not already_added:
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
            logger.info(
                f"Handshake accepted from {peer_username} "
                f"({peer_pubkey[:16]}...)"
            )

        return self._json_response(request, {
            "accepted": True,
            "public_key_hex": self.account_info.get("public_key_hex", ""),
            "username": self.account_info.get("username", ""),
            "invite_code": self.account_info.get("invite_code", ""),
        })

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

        Request:  {public_key_hex, signature, nonce, since (optional ISO timestamp)}
          signature = Ed25519_sign("history_request:{nonce}")
        Response: {messages: [{message_uuid, direction, encrypted, timestamp}]}
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
        except (json.JSONDecodeError, Exception):
            return self._json_response(
                request, {"error": "invalid JSON"}, status=400
            )

        if not requester_pubkey:
            return self._json_response(
                request, {"error": "missing public_key_hex"}, status=400
            )

        # Verify proof-of-possession: requester must sign a nonce
        if not signature_hex or not nonce:
            return self._json_response(
                request, {"error": "missing signature or nonce"}, status=400
            )

        from desktop.node_identity import verify_signature
        try:
            sig_bytes = bytes.fromhex(signature_hex)
            msg_bytes = f"history_request:{nonce}".encode("utf-8")
            if not verify_signature(msg_bytes, sig_bytes, requester_pubkey):
                return self._json_response(
                    request, {"error": "invalid signature"}, status=403
                )
        except (ValueError, Exception):
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
        for msg in messages:
            encrypted = self._chat_service.encrypt_message(
                msg["content"], requester_pubkey
            )
            ts = msg["timestamp"]
            ts_str = (
                ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            )
            if "+" not in ts_str and not ts_str.endswith("Z"):
                ts_str += "+00:00"
            result_messages.append({
                "message_uuid": msg["message_uuid"],
                "direction": msg["direction"],
                "encrypted": encrypted,
                "timestamp": ts_str,
            })

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
                        "WHERE reciprocated = FALSE LIMIT 1"
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
        self._app = web.Application()
        self._app.router.add_get("/health", self.handle_health)
        self._app.router.add_post("/api/sync/inventory", self.handle_inventory)
        self._app.router.add_post(
            "/api/sync/pull/{category}", self.handle_pull
        )
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
        logger.info("Sync server stopped")
