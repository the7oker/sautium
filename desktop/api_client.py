"""
Minimal HTTP/HTTPS client for communicating with the Sautium backend.

Uses only urllib (no extra dependencies) to fetch stats and health info.
Supports HTTPS with self-signed certificates for P2P connections.

Every request is signed with HMAC-SHA256 over
``METHOD\\nPATH_AND_QUERY\\nTS\\nsha256_hex(body)`` using the secret
shared with the backend (lives in ``backend/data/.api_secret``).
Signature headers: ``X-Sautium-Ts``, ``X-Sautium-Sig``. Endpoints
that the backend whitelists (e.g. ``/api/sync/*``) tolerate
unsigned requests — see backend/auth_hmac.py.
"""

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
from typing import Optional

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
    """HTTP/HTTPS client for the backend API."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000"):
        self.base_url = base_url.rstrip("/")
        self._ssl_ctx = (
            _get_ssl_context() if self.base_url.startswith("https://") else None
        )

    def set_port(self, port: int):
        """Update the backend port."""
        self.base_url = f"http://127.0.0.1:{port}"
        self._ssl_ctx = None

    def _get_json(self, path: str, timeout: int = 5) -> Optional[dict]:
        """GET request returning parsed JSON, or None on failure."""
        url = f"{self.base_url}{path}"
        try:
            req = urllib.request.Request(url, method="GET")
            for k, v in _sign_headers("GET", path, b"").items():
                req.add_header(k, v)
            resp = urllib.request.urlopen(
                req, timeout=timeout, context=self._ssl_ctx
            )
            return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.debug(f"API request failed: {url} — {e}")
            return None

    def _post_json(self, path: str, body: dict = None, timeout: int = 600) -> Optional[dict]:
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
            for k, v in _sign_headers("POST", path, data).items():
                req.add_header(k, v)
            resp = urllib.request.urlopen(
                req, timeout=timeout, context=self._ssl_ctx
            )
            return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body_resp = json.loads(e.read().decode("utf-8"))
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
        """Fetch health status from GET /health."""
        return self._get_json("/health")

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

    def lastfm_auth_start(self) -> Optional[dict]:
        """Start Last.fm OAuth flow. Returns {"auth_url": "..."}."""
        return self._post_json("/lastfm/auth/start", timeout=10)

    def lastfm_auth_complete(self) -> Optional[dict]:
        """Complete Last.fm OAuth flow. Returns {"session_key": "..."}."""
        return self._post_json("/lastfm/auth/complete", timeout=10)

    def normalize_artists(self, pass2: bool = False) -> Optional[dict]:
        """Run artist normalization (Pass 1 by default, Pass 2 if pass2=True)."""
        params = f"pass2={str(pass2).lower()}"
        return self._post_json(f"/normalize-artists?{params}", timeout=120)

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
