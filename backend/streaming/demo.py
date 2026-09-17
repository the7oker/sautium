"""The demo policy: the demo channel (a provider with ``manifest.demo_limited``
— YouTube) streams a track in full at most ONCE. Streaming here is an
acquaintance tool, not a free replacement for a streaming service; past its
one listen a track streams only as the catalog's 30 s excerpt.

One ledger (``demo_plays``: track_id, provider, played_at — life data, backed
up and merged, never synced) and three readers/writers of it:

- the resolve waterfall (routers/player.py) asks ``consumed`` once per pass
  and builds a spent track's chain WITHOUT its demo-limited providers, so the
  chain holds the excerpt provider's real answer, never a lazy guess;
- the media proxy asks ``link_admissible`` before every chain link it is
  about to fetch — a chain is a plan made at resolve time, the decision "may
  this stream from the demo channel NOW" belongs to the moment the bytes are
  fetched;
- the status observer writes the ledger the moment a demo-channel listen
  passes CONSUMED_FRACTION, and when that listen ends drops the spent buffer
  from the proxy: a replay refetches, and the gate cascades it to the
  excerpt. What an output has already buffered for itself (a browser blob,
  a renderer's cache) is beyond reach — a documented limit, not a hole in
  the ledger.

A listen is counted by position, not by the tracker's high-water mark: a
seek to the last tenth is a listener who has heard what they came for.
"""
from __future__ import annotations

import logging
import threading
from typing import Iterable, Optional

from db_pool import db_execute, db_query

from .events import preview_events

logger = logging.getLogger(__name__)

CONSUMED_FRACTION = 0.9


# ---- the ledger --------------------------------------------------------

def consumed(track_ids: Iterable[str]) -> set:
    """The track ids among `track_ids` whose demo listen is spent."""
    ids = sorted({str(t) for t in track_ids if t})
    if not ids:
        return set()
    rows = db_query(
        "SELECT track_id::text AS track_id FROM demo_plays "
        "WHERE track_id = ANY(CAST(%(ids)s AS uuid[]))", {"ids": ids})
    return {r["track_id"] for r in rows}


def mark_consumed(track_id: str, provider_id: str) -> None:
    db_execute(
        "INSERT INTO demo_plays (track_id, provider) VALUES (%(t)s::uuid, %(p)s) "
        "ON CONFLICT (track_id) DO NOTHING", {"t": track_id, "p": provider_id})
    logger.info("demo listen spent: track %s via %s — streams as an excerpt from now on",
                track_id, provider_id)
    preview_events.ping()   # the open album page re-reads availability → the row tag


def admits(provider, track_id: Optional[str], spent: set) -> bool:
    """Whether `provider` may serve `track_id`, given the spent set a pass
    read up front — the resolve waterfall's per-link test."""
    return not (provider.manifest.demo_limited and track_id in spent)


def link_admissible(provider, query) -> bool:
    """The media proxy's fetch-time gate (MediaProxy.link_admissible): one
    ledger lookup, only for a demo-limited provider."""
    if not provider.manifest.demo_limited or not query.track_id:
        return True
    return not consumed([query.track_id])


# ---- the listen ----------------------------------------------------------

_lock = threading.Lock()
_watch: Optional[dict] = None    # the demo-channel listen in progress


def _demo_provider_id(item) -> Optional[str]:
    """The item's provider id when the item is a full-length stream from a
    demo-limited provider — the only kind of play this policy watches."""
    if item is None or not item.preview or item.excerpt or not item.track_id:
        return None
    from . import service
    prov = service.get_provider(item.provider) if item.provider else None
    return item.provider if prov is not None and prov.manifest.demo_limited else None


def _drop_buffer(track_id: str) -> None:
    from . import service
    proxy = service.get_proxy()
    if proxy is not None and proxy.drop_audio(track_id):
        logger.info("demo buffer dropped: track %s — a replay refetches past the demo channel",
                    track_id)


def status_observer(new_data: dict, item) -> None:
    """PlaybackManager status observer (subscribe_status): runs on the active
    output's status thread, after the tracker, on every tick."""
    global _watch
    state = new_data.get("state")
    provider_id = _demo_provider_id(item)
    tid = item.track_id if provider_id else None
    ended = None
    mark = False
    with _lock:
        if _watch is not None and (_watch["track_id"] != tid or state == "stopped"):
            ended, _watch = _watch, None
        if tid is not None and state != "stopped":
            if _watch is None:
                _watch = {"track_id": tid, "provider": provider_id, "marked": False}
            length = new_data.get("length") or 0.0
            position = new_data.get("position") or 0.0
            if (not _watch["marked"] and state == "playing" and length > 0
                    and position / length >= CONSUMED_FRACTION):
                _watch["marked"] = mark = True
    if ended is not None and ended["marked"]:
        _drop_buffer(ended["track_id"])
    if mark:
        mark_consumed(tid, provider_id)
