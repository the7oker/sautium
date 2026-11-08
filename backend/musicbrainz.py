"""MusicBrainz's web service (WS/2), asked by MBID only.

A node without the MusicBrainz database canonicalizes on catalogue slices
other nodes signed, and any node may sign one. So before the canon merges,
renames or mints on what a slice says, the entities the decision rests on
are looked up here by their MBIDs and the decision is re-made on
MusicBrainz's own answers (backend/mb_answers.py keeps them,
backend/canon/confirm.py decides on them). Nothing here asks by name: a name
search would tell MusicBrainz what the owner's library holds in words, and
the slice already names the candidates.

MusicBrainz's rules (its WS/2 documentation): one request a second per
address on average — past it every request is declined with a 503 until the
rate drops — and a User-Agent naming the application and a contact. Hence
one pace per process (MIN_INTERVAL), the project's site as the contact, never
a person's, and three kinds of failure, so that what one entity's request
meets never holds back the rest:

- `RateLimited` — a 503 or a 429, the pace rule: nothing is asked for a short
  pause that grows while the refusals go on (RATE_PAUSE…RATE_PAUSE_CAP, with
  jitter);
- `Unavailable` — MusicBrainz cannot be reached, refuses this client (401,
  403), or answers with something that is not its JSON (a captive portal, a
  proxy's page): the persistent cooldown is armed (api_cooldown, source
  'musicbrainz') and the queue waits for it;
- `Failed` — the request for this one entity went wrong (another 4xx but
  404, a 5xx but 503, an answer that broke off or timed out): the item backs
  off on its own and the queue goes on (mb_answers).

A 404 is an answer: the entity is unknown.
"""

import random
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import httpx

import api_cooldown

BASE = "https://musicbrainz.org/ws/2"
USER_AGENT = "Sautium/1.0 ( https://sautium.net )"
MIN_INTERVAL = 1.05
TIMEOUT = 20.0
BROWSE_PAGE = 100
COOLDOWN_SOURCE = "musicbrainz"
RATE_PAUSE = 2.0
RATE_PAUSE_CAP = 600.0

# What each lookup includes. One artist lookup serves the name checks
# (aliases), the genre channel, the "member of band" check and the
# artist-level Buy page; one release lookup serves a tracklist, its group and
# credits, and the release's Buy page.
INC = {
    "artist": "aliases+genres+artist-rels+url-rels",
    "release_group": "artist-credits",
    "release": "recordings+artist-credits+release-groups+url-rels",
    "recording": "artist-credits",
}
_PATH = {"artist": "artist", "release_group": "release-group",
         "release": "release", "recording": "recording"}

# Not MusicBrainz's doing for one entity: the network, a proxy, a portal.
_UNREACHABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError,
                httpx.UnsupportedProtocol, httpx.TooManyRedirects, httpx.DecodingError)

_lock = threading.Lock()
_last_call = 0.0
_refusals = 0           # 503s and 429s since the last answer
_paused_until = 0.0     # time.monotonic() before which nothing is asked


class NotFound(Exception):
    """A definitive 404: MusicBrainz has no entity of that kind under the MBID
    (removed, or never issued — WS/2 does not tell them apart)."""


class Unavailable(Exception):
    """No answer, and none about the entity: MusicBrainz cannot be reached or
    answered with something that is not its JSON. The persistent cooldown is
    armed; the queue waits."""


class RateLimited(Unavailable):
    """A 503 or a 429: nothing is asked for `retry_after` seconds."""

    def __init__(self, reason: str, retry_after: float):
        super().__init__(reason)
        self.retry_after = retry_after


class Failed(Exception):
    """The request for this entity failed; the item backs off, the rest of the
    queue goes on."""


def cooldown_active() -> bool:
    return api_cooldown.cooling_down(COOLDOWN_SOURCE) is not None


def _unreachable(reason: str) -> Unavailable:
    api_cooldown.arm(COOLDOWN_SOURCE, reason)
    return Unavailable(reason)


