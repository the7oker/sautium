"""Last.fm artist photo fetcher.

The Last.fm REST API stopped returning artist images in 2019, but the
public web pages still serve them via the standard `og:image` meta tag.
We fetch the dedicated images page (`/music/<artist>/+images`) and pull
the og:image URL — that's the photo Last.fm itself shows as the
artist's primary image, chosen by community votes.

robots.txt allows /music/<artist>/+images. Called from the lazy
/api/covers/by-artist/{id} resolver, so requests are naturally
distributed across user actions — no artificial rate limit needed.

Two failure modes the resolver must distinguish, because the cost of
conflating them is permanent: transient network errors leave the
artist unresolved (caller retries on the next request), permanent
failures (404 / no og:image / placeholder photo) pin the SENTINEL so
we don't re-scrape on every page load. We signal transient via the
TransientFetchError exception and permanent via a None return.
"""

import logging
import re
from typing import Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


class TransientFetchError(Exception):
    """Raised when the Last.fm fetch failed for a network reason
    (timeout, connection reset, etc). The caller leaves the artist's
    photo_cover_id NULL so the next request retries."""


_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0 Safari/537.36"
)

_OG_IMAGE_RE = re.compile(
    r'<meta\s+property="og:image"\s+content="([^"]+)"',
    re.IGNORECASE,
)


def fetch_lastfm_photo_url(artist_name: str) -> Optional[str]:
    """Return the canonical og:image URL for an artist on Last.fm.

    Returns:
      - URL string when the page has a real photo.
      - None when the page exists but has no usable image: 404, no
        og:image meta tag, or the well-known cardboard-star
        placeholder. Caller pins SENTINEL.

    Raises TransientFetchError on network / timeout failures so the
    caller leaves photo_cover_id NULL and retries on the next request.
    """
    if not artist_name or not artist_name.strip():
        return None
    slug = quote(artist_name.strip().replace(" ", "+"), safe="+")
    url = f"https://www.last.fm/music/{slug}/+images"
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            resp = client.get(url, headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html",
                "Accept-Language": "en-US,en;q=0.9",
            })
    except httpx.HTTPError as e:
        # Timeout, connection reset, DNS hiccup, TLS handshake — all
        # transient by nature. Surface as exception so the resolver
        # doesn't pin a sentinel on a recoverable error.
        raise TransientFetchError(str(e)) from e

    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        # 5xx and other non-200 are likely transient (server overload,
        # CDN hiccup) — retry next time rather than giving up forever.
        raise TransientFetchError(f"HTTP {resp.status_code}")

    m = _OG_IMAGE_RE.search(resp.text)
    if not m:
        return None
    photo_url = m.group(1)
    # Last.fm serves a generic "no image" placeholder for empty
    # galleries. The placeholder image hash is well-known; skip it
    # so we don't store a cardboard star.
    if "2a96cbd8b46e442fc41c2b86b821562f" in photo_url:
        return None
    return photo_url
