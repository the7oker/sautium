"""
HMAC request signing middleware for the Sautium FastAPI backend.

Each privileged request must carry two headers, and every signer of this
tree sends two more:

    X-Sautium-Ts:          unix seconds (int as string)
    X-Sautium-Nonce:       32 lowercase hex digits the signer drew for this request
    X-Sautium-Body-Sha256: sha256_hex(body), the digest the canonical string carries
    X-Sautium-Sig:         hex(HMAC-SHA256(secret, canonical))

where:

    canonical = METHOD + "\n" + PATH_AND_QUERY + "\n" + TS + "\n" + NONCE + "\n" + sha256_hex(body)

or, without a nonce, the same with the NONCE line left out.

The middleware is the app's outermost, pure ASGI, and decides everything the
headers decide before a byte of a body is read: the Host, the whitelist, the
headers' shape and age, and — over the declared digest, or the empty body's
when the request has none — the signature itself. A body is then read whole
(serving.BodyBudget): a signer's from a large budget only a key holder can
reach, every other body — a whitelisted route's, a signer's that declares no
digest (a page loaded before signers declared one) — from a small one; a body
that differs from its declared digest is refused as a bad signature.

PATH_AND_QUERY is the request-target as sent — the path's own bytes,
percent-encoding and all, and the raw query (serving.request_target) — and
whether a path needs a signature at all is judged on the path the router
routes (`scope["path"]`). Neither is read off `request.url`, which Starlette
builds again from the Host header.

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
    /favicon.ico         (asked by the browser itself; no route serves it)
    /static/*            (CSS, JS, fonts)
    /api/covers/*        (album cover art — loaded via <img src>,
                          which cannot attach custom headers)
    /api/p2p/chat/wake   (the launcher's sync server wakes the chat
                          streams; the route asks no caller who it is)
    /api/auth/status, /handshake, /login, /pair, /create-account
                         (the credential checks themselves — see below)
    /api/player/media/*  (<audio> bytes, behind their own signed query)
    /lastfm/auth/callback/*  (Last.fm's redirect back, on its nonce)
    /api/csp-report      (the browser's own Content-Security-Policy reports)

Replay window: ±60 seconds. A request older than 60s or 60s in the
future is rejected even with a valid signature, and a signature this node
has already accepted is refused while the window could still take it, and
for two windows at least (`replayed`): within the window a request is
accepted once. The nonce is what lets a signer send the same request twice
in a second; a page loaded before signers had one still signs without it.
"""

import errno
import hashlib
import heapq
import hmac
import ipaddress
import itertools
import logging
import os
import re
import secrets
import sys
import time
from collections import deque
from pathlib import Path
from typing import Optional

from starlette.datastructures import Headers

import device_auth
from serving import BodyBudget, refuse, replaying, request_target

logger = logging.getLogger(__name__)

REPLAY_WINDOW_SECONDS = 60
# What each signature header may be, as every signer of the tree writes it
TS_FORM = re.compile(r"[0-9]{1,12}")
NONCE_FORM = re.compile(r"[0-9a-f]{32}")
DIGEST_FORM = re.compile(r"[0-9a-f]{64}")
SIG_FORM = re.compile(r"[0-9a-f]{64}")
_EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()

# The bodies a request may carry (serving.BodyBudget): a signer's, its
# signature verified over the digest it declares, from the large budget — the
# largest body the Web UI sends is an avatar (routers/profile.py, 10 MiB), and
# the deadline leaves one that size two minutes on a slow uplink; every other
# body from the small one — a credential exchange is a few kilobytes, a policy
# report 16 KiB, a request of a page from before declared digests a JSON
# document (such a tab's avatar upload is refused until it reloads). The small
# budget is any sender's: it can be held, and with it the sign-in doors — which
# their derivation bound already lets a crowd hold — never the owner's changes.
SIGNED_BODIES = {"cap": 12 * 1024 * 1024, "budget": 48 * 1024 * 1024, "deadline": 120.0}
OPEN_BODIES = {"cap": 64 * 1024, "budget": 1024 * 1024, "deadline": 20.0}

