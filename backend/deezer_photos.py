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

NAMESAKES. A name is not an identity: `search/artist?q=vangelis` returns
three artists called exactly "Vangelis", and the one Deezer ranks first
has a single album and 20 fans while the composer has 68 and 209k. Taking
the top hit therefore put a stranger's face on the artist page. So when
several exact-name candidates come back we ask for evidence instead of
trusting the ranking: the caller passes album titles it already knows,
and the candidate whose Deezer catalogue actually contains one of them
wins. Popularity only breaks a tie nothing else could — a prior, never
the argument. (Deezer's own advanced query `artist:"X" album:"Y"` is not
a shortcut here: it answers with other artists' COVERS of the album, so
it would trade a wrong namesake for an outright wrong artist.)
"""

import logging
import re
from typing import Optional, Sequence

import httpx

from photo_fetch import RateLimitError, TransientFetchError

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.deezer.com/search/artist"
_ALBUMS_URL = "https://api.deezer.com/artist/{}/albums"

_CANDIDATES = 5          # exact-name namesakes to look at
_DISAMBIGUATE_MAX = 3    # ...of which this many are worth a catalogue request
_CATALOGUE_LIMIT = 100   # albums pulled per candidate (Vangelis alone has 68)

# Deezer error codes that mean "back off", not "no such artist".
_QUOTA_CODES = {4, 700}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _base(title: str) -> str:
    """The title without its edition baggage: both sides carry their own and
    it rarely agrees — ours says "Blade Runner (Esper Edition MK2)" where
    Deezer says "Blade Runner (Music From The Original Soundtrack)"."""
    return _norm(re.sub(r"[\(\[].*?[\)\]]|\s[-–]\s.*$", " ", title or ""))


def _titles_match(ours: str, theirs: str) -> bool:
    a, b = _norm(ours), _norm(theirs)
    if not a or not b:
        return False
    if a == b:
        return True
    # One side annotated, the other bare ("Blade Runner" ⊂ "Blade Runner
    # (Esper Edition MK2)"), then both annotated differently.
    if len(a) >= 5 and len(b) >= 5 and (a in b or b in a):
        return True
    ba, bb = _base(ours), _base(theirs)
    return ba == bb and len(ba) >= 5


def _get(url: str, params: dict) -> dict:
    try:
        with httpx.Client(timeout=10.0) as client:
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


def _picture(artist: dict) -> Optional[str]:
    pic = artist.get("picture_xl") or artist.get("picture_big") or ""
    # Artists with no real image still 200, but the URL carries an empty
    # id segment (`/images/artist//...`) — that's the generic silhouette.
    return None if (not pic or "/artist//" in pic) else pic


def _catalogue_overlap(artist_id, titles: Sequence[str]) -> int:
    """How many of the albums we know this artist by are in their Deezer
    catalogue. A COUNT, not a flag: generic titles ("Greatest Hits", "Live")
    collide across artists, so one hit is weak evidence and several is strong
    — and the namesake that matches most is the one we mean."""
    data = _get(_ALBUMS_URL.format(artist_id), {"limit": _CATALOGUE_LIMIT}).get("data") or []
    theirs = [a.get("title", "") for a in data]
    return sum(1 for ours in titles if any(_titles_match(ours, t) for t in theirs))


def _pick(candidates: list, name: str, album_titles: Sequence[str]) -> dict:
    """The candidate our albums point at; the most-followed one otherwise."""
    exact = [a for a in candidates if _norm(a.get("name", "")) == _norm(name)]
    if not exact:
        return candidates[0]        # no exact namesake — Deezer's own best guess
    if len(exact) == 1:
        return exact[0]

    exact.sort(key=lambda a: a.get("nb_fan") or 0, reverse=True)
    if album_titles:
        best, best_overlap = None, 0
        for cand in exact[:_DISAMBIGUATE_MAX]:
            overlap = _catalogue_overlap(cand["id"], album_titles)
            if overlap > best_overlap:
                best, best_overlap = cand, overlap
        if best is not None:
            logger.info("deezer namesakes for %r: %d exact — picked id=%s, "
                        "%d/%d albums match its catalogue",
                        name, len(exact), best["id"], best_overlap, len(album_titles))
            return best
    logger.info("deezer namesakes for %r: %d exact — no catalogue evidence, "
                "picked id=%s on %s followers", name, len(exact),
                exact[0]["id"], exact[0].get("nb_fan"))
    return exact[0]


def fetch_deezer_photo_url(artist_name: str,
                           album_titles: Sequence[str] = ()) -> Optional[str]:
    """Return the chosen artist's `picture_xl` URL, or None.

    `album_titles` are albums WE credit to this artist — the evidence that
    settles a namesake (see the module docstring). Passing none is safe; it
    just leaves popularity as the only tiebreak.

    None = Deezer has no match or only a silhouette placeholder; the
    caller pins SENTINEL. Raises RateLimitError on quota/429 and
    TransientFetchError on network errors so the caller backs off
    instead of marking the artist permanently photo-less.
    """
    if not artist_name or not artist_name.strip():
        return None
    name = artist_name.strip()
    data = _get(_SEARCH_URL, {"q": name, "limit": _CANDIDATES}).get("data") or []
    if not data:
        return None
    return _picture(_pick(data, name, album_titles))
