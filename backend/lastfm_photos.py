"""Last.fm artist photo fetcher.

The Last.fm REST API stopped returning artist images in 2019, but the
public web pages still serve them via the standard `og:image` meta tag.
We fetch the dedicated images page (`/music/<artist>/+images`) and pull
the og:image URL — that's the photo Last.fm itself shows as the
artist's primary image, chosen by community votes.

robots.txt allows /music/<artist>/+images. Called from the lazy
/api/covers/by-artist/{id} resolver, so requests are naturally
distributed across user actions — no artificial rate limit needed.
"""

import logging
import re
from typing import Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0 Safari/537.36"
)

_OG_IMAGE_RE = re.compile(
    r'<meta\s+property="og:image"\s+content="([^"]+)"',
    re.IGNORECASE,
)


def fetch_lastfm_photo_url(artist_name: str) -> Optional[str]:
    """Return the canonical og:image URL for an artist on Last.fm, or None
    if the page doesn't exist / has no preferred photo. Network and
    parse failures return None — the caller maps None to a sentinel so
    we don't re-scrape on every request."""
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
        if resp.status_code != 200:
            logger.info(
                f"last.fm photo lookup for {artist_name!r}: "
                f"HTTP {resp.status_code}"
            )
            return None
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
    except httpx.HTTPError as e:
        logger.warning(f"last.fm photo fetch failed for {artist_name!r}: {e}")
        return None
