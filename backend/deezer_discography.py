"""Deezer discography lookup — the source for an artist's full album list.

Companion to `deezer_photos.py` (same public JSON API, no key, same
back-off contract). Two calls:

  search/artist?q=NAME      → resolve a name to a Deezer artist id
  artist/{id}/albums        → the artist's releases (paginated)

Like the photo lookup, Deezer signals overload either with HTTP 429 or
an in-band ``{"error": {"code": 4|700}}`` body on HTTP 200 — both raise
RateLimitError so the caller arms the shared cooldown in
`covers.py` rather than hammering the API. Never call this in an
unthrottled loop; the background step paces it and the fetch-on-view
path is gated by `artists.last_album_sync`.
"""

import logging
from typing import List, Optional

import httpx

from lastfm_photos import RateLimitError, TransientFetchError

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.deezer.com/search/artist"
_ALBUMS_URL = "https://api.deezer.com/artist/{id}/albums"

# Deezer error codes that mean "back off", not "no such resource".
_QUOTA_CODES = {4, 700}

# Cap pagination — a 100/page artist with >5 pages is a label/various-
# artists bucket, not a real discography. 500 releases is already noise.
_MAX_PAGES = 5
_PAGE_SIZE = 100


def _get_json(client: httpx.Client, url: str, params: Optional[dict] = None) -> dict:
    try:
        resp = client.get(url, params=params)
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
    return payload


def resolve_deezer_artist_id(artist_name: str) -> Optional[int]:
    """Return the best-match Deezer artist id for a name, or None.

    Raises RateLimitError / TransientFetchError so the caller backs off
    instead of treating a transient failure as 'artist not on Deezer'.
    """
    if not artist_name or not artist_name.strip():
        return None
    with httpx.Client(timeout=10.0) as client:
        payload = _get_json(client, _SEARCH_URL,
                            {"q": artist_name.strip(), "limit": 1})
    data = payload.get("data") or []
    if not data:
        return None
    did = data[0].get("id")           # Deezer ids are integers
    return int(did) if did else None


def _parse_year(release_date: Optional[str]) -> Optional[int]:
    """Deezer release_date is 'YYYY-MM-DD'. Pull the year, tolerate junk."""
    if not release_date or len(release_date) < 4:
        return None
    head = release_date[:4]
    return int(head) if head.isdigit() else None


def fetch_artist_albums(deezer_id: int) -> List[dict]:
    """Return the artist's releases as
    ``[{deezer_album_id, title, year, cover_url, record_type}, ...]``.

    Follows Deezer's ``next`` pagination up to `_MAX_PAGES`. Raises on
    quota/network errors (caller backs off).
    """
    out: List[dict] = []
    url: Optional[str] = _ALBUMS_URL.format(id=deezer_id)
    params: Optional[dict] = {"limit": _PAGE_SIZE}
    with httpx.Client(timeout=10.0) as client:
        for _ in range(_MAX_PAGES):
            payload = _get_json(client, url, params)
            for a in payload.get("data") or []:
                title = (a.get("title") or "").strip()
                if not title:
                    continue
                out.append({
                    "deezer_album_id": str(a.get("id")) if a.get("id") else None,
                    "title": title,
                    "year": _parse_year(a.get("release_date")),
                    "cover_url": a.get("cover_xl") or a.get("cover_big") or None,
                    "record_type": a.get("record_type") or "album",
                })
            # `next` carries the full URL with its own params baked in.
            url = payload.get("next")
            params = None
            if not url:
                break
    return out