# Signatures taken — each once it verifies, from its headers, and given back
# when its client leaves before its body arrived (the request did not run, and
# a browser sends it again after a cut connection) — and kept while a request
# the window would still take could come again: until the wall clock is past
# their own time plus the window, and two windows on the monotonic clock at
# least. A step of either clock while one is kept reopens nothing; a step back
# of the wall clock past its forgetting reopens it, as a restart does. On the
# event loop.
_SEEN_SECONDS = 2 * REPLAY_WINDOW_SECONDS
_seen: dict = {}                 # signature -> its entry (monotonic, wall, signature, serial)
_seen_order: deque = deque()     # entries, by the monotonic time they may be forgotten
_seen_wall: list = []            # heap: entries the monotonic clock let go, by their wall time
_serials = itertools.count()

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
    # (first-run setup), with the setup code the node wrote at its start,
    # and refuses every call after that.
    "/api/auth/status",
    "/api/auth/handshake",
    "/api/auth/login",
    "/api/auth/pair",
    "/api/auth/create-account",
    # The browser posts a policy violation on its own, unsigned (csp.py):
    # the route bounds the body and logs each kind once, so many at most.
    "/api/csp-report",
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
_SCAN_GAP_S = 1.0
_last_scan = float("-inf")              # monotonic time the interfaces were last read


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
    # This machine's own names (a tailnet's, mDNS's) are its owner's: DEBUG.
    logger.debug("Host guard accepts: %s", sorted(allowed))
    return allowed


def is_own_address(host: Optional[str]) -> bool:
    """Does this name or literal reach THIS machine — loopback, the Docker
    host, one of our interfaces, one of our configured names? What the
    Host guard accepts, asked about an address we dial out to: an HQPlayer
    there shares this node's disks and opens the library by path; one
    anywhere else is handed streams. Unset counts as here."""
    if not host:
        return True
    h = host.strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    allowed = _allowed_host_set()
    return h in allowed or f"[{h}]" in allowed or _own_ipv4_literal(h, allowed)


# What a Host header may be: a name or an IPv4 literal, or a bracketed IPv6
# literal, then at most a port. Anything else — a path, a query, userinfo, a
# port that is not digits — is no address of ours, whatever it starts with.
_HOST_FORM = re.compile(r"(?:\[(?P<v6>[0-9a-f:.]+)\]|(?P<name>[a-z0-9._-]+))(?::[0-9]{1,5})?")


def host_allowed(host_header: str) -> bool:
    if not host_header:
        return True          # HTTP/1.0 and health probes send none
    form = _HOST_FORM.fullmatch(host_header.strip().lower())
    if form is None:
        return False
    allowed = _allowed_host_set()
    if form["v6"] is not None:
        return f"[{form['v6']}]" in allowed or form["v6"] in allowed
    return form["name"] in allowed or _own_ipv4_literal(form["name"], allowed)


def _own_ipv4_literal(host: str, allowed: set) -> bool:
    """A literal the startup set does not hold may be an interface that came
    up since. Re-read the interfaces and learn every address they carry; a
    refusal is held for _MISS_TTL_S so a flood of bogus Host headers costs one
    enumeration per literal, not one per request — and the interfaces are
    read at most once per _SCAN_GAP_S whatever the literals: a literal asked
    inside the gap is refused unread, and asked again after it, read."""
    global _last_scan
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False                          # a name — never resolved
    if not isinstance(addr, ipaddress.IPv4Address):
        return False
    now = time.monotonic()
    if _host_misses.get(host, 0.0) > now or now - _last_scan < _SCAN_GAP_S:
        return False
    _last_scan = now
    from tls_gen import detect_own_ipv4s
    new = set(detect_own_ipv4s()) - allowed
    if new:
        allowed |= new
        logger.debug("Host guard accepts: %s (interfaces up since startup)", sorted(new))
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
    # <audio> elements can't set HMAC headers — these routes verify their
    # own short-lived query-param signatures instead (media_urls.verify).
    "/api/player/media/",
    # Last.fm sends the browser here once the user has granted access — a
    # redirect cannot sign. Admitted on the nonce in the path (lastfm_auth):
    # minted only for a signed caller, good for one connection, dead with
    # the flow; the page never echoes the token or the session key.
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


def _owners_only(path: Path, mode: int) -> None:
    """`mode` on `path` where this process may set it — it owns the path, or
    is root. A path another user owns (a mount that refused the hand-over to
    the node's user) keeps its mode, and the log says so."""
    if sys.platform != "win32":
        st = os.stat(path)
        if st.st_mode & 0o777 == mode:
            return
        if st.st_uid != os.geteuid() and os.geteuid() != 0:
            logger.warning("%s is another user's (uid %d): its mode stays %o", path, st.st_uid,
                           st.st_mode & 0o777)
            return
    os.chmod(path, mode)


