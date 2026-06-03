"""MusicBrainz client — the canonicalization authority.

Used by the artist-canonicalization background pass (and its read-only
audit). MB gives a *structural* signal Last.fm can't: an entity either
exists or it doesn't, threshold-free, so it works for niche artists where
listener counts fail. Its alias system bridges non-canonical names
(diacritics, abbreviations, transliterations) to the canonical entity.

MB etiquette is strict and non-negotiable:
  - **1 request/second** hard cap (bursts get 503). Serialized + paced here.
  - A descriptive **User-Agent** is required; generic/absent UA gets blocked.

503 (rate-limited / overloaded) arms a short module-level cooldown so a
caller stops hammering, mirroring the Deezer cooldown in `covers.py`.
"""

import logging
import threading
import time
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://musicbrainz.org/ws/2"
# Identifies the app per MB policy. Contact is the maintainer's address.
_UA = "Sautium/0.1 ( valery.grygorash@gmail.com )"

_MIN_INTERVAL = 1.05            # >1s between calls (MB hard limit is 1/s)
_COOLDOWN_SEC = 60.0           # back off this long after a 503

_lock = threading.Lock()
_last_call = 0.0
_cooldown_until = 0.0


class MBRateLimited(Exception):
    """MB returned 503 / armed the cooldown — caller should back off."""


def cooldown_active() -> bool:
    return time.monotonic() < _cooldown_until


def _get(path: str, params: dict) -> dict:
    """Single paced MB GET. Serialized process-wide at <1 req/s."""
    global _last_call, _cooldown_until
    if cooldown_active():
        raise MBRateLimited("MB cooldown active")
    params = {**params, "fmt": "json"}
    with _lock:
        gap = time.monotonic() - _last_call
        if gap < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - gap)
        try:
            resp = httpx.get(f"{_BASE}/{path}", params=params,
                             headers={"User-Agent": _UA}, timeout=20.0)
        finally:
            _last_call = time.monotonic()
    if resp.status_code == 503:
        _cooldown_until = time.monotonic() + _COOLDOWN_SEC
        raise MBRateLimited("MB 503")
    resp.raise_for_status()
    return resp.json()


def search_artist(name: str, limit: int = 5) -> List[dict]:
    """Search artists by name (alias-aware on MB's side). Returns
    ``[{mbid, name, score, type, country, disambiguation}, ...]`` best-first.
    """
    if not name or not name.strip():
        return []
    payload = _get("artist", {"query": f'artist:"{name.strip()}"', "limit": limit})
    out = []
    for a in payload.get("artists", []):
        out.append({
            "mbid": a.get("id"),
            "name": a.get("name"),
            "score": a.get("score"),
            "type": a.get("type"),
            "country": a.get("country"),
            "disambiguation": a.get("disambiguation", ""),
        })
    return out


def fetch_album_release_groups(mbid: str) -> List[dict]:
    """Album release-groups for an artist MBID, with type classification.

    Queries `type=album` (server-side primary-type filter) and returns
    ``[{title, primary_type, secondary_types, first_year}, ...]``. The
    caller filters secondary-types (Compilation/Live/Remix/DJ-mix/…) to
    decide what counts as a studio album.
    """
    out: List[dict] = []
    offset = 0
    while True:
        payload = _get("release-group", {
            "artist": mbid, "type": "album", "limit": 100, "offset": offset,
        })
        groups = payload.get("release-groups", [])
        for g in groups:
            date = g.get("first-release-date") or ""
            year = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None
            out.append({
                "title": g.get("title") or "",
                "primary_type": g.get("primary-type"),
                "secondary_types": g.get("secondary-types") or [],
                "release_titles": [],  # local-dump-only signal (see mb_local)
                "first_year": year,
            })
        total = payload.get("release-group-count", 0)
        offset += len(groups)
        if not groups or offset >= total:
            break
    return out
