"""
HMAC request signing middleware for the Sautium FastAPI backend.

Each privileged request must carry two headers:

    X-Sautium-Ts:  unix seconds (int as string)
    X-Sautium-Sig: hex(HMAC-SHA256(secret, canonical))

where:

    canonical = METHOD + "\n" + PATH_AND_QUERY + "\n" + TS + "\n" + sha256_hex(body)

The server secret lives beside the node's identity (``<identity dir>/
.api_secret``, 32 bytes) — it is part of who this node IS, not of the code
it runs. It used to sit in ``backend/data/`` inside the checkout, which made
it survive deleting the node and made the launcher and Docker nodes on one
machine share a credential (compose bind-mounts ./backend); the file is
created on first startup if missing. Callers on the host (launcher, MCP
server) read it and sign with it directly; a browser signs with the DEVICE
TOKEN it earned through routers/auth.py (device_auth.py has the model).

The Web UI rides plain HTTP on the LAN. Signing does not need the transport
(the key never travels — only signatures with a 60 s life), and the one
exchange that does carry a credential, earning the token, is boxed end to
end by device_auth's credential channel.

Whitelisted paths (no signature required):

    /health              (Docker healthcheck)
    /                    (HTML root — the page carries no key)
    /static/*            (CSS, JS, fonts)
    /api/covers/*        (album cover art — loaded via <img src>,
                          which cannot attach custom headers)
    /api/sync/*          (P2P sync from remote launchers — has its
                          own Ed25519 auth in sync_server.py)
    /sync/*              (legacy P2P sync)
    /api/p2p/chat/wake   (loopback-only "ping" from sync_server)
    /api/auth/*          (the credential checks themselves — see below)

Replay window: ±60 seconds. A request older than 60s or 60s in the
future is rejected even with a valid signature.

The middleware reads request body once and stashes it in
``request._body`` so downstream handlers see the same bytes.
"""

import errno
import hashlib
import hmac
import ipaddress
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
    # Browsers fetch this on their own, outside the signing fetch wrapper.
    # No route serves it — whitelisting turns a misleading 401 in the access
    # log into an honest 404.
    "/favicon.ico",
    "/api/p2p/chat/wake",
    # A client with no token yet cannot sign — these ARE the credential
    # checks. They defend themselves: /login costs an Argon2id derivation
    # under a semaphore, /pair burns one of five attempts under a lock, and
    # /create-account only answers while the node has no identity at all
    # (first-run setup) and refuses every call after that.
    "/api/auth/status",
    "/api/auth/handshake",
    "/api/auth/login",
    "/api/auth/pair",
    "/api/auth/create-account",
}

# -- Host guard ---------------------------------------------------------------
#
# DNS rebinding: a page on evil.com whose name resolves to this node's address.
# The browser then talks to us from that page, same-origin as far as it is
# concerned, and sends `Host: evil.com`. Signed routes already survive that —
# the device token lives in localStorage, which is bound to an origin the
# attacker does not have — but the WHITELIST does not, and the whitelist is
# where account creation, login and pairing live.
#
# The question to ask is NOT "is this address private". That was the obvious
# predicate and it is wrong twice over: it says yes to any RFC1918 name an
# attacker points at us, and no to 100.64/10, which is how a phone off the
# home network legitimately reaches this node. The question is "is this one of
# MY addresses" — a closed set we compute from our own interfaces and our own
# configuration. Interfaces change while we run (a tunnel started after the
# backend, a new lease), so an IP literal the set does not hold is checked
# against the interfaces once more and the answer remembered either way.
#
# Nothing here resolves a name the client supplied. Resolving attacker input is
# the attack; our own configured names are resolved once, at startup.
_allowed_hosts: set | None = None
_host_misses: dict[str, float] = {}     # IP literal -> monotonic time its refusal expires
_MISS_TTL_S = 10.0
_MISS_CAP = 256


def _allowed_host_set() -> set:
    global _allowed_hosts
    if _allowed_hosts is not None:
        return _allowed_hosts
    import os
    from tls_gen import detect_reachable_host_ips
    entries = [s.strip() for s in os.getenv("SAUTIUM_HOST_IPS", "").split(",")
               if s.strip()]
    allowed = {"localhost", "host.docker.internal", "127.0.0.1", "::1", "[::1]"}
    allowed |= set(detect_reachable_host_ips(entries))
    # The configured entries themselves, so browsing to the node by name works
    # — a MagicDNS name is a legitimate way to reach it, and it is OUR name.
    allowed |= {e.lower() for e in entries}
    # Escape hatch for anything the operator knows about and we cannot infer
    # (a reverse proxy's name, a second tunnel).
    allowed |= {s.strip().lower()
                for s in os.getenv("SAUTIUM_ALLOWED_HOSTS", "").split(",")
                if s.strip()}
    _allowed_hosts = allowed
    logger.info("Host guard accepts: %s", sorted(allowed))
    return allowed


