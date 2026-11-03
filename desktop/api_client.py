"""
Minimal HTTP/HTTPS client for communicating with the Sautium backend.

Uses only urllib (no extra dependencies) to fetch stats and health info.
The local backend is plain HTTP; a peer is HTTPS pinned to its node key.

Two signing modes, chosen at construction:

- LOCAL backend (default): HMAC-SHA256 over
  ``METHOD\\nPATH_AND_QUERY\\nTS\\nNONCE\\nsha256_hex(body)`` with the secret
  shared with the backend (the identity folder's ``.api_secret``); headers
  ``X-Sautium-Ts``, ``X-Sautium-Nonce``, ``X-Sautium-Body-Sha256``,
  ``X-Sautium-Sig``; see backend/auth_hmac.py.
- PEER (``peer=PeerIdentity(...)``): Ed25519 by this node's key over the
  wire-format-v1 message (desktop/p2p/peer_auth.py) — headers
  ``X-Sautium-Peer-Pubkey/-Ts/-Sig`` — bound to the peer's pubkey, which is
  the TLS channel's verified node-key binding (peer_auth.pinned_ssl_context;
  the ``/health`` body must agree with it, it is never trusted on its own).
  The {certificate, proof} bundle rides along as ``X-Sautium-Peer-Cert`` on
  the first request and whenever the peer answers
  ``X-Sautium-Peer-Identity: unknown``. Until the channel identity is known
  the request goes out unsigned (the anonymous lane), and gate quotes are
  never paid — work is only ever burned for an authenticated recipient.
  The local secret is never sent to a peer.
"""

import gzip
import hashlib
import hmac
import http.client
import itertools
import json
import logging
import secrets
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import Iterator, Optional

from desktop.p2p import admission, peer_auth, wire
from desktop.p2p.contact_log import endpoint_family

logger = logging.getLogger(__name__)

# What a peer may answer, per endpoint family: (bytes after inflation,
# seconds for the whole exchange — connect, TLS, headers and body). A reply
# past its bound is refused at the chunk that crosses it, and an exchange
# past its deadline has its socket shut down (_Watchdog). urllib's timeout
# bounds each read, never the exchange: a peer that sent a byte inside every
# timeout held the request — and the walk or slice run waiting on it — for as
# long as it liked.
_PEER_BOUNDS = {
    "health": (256 * wire.KiB, 15),
    "gate.quote": (256 * wire.KiB, 15),
    "sync.inventory": (16 * wire.MiB, 300),
    "sync.holdings": (64 * wire.MiB, 300),
    "sync.pull": (64 * wire.MiB, 300),
    "sync.offer": (16 * wire.MiB, 60),
    "sync.push": (256 * wire.KiB, 300),
    "mb.slice": (128 * wire.MiB, 600),
    "lb.slice": (32 * wire.MiB, 120),
}
_PEER_DEFAULT_BOUND = (4 * wire.MiB, 60)
# A refusal's body is read for its message and Retry-After only.
_ERROR_BODY_CAP = 64 * wire.KiB
# One peer's whole session — a sync then its carry offer, or a slice run's
# batches — ends by this, whatever each exchange in it took
# (BackendAPIClient.session_deadline). What it leaves undone waits for the
# next run: the walk and the cycles fill gaps, no one session must finish.
PEER_SESSION_S = 20 * 60


class _Watchdog:
    """Shuts a peer exchange's socket down once its deadline has passed, so
    the read blocked on it returns. One thread for the process, asleep until
    the nearest deadline or the next one armed."""

    def __init__(self):
        self._cond = threading.Condition()
        self._armed: dict = {}
        self._tokens = itertools.count()
        self._thread: Optional[threading.Thread] = None

    def arm(self, sock: socket.socket, deadline: float) -> int:
        with self._cond:
            token = next(self._tokens)
            self._armed[token] = (deadline, sock)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, daemon=True, name="peer-deadlines")
                self._thread.start()
            self._cond.notify()
            return token

    def disarm(self, token: int) -> None:
        with self._cond:
            self._armed.pop(token, None)

    def _run(self) -> None:
        with self._cond:
            while True:
                now = time.monotonic()
                for token in [t for t, (d, _) in self._armed.items() if d <= now]:
                    _, sock = self._armed.pop(token)
                    logger.info("peer exchange past its deadline — connection closed")
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass   # the exchange ended in between and its reader closed the socket
                nearest = min((d for d, _ in self._armed.values()), default=None)
                self._cond.wait(None if nearest is None else max(0.0, nearest - now))


