"""Selects the MusicBrainz data source for canonicalization.

Local dump (``mb_local``) when the ``mb_*`` tables are populated, else the
throttled HTTP API (``musicbrainz``). Resolved at import and re-evaluated by
``refresh()`` after a dump load or a slice import, so consumers
(``mb_audit``, ``mb_canonicalize``) ``import mb_backend as mb`` and read the
source as a module attribute, with no rate-limit logic of their own.

The process that launched this one waited for the database before any
import (Docker: entrypoint.py; launcher: service_manager), so only a missing
``mb_artist`` table — a fresh install whose schema the lifespan is about to
build — means "no dump". A connection failure is a real failure and
propagates: swallowed, it bound the HTTP API on the master for as long as
nothing called ``refresh()`` (2026-09-22).
"""

import logging

import psycopg2.errors

from db_pool import db_query_one

logger = logging.getLogger(__name__)


def _dump_loaded() -> bool:
    try:
        return db_query_one("SELECT 1 FROM mb_artist LIMIT 1") is not None
    except psycopg2.errors.UndefinedTable:
        return False


# True = the offline twin is active → BULK canon (canon.content) is safe (no IP-ban risk).
# When False, callers that bulk-canon must NO-OP, not fall back to the HTTP API (MB is an
# optional layer; sync/enrich proceed on tier-3 deterministic names without it).


def _bind(local: bool) -> None:
    """(Re)point the exported callables at the chosen backend."""
    global _b, search_artist, fetch_album_release_groups, fetch_release_tracklists
    global artist_alt_names, cooldown_active, MBRateLimited
    if local:
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
    # Local-only: alias-bridged alternate artist names for streaming resolve;
    # not worth an HTTP round-trip (and its ban risk) on the playback path.
    artist_alt_names = getattr(_b, "artist_alt_names", lambda *a, **k: [])
    cooldown_active = _b.cooldown_active
    MBRateLimited = _b.MBRateLimited


def refresh() -> bool:
    """Re-evaluate the data source. LOCAL_DUMP is otherwise fixed at import, so a
    backend that started BEFORE the dump was downloaded would never see it and its
    canon would silently no-op (`if not mb.LOCAL_DUMP: skip`). Call this right after
    a dump load to activate the local twin without a backend restart. Returns the
    new LOCAL_DUMP. Consumers must read `mb.LOCAL_DUMP` / `mb.search_artist` (module
    attribute access), never `from mb_backend import ...`, for the rebind to reach them."""
    global LOCAL_DUMP
    LOCAL_DUMP = _dump_loaded()
    _bind(LOCAL_DUMP)
    return LOCAL_DUMP


LOCAL_DUMP = _dump_loaded()
_bind(LOCAL_DUMP)