def ensure_secret(secret_path: Path) -> bytes:
    """Return the secret bytes; generate the file if missing.

    The file holds urlsafe base64 of 32 random bytes (43 chars). We
    use the printable form so it can be inlined into HTML/headers
    without escaping. The HMAC key is the printable string itself
    (utf-8 bytes), not the decoded random bytes — keeps the JS side
    simple (no base64 decode in WebCrypto importKey).

    The owner's alone: 0600 in a 0700 folder, applied again at every start
    (a restore or an earlier release may have left wider modes). Its other
    readers, the agents' MCP servers and the launcher, run as the same user
    — the desktop user, or the Docker node's own; a Docker backend still
    running as root (an image from before the node's own user) gives the
    file to the user its agents run as. A new secret is written whole or not
    at all, and an empty file — a write that did not finish — counts as none.
    """
    folder = secret_path.parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    _owners_only(folder, 0o700)
    data = _read_existing(secret_path)
    if data:
        _owners_only(secret_path, 0o600)
    else:
        data = secrets.token_urlsafe(32)
        part = secret_path.with_name(secret_path.name + ".part")
        part.unlink(missing_ok=True)
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(data)
        os.replace(part, secret_path)
        logger.info(f"Generated new API secret at {secret_path}")
    if sys.platform == "linux" and os.geteuid() == 0:
        import pwd
        from claude_code_runner import AGENT_USER
        pw = pwd.getpwnam(AGENT_USER)
        for path in (folder, secret_path):
            os.chown(path, pw.pw_uid, pw.pw_gid)
    return data.encode("ascii")


def _mac(secret: bytes, method: str, path_and_query: str, ts: str, nonce: Optional[str],
         body_digest: str) -> str:
    head = f"{method}\n{path_and_query}\n{ts}\n" + (f"{nonce}\n" if nonce is not None else "")
    return hmac.new(secret, f"{head}{body_digest}".encode("utf-8"), hashlib.sha256).hexdigest()


def sign(secret: bytes, method: str, path_and_query: str, ts: str, body: bytes,
         nonce: Optional[str] = None) -> str:
    """The hex HMAC of a request (the module doc's canonical string), with
    the nonce line when the signer sent one."""
    return _mac(secret, method, path_and_query, ts, nonce, hashlib.sha256(body).hexdigest())


def _take(sig: str, ts: int) -> Optional[tuple]:
    """`sig`'s entry, taken now — None when it is taken already."""
    now, wall = time.monotonic(), time.time()
    while _seen_order and _seen_order[0][0] <= now:
        entry = _seen_order.popleft()
        if _seen.get(entry[2]) is entry:
            heapq.heappush(_seen_wall, (entry[1], entry[3], entry))
    while _seen_wall and _seen_wall[0][0] < wall:
        entry = heapq.heappop(_seen_wall)[2]
        if _seen.get(entry[2]) is entry:
            del _seen[entry[2]]
    if sig in _seen:
        return None
    entry = (now + _SEEN_SECONDS, ts + REPLAY_WINDOW_SECONDS, sig, next(_serials))
    _seen[sig] = entry
    _seen_order.append(entry)
    return entry


def _give_back(entry: tuple) -> None:
    """The request `entry` was taken for did not run: its signature may come
    once more. Its place in the order stays until its time, so the order is
    compacted once it is more than twice what is kept: one signature taken
    and given back again and again holds one place."""
    if _seen.get(entry[2]) is entry:
        del _seen[entry[2]]
    if len(_seen_order) > 2 * len(_seen) + 64:
        kept = [e for e in _seen_order if _seen.get(e[2]) is e]
        _seen_order.clear()
        _seen_order.extend(kept)


def _carries_body(headers: Headers) -> bool:
    return headers.get("content-length", "0") != "0" or "transfer-encoding" in headers


def _auth_error(kind: str) -> tuple:
    # The error kind rides a response header so the client can tell a
    # replay-window rejection (re-sign and retry — the token is fine) from a
    # revoked token (re-authenticate). A frozen phone tab flushes queued
    # requests with timestamps minutes old on wake; treating that 401 as
    # revocation logged the user out on every screen-on.
    return ((b"x-sautium-auth-error", kind.encode()),)


