"""
Minimal HTTP/HTTPS client for communicating with the Sautium backend.

Uses only urllib (no extra dependencies) to fetch stats and health info.
Supports HTTPS with self-signed certificates for P2P connections.

Two signing modes, chosen at construction:

- LOCAL backend (default): HMAC-SHA256 over
  ``METHOD\\nPATH_AND_QUERY\\nTS\\nsha256_hex(body)`` with the secret shared
  with the backend (``backend/data/.api_secret``); headers ``X-Sautium-Ts``,
  ``X-Sautium-Sig``; see backend/auth_hmac.py.
- PEER (``peer=PeerIdentity(...)``): Ed25519 by this node's key over the
  wire-format-v1 message (desktop/p2p/peer_auth.py) — headers
  ``X-Sautium-Peer-Pubkey/-Ts/-Sig`` — bound to the peer's pubkey learned
  from its ``/health``; the {certificate, proof} bundle rides along as
  ``X-Sautium-Peer-Cert`` on the first request and whenever the peer
  answers ``X-Sautium-Peer-Identity: unknown``. Until ``/health`` has been
  read the request goes out unsigned (the anonymous lane). The local
  secret is never sent to a peer.
"""

import gzip
import hashlib
import hmac
import json
import logging
import ssl
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import Iterator, Optional

from desktop.p2p import admission, peer_auth
from desktop.p2p.contact_log import endpoint_family

logger = logging.getLogger(__name__)

# Secret file is written by the backend on first startup. We share the
# same file because launcher and backend run on the same host (Docker
# bind-mounts ./backend into the container, so /app/data/.api_secret
# inside the container is the same inode as backend/data/.api_secret
# on the host).
_SECRET_PATH = Path(__file__).parent.parent / "backend" / "data" / ".api_secret"
_cached_secret: Optional[bytes] = None

# SSL context for self-signed P2P certificates
_p2p_ssl_ctx: Optional[ssl.SSLContext] = None


def _load_secret() -> Optional[bytes]:
    """Read the HMAC secret, retrying briefly if the backend hasn't
    written it yet (startup race window)."""
    global _cached_secret
    if _cached_secret is not None:
        return _cached_secret
    for _ in range(10):
        try:
            data = _SECRET_PATH.read_text(encoding="ascii").strip()
            if data:
                _cached_secret = data.encode("ascii")
                return _cached_secret
        except FileNotFoundError:
            pass
        time.sleep(0.5)
    logger.warning(
        f"API secret file not found at {_SECRET_PATH}; requests will be unsigned"
    )
    return None


def _sign_headers(method: str, path_and_query: str, body: bytes) -> dict:
    """Build X-Sautium-Ts/X-Sautium-Sig headers for the request, or
    {} if the secret isn't available yet."""
    secret = _load_secret()
    if not secret:
        return {}
    ts = str(int(time.time()))
    body_hash = hashlib.sha256(body or b"").hexdigest()
    canonical = f"{method}\n{path_and_query}\n{ts}\n{body_hash}"
    sig = hmac.new(secret, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"X-Sautium-Ts": ts, "X-Sautium-Sig": sig}


def _read_json_body(resp) -> dict:
    """Read a (possibly gzip-encoded) JSON response body. Both servers we
    talk to compress large payloads when the client advertises gzip: the
    backend via GZipMiddleware, the P2P sync server in _json_response."""
    data = resp.read()
    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
        data = gzip.decompress(data)
    return json.loads(data.decode("utf-8"))


def _get_ssl_context() -> ssl.SSLContext:
    """Get or create SSL context that accepts self-signed certificates."""
    global _p2p_ssl_ctx
    if _p2p_ssl_ctx is None:
        _p2p_ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        _p2p_ssl_ctx.check_hostname = False
        _p2p_ssl_ctx.verify_mode = ssl.CERT_NONE
        _p2p_ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return _p2p_ssl_ctx


