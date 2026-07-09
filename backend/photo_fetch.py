"""Shared exceptions for external artist-photo / album-cover fetching.

Both remaining image sources — Deezer's JSON API (`deezer_photos`) for
artist photos and Last.fm's `album.getInfo` cover lookup (`lastfm`) for
album art — signal recoverable failures through these types so the cover
resolver can tell three outcomes apart:

  - a transient hiccup (timeout, 5xx)  → leave unresolved, retry later;
  - an active rate-limit (429/quota)   → enter a global cooldown;
  - a genuine "no such image"          → pin the sentinel (a None return,
                                          not an exception).

The transient-vs-permanent distinction is load-bearing: conflating them
would mark a real artist photo-less forever on one bad moment.

(The Last.fm artist-photo HTML scraper that used to live here was
retired — it was the sole source of the 406 IP-bans, and Deezer covers
every artist it did. History: `git log -- backend/lastfm_photos.py`.)
"""


class TransientFetchError(Exception):
    """A photo/cover fetch failed for a recoverable reason (timeout,
    connection reset, 5xx). The caller leaves the target unresolved so
    the next request retries instead of pinning a permanent sentinel."""


class RateLimitError(TransientFetchError):
    """The source actively rate-limited us — HTTP 429, or a provider
    quota code (Deezer error 4 "Quota limit exceeded" / 700 "Service
    busy"). Distinct from a generic transient error so the caller can
    enter a global cooldown instead of hammering the throttled source
    on every retry."""