class HMACAuthMiddleware:
    """Pure ASGI, the app's outermost: the Web UI port's door (the module
    doc). `signed_bodies` and `open_bodies` size its two body budgets."""

    def __init__(self, app, secret_path: Path, *, signed_bodies: Optional[dict] = None,
                 open_bodies: Optional[dict] = None):
        self.app = app
        self._secret_path = secret_path
        self._secret: Optional[bytes] = None
        self._signed_bodies = BodyBudget(**(signed_bodies or SIGNED_BODIES))
        self._open_bodies = BodyBudget(**(open_bodies or OPEN_BODIES))

    def _get_secret(self) -> bytes:
        if self._secret is None:
            self._secret = ensure_secret(self._secret_path)
        return self._secret

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        method, path = scope["method"], scope["path"]

        if not host_allowed(headers.get("host", "")):
            # Refuse before anything else, whitelist included — the whitelist
            # is precisely what a rebinding attack has to work with.
            return await self._refused(scope, receive, send, 421, "unrecognised Host")

        if _is_whitelisted(path):
            return await self._admit(scope, receive, send, self._open_bodies)

        ts_raw, sig = headers.get("x-sautium-ts"), headers.get("x-sautium-sig")
        if not ts_raw or not sig:
            return await self._refused(scope, receive, send, 401, "missing signature headers", "missing")
        if not TS_FORM.fullmatch(ts_raw):
            return await self._refused(scope, receive, send, 401, "bad timestamp", "bad-ts")
        if not SIG_FORM.fullmatch(sig):
            return await self._refused(scope, receive, send, 401, "bad signature", "bad-sig")
        ts_int = int(ts_raw)
        nonce = headers.get("x-sautium-nonce")
        if nonce is not None and not NONCE_FORM.fullmatch(nonce):
            return await self._refused(scope, receive, send, 401, "bad nonce", "bad-nonce")
        digest = headers.get("x-sautium-body-sha256")
        if digest is not None and not DIGEST_FORM.fullmatch(digest):
            return await self._refused(scope, receive, send, 401, "bad body digest", "bad-digest")

        skew = abs(time.time() - ts_int)
        if skew > REPLAY_WINDOW_SECONDS:
            logger.warning("stale request timestamp (%ds skew): %s %s", int(skew), method, path)
            return await self._refused(scope, receive, send, 401, "stale timestamp", "stale-ts")

        target = request_target(scope)
        if digest is None and not _carries_body(headers):
            digest = _EMPTY_DIGEST
        if digest is None:
            # A signer that declares no digest — a page loaded before signers
            # declared one: its body comes from the small budget and is judged
            # once read
            return await self._admit(scope, receive, send, self._open_bodies, judge=lambda body: self._judge(
                method, path, target, ts_raw, ts_int, nonce, hashlib.sha256(body).hexdigest(), sig)[0])
        refusal, taken = self._judge(method, path, target, ts_raw, ts_int, nonce, digest, sig)
        if refusal is not None:
            return await self._refused(scope, receive, send, 401, *refusal)
        return await self._admit(scope, receive, send, self._signed_bodies, digest=digest, taken=taken)

    async def _refused(self, scope, receive, send, status: int, detail: str,
                       kind: Optional[str] = None) -> None:
        """A refusal the headers decided. A body on its way that fits the small
        cap is read and dropped first, held by no budget (BodyBudget.dropping):
        a page must read a refusal's kind (stale-ts) to sign again."""
        await self._open_bodies.dropping(scope, receive)
        await refuse(send, status, detail, _auth_error(kind) if kind else ())

    def _judge(self, method, path, target, ts_raw, ts_int, nonce, digest, sig) -> tuple:
        """(None, the signature's entry) for a signature that verifies and is
        new — taken now (_take) — else (the refusal's detail and kind, None).

        Two keys are accepted, for two kinds of caller:
          * the DEVICE TOKEN — what a browser gets after logging in. The page
            carries no key, so this is the only thing a remote client can
            sign with, and one epoch bump revokes it.
          * the SERVER SECRET — for callers that already live on the host and
            read the file directly (launcher, MCP server). Nothing is gained
            by rejecting them: whoever can read the file has the host
            anyway."""
        secret = self._get_secret()
        if not any(hmac.compare_digest(sig, _mac(key, method, target, ts_raw, nonce, digest))
                   for key in (device_auth.current_token(secret).encode(), secret)):
            logger.warning("bad request signature: %s %s", method, path)
            return ("bad signature", "bad-sig"), None
        taken = _take(sig, ts_int)
        if taken is None:
            logger.warning("request signature used again: %s %s", method, path)
            return ("signature already used", "replayed"), None
        return None, taken

    async def _admit(self, scope, receive, send, budget: BodyBudget, *,
                     digest: Optional[str] = None, taken: Optional[tuple] = None, judge=None) -> None:
        """The request's body read whole from `budget`, held to its signed
        `digest` or put to `judge`, then the app with it. A client that leaves
        before its body arrived gives its signature back (`taken`): the request
        did not run, and a browser sends it again after a cut connection. One
        answered 413, 408 or 503 signs its next try afresh — its signature
        stays spent."""
        async with budget.reading(scope, receive, send) as (body, refused):
            if body is None:
                if taken is not None and refused == 400:
                    _give_back(taken)
                return
            if digest is not None and not hmac.compare_digest(hashlib.sha256(body).hexdigest(), digest):
                logger.warning("request body differs from its signed digest: %s %s",
                               scope["method"], scope["path"])
                return await refuse(send, 401, "bad signature", _auth_error("bad-sig"))
            refusal = judge(body) if judge is not None else None
            if refusal is not None:
                return await refuse(send, 401, refusal[0], _auth_error(refusal[1]))
            await self.app(scope, replaying(body, receive), send)