def _refused(status: int, retry_after: Optional[str]) -> RateLimited:
    global _refusals, _paused_until
    _refusals += 1
    pause = min(RATE_PAUSE_CAP, RATE_PAUSE * 2 ** min(_refusals - 1, 16)) * random.uniform(0.5, 1.5)
    # Retry-After in seconds is honoured up to the cap; its HTTP-date form is not
    # worth a clock comparison with MusicBrainz's.
    if retry_after and retry_after.strip().isdigit():
        pause = max(pause, min(float(retry_after), RATE_PAUSE_CAP))
    _paused_until = time.monotonic() + pause
    return RateLimited(f"HTTP {status}", pause)


def _get(path: str, params: Dict[str, str], shaped: Callable, fmt: str = "json"):
    """One paced GET, serialized process-wide, at most one a MIN_INTERVAL; its
    JSON (or, for `fmt="txt"`, its text), when `shaped` finds it
    MusicBrainz's."""
    global _last_call, _refusals
    if cooldown_active():
        raise Unavailable("cooling down")
    with _lock:
        left = _paused_until - time.monotonic()
        if left > 0:
            raise RateLimited("pausing for MusicBrainz's pace", left)
        gap = time.monotonic() - _last_call
        if gap < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - gap)
        try:
            resp = httpx.get(f"{BASE}/{path}", params={**params, "fmt": fmt},
                             headers={"User-Agent": USER_AGENT,
                                      "Accept": "application/json" if fmt == "json" else "text/plain"},
                             timeout=TIMEOUT, follow_redirects=True)
        except _UNREACHABLE as e:
            raise _unreachable(f"MusicBrainz unreachable: {type(e).__name__}") from e
        except httpx.TransportError as e:
            raise Failed(f"{type(e).__name__}") from e
        except httpx.HTTPError as e:
            raise _unreachable(f"MusicBrainz unreachable: {type(e).__name__}") from e
        finally:
            _last_call = time.monotonic()
        if resp.status_code in (503, 429):
            raise _refused(resp.status_code, resp.headers.get("Retry-After"))
    if resp.status_code == 404:
        # MusicBrainz's own 404 carries its JSON error; one from anything else
        # (a proxy, a portal, an HTML error page) says nothing about the entity.
        try:
            error = resp.json()
        except ValueError:
            error = None
        if fmt != "json" or not (isinstance(error, dict) and "error" in error):
            raise _unreachable("a 404 that is not MusicBrainz's")
        _refusals = 0
        api_cooldown.clear(COOLDOWN_SOURCE)
        raise NotFound(path)
    if resp.status_code == 200:
        _refusals = 0
    if resp.status_code in (401, 403):
        raise _unreachable(f"MusicBrainz refused this client: HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise Failed(f"HTTP {resp.status_code}")
    if fmt != "json":
        body = resp.text
        if not shaped(body):
            raise _unreachable("an answer that is not MusicBrainz's text")
        api_cooldown.clear(COOLDOWN_SOURCE)
        return body
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not shaped(body):
        raise _unreachable("an answer that is not MusicBrainz's JSON")
    api_cooldown.clear(COOLDOWN_SOURCE)
    return body


def lookup(kind: str, mbid: str) -> dict:
    """The entity of `kind` under `mbid`, as WS/2's JSON. A merged MBID
    answers with the entity it was merged into: the body's `id` is then not
    the MBID asked. Raises NotFound, RateLimited, Unavailable or Failed."""
    return _get(f"{_PATH[kind]}/{mbid}", {"inc": INC[kind]},
                lambda body: isinstance(body.get("id"), str))


def browse_release_groups_page(artist_mbid: str, offset: int) -> Tuple[List[dict], int]:
    """One page (BROWSE_PAGE) of the release groups MusicBrainz credits to the
    artist, of every type and status, with their artist credits, from
    `offset`; and how many there are in all. Raises NotFound, RateLimited,
    Unavailable or Failed."""
    page = _get("release-group", {"artist": artist_mbid, "inc": "artist-credits",
                                  "release-group-status": "all",
                                  "limit": str(BROWSE_PAGE), "offset": str(offset)},
                lambda body: isinstance(body.get("release-groups"), list)
                and isinstance(body.get("release-group-count"), int))
    return page["release-groups"], page["release-group-count"]


def genre_names() -> List[str]:
    """MusicBrainz's curated genre vocabulary (its plain-text list, one request
    at the one pace). Raises RateLimited, Unavailable or Failed."""
    text = _get("genre/all", {}, lambda body: bool(body.strip()), fmt="txt")
    return sorted({line.strip() for line in text.splitlines() if line.strip()})
