"""Deezer artist photo lookup — the sole artist-image source.

Last.fm dropped artist images from its REST API in 2019, leaving only
fragile HTML scraping that rate-limits (and ultimately bans) the whole
IP — so that fallback was retired and we rely on Deezer alone. A
library-wide probe found Deezer covers 100% of artists that previously
had a resolved photo, so nothing was lost. Deezer's public JSON API
needs no key and returns clean artist images via `search/artist` →
`picture_xl` (1000x1000), with far more generous limits.

Deezer signals overload two ways: HTTP 429, or — more commonly — an
in-band `{"error": {"code": 4, ...}}` body served with HTTP 200 ("Quota
limit exceeded", code 4; "Service busy", code 700). Both surface as
RateLimitError so the caller enters its global cooldown rather than
hammering the API. Lookups run behind the shared throttle in
`routers/covers.py` — never call this in an unthrottled loop.
"""

import logging
from typing import Optional

import httpx

from photo_fetch import RateLimitError, TransientFetchError

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.deezer.com/search/artist"

# Deezer error codes that mean "back off", not "no such artist".
_QUOTA_CODES = {4, 700}


def fetch_deezer_photo_url(artist_name: str) -> Optional[str]:
    """Return the best-match artist's `picture_xl` URL, or None.

    None = Deezer has no match or only a silhouette placeholder; the
    caller pins SENTINEL. Raises RateLimitError on quota/429 and
    TransientFetchError on network errors so the caller backs off
    instead of marking the artist permanently photo-less.
    """
    if not artist_name or not artist_name.strip():
        return None
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(_SEARCH_URL, params={"q": artist_name.strip(), "limit": 1})
    except httpx.HTTPError as e:
        raise TransientFetchError(str(e)) from e

    if resp.status_code == 429:
        raise RateLimitError("HTTP 429")
    if resp.status_code != 200:
        raise TransientFetchError(f"HTTP {resp.status_code}")

    try:
        payload = resp.json()
    except ValueError as e:
        raise TransientFetchError(f"bad JSON: {e}") from e

    err = payload.get("error")
    if err:
        code = err.get("code") if isinstance(err, dict) else None
        if code in _QUOTA_CODES:
            raise RateLimitError(f"deezer quota (code {code})")
        raise TransientFetchError(f"deezer error: {err}")

    data = payload.get("data") or []
    if not data:
        return None
    pic = data[0].get("picture_xl") or data[0].get("picture_big") or ""
    # Artists with no real image still 200, but the URL carries an empty
    # id segment (`/images/artist//...`) — that's the generic silhouette.
    if not pic or "/artist//" in pic:
        return None
    return pic
