"""The P2P sync surface — the only Sautium surface that may face the internet.

Runs as a SECOND uvicorn server (see main.py `_start_p2p_server`) on its own
port, serving nothing but the peer protocol: inventory, enrichment pulls, MB
slices, and a `/health` peers identify the node by.

Why a separate port at all: the Web UI on 8800 inlines the API secret into
its HTML (window.__SAUTIUM_SECRET), so forwarding that port hands an
attacker the whole API — playback, settings, library, AI DJ. The launcher
has had this split from the start (Web UI on 18000, sync on a random
20000-29999 port); Docker had both on 8800, which is why a Docker master
could never be a reachable node. Everything here is readable by any peer
that connects, which is the P2P protocol's own model (see the
public/friends flag discussion in docs/design/P2P-SYNC-INTEGRITY.md) — so
this app must never gain a route that writes, configures, or reveals a
secret.

Rate limiting is per-IP and in-process, mirroring the launcher's aiohttp
server: an internet-facing port meets scanners, and a peer that wants more
than a request a second is not syncing.
"""

import logging
import time
from collections import defaultdict

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from routers.sync import (
    SYNC_CAPABILITIES, mb_dump_version, mb_router, node_pubkey_hex, router,
)

logger = logging.getLogger(__name__)

RATE_LIMIT_PER_MINUTE = 60
RATE_LIMIT_WINDOW = 60


app = FastAPI(
    title="Sautium P2P",
    description="Peer sync surface",
    # No interactive docs: this port faces the internet and the schema is
    # not part of the peer contract.
    docs_url=None, redoc_url=None, openapi_url=None,
)

app.add_middleware(GZipMiddleware, minimum_size=1000)

_hits: dict[str, list[float]] = defaultdict(list)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    recent = [t for t in _hits.get(ip, ()) if now - t < RATE_LIMIT_WINDOW]
    if len(recent) >= RATE_LIMIT_PER_MINUTE:
        _hits[ip] = recent
        return JSONResponse({"error": "rate limited"}, status_code=429)
    recent.append(now)
    _hits[ip] = recent
    if len(_hits) > 10_000:                    # bound the table under a flood
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > RATE_LIMIT_WINDOW]:
            _hits.pop(k, None)
    return await call_next(request)


@app.get("/health")
async def health() -> dict:
    """Peer-facing identity. Same contract as the launcher's sync server —
    node_id + mb_dump + capabilities are what a peer picks sources by."""
    return {
        "status": "ok",
        "type": "sautium-peer",
        "node_id": node_pubkey_hex(),
        "mb_dump": mb_dump_version(),
        "capabilities": SYNC_CAPABILITIES,
    }


app.include_router(router)
app.include_router(mb_router)