_WATCHDOG = _Watchdog()


class _Exchange:
    """One peer request's deadline. The watchdog holds a duplicate of each
    socket the request opens — taken before TLS wraps (and detaches) the
    original, so the handshake is inside the deadline too; shutdown() on the
    duplicate ends the connection for every descriptor of it."""

    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + seconds
        self._held: list = []

    def watched(self, sock: socket.socket) -> socket.socket:
        dup = sock.dup()
        self._held.append((dup, _WATCHDOG.arm(dup, self.deadline)))
        return sock

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline

    def within(self, timeout: float) -> float:
        """`timeout` cut to what is left of the deadline — the connect
        happens before the watchdog holds the socket."""
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("peer exchange past its deadline")
        return min(timeout, left)

    def close(self) -> None:
        for dup, token in self._held:
            _WATCHDOG.disarm(token)
            dup.close()
        self._held = []


def _peer_opener(ssl_ctx, exchange: _Exchange) -> urllib.request.OpenerDirector:
    """An opener whose connections are watched by `exchange` and which
    follows no redirect: the peer protocol has none, and a 3xx would hand
    the request to an address the peer chose — over plain HTTP, outside the
    node-key pin."""

    def watched(base):
        class _Connection(base):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._create_connection = (
                    lambda address, timeout, *rest: exchange.watched(
                        socket.create_connection(address, exchange.within(timeout), *rest)))
        return _Connection

    class _HTTP(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(watched(http.client.HTTPConnection), req)

    class _HTTPS(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(watched(http.client.HTTPSConnection), req,
                                context=self._context)

    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler(), _HTTP(), _HTTPS(context=ssl_ctx),
                    urllib.request.HTTPDefaultErrorHandler(),
                    urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener

# Secret file is written by the backend on first startup, beside the node's
# identity — the launcher and its backend are the same node, so they share it
# there. NOT in the checkout, where it used to live: that made one credential
# serve every node the checkout ever ran, including the Docker one alongside.
def _secret_path() -> Path:
    from desktop.config_manager import get_config_dir
    return get_config_dir() / "node_identity" / ".api_secret"

_cached_secret: Optional[bytes] = None


def forget_secret() -> None:
    """After a restore wrote another node's .api_secret: the next request
    reads the file again instead of signing with the retired value."""
    global _cached_secret
    _cached_secret = None

def _load_secret() -> Optional[bytes]:
    """Read the HMAC secret, retrying briefly if the backend hasn't
    written it yet (startup race window)."""
    global _cached_secret
    if _cached_secret is not None:
        return _cached_secret
    for _ in range(10):
        try:
            data = _secret_path().read_text(encoding="ascii").strip()
            if data:
                _cached_secret = data.encode("ascii")
                return _cached_secret
        except FileNotFoundError:
            pass
        time.sleep(0.5)
    logger.warning(
        f"API secret file not found at {_secret_path()}; requests will be unsigned"
    )
    return None


def _sign_headers(method: str, path_and_query: str, body: bytes) -> dict:
    """The signature headers for the request (backend/auth_hmac.py), or {}
    if the secret isn't available yet."""
    secret = _load_secret()
    if not secret:
        return {}
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    body_hash = hashlib.sha256(body or b"").hexdigest()
    canonical = f"{method}\n{path_and_query}\n{ts}\n{nonce}\n{body_hash}"
    sig = hmac.new(secret, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"X-Sautium-Ts": ts, "X-Sautium-Nonce": nonce,
            "X-Sautium-Body-Sha256": body_hash, "X-Sautium-Sig": sig}


def _read_json_body(resp, limit: Optional[int] = None,
                    exchange: Optional["_Exchange"] = None) -> dict:
    """Read a (possibly gzip-encoded) JSON response body. Both servers we
    talk to compress large payloads when the client advertises gzip: the
    backend via GZipMiddleware, the P2P sync server in _json_response.

    With a `limit` (every peer reply) the body is read in chunks and
    inflated as it arrives; past `limit` bytes either way, truncated, or
    past the exchange's deadline, it is refused (wire.WireError)."""
    gzipped = (resp.headers.get("Content-Encoding") or "").lower() == "gzip"
    if limit is None:
        data = resp.read()
        if gzipped:
            data = gzip.decompress(data)
        return json.loads(data.decode("utf-8"))
    inflater = wire.Inflater(limit) if gzipped else None
    parts, size = [], 0
    while True:
        chunk = resp.read(256 * wire.KiB)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise wire.WireError("reply over its bound")
        parts.append(inflater.feed(chunk) if inflater else chunk)
    if exchange is not None and exchange.expired():
        raise wire.WireError("reply past its deadline")
    if inflater:
        inflater.finish()
    reply = wire.loads(b"".join(parts).decode("utf-8"))
    if not isinstance(reply, dict):
        raise wire.WireError("reply is not an object")
    return reply


def _read_breaker(resp) -> socket.socket:
    """A duplicate of the response's socket, taken by the thread that reads
    it, so another thread can end the blocked read with shutdown()
    (close_streams). Closing the RESPONSE from that other thread waits on the
    buffer lock the blocked readline() holds — until the server's next
    keepalive, which froze the launcher window for up to 20 s on every Quit
    and before every update relaunch (2026-09-25). A duplicate rather than
    the fd number: the reader closes its own fd when it ends, and a number
    handed out again would shut some other connection down."""
    borrowed = socket.socket(fileno=resp.fileno())
    try:
        return borrowed.dup()
    finally:
        borrowed.detach()


class BackendAPIClient:
    """Client for the local backend API (plain HTTP on loopback, HMAC-signed)
    or a peer node (HTTPS pinned to the node key)."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000",
                 peer: Optional[peer_auth.PeerIdentity] = None,
                 expected_pubkey: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        # A peer channel must authenticate as a node: the pinning context
        # refuses certs without a valid node-key binding, and locks onto the
        # first verified key when the caller has no expectation
        # (DHT-discovered strangers).
        self._ssl_ctx = (
            peer_auth.pinned_ssl_context(expected_pubkey)
            if peer is not None and self.base_url.startswith("https://") else None
        )
        self._streams: list = []
        self._stream_generation = 0     # close_streams() ends every reader of the current one
        self.peer = peer
        self._server_pubkey: Optional[str] = None
        self._introduced = False
        self.last_health: Optional[dict] = None          # the last /health that passed its checks
        self.last_http_error: Optional[int] = None       # the status the last GET was refused with
        self.last_peer_identity: Optional[str] = None   # last X-Sautium-Peer-Identity seen
        self.last_peer_lane: Optional[str] = None
        self.last_gate_result: Optional[str] = None
        self._gate_header: Optional[str] = None       # a solved quote, attached to the next request once
        # time.monotonic() past which no further request to this peer is
        # started: one peer's whole session (a sync, a slice run) is
        # bounded, not only each exchange in it.
        self.session_deadline: Optional[float] = None

    def _auth_headers(self, method: str, path: str, data: bytes) -> dict:
        if self.peer is None:
            return _sign_headers(method, path, data)
        if not self._server_pubkey and path != "/health":
            self.get_health()            # learn the recipient before signing
        if not self._server_pubkey:
            return {}
        headers = peer_auth.sign_headers(self.peer.sign, self.peer.pubkey,
                                         self._server_pubkey, method, path, data)
        if not self._introduced:
            bundle = self.peer.cert_bundle()
            if bundle and bundle.get("cert"):
                headers[peer_auth.HDR_CERT] = peer_auth.encode_cert_bundle(
                    bundle["cert"], bundle.get("proof"))
                self._introduced = True
        if self._gate_header:
            headers[admission.HDR_GATE] = self._gate_header
            self._gate_header = None
        return headers

    def gate_pay(self, action: str = "") -> Optional[str]:
        """Admission gate: fetch this peer's quote for us and for `action` (the
        endpoint family we are about to call — a quote is bound to one),
        verify it is theirs and ours, solve every task (a small thread pool),
        return the X-Sautium-Gate value — or None when anything about the
        quote is off (a wrong quote is never worth working on)."""
        if self.peer is None or not self._server_pubkey:
            return None
        q = self._get_json(f"/api/gate/quote?pubkey={self.peer.pubkey}&action={action}", timeout=15)
        if not q or "quote" not in q or not admission.verify_quote(q["quote"], q.get("sig", ""),
                                                                    self._server_pubkey):
            return None
        core = q["quote"]
        if core.get("client") != self.peer.pubkey or core.get("action", "") != action:
            return None
        try:
            inputs = [bytes.fromhex(t) for t in q.get("tasks", [])]
        except (ValueError, TypeError):
            return None
        if len(inputs) != core.get("n") or admission.tasks_digest(inputs) != core.get("tasks_digest"):
            return None
        answers = admission.solve_all(inputs) if inputs else []
        return admission.encode_submission(core, q["sig"], answers)

    def gate_prepay(self, action: str = "") -> bool:
        """Solve a quote now and attach it to the next request (tests, or a
        client that knows the peer is armed)."""
        self._gate_header = self.gate_pay(action)
        return self._gate_header is not None

    def _note_peer_response(self, headers) -> None:
        if self.peer is None:
            return
        gate = headers.get(admission.HDR_GATE_RESULT)
        if gate:
            self.last_gate_result = gate
        identity = headers.get(peer_auth.HDR_IDENTITY)
        if identity:
            self.last_peer_identity = identity
            if identity == "unknown":
                self._introduced = False        # re-send the bundle next time
        lane = headers.get(peer_auth.HDR_LANE)
        if lane:
            self.last_peer_lane = lane

    def set_port(self, port: int):
        """Update the backend port."""
        self.base_url = f"http://127.0.0.1:{port}"

    def _bound(self, path: str):
        """(reply bound, exchange) for a request to `path`: a peer request
        is bounded in bytes and in time (_PEER_BOUNDS, and what is left of
        `session_deadline`) and its connection is watched; the local
        backend is not a peer — (None, None)."""
        if self.peer is None:
            return None, None
        limit, seconds = _PEER_BOUNDS.get(endpoint_family(path), _PEER_DEFAULT_BOUND)
        if self.session_deadline is not None:
            seconds = min(seconds, self.session_deadline - time.monotonic())
            if seconds <= 0:
                raise TimeoutError("peer session past its deadline")
        return limit, _Exchange(seconds)

    def _open(self, req, timeout: int, exchange: Optional[_Exchange]):
        if exchange is None:
            return urllib.request.urlopen(req, timeout=timeout, context=self._ssl_ctx)
        return _peer_opener(self._ssl_ctx, exchange).open(req, timeout=timeout)

    def _error_body(self, e: urllib.error.HTTPError) -> dict:
        body = _read_json_body(e, _ERROR_BODY_CAP if self.peer is not None else None)
        if not isinstance(body, dict):
            raise ValueError("refusal body is not an object")
        return body

    def _get_json(self, path: str, timeout: int = 5, _paid: bool = False) -> Optional[dict]:
        """GET request returning parsed JSON, or None on failure."""
        url = f"{self.base_url}{path}"
        exchange = None
        try:
            limit, exchange = self._bound(path)
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept-Encoding", "gzip")
            for k, v in self._auth_headers("GET", path, b"").items():
                req.add_header(k, v)
            resp = self._open(req, timeout, exchange)
            self.last_http_error = None
            self._note_peer_response(resp.headers)
            return _read_json_body(resp, limit, exchange)
        except urllib.error.HTTPError as e:
            self.last_http_error = e.code
            self._note_peer_response(e.headers)
            if e.code == 402 and self.peer is not None and not _paid and self.gate_prepay(endpoint_family(path)):
                return self._get_json(path, timeout, _paid=True)     # priced: pay once and retry
            logger.debug(f"API request failed: {url} — {e}")
            return None
        except Exception as e:
            self.last_http_error = None
            logger.debug(f"API request failed: {url} — {e}")
            return None
        finally:
            if exchange is not None:
                exchange.close()

    def _post_json(self, path: str, body: dict = None, timeout: int = 600,
                   _paid: bool = False) -> Optional[dict]:
        """POST request returning parsed JSON, or None on failure."""
        url = f"{self.base_url}{path}"
        exchange = None
        try:
            limit, exchange = self._bound(path)
            if body is not None:
                data = json.dumps(body).encode("utf-8")
                req = urllib.request.Request(
                    url, method="POST", data=data,
                    headers={"Content-Type": "application/json"},
                )
            else:
                data = b""
                req = urllib.request.Request(url, method="POST", data=data)
            req.add_header("Accept-Encoding", "gzip")
            for k, v in self._auth_headers("POST", path, data).items():
                req.add_header(k, v)
            resp = self._open(req, timeout, exchange)
            self._note_peer_response(resp.headers)
            return _read_json_body(resp, limit, exchange)
        except urllib.error.HTTPError as e:
            self._note_peer_response(e.headers)
            if e.code == 402 and self.peer is not None and not _paid and self.gate_prepay(endpoint_family(path)):
                return self._post_json(path, body, timeout, _paid=True)   # priced: pay once and retry
            try:
                body_resp = self._error_body(e)
            except Exception:
                body_resp = {"detail": f"HTTP {e.code}"}
            if e.code == 429 or (e.code == 503 and e.headers.get("Retry-After")):
                # The peer says when the oldest stamp leaves its window —
                # the earliest a retry can succeed —, or that it is busy for
                # so long (a 503 with Retry-After: its body slots). A peer on
                # older code sends neither the header nor the body field;
                # the caller then falls back to the window length. Whole
                # seconds, an hour at most: the callers sleep on it.
                wait = body_resp.get("retry_after")
                if not isinstance(wait, int) or isinstance(wait, bool):
                    try:
                        wait = int(e.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        wait = 0
                body_resp["retry_after"] = min(max(wait, 1), 3600) if wait > 0 else None
            logger.warning(f"API POST {url} returned {e.code}: {str(body_resp)[:300]}")
            return body_resp
        except Exception as e:
            logger.debug(f"API POST failed: {url} — {e}")
            return None
        finally:
            if exchange is not None:
                exchange.close()

    def get_stats(self) -> Optional[dict]:
        """Fetch library statistics from GET /stats."""
        return self._get_json("/stats")

    @property
    def peer_pubkey(self) -> Optional[str]:
        """The node key the TLS channel pinned on the last /health — what a
        cache of this peer's published state is keyed by."""
        return self._server_pubkey

    def get_health(self) -> Optional[dict]:
        """Fetch health status from GET /health. For a peer this is also
        where its pubkey is learned — every later request is signed for
        that recipient. The authoritative key is the TLS channel's verified
        node-key binding; a /health body that claims a different node_id is
        an impostor (or a relay answering for someone else) and the peer is
        treated as unreachable. The peer's certificate in the body
        (`birth_cert`) goes to the identity's witness sink with the
        channel's key — the sink checks it is that key's before it records."""
        health = self._get_json("/health")
        if self.peer is not None and health:
            channel = (peer_auth.pinned_pubkey(self._ssl_ctx)
                       if self._ssl_ctx is not None else None)
            node_id = str(health.get("node_id") or "").lower()
            if channel is None or node_id != channel:
                logger.warning(
                    "Peer %s /health claims node_id %s… but the TLS channel "
                    "belongs to %s… — refusing",
                    self.base_url, node_id[:16] or "<none>",
                    (channel or "<unauthenticated>")[:16])
                self.last_health = None
                return None
            if channel != self._server_pubkey:
                self._server_pubkey = channel
                self._introduced = False
            cert = health.get("birth_cert")
            if self.peer.witness is not None and isinstance(cert, dict):
                self.peer.witness(cert, channel)
        self.last_health = health
        return health

    def start_scan(self, subpath: str = None) -> Optional[dict]:
        """Start library scan as background task."""
        params = "skip_existing=true"
        if subpath:
            params += f"&subpath={urllib.parse.quote(subpath)}"
        return self._post_json(f"/scan/start?{params}", timeout=10)

    def scan_status(self) -> Optional[dict]:
        """Poll scan progress."""
        return self._get_json("/scan/status", timeout=5)

    def scan_cancel(self) -> Optional[dict]:
        """Cancel running scan."""
        return self._post_json("/scan/cancel", timeout=5)

    def enrich_status(self) -> Optional[dict]:
        """Read enrichment progress — for the support bundle. The run itself
        is started and cancelled from the Web UI's Library screen."""
        return self._get_json("/enrich/status", timeout=5)

    def mb_dump_start(self) -> Optional[dict]:
        """Start the MusicBrainz catalogue download+load in the background
        (the wizard's opt-in, fired once after first start)."""
        return self._post_json("/api/settings/musicbrainz/update", timeout=30)

    def lb_dump_start(self) -> Optional[dict]:
        """Start the ListenBrainz statistics download+aggregate in the
        background (the wizard's second opt-in; queued behind the catalogue)."""
        return self._post_json("/api/settings/listenbrainz/update", timeout=30)

    def refresh_gear_registries(self) -> Optional[dict]:
        """Refresh measurement registries (spinorama fetched by the
        backend itself; AutoEq reimported from its mount). Import
        guardrails run server-side; slow-ish — network + ~2k upserts."""
        return self._post_json("/api/gear-models/registry/refresh", timeout=300)

    def lastfm_auth_start(self) -> Optional[dict]:
        """Start the Last.fm authorization flow: {"auth_url": ...} to open in
        the browser. Last.fm sends the browser back to the origin named here
        — this client's own, the one the machine's browser reaches too."""
        return self._post_json("/lastfm/auth/start",
                               body={"origin": self.base_url}, timeout=15)

    def lastfm_auth_status(self) -> Optional[dict]:
        """The flow's outcome, read after a wake on /lastfm/auth/stream."""
        return self._get_json("/lastfm/auth/status", timeout=5)

    def canonicalize(self) -> Optional[dict]:
        """Trigger backend canonicalization in the background (returns immediately)."""
        return self._post_json("/canonicalize", timeout=10)

    # -- Sync API ----------------------------------------------------------

    def sync_inventory(self, track_uuids: list[str]) -> Optional[dict]:
        """What the peer holds sealed for these track UUIDs."""
        return self._post_json("/api/sync/inventory",
                               body={"track_uuids": track_uuids}, timeout=300)

    def sync_holdings(self, have: Optional[str] = None) -> Optional[dict]:
        """The peer's holdings filters (GET /api/sync/holdings); `have` =
        the version already cached, answered with a stub when unchanged."""
        path = "/api/sync/holdings"
        if have:
            path += "?have=" + urllib.parse.quote(have, safe="")
        return self._get_json(path, timeout=300)

    def sync_pull(self, category: str, uuids: list[str]) -> Optional[dict]:
        """Pull enrichment data for a category and list of UUIDs."""
        return self._post_json(
            f"/api/sync/pull/{category}",
            body={"uuids": uuids},
            timeout=300,
        )

    def carry_offer(self, recording_mbids: list[str]) -> Optional[dict]:
        """Offer recordings whose analysis we could contribute; the peer
        answers with its own EXISTING track uuids per category (carry v4 —
        see sync_queries carry section)."""
        return self._post_json(
            "/api/sync/offer",
            body={"recordings": recording_mbids},
            timeout=60,
        )

    def carry_push(self, category: str, payload: dict) -> Optional[dict]:
        """Push one pull-shaped payload to a peer that asked for it."""
        return self._post_json(
            f"/api/sync/push/{category}",
            body=payload,
            timeout=300,
        )

    def request_pairing_code(self) -> Optional[dict]:
        """The current device-pairing code and how long it has left.
        Authorised by the shared API secret this process reads from disk —
        which is the point: the code must come from the machine running
        Sautium, not from a browser that would already need to be signed in
        to ask.

        Idempotent — every caller here (the QR, the Open Web UI button) must
        see the same code, so this reads rather than mints."""
        return self._get_json("/api/auth/pin", timeout=15)

    def stream(self, path: str,
               stop: Optional[threading.Event] = None) -> Iterator[None]:
        """Yield once per server-sent event on `path`, forever — or until
        `stop` is set: checked on every frame, keepalives included, so a
        subscriber scoped to a dialog is gone within one keepalive period
        of the dialog closing.

        Deliberately not a payload reader: every channel we consume is a wake
        event whose whole meaning is "re-read state over the signed API", so
        parsing frames would only invite the two to disagree. Blocks; run it
        on a thread and end it with close_streams().

        Reconnects on drop with a backoff, because the launcher restarts the
        backend under itself (a scan does exactly that) and a tight retry
        would spin through the whole restart."""
        delay = 1.0
        generation = self._stream_generation
        stopped = lambda: (self._stream_generation != generation
                           or (stop is not None and stop.is_set()))
        while not stopped():
            try:
                url = f"{self.base_url}{path}"
                req = urllib.request.Request(url, method="GET")
                req.add_header("Accept", "text/event-stream")
                for k, v in _sign_headers("GET", path, b"").items():
                    req.add_header(k, v)
                # No read timeout: an idle channel is normal, and the server
                # sends a keepalive comment every 20s to prove it is alive.
                resp = urllib.request.urlopen(req, context=self._ssl_ctx)
                breaker = _read_breaker(resp)
                self._streams.append(breaker)
                delay = 1.0
                try:
                    for raw in resp:
                        if stopped():
                            return
                        if raw.startswith(b"data:"):
                            yield None
                finally:
                    self._streams.remove(breaker)
                    breaker.close()
                    resp.close()
            except GeneratorExit:
                raise
            except Exception as e:
                logger.debug(f"SSE {path} dropped — {e}")
            if stopped():
                return
            time.sleep(delay)
            # Capped low: the far end is localhost, and the gap after a
            # restart is a gap in visible scan progress.
            delay = min(delay * 2, 5.0)

    def close_streams(self) -> None:
        """Unblock every reader so launcher shutdown does not wait on a
        connection that is, by design, never going to end on its own. Only
        ends the read — each reader closes its own response (_read_breaker).
        A generation, not a flag: every reader that exists now ends, one
        asleep in its reconnect backoff included, while a reader started
        later (a relaunch that failed and stays) runs."""
        self._stream_generation += 1
        for breaker in list(self._streams):
            try:
                breaker.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass   # that stream ended in between and its reader closed the breaker

    def mb_slice(self, names: list[str]) -> Optional[dict]:
        """Fetch raw mb_* rows for artist names from a dump-holding peer.
        Long timeout: a batch of prolific namesakes is a large payload."""
        return self._post_json(
            "/api/mb/slice",
            body={"names": names},
            timeout=600,
        )

    def lb_slice(self, artist_mbids: list[str],
                 min_version: Optional[str] = None) -> Optional[dict]:
        """Fetch signed ListenBrainz statistics slices for artist MBIDs from a
        peer that holds the dump or re-serves slices. ``min_version``: answer
        only with data at least that fresh (the requester learnt a newer dump
        exists) — older cached blobs come back as ``missing``."""
        return self._post_json(
            "/api/lb/slice",
            body={"artist_mbids": artist_mbids, "min_version": min_version},
            timeout=120,
        )
