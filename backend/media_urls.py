"""
Signed media URLs for the browser output.

<audio> elements can't attach the HMAC headers auth.js signs every fetch
with, so media byte routes authenticate via short-lived query-param
signatures derived from the SAME shared secret: a leaked URL exposes one
track for a few hours, never the API. Verified by routers/media.py; the
path prefix is whitelisted from the header middleware (auth_hmac) because
these routes carry their own check.
"""

import hashlib
import hmac
import time
from pathlib import Path
from typing import Optional

from auth_hmac import ensure_secret

_SECRET_PATH = Path(__file__).parent / "data" / ".api_secret"
_SECRET_CACHE: Optional[bytes] = None
_TTL_SECONDS = 4 * 3600


def _secret() -> bytes:
    global _SECRET_CACHE
    if _SECRET_CACHE is None:
        _SECRET_CACHE = ensure_secret(_SECRET_PATH)
    return _SECRET_CACHE


def _sign(kind: str, ident: str, exp: int) -> str:
    canonical = f"media\n{kind}\n{ident}\n{exp}"
    return hmac.new(_secret(), canonical.encode(), hashlib.sha256).hexdigest()


def signed_media_url(kind: str, ident: str) -> str:
    """Relative URL (same HTTPS origin as the app — no mixed content) for
    one media object. kind ∈ file (owned media_file_id) | preview (proxy
    token)."""
    exp = int(time.time()) + _TTL_SECONDS
    sig = _sign(kind, str(ident), exp)
    return f"/api/player/media/{kind}/{ident}?exp={exp}&sig={sig}"


def verify(kind: str, ident: str, exp: Optional[str], sig: Optional[str]) -> bool:
    if not exp or not sig:
        return False
    try:
        exp_i = int(exp)
    except ValueError:
        return False
    if exp_i < time.time():
        return False
    return hmac.compare_digest(_sign(kind, str(ident), exp_i), sig)
