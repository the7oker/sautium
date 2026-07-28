"""
HMAC request signing middleware for the Sautium FastAPI backend.

Each privileged request must carry two headers:

    X-Sautium-Ts:  unix seconds (int as string)
    X-Sautium-Sig: hex(HMAC-SHA256(secret, canonical))

where:

    canonical = METHOD + "\n" + PATH_AND_QUERY + "\n" + TS + "\n" + sha256_hex(body)

The shared secret lives in ``data/.api_secret`` (32 bytes, mode 0600);
the file is created on first startup if missing. Native launcher
clients and the JS frontend read the same file (frontend gets it
inlined into the HTML at GET / so it never travels through a
separate fetch).

Whitelisted paths (no signature required):

    /health              (Docker healthcheck)
    /                    (HTML root — must be unauthenticated so the
                          page can load and read the inlined secret)
    /static/*            (CSS, JS, fonts)
    /api/covers/*        (album cover art — loaded via <img src>,
                          which cannot attach custom headers)
    /api/sync/*          (P2P sync from remote launchers — has its
                          own Ed25519 auth in sync_server.py)
    /sync/*              (legacy P2P sync)
    /api/p2p/chat/wake   (loopback-only "ping" from sync_server)

Replay window: ±60 seconds. A request older than 60s or 60s in the
future is rejected even with a valid signature.

The middleware reads request body once and stashes it in
``request._body`` so downstream handlers see the same bytes.
"""

import errno
import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

import device_auth

logger = logging.getLogger(__name__)

REPLAY_WINDOW_SECONDS = 60

WHITELIST_EXACT = {
    "/health",
    "/",
    "/api/p2p/chat/wake",
    # A client with no token yet cannot sign — these ARE the credential
    # checks. They defend themselves: /login costs an Argon2id derivation
    # under a semaphore, /pair burns one of five attempts under a lock.
    "/api/auth/status",
    "/api/auth/login",
    "/api/auth/pair",
}

WHITELIST_PREFIX = (
    "/static/",
    "/api/covers/",
    "/api/sync/",
    "/api/mb/",
    "/sync/",
    # <audio> elements can't set HMAC headers — these routes verify their
    # own short-lived query-param signatures instead (media_urls.verify).
    "/api/player/media/",
)


def _is_whitelisted(path: str) -> bool:
    if path in WHITELIST_EXACT:
        return True
    return any(path.startswith(p) for p in WHITELIST_PREFIX)


def _read_existing(secret_path: Path) -> Optional[str]:
    """Return secret contents if the file is present, else None.

    Retries on EIO: WSL2 9p drvfs (the bind-mount that backs
    ``backend/data/`` from the Windows host) intermittently returns
    "Input/output error" on reads when the host filesystem is under
    contention (antivirus scan, sleep/wake, etc). The condition
    clears within milliseconds, so a short backoff recovers cleanly.
    Other OSErrors (FileNotFoundError, permission, etc) propagate.
    """
    for attempt in range(3):
        try:
            if not secret_path.exists():
                return None
            return secret_path.read_text(encoding="ascii").strip()
        except OSError as exc:
            if exc.errno == errno.EIO and attempt < 2:
                time.sleep(0.05 * (attempt + 1))
                continue
            raise


def ensure_secret(secret_path: Path) -> bytes:
    """Return the secret bytes; generate the file if missing.

    The file holds urlsafe base64 of 32 random bytes (43 chars). We
    use the printable form so it can be inlined into HTML/headers
    without escaping. The HMAC key is the printable string itself
    (utf-8 bytes), not the decoded random bytes — keeps the JS side
    simple (no base64 decode in WebCrypto importKey).
    """
    data = _read_existing(secret_path)
    if data:
        # Re-apply readable mode every startup: Docker writes as
        # root, native launcher needs to read it. See note below
        # about why 0644 is acceptable here.
        try:
            os.chmod(secret_path, 0o644)
        except OSError:
            pass
        return data.encode("ascii")

    secret_path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    secret_path.write_text(token, encoding="ascii")
    # 0644 (not 0600): Docker container writes the file as root, the
    # native launcher reads it as the host user. Cross-UID readability
    # matters more here than guarding against other host users —
    # hostile local users are explicitly out of scope (see CLAUDE.md
    # "Security Posture", threat model).
    try:
        os.chmod(secret_path, 0o644)
    except OSError:
        pass  # Windows ACLs handled separately; not fatal here
    logger.info(f"Generated new API secret at {secret_path}")
    return token.encode("ascii")


def sign(secret: bytes, method: str, path_and_query: str, ts: str, body: bytes) -> str:
    """Compute the hex HMAC for a request. Used by sync_server's
    loopback callbacks and by tests."""
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = f"{method}\n{path_and_query}\n{ts}\n{body_hash}"
    return hmac.new(secret, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


# Monotonic timestamp of the last HMAC-VERIFIED request — a "someone is
# actively using the Web UI" signal for background schedulers (the DHT
# announcer yields to foreground traffic). Whitelisted paths (health probes,
# static, media) deliberately don't count: the launcher polls /health every
# 30s and would otherwise keep the node permanently "busy".
_last_ui_activity: float = 0.0


def _mark_ui_activity() -> None:
    global _last_ui_activity
    _last_ui_activity = time.monotonic()


def seconds_since_ui_activity() -> float:
    """Seconds since the last authenticated UI request (inf if never)."""
    if not _last_ui_activity:
        return float("inf")
    return time.monotonic() - _last_ui_activity


class HMACAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, secret_path: Path):
        super().__init__(app)
        self._secret_path = secret_path
        self._secret: Optional[bytes] = None

    def _get_secret(self) -> bytes:
        if self._secret is None:
            self._secret = ensure_secret(self._secret_path)
        return self._secret

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if _is_whitelisted(path):
            return await call_next(request)

        ts_raw = request.headers.get("x-sautium-ts")
        sig = request.headers.get("x-sautium-sig")
        if not ts_raw or not sig:
            return JSONResponse(
                {"detail": "missing signature headers"}, status_code=401
            )

        try:
            ts_int = int(ts_raw)
        except ValueError:
            return JSONResponse({"detail": "bad timestamp"}, status_code=401)

        skew = abs(time.time() - ts_int)
        if skew > REPLAY_WINDOW_SECONDS:
            return JSONResponse({"detail": "stale timestamp"}, status_code=401)

        body = await request.body()
        # Restore body for downstream handlers — Starlette consumes the
        # underlying stream when we await request.body() here.
        # Setting _body on the Request object makes subsequent body()
        # calls hit the cached value.
        request._body = body  # noqa: SLF001 — documented Starlette workaround

        path_and_query = path
        if request.url.query:
            path_and_query = f"{path}?{request.url.query}"

        # Two keys are accepted, for two kinds of caller:
        #   * the DEVICE TOKEN — what a browser gets after logging in. The
        #     page no longer carries any key, so this is the only thing a
        #     remote client can sign with, and one epoch bump revokes it.
        #   * the SERVER SECRET — for callers that already live on the host
        #     and read the file directly (launcher, MCP server). Nothing is
        #     gained by rejecting them: whoever can read the file has the
        #     host anyway.
        secret = self._get_secret()
        for key in (device_auth.current_token(secret).encode(), secret):
            if hmac.compare_digest(
                    sig, sign(key, request.method, path_and_query, ts_raw, body)):
                break
        else:
            return JSONResponse({"detail": "bad signature"}, status_code=401)

        _mark_ui_activity()
        return await call_next(request)
