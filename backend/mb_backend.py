"""Whether this node holds MusicBrainz rows, and the queries the canon runs on them.

The canon reads the local ``mb_*`` tables (``mb_local``): a full dump, or the
slices other nodes signed. ``LOCAL_DUMP`` says whether any rows are here —
resolved at import and re-evaluated by ``refresh()`` after a dump load or a
slice import — and without them every canon step is a no-op. A node with
neither the dump nor a slice asks MusicBrainz nothing about its library: its
web service is only asked, by MBID, to confirm what a slice says
(``mb_answers``). Consumers ``import mb_backend as mb`` and read
``mb.LOCAL_DUMP`` as a module attribute, so a refresh reaches them.

The process that launched this one waited for the database before any
import (Docker: entrypoint.py; launcher: service_manager), so only a missing
``mb_artist`` table — a fresh install whose schema the lifespan is about to
build — means "no rows". A connection failure is a real failure and
propagates (2026-09-22: swallowed, it hid the dump from the master for as long
as nothing called ``refresh()``).
"""

import psycopg2.errors

from db_pool import db_query_one
from mb_local import (artist_alt_names, fetch_album_release_groups,
                      fetch_release_tracklists, search_artist)

__all__ = ["LOCAL_DUMP", "refresh", "artist_alt_names", "fetch_album_release_groups",
           "fetch_release_tracklists", "search_artist"]


def _dump_loaded() -> bool:
    try:
        return db_query_one("SELECT 1 FROM mb_artist LIMIT 1") is not None
    except psycopg2.errors.UndefinedTable:
        return False


def refresh() -> bool:
    """Re-evaluate LOCAL_DUMP — otherwise fixed at import, so a backend that
    started before the dump or the first slice arrived would keep its canon a
    no-op. Called after a dump load and a slice import; returns the new value."""
    global LOCAL_DUMP
    LOCAL_DUMP = _dump_loaded()
    return LOCAL_DUMP


LOCAL_DUMP = _dump_loaded()
