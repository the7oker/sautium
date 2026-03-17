"""
aiohttp-based HTTP sync server for P2P data exchange.

Exposes the same endpoints as backend/routers/sync.py so that other
launchers can sync enrichment data from this node using the standard
SyncClient / BackendAPIClient.

Runs in a background asyncio event loop thread alongside the DHT service.
"""

import asyncio
import gzip
import json
import logging
import time
from collections import defaultdict
from functools import partial
from typing import Optional

import psycopg2

from aiohttp import web

from desktop.p2p import sync_queries

logger = logging.getLogger(__name__)

# Rate limiting: max requests per IP per minute
RATE_LIMIT_PER_MINUTE = 60
RATE_LIMIT_WINDOW = 60  # seconds


class SyncServer:
    """HTTP server that serves sync endpoints for peer-to-peer data exchange."""

    def __init__(self, db_dsn: str, port: int = 19000, node_id: str = ""):
        self.db_dsn = db_dsn
        self.port = port
        self.node_id = node_id
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._conn: Optional[psycopg2.extensions.connection] = None
        # Rate limiting state
        self._request_counts: dict[str, list[float]] = defaultdict(list)

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
            "type": "musicaidj-peer",
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

        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        try:
            await self._site.start()
            logger.info(f"Sync server listening on port {self.port}")
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
