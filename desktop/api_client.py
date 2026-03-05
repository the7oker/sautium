"""
Minimal HTTP client for communicating with the Music AI DJ backend.

Uses only urllib (no extra dependencies) to fetch stats and health info.
"""

import json
import logging
import urllib.parse
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger(__name__)


class BackendAPIClient:
    """HTTP client for the backend API."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000"):
        self.base_url = base_url.rstrip("/")

    def set_port(self, port: int):
        """Update the backend port."""
        self.base_url = f"http://127.0.0.1:{port}"

    def _get_json(self, path: str, timeout: int = 5) -> Optional[dict]:
        """GET request returning parsed JSON, or None on failure."""
        url = f"{self.base_url}{path}"
        try:
            req = urllib.request.urlopen(url, timeout=timeout)
            return json.loads(req.read().decode("utf-8"))
        except Exception as e:
            logger.debug(f"API request failed: {url} — {e}")
            return None

    def _post_json(self, path: str, timeout: int = 600) -> Optional[dict]:
        """POST request returning parsed JSON, or None on failure."""
        url = f"{self.base_url}{path}"
        try:
            req = urllib.request.Request(url, method="POST", data=b"")
            resp = urllib.request.urlopen(req, timeout=timeout)
            return json.loads(resp.read().decode("utf-8"))
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
        """Start library scan. Returns scan results dict or None."""
        params = "skip_existing=true"
        if subpath:
            params += f"&subpath={urllib.parse.quote(subpath)}"
        return self._post_json(f"/scan?{params}")