class BackendAPIClient:
    """HTTP/HTTPS client for the backend API (local) or a peer node."""

    def __init__(self, base_url: str = "https://127.0.0.1:8000",
                 peer: Optional[peer_auth.PeerIdentity] = None):
        self.base_url = base_url.rstrip("/")
        self._ssl_ctx = (
            _get_ssl_context() if self.base_url.startswith("https://") else None
        )
        self._streams: list = []
        self._stream_closed = False
        self.peer = peer
        self._server_pubkey: Optional[str] = None
        self._introduced = False
        self.last_peer_identity: Optional[str] = None   # last X-Sautium-Peer-Identity seen
        self.last_peer_lane: Optional[str] = None
        self.last_gate_result: Optional[str] = None
        self._gate_header: Optional[str] = None       # a solved quote, attached to the next request once

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
        self.base_url = f"https://127.0.0.1:{port}"
        self._ssl_ctx = _get_ssl_context()

    def _get_json(self, path: str, timeout: int = 5, _paid: bool = False) -> Optional[dict]:
        """GET request returning parsed JSON, or None on failure."""
        url = f"{self.base_url}{path}"
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept-Encoding", "gzip")
            for k, v in self._auth_headers("GET", path, b"").items():
                req.add_header(k, v)
            resp = urllib.request.urlopen(
                req, timeout=timeout, context=self._ssl_ctx
            )
            self._note_peer_response(resp.headers)
            return _read_json_body(resp)
        except urllib.error.HTTPError as e:
            self._note_peer_response(e.headers)
            if e.code == 402 and self.peer is not None and not _paid and self.gate_prepay(endpoint_family(path)):
                return self._get_json(path, timeout, _paid=True)     # priced: pay once and retry
            logger.debug(f"API request failed: {url} — {e}")
            return None
        except Exception as e:
            logger.debug(f"API request failed: {url} — {e}")
            return None

    def _post_json(self, path: str, body: dict = None, timeout: int = 600,
                   _paid: bool = False) -> Optional[dict]:
        """POST request returning parsed JSON, or None on failure."""
        url = f"{self.base_url}{path}"
        try:
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
            resp = urllib.request.urlopen(
                req, timeout=timeout, context=self._ssl_ctx
            )
            self._note_peer_response(resp.headers)
            return _read_json_body(resp)
        except urllib.error.HTTPError as e:
            self._note_peer_response(e.headers)
            if e.code == 402 and self.peer is not None and not _paid and self.gate_prepay(endpoint_family(path)):
                return self._post_json(path, body, timeout, _paid=True)   # priced: pay once and retry
            try:
                body_resp = _read_json_body(e)
                logger.warning(f"API POST {url} returned {e.code}: {body_resp}")
                return body_resp
            except Exception:
                logger.warning(f"API POST {url} returned {e.code}")
                return {"detail": f"HTTP {e.code}"}
        except Exception as e:
            logger.debug(f"API POST failed: {url} — {e}")
            return None

    def get_stats(self) -> Optional[dict]:
        """Fetch library statistics from GET /stats."""
        return self._get_json("/stats")

    def get_health(self) -> Optional[dict]:
        """Fetch health status from GET /health. For a peer this is also
        where its pubkey (node_id) is learned — every later request is
        signed for that recipient."""
        health = self._get_json("/health")
        if self.peer is not None and health and health.get("node_id"):
            node_id = str(health["node_id"]).lower()
            if node_id != self._server_pubkey:
                self._server_pubkey = node_id
                self._introduced = False
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

    def enrich_start(self) -> Optional[dict]:
        """Start background enrichment (all steps)."""
        return self._post_json("/enrich/start", timeout=10)

    def enrich_status(self) -> Optional[dict]:
        """Poll enrichment progress."""
        return self._get_json("/enrich/status", timeout=5)

    def enrich_cancel(self) -> Optional[dict]:
        """Cancel running enrichment."""
        return self._post_json("/enrich/cancel", timeout=5)

    def mb_dump_start(self) -> Optional[dict]:
        """Start the MusicBrainz catalogue download+load in the background
        (the wizard's opt-in, fired once after first start)."""
        return self._post_json("/api/settings/musicbrainz/update", timeout=30)

    def refresh_gear_registries(self) -> Optional[dict]:
        """Refresh measurement registries (spinorama fetched by the
        backend itself; AutoEq reimported from its mount). Import
        guardrails run server-side; slow-ish — network + ~2k upserts."""
        return self._post_json("/api/gear-models/registry/refresh", timeout=300)

    def lastfm_auth_start(self) -> Optional[dict]:
        """Start Last.fm OAuth flow. Returns {"auth_url": "..."}."""
        return self._post_json("/lastfm/auth/start", timeout=10)

    def lastfm_auth_complete(self) -> Optional[dict]:
        """Complete Last.fm OAuth flow. Returns {"session_key": "..."}."""
        return self._post_json("/lastfm/auth/complete", timeout=10)

    def normalize_artists(self) -> Optional[dict]:
        """Run artist normalization (deterministic Pass 1 — feat./vs. splits only)."""
        return self._post_json("/normalize-artists", timeout=120)

    def canonicalize(self) -> Optional[dict]:
        """Trigger backend canonicalization in the background (returns immediately)."""
        return self._post_json("/canonicalize", timeout=10)

    # -- Sync API ----------------------------------------------------------

    def sync_inventory(self, track_uuids: list[str]) -> Optional[dict]:
        """Get available enrichment data for given track UUIDs."""
        return self._post_json(
            "/api/sync/inventory",
            body={"track_uuids": track_uuids},
            timeout=300,
        )

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

    def stream(self, path: str) -> Iterator[None]:
        """Yield once per server-sent event on `path`, forever.

        Deliberately not a payload reader: every channel we consume is a wake
        event whose whole meaning is "re-read state over the signed API", so
        parsing frames would only invite the two to disagree. Blocks; run it
        on a thread and stop it by closing the handle this stores.

        Reconnects on drop with a backoff, because the launcher restarts the
        backend under itself (a scan does exactly that) and a tight retry
        would spin through the whole restart."""
        delay = 1.0
        while not self._stream_closed:
            try:
                url = f"{self.base_url}{path}"
                req = urllib.request.Request(url, method="GET")
                req.add_header("Accept", "text/event-stream")
                for k, v in _sign_headers("GET", path, b"").items():
                    req.add_header(k, v)
                # No read timeout: an idle channel is normal, and the server
                # sends a keepalive comment every 20s to prove it is alive.
                resp = urllib.request.urlopen(req, context=self._ssl_ctx)
                self._streams.append(resp)
                delay = 1.0
                try:
                    for raw in resp:
                        if self._stream_closed:
                            return
                        if raw.startswith(b"data:"):
                            yield None
                finally:
                    self._streams.remove(resp)
                    resp.close()
            except GeneratorExit:
                raise
            except Exception as e:
                logger.debug(f"SSE {path} dropped — {e}")
            if self._stream_closed:
                return
            time.sleep(delay)
            # Capped low: the far end is localhost, and the gap after a
            # restart is a gap in visible scan progress.
            delay = min(delay * 2, 5.0)

    def close_streams(self) -> None:
        """Unblock every reader so launcher shutdown does not wait on a
        connection that is, by design, never going to end on its own."""
        self._stream_closed = True
        for resp in list(self._streams):
            try:
                resp.close()
            except Exception:
                pass

    def mb_slice(self, names: list[str]) -> Optional[dict]:
        """Fetch raw mb_* rows for artist names from a dump-holding peer.
        Long timeout: a batch of prolific namesakes is a large payload."""
        return self._post_json(
            "/api/mb/slice",
            body={"names": names},
            timeout=600,
        )