def host_allowed(host_header: str) -> bool:
    if not host_header:
        return True          # HTTP/1.0 and health probes send none
    host = host_header.strip().lower()
    if host.startswith("["):                  # [::1]:8000
        host = host.split("]")[0] + "]"
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    allowed = _allowed_host_set()
    return host in allowed or _own_ipv4_literal(host, allowed)


def _own_ipv4_literal(host: str, allowed: set) -> bool:
    """A literal the startup set does not hold may be an interface that came
    up since. Re-read the interfaces and learn every address they carry; a
    refusal is held for _MISS_TTL_S so a flood of bogus Host headers costs one
    enumeration per literal, not one per request."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False                          # a name — never resolved
    if not isinstance(addr, ipaddress.IPv4Address):
        return False
    now = time.monotonic()
    if _host_misses.get(host, 0.0) > now:
        return False
    from tls_gen import detect_own_ipv4s
    new = set(detect_own_ipv4s()) - allowed
    if new:
        allowed |= new
        logger.info("Host guard accepts: %s (interfaces up since startup)", sorted(new))
    if host in allowed:
        return True
    if len(_host_misses) >= _MISS_CAP:
        for stale in [h for h, until in _host_misses.items() if until <= now]:
            del _host_misses[stale]
        if len(_host_misses) >= _MISS_CAP:
            del _host_misses[next(iter(_host_misses))]
    _host_misses[host] = now + _MISS_TTL_S
    return False


WHITELIST_PREFIX = (
    "/static/",
    "/api/covers/",
    "/api/sync/",
    "/api/mb/",
    "/sync/",
    # <audio> elements can't set HMAC headers — these routes verify their
    # own short-lived query-param signatures instead (media_urls.verify).
    "/api/player/media/",
    # Last.fm sends the browser here once the user has granted access — a
    # redirect cannot sign. Admitted on the nonce in the path (lastfm_auth):
    # minted only for a signed caller, single use, dead with the flow; the
    # page never echoes the token or the session key.
    "/lastfm/auth/callback/",
)


def _is_whitelisted(path: str) -> bool:
    if path in WHITELIST_EXACT:
        return True
    return any(path.startswith(p) for p in WHITELIST_PREFIX)


def secret_path() -> Path:
    """Where this node keeps its API secret. Anchored to the identity
    directory, which is what every runtime already treats as the node's own
    state: the launcher points it at %APPDATA%/Sautium (or ~/.config on mac),
    Docker at the mounted ./data/node_identity. Deleting the node therefore
    deletes the secret, and two nodes sharing a checkout no longer share it."""
    from config import settings
    return Path(settings.p2p_identity_dir) / ".api_secret"


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

        if not host_allowed(request.headers.get("host", "")):
            # Refuse before anything else, whitelist included — the whitelist
            # is precisely what a rebinding attack has to work with.
            return JSONResponse({"detail": "unrecognised Host"}, status_code=421)

        if _is_whitelisted(path):
            return await call_next(request)

        # The error kind rides a response header so the client can tell a
        # replay-window rejection (re-sign and retry — the token is fine)
        # from a revoked token (re-authenticate). A frozen phone tab flushes
        # queued requests with timestamps minutes old on wake; treating that
        # 401 as revocation logged the user out on every screen-on.
        ts_raw = request.headers.get("x-sautium-ts")
        sig = request.headers.get("x-sautium-sig")
        if not ts_raw or not sig:
            return JSONResponse(
                {"detail": "missing signature headers"}, status_code=401,
                headers={"X-Sautium-Auth-Error": "missing"},
            )

        try:
            ts_int = int(ts_raw)
        except ValueError:
            return JSONResponse({"detail": "bad timestamp"}, status_code=401,
                                headers={"X-Sautium-Auth-Error": "bad-ts"})

        skew = abs(time.time() - ts_int)
        if skew > REPLAY_WINDOW_SECONDS:
            logger.warning("stale request timestamp (%ds skew): %s %s",
                           int(skew), request.method, path)
            return JSONResponse({"detail": "stale timestamp"}, status_code=401,
                                headers={"X-Sautium-Auth-Error": "stale-ts"})

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
            logger.warning("bad request signature: %s %s",
                           request.method, path)
            return JSONResponse({"detail": "bad signature"}, status_code=401,
                                headers={"X-Sautium-Auth-Error": "bad-sig"})

        return await call_next(request)
