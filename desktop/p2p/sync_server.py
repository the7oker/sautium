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


class SyncServer:
    """HTTP server that serves sync endpoints for peer-to-peer data exchange."""

    def __init__(self, db_dsn: str, port: int = 19000, node_id: str = "",
                 account_info: Optional[dict] = None):
        self.db_dsn = db_dsn
        self.port = port
        self.node_id = node_id
        self.account_info = account_info  # {username, public_key_hex, invite_code}
        self._chat_service = None  # set via set_chat_service()
        self._on_message_cb: Optional[Callable] = None
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._conn: Optional[psycopg2.extensions.connection] = None
        # Rate limiting state
        self._request_counts: dict[str, list[float]] = defaultdict(list)

    def set_chat_service(self, chat_service, on_message_cb: Callable = None):
        """Attach chat service for handling chat endpoints."""
        self._chat_service = chat_service
        self._on_message_cb = on_message_cb

    def _get_db(self) -> psycopg2.extensions.connection:
        """Get or create a persistent DB connection."""
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self.db_dsn)
            self._conn.autocommit = True
        return self._conn

    def _check_rate_limit(self, ip: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = time.time()
        # Clean old entries
        self._request_counts[ip] = [
            t for t in self._request_counts[ip]
            if now - t < RATE_LIMIT_WINDOW
        ]
        if len(self._request_counts[ip]) >= RATE_LIMIT_PER_MINUTE:
            return False
        self._request_counts[ip].append(now)
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
        conn = self._get_db()
        return await loop.run_in_executor(None, partial(func, conn, *args))

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

        try:
            result = await self._run_query(
                sync_queries.get_inventory, track_uuids
            )
            return self._json_response(request, result)
        except Exception as e:
            logger.error(f"Inventory query failed: {e}")
            return self._json_response(
                request, {"error": str(e)}, status=500
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

        try:
            result = await self._run_query(handler, uuids)
            return self._json_response(request, result)
        except Exception as e:
            logger.error(f"Pull {category} failed: {e}")
            return self._json_response(
                request, {"error": str(e)}, status=500
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

        # Auto-accept: add to friends if chat service available
        if self._chat_service:
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
        except (json.JSONDecodeError, Exception):
            return self._json_response(
                request, {"error": "invalid JSON"}, status=400
            )

        if not sender_pubkey or not encrypted or not timestamp:
            return self._json_response(
                request, {"error": "missing fields"}, status=400
            )

        result = self._chat_service.handle_incoming(
            sender_pubkey, encrypted, timestamp
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

        return self._json_response(request, {"status": "delivered"})

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
            "/api/chat/key-rotation", self.handle_chat_key_rotation
        )

        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()

        # Enable TLS with self-signed certificate
        ssl_ctx = None
        try:
            from desktop.node_identity import get_server_ssl_context
            ssl_ctx = get_server_ssl_context()
        except Exception as e:
            logger.warning(f"TLS not available, falling back to HTTP: {e}")

        self._site = web.TCPSite(
            self._runner, "0.0.0.0", self.port, ssl_context=ssl_ctx
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
        if self._conn and not self._conn.closed:
            self._conn.close()
        logger.info("Sync server stopped")
