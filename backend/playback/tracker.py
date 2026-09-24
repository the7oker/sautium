"""
Play-event tracking: listening_history + local_play_stats + Last.fm.

Consolidated into the status poller (was a separate playback_tracker.py
daemon resolving the playing track by file_path → media_files.id, which
silently dropped phantom http previews). The poller reads player status
every tick and resolves the playing row's SOURCE-AGNOSTIC identity from
the playlist payload — every row carries a `track_id` UUID (owned AND
phantom), with `media_files.id` as the optional physical file. Keying
play events on that UUID makes streamed phantom plays first-class: they
land in listening_history / local_play_stats and scrobble exactly like
owned files, with no per-source code.

This is the single tracking point above the output-backend abstraction
(HARDWARE-TIERS §2.6) — it consumes status ticks, never a specific player.
"""

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from api_cooldown import cooling_down
from config import settings
from db_pool import db_execute as _db_execute
from play_stats import PLAY_STATS_SQL

logger = logging.getLogger(__name__)

_SCROBBLE_MIN_SECONDS = 240  # Last.fm: scrobble after >50% OR >4 min, whichever first

_play_session: "Optional[_PlaySession]" = None
_scrobbler = None


class _PlaySession:
    """One listening session for the currently-playing track, keyed on the
    track UUID (source-agnostic). `media_file_id` is the optional physical file
    (owned only; None for phantom previews)."""

    def __init__(self, ident: dict, started_at: datetime, track_length: float):
        self.track_id = ident["track_id"]
        self.media_file_id = ident.get("media_file_id")
        self.artist = ident.get("artist") or ""
        self.title = ident.get("title") or ""
        self.album = ident.get("album") or None
        self.duration = ident.get("duration")
        self.started_at = started_at
        self.track_length = track_length
        self.max_position = 0.0
        self.scrobbled = False

    @property
    def percent_listened(self) -> float:
        if self.track_length > 0:
            return min(100.0, (self.max_position / self.track_length) * 100)
        return 0.0

    @property
    def scrobble_ready(self) -> bool:
        if self.max_position < 30:
            return False
        return (self.percent_listened >= 50
                or self.max_position >= _SCROBBLE_MIN_SECONDS)

    @property
    def completed(self) -> bool:
        return self.scrobble_ready or (
            self.track_length > 0 and self.max_position >= self.track_length - 5)

    def update_position(self, position: float) -> None:
        self.max_position = max(self.max_position, position)


def _get_scrobbler():
    """The Last.fm network scrobbles go through, built from the session the
    node holds NOW. `settings` carries what the authorization flow persisted
    (lastfm_auth — the DB row overlays .env at startup, the flow updates it
    at runtime), so a session earned while the backend runs scrobbles the
    next play: no restart, no environment variable. None while there is no
    session."""
    global _scrobbler
    session_key = settings.lastfm_session_key
    if not (settings.lastfm_api_key and settings.lastfm_api_secret and session_key):
        _scrobbler = None
        return None
    if _scrobbler is None or _scrobbler.session_key != session_key:
        from lastfm import lastfm_network
        _scrobbler = lastfm_network(session_key, settings.lastfm_username or "")
        logger.info("Last.fm scrobbler initialized (user=%s)", settings.lastfm_username or "?")
    return _scrobbler


def _scrobble_async(method: str, **kwargs) -> None:
    """Fire a Last.fm call (scrobble / update_now_playing) off the poller thread
    — the network round-trip must never stall status polling."""
    net = _get_scrobbler()
    if net is None:
        return
    if cooling_down('lastfm'):
        return  # Last.fm is rate-limiting us — skip the scrobble, keep polling

    def _work():
        try:
            getattr(net, method)(**kwargs)
            logger.info("Last.fm %s: %s — %s", method,
                        kwargs.get("artist"), kwargs.get("title"))
        except Exception as e:
            logger.error("Last.fm %s failed: %s", method, e)

    threading.Thread(target=_work, daemon=True, name="lastfm").start()


def _scrobbling_enabled() -> bool:
    from routers.profile import _read_scrobbling
    try:
        return _read_scrobbling()
    except Exception as e:
        logger.warning("scrobbling toggle read failed: %s", e)
        return True


def _play_identity(item) -> Optional[dict]:
    """Source-agnostic identity for the playing QueueItem: its track UUID
    (owned AND phantom carry it) + optional media_file_id. None when the item
    is not a known Sautium track (a foreign / out-of-library URI in the queue)."""
    if item is None or not item.track_id:
        return None
    return {
        "track_id": item.track_id,
        "media_file_id": item.media_file_id,
        "artist": item.artist,
        "title": item.title,
        "album": item.album or None,
        "duration": item.duration_seconds,
    }


