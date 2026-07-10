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
from datetime import datetime
from typing import Optional

from api_cooldown import cooling_down
from db_pool import db_execute as _db_execute

logger = logging.getLogger(__name__)

_SCROBBLE_MIN_SECONDS = 240  # Last.fm: scrobble after >50% OR >4 min, whichever first

_play_session: "Optional[_PlaySession]" = None
_play_track_index: Optional[int] = None
_scrobbler = None
_scrobbler_init = False


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


def current_track_index() -> Optional[int]:
    """Playlist index of the in-flight play session (None between tracks).
    The poller compares it against the live status to force a playlist
    refresh the moment the track changes."""
    return _play_track_index


def _get_scrobbler():
    """Lazy Last.fm network from env credentials (LASTFM_API_KEY/_API_SECRET/
    _SESSION_KEY/_USERNAME). None when scrobbling isn't configured."""
    global _scrobbler, _scrobbler_init
    if _scrobbler_init:
        return _scrobbler
    _scrobbler_init = True
    import os
    key = os.environ.get("LASTFM_API_KEY")
    secret = os.environ.get("LASTFM_API_SECRET")
    session_key = os.environ.get("LASTFM_SESSION_KEY")
    username = os.environ.get("LASTFM_USERNAME") or ""
    if key and secret and session_key:
        try:
            import pylast
            _scrobbler = pylast.LastFMNetwork(
                api_key=key, api_secret=secret,
                session_key=session_key, username=username)
            logger.info("Last.fm scrobbler initialized (user=%s)", username or "?")
        except Exception as e:
            logger.error("Last.fm scrobbler init failed: %s", e)
    else:
        logger.info("Last.fm scrobbling disabled (missing credentials)")
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


def _play_identity(pl_row: Optional[dict]) -> Optional[dict]:
    """Source-agnostic identity for the playing playlist row: its track UUID
    (owned AND phantom carry it) + optional media_file_id. None when the row is
    not a known Sautium track (a foreign / out-of-library URI in the queue)."""
    if not pl_row:
        return None
    tid = pl_row.get("track_id")
    if not tid:
        return None
    return {
        "track_id": tid,
        "media_file_id": pl_row.get("id"),
        "artist": pl_row.get("artist"),
        "title": pl_row.get("title"),
        "album": pl_row.get("album"),
        "duration": pl_row.get("duration_seconds"),
    }


def _save_play_session(s: "_PlaySession") -> None:
    """Persist a finished session to listening_history + local_play_stats and
    scrobble. Source-agnostic: keys on the track UUID, so phantom plays persist
    exactly like owned (media_file_id is NULL for phantoms)."""
    try:
        completed = s.completed
        skipped = not s.scrobble_ready
        _db_execute(
            "INSERT INTO listening_history "
            "(media_file_id, track_id, started_at, ended_at, "
            " duration_listened, percent_listened, completed, skipped) "
            "VALUES (%(mf)s, %(tid)s::uuid, %(start)s, now(), "
            "        %(dur)s, %(pct)s, %(comp)s, %(skip)s)",
            {"mf": s.media_file_id, "tid": s.track_id, "start": s.started_at,
             "dur": s.max_position, "pct": s.percent_listened,
             "comp": completed, "skip": skipped},
        )
        if completed:
            _db_execute(
                "INSERT INTO local_play_stats "
                "(track_id, play_count, skip_count, total_listen_time, "
                " avg_percent_listened, last_played_at) "
                "VALUES (%(tid)s::uuid, 1, 0, %(dur)s, %(pct)s, now()) "
                "ON CONFLICT (track_id) DO UPDATE SET "
                "  play_count = local_play_stats.play_count + 1, "
                "  total_listen_time = local_play_stats.total_listen_time "
                "                      + EXCLUDED.total_listen_time, "
                "  avg_percent_listened = (local_play_stats.avg_percent_listened "
                "      * local_play_stats.play_count + EXCLUDED.avg_percent_listened) "
                "      / (local_play_stats.play_count + 1), "
                "  last_played_at = EXCLUDED.last_played_at, "
                "  updated_at = now()",
                {"tid": s.track_id, "dur": s.max_position, "pct": s.percent_listened},
            )
            if not s.scrobbled and _scrobbling_enabled():
                _scrobble_async(
                    "scrobble", artist=s.artist, title=s.title,
                    timestamp=int(s.started_at.timestamp()), album=s.album,
                    duration=int(s.duration) if s.duration else None)
                s.scrobbled = True
            logger.info("play: %s — %s (%.0f%%) track=%s",
                        s.artist, s.title, s.percent_listened, s.track_id)
        else:
            _db_execute(
                "INSERT INTO local_play_stats "
                "(track_id, play_count, skip_count, total_listen_time, "
                " avg_percent_listened, last_played_at) "
                "VALUES (%(tid)s::uuid, 0, 1, %(dur)s, %(pct)s, now()) "
                "ON CONFLICT (track_id) DO UPDATE SET "
                "  skip_count = local_play_stats.skip_count + 1, "
                "  updated_at = now()",
                {"tid": s.track_id, "dur": s.max_position, "pct": s.percent_listened},
            )
            logger.info("skip: %s — %s (%.0f%%) track=%s",
                        s.artist, s.title, s.percent_listened, s.track_id)
    except Exception as e:
        logger.error("save play session failed: %s", e)


def track_play_event(state_name: str, position: float, length: float,
                     track_index, pl_row: Optional[dict]) -> None:
    """Advance listening-history / scrobble state from one status tick. Mirrors
    the retired daemon's _handle_event, but resolves identity from the
    already-built playlist payload (source-agnostic) — so phantom previews are
    tracked too. Runs in the poller thread, OUTSIDE the HQPlayer status lock;
    DB writes go through the autocommit pool, Last.fm calls are fired async."""
    global _play_session, _play_track_index

    if state_name != "playing":
        if _play_session is not None:
            _save_play_session(_play_session)
            _play_session = None
            _play_track_index = None
        return

    if track_index != _play_track_index:
        if _play_session is not None:
            _save_play_session(_play_session)
        ident = _play_identity(pl_row)
        if ident is None:
            _play_session = None
            _play_track_index = track_index
            return
        _play_session = _PlaySession(ident, datetime.now(), length)
        _play_track_index = track_index
        _scrobble_async(
            "update_now_playing", artist=ident["artist"] or "",
            title=ident["title"] or "", album=ident.get("album"),
            duration=int(ident["duration"]) if ident.get("duration") else None)

    if _play_session is not None:
        _play_session.update_position(position)
        if (not _play_session.scrobbled and _play_session.scrobble_ready
                and _scrobbling_enabled()):
            _scrobble_async(
                "scrobble", artist=_play_session.artist, title=_play_session.title,
                timestamp=int(_play_session.started_at.timestamp()),
                album=_play_session.album,
                duration=int(_play_session.duration) if _play_session.duration else None)
            _play_session.scrobbled = True
