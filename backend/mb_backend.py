"""Selects the MusicBrainz data source for canonicalization.

Local dump (``mb_local``) when the ``mb_*`` tables are populated, else the
throttled HTTP API (``musicbrainz``). Resolved once at import — the dump is
loaded by an offline batch job, never mid-process, and the backend is
restarted after a load — so consumers (``mb_audit``, ``mb_canonicalize``) just
``import mb_backend as mb`` and get the right source with no rate-limit logic
of their own to change.
"""

import logging

from db_pool import db_query_one

logger = logging.getLogger(__name__)


def _dump_loaded() -> bool:
    try:
        return db_query_one("SELECT 1 FROM mb_artist LIMIT 1") is not None
    except Exception:
        return False  # tables not yet created (fresh install) → API


if _dump_loaded():
    import mb_local as _b
    logger.info("MB canonicalization using LOCAL dump (mb_* tables)")
else:
    import musicbrainz as _b
    logger.info("MB canonicalization using HTTP API (dump not loaded)")

search_artist = _b.search_artist
fetch_album_release_groups = _b.fetch_album_release_groups
# Local-only (tracklist content fingerprint); the HTTP API can't page every
# release's tracklist cheaply → empty there, the algorithm degrades to album-overlap.
fetch_release_tracklists = getattr(_b, "fetch_release_tracklists", lambda *a, **k: [])
cooldown_active = _b.cooldown_active
MBRateLimited = _b.MBRateLimited