def _save_play_session(s: "_PlaySession") -> None:
    """Persist a finished session to listening_history, re-derive the track's
    local_play_stats row from its history (play_stats.PLAY_STATS_SQL — the
    stats are a function of the history, never counters of their own) and
    scrobble. Source-agnostic: keys on the track UUID, so phantom plays persist
    exactly like owned (media_file_id is NULL for phantoms)."""
    try:
        completed = s.completed
        skipped = not s.scrobble_ready
        # Both ends from the one clock the start came from: an end stamped by
        # PostgreSQL's now() landed up to ~2 s before its own start on the master.
        _db_execute(
            "INSERT INTO listening_history "
            "(media_file_id, track_id, started_at, ended_at, "
            " duration_listened, percent_listened, completed, skipped) "
            "VALUES (%(mf)s, %(tid)s::uuid, %(start)s, %(end)s, "
            "        %(dur)s, %(pct)s, %(comp)s, %(skip)s)",
            {"mf": s.media_file_id, "tid": s.track_id, "start": s.started_at,
             "end": datetime.now(timezone.utc),
             "dur": s.max_position, "pct": s.percent_listened,
             "comp": completed, "skip": skipped},
        )
        _db_execute(PLAY_STATS_SQL, {"ids": [s.track_id]})
        if completed:
            if not s.scrobbled and _scrobbling_enabled():
                _scrobble_async(
                    "scrobble", artist=s.artist, title=s.title,
                    timestamp=int(s.started_at.timestamp()), album=s.album,
                    duration=int(s.duration) if s.duration else None)
                s.scrobbled = True
            logger.info("play: %s — %s (%.0f%%) track=%s",
                        s.artist, s.title, s.percent_listened, s.track_id)
        else:
            logger.info("skip: %s — %s (%.0f%%) track=%s",
                        s.artist, s.title, s.percent_listened, s.track_id)
    except Exception as e:
        logger.error("save play session failed: %s", e)


def track_play_event(state_name: str, position: float, length: float,
                     item) -> None:
    """Advance listening-history / scrobble state from one status tick. Mirrors
    the retired daemon's _handle_event, but resolves identity from the
    canonical queue item (source-agnostic) — so phantom previews are tracked
    too. Runs on the active backend's status thread, outside its locks;
    DB writes go through the autocommit pool, Last.fm calls are fired async.

    Sessions are keyed on the item's track UUID, NOT its queue slot: queue
    mutations shift every slot (a removal ahead of the playing track moves it
    down one) while the LISTEN doesn't change — slot-keyed sessions were
    closed+reopened on every shift, spraying a phantom play + scrobble per
    removal. A tick whose slot resolves to no item (mid-mutation, the
    backend's index momentarily stale) carries no identity: it only advances
    the current session and must never close it. The same track re-starting
    from the top (repeat-one, a duplicate slot, a deliberate replay) IS a new
    listen — detected by the position falling back to the start."""
    global _play_session

    ident = _play_identity(item)
    same_track = (_play_session is not None and ident is not None
                  and ident["track_id"] == _play_session.track_id)
    if state_name != "playing":
        if _play_session is not None and (
                state_name == "paused"
                or (state_name == "loading" and same_track)):
            # Interruptions, not the end of the listen: a pause, or a seek
            # that reloads the same track (Opus streams re-encode from the
            # offset). Closing the session here wrote a "skipped" history
            # row and a skip_count per pause, and the resume re-opened one
            # that re-counted the prefix and re-sent Last.fm now-playing
            # with the resume as its start time.
            return
        if _play_session is not None:
            _save_play_session(_play_session)
            _play_session = None
        return

    if item is not None and item.excerpt:
        # A 30 s excerpt is not a listen: no history row (25 s of it would
        # read as `completed`), no stats, no scrobble, no now-playing. It does
        # end whatever listen was open — the track before it is over.
        if _play_session is not None:
            _save_play_session(_play_session)
            _play_session = None
        return

    if ident is None:
        if _play_session is not None:
            _play_session.update_position(position)
        return

    restarted = (same_track and position < 5.0
                 and _play_session.max_position > 30.0)
    if not same_track or restarted:
        if _play_session is not None:
            _save_play_session(_play_session)
        # Aware UTC: a naive local clock written through the UTC session put
        # every launcher listen hours off (play_stats.repaired_start).
        _play_session = _PlaySession(ident, datetime.now(timezone.utc), length)
        _scrobble_async(
            "update_now_playing", artist=ident["artist"] or "",
            title=ident["title"] or "", album=ident.get("album"),
            duration=int(ident["duration"]) if ident.get("duration") else None)

    _play_session.update_position(position)
    if (not _play_session.scrobbled and _play_session.scrobble_ready
            and _scrobbling_enabled()):
        _scrobble_async(
            "scrobble", artist=_play_session.artist, title=_play_session.title,
            timestamp=int(_play_session.started_at.timestamp()),
            album=_play_session.album,
            duration=int(_play_session.duration) if _play_session.duration else None)
        _play_session.scrobbled = True
