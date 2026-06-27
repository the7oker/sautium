"""
REST API for HQPlayer control.

Mirrors MCP server patterns (lazy singleton, path conversion, tracker registration)
but exposed as HTTP endpoints for the Web UI.
"""

import asyncio
import json
import logging
import threading
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import settings
from db_pool import (
    db_query as _db_query,
    db_query_one as _db_query_one,
    db_execute as _db_execute,
    get_conn as _get_conn,
)
from hqplayer_client import HQPlayerClient, PlaybackState, format_time, file_path_to_uri
from lrclib import LrclibService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/player", tags=["player"])

# -- SSE infrastructure -------------------------------------------------------

_latest_status: dict = {"state": "disconnected"}
_status_version: int = 0
_status_changed = threading.Event()
_sse_clients: list = []           # list of (asyncio.Event, asyncio.AbstractEventLoop)
_sse_clients_lock = threading.Lock()
_poller_thread: Optional[threading.Thread] = None
_poller_running = False

# Playlist cache. Refreshed by the status poller every PLAYLIST_REFRESH_EVERY
# ticks (and immediately when a write command marks it stale via
# _invalidate_playlist). Served instantly by /api/player/playlist so the UI
# never waits on HQPlayer for the queue panel.
#
# `_playlist_version` is monotonically bumped only when the cached payload
# actually changes (deep-equality on the serialized track list). The value
# is embedded in every status payload so SSE clients can detect playlist
# mutations and refetch deterministically — no setTimeout/polling on the
# client side. Covers both our own write endpoints and external mutations
# (HQPDesktop UI, MCP, other clients) that the poller picks up on its
# periodic full refresh.
_latest_playlist: dict = {"tracks": [], "count": 0}
_playlist_version: int = 0

# Radio Mode flag. Surfaced in /status so the UI's Now Playing
# toggle reflects state across reloads / tabs. Set true by
# /api/player/radio/start, false by /radio/stop. Pure in-memory —
# the next backend restart starts at off.
_radio_mode: bool = False
_playlist_dirty: bool = True   # force refresh on first poll
PLAYLIST_REFRESH_EVERY = 5      # otherwise refresh every N status polls

# A single status read can stall past the socket timeout — the WSL2→Windows
# hop to HQPlayer routinely does. Blanking the cached status on one miss
# tears down the whole Now Playing UI (track, progress, "Similar"), so we
# keep serving the last-known-good status until this many polls fail in a
# row, then surface "disconnected".
_consecutive_status_failures: int = 0
STATUS_FAILURE_THRESHOLD = 3


def _wake_sse_clients():
    """Thread-safe: signal all SSE async generators to send new data."""
    with _sse_clients_lock:
        for evt, loop in _sse_clients:
            loop.call_soon_threadsafe(evt.set)


def _register_status_failure():
    """Record a failed status poll (read timeout or socket error). Tolerate a
    short burst — a brief HQPlayer stall must not blank Now Playing — and only
    flip the cache to 'disconnected' once failures cross the threshold. The
    last-known-good status keeps being served (no version bump) until then."""
    global _latest_status, _status_version, _consecutive_status_failures
    _consecutive_status_failures += 1
    if (_consecutive_status_failures >= STATUS_FAILURE_THRESHOLD
            and _latest_status.get("state") != "disconnected"):
        _latest_status = {"state": "disconnected"}
        _status_version += 1
        _wake_sse_clients()


def _status_poller():
    """Background thread: poll HQPlayer every ~1s, update cache, wake SSE clients.
    Also refreshes the playlist cache every PLAYLIST_REFRESH_EVERY ticks (or
    immediately when _playlist_dirty was set by a write endpoint)."""
    global _latest_status, _status_version, _consecutive_status_failures
    tick = 0
    while _poller_running:
        try:
            with _hqp_status_lock:
                try:
                    hqp = _get_hqp_status()
                    status = hqp.get_status()
                except (BrokenPipeError, ConnectionError, OSError):
                    _reset_hqp_status()
                    hqp = _get_hqp_status()
                    status = hqp.get_status()

                # Playlist cache refresh — share the status socket
                if _playlist_dirty or (tick % PLAYLIST_REFRESH_EVERY == 0):
                    _refresh_playlist_cache()

            if status is None:
                # Transient read miss (e.g. HQPlayer stalled past the socket
                # timeout). Keep serving the last good status; don't blank UI.
                _register_status_failure()
            else:
                _consecutive_status_failures = 0
                state_names = {
                    PlaybackState.STOPPED: "stopped",
                    PlaybackState.PAUSED: "paused",
                    PlaybackState.PLAYING: "playing",
                    PlaybackState.STOPREQ: "stopping",
                }
                # Resolve media_file_id / cover_id from the playlist cache
                # so the SSE payload is self-sufficient: subscribers no
                # longer need to cross-reference track_index against
                # `currentPlaylist` to fetch detail / similar / cover.
                idx = status.track_index
                pl_tracks = _latest_playlist.get("tracks") or []
                pl_row = (
                    pl_tracks[idx - 1]
                    if isinstance(idx, int) and 1 <= idx <= len(pl_tracks)
                    else None
                )
                # Phantom previews carry 'HTTP stream' in HQPlayer's own metadata
                # — surface the provider's real artist/title from the playlist cache.
                preview = bool(pl_row and pl_row.get("preview"))
                new_data = {
                    "state": state_names.get(status.state, "unknown"),
                    "artist": pl_row["artist"] if preview else status.artist,
                    "album": (pl_row.get("album") or "") if preview else status.album,
                    "song": pl_row["title"] if preview else status.song,
                    "genre": status.genre,
                    "position": status.position,
                    "length": status.length,
                    "volume": status.volume,
                    "process_speed": status.process_speed,
                    "track_index": status.track_index,
                    "media_file_id": pl_row["id"] if pl_row else None,
                    "cover_id": pl_row["cover_id"] if pl_row else None,
                    "progress_percent": round(status.progress_percent, 1),
                    "position_formatted": format_time(status.position),
                    "length_formatted": format_time(status.length),
                    "playlist_version": _playlist_version,
                    "radio_mode": _radio_mode,
                    "preview": preview,
                    "provider": pl_row.get("provider") if preview else None,
                }

                # Natural end-of-queue: HQPlayer stopped on the last track.
                # Archive the active session so a fully-listened album/queue
                # lands in history without a follow-up play. Only when the
                # previous tick was PLAYING the last track (track_index ==
                # playlist length) — a manual stop mid-queue is left alone so
                # resume doesn't fragment the session.
                if (new_data["state"] == "stopped"
                        and _latest_status.get("state") == "playing"
                        and len(pl_tracks) > 0
                        and _latest_status.get("track_index") == len(pl_tracks)):
                    try:
                        _close_active_session()
                    except Exception as e:
                        logger.warning(f"end-of-queue archive failed: {e}")

                if new_data != _latest_status:
                    _latest_status = new_data
                    _status_version += 1
                    _wake_sse_clients()

        except Exception:
            _register_status_failure()

        # Back off when HQPlayer stops answering. It accepts the TCP connect
        # but doesn't reply to <Status/> (control thread busy under DSP load),
        # so each retry is a connect->read-timeout->disconnect churn cycle that
        # only piles load onto an already-struggling control port. Exponential
        # backoff (2,4,8..30s) lets it recover; a successful poll resets to 1s.
        if _consecutive_status_failures > 0:
            poll_interval = min(2.0 ** _consecutive_status_failures, 30.0)
        else:
            poll_interval = 1.0
        _status_changed.wait(timeout=poll_interval)
        _status_changed.clear()
        tick += 1


def _notify_update():
    """Wake poller for an immediate re-poll after a command."""
    _status_changed.set()


def start_status_poller():
    """Start the background status polling thread."""
    global _poller_thread, _poller_running
    if _poller_thread and _poller_thread.is_alive():
        return
    _poller_running = True
    _poller_thread = threading.Thread(target=_status_poller, daemon=True, name="sse-poller")
    _poller_thread.start()
    logger.info("SSE status poller started")


def stop_status_poller():
    """Stop the background status polling thread."""
    global _poller_running
    _poller_running = False
    _status_changed.set()  # unblock wait
    if _poller_thread:
        _poller_thread.join(timeout=3)
    logger.info("SSE status poller stopped")


# -- Lazy singletons ----------------------------------------------------------
#
# Two independent HQPlayer connections so the background status poller and
# user-initiated commands never contend for the same socket / lock:
#
#   * `_hqp_status_client` + `_hqp_status_lock`
#       Owned exclusively by `_status_poller`. Fast 2 s socket timeout so a
#       lagging HQPlayer is detected quickly without freezing the rest of
#       the request flow.
#
#   * `_hqp_client` + `_hqp_lock`
#       Owned by all command endpoints (play / pause / next / play-track /
#       /api/player/playlist etc). 5 s timeout for write operations that
#       may take longer to acknowledge (e.g. play-album loads many URIs).
#
# HQPlayer's control API accepts multiple concurrent TCP clients, so the
# two sockets co-exist cleanly on the HQP side.

_hqp_client: Optional[HQPlayerClient] = None
_hqp_lock = threading.Lock()  # cmd commands

# Bumped every time a NEW playback queue is started (any clear_first add). A
# background phantom-album filler captures it and stops appending the instant the
# user moves to another queue, so a slow album fill never bleeds tracks into an
# unrelated session. Only ever mutated under _hqp_lock.
_playback_generation = 0

_hqp_status_client: Optional[HQPlayerClient] = None
_hqp_status_lock = threading.Lock()  # status poller


def _make_client(timeout: float) -> HQPlayerClient:
    return HQPlayerClient(
        host=settings.hqplayer_host,
        port=settings.hqplayer_port,
        timeout=timeout,
    )


# Circuit breaker for a stalled HQPlayer control port. A control-port stall
# (HQPlayer accepts the TCP connect but never replies, or stops accepting
# connects) makes every reconnect block for the full socket timeout. Without
# this, one play action fans out into rotate + stop + add × retries = tens of
# seconds of stacked connect timeouts while holding _hqp_lock, which also
# blocks every other request queued behind that lock. After a failed connect
# we "open" the breaker for a short cooldown: further reconnects fail fast
# instead of stacking timeouts. The next successful connect (e.g. the status
# poller once HQPlayer answers again) closes it.
_hqp_unreachable_until: float = 0.0
HQP_CIRCUIT_COOLDOWN = 6.0


def _ensure_connected(client: Optional[HQPlayerClient], timeout: float, label: str
                      ) -> HQPlayerClient:
    """Return a healthy HQPlayer client; reconnect if the cached one is stale.

    Caller must hold the appropriate lock. `client` is the previous instance
    (may be None). Returns the (possibly new) instance. Raises ConnectionError
    if the connection cannot be established.
    """
    need_reconnect = client is None or not client.is_connected()

    # Detect remote-side close by peeking the socket.
    if not need_reconnect and client and client.socket:
        import select
        try:
            ready = select.select([client.socket], [], [], 0)
            if ready[0]:
                peek = client.socket.recv(1, 0x02)  # MSG_PEEK
                if not peek:
                    logger.info(f"HQPlayer ({label}) closed by remote, reconnecting...")
                    need_reconnect = True
        except Exception:
            need_reconnect = True

    if need_reconnect:
        global _hqp_unreachable_until
        # Breaker open — fail fast rather than eat another connect timeout.
        if time.monotonic() < _hqp_unreachable_until:
            raise ConnectionError("HQPlayer unreachable (circuit open)")
        if client:
            try:
                client.disconnect()
            except Exception:
                pass
        client = _make_client(timeout=timeout)
        if not client.connect():
            _hqp_unreachable_until = time.monotonic() + HQP_CIRCUIT_COOLDOWN
            raise ConnectionError(
                f"Cannot connect to HQPlayer at {settings.hqplayer_host}:{settings.hqplayer_port}"
            )
        _hqp_unreachable_until = 0.0  # connected — close the breaker
        logger.info(f"HQPlayer ({label}) connected")
    return client


def _get_hqp() -> HQPlayerClient:
    """Get or create HQPlayer command client. Must be called inside _hqp_lock."""
    global _hqp_client
    _hqp_client = _ensure_connected(_hqp_client, timeout=5.0, label="cmd")
    return _hqp_client


def _get_hqp_status() -> HQPlayerClient:
    """Get or create HQPlayer status-poller client. Must be called inside _hqp_status_lock."""
    global _hqp_status_client
    # 4s, not 2s: HQPlayer's control thread can lag a few seconds behind a
    # <Status/> while busy rendering. A tight timeout turns a slow-but-alive
    # reply into a needless disconnect + reconnect churn cycle.
    _hqp_status_client = _ensure_connected(_hqp_status_client, timeout=4.0, label="status")
    return _hqp_status_client


def _reset_hqp():
    """Force-close HQPlayer command client so next _get_hqp() reconnects."""
    global _hqp_client
    if _hqp_client:
        try:
            _hqp_client.disconnect()
        except Exception:
            pass
        _hqp_client = None


def _reset_hqp_status():
    """Force-close HQPlayer status client so next _get_hqp_status() reconnects."""
    global _hqp_status_client
    if _hqp_status_client:
        try:
            _hqp_status_client.disconnect()
        except Exception:
            pass
        _hqp_status_client = None


def reset_all_clients() -> None:
    """Public entry point — called by /api/settings/hqplayer after the
    host or port changes so the next status/command call reconnects
    against the new address instead of holding onto the old socket."""
    with _hqp_lock:
        _reset_hqp()
    with _hqp_status_lock:
        _reset_hqp_status()


def _hqp_cmd(func):
    """Execute a function with HQPlayer client under lock. Auto-reconnects on broken pipe."""
    with _hqp_lock:
        try:
            hqp = _get_hqp()
            return func(hqp)
        except (BrokenPipeError, ConnectionError, OSError) as e:
            logger.warning(f"HQPlayer connection lost ({e}), reconnecting...")
            _reset_hqp()
            hqp = _get_hqp()
            return func(hqp)


def _add_uris_with_retry(uris: list[str], *, clear_first: bool = False) -> int:
    """Append URIs to the HQPlayer playlist, surviving a mid-batch
    connection drop. MUST be called while holding `_hqp_lock`.

    `playlist_add` returns False (it does not raise) when the control
    socket is down, so a plain `for` loop silently drops tracks while the
    endpoint still reports success — exactly the failure that made a Queue
    action add only the one track that landed before HQPlayer stalled.
    Here every add is verified: on a falsey result or a dropped socket we
    reset the connection and retry that one URI once. Returns the count
    actually added so the caller can surface a short count instead of a
    fake 'ok'.

    `clear_first=True` issues clear=True on the first URI (replace-queue
    semantics); the rest append. The clear only ever fires on i == 0, so a
    mid-batch reconnect never re-clears already-added tracks.
    """
    if clear_first:
        # New queue = new playback generation (retires any phantom filler).
        global _playback_generation
        _playback_generation += 1
    added = 0
    for i, uri in enumerate(uris):
        clear = clear_first and i == 0
        ok = False
        for attempt in (1, 2):
            try:
                ok = _get_hqp().playlist_add(uri, clear=clear)
            except (BrokenPipeError, ConnectionError, OSError):
                ok = False
            if ok:
                break
            if attempt == 1:
                _reset_hqp()  # force a fresh socket before the single retry
        if ok:
            added += 1
        else:
            logger.warning(f"playlist_add failed after reconnect: {uri}")
    return added


def _hqp_safe(action) -> None:
    """Run one HQPlayer command (stop / play / clear / select_track)
    tolerantly inside an existing `_hqp_lock`: one reconnect-and-retry,
    never raises. Frames a resilient multi-add so a churning control port
    can't abort the whole operation at its stop()/play() bookends before
    the add even runs."""
    for attempt in (1, 2):
        try:
            action(_get_hqp())
            return
        except (BrokenPipeError, ConnectionError, OSError):
            if attempt == 1:
                _reset_hqp()




# -- Listening sessions (queue-lifetime snapshots) ----------------------------
#
# Each destructive play endpoint archives the live queue as an immutable
# snapshot and opens a new active session. HQPlayer stays the single source
# of truth for the live queue — we never duplicate it; we only persist
# archived snapshots, captured at the instant the queue is replaced. `origin`
# (how the queue started) is the one fact HQPlayer doesn't know, so it rides
# on the active-session marker.

_SESSION_ORIGINS = ("album", "track", "radio", "mix")
# Idempotent-replay window: a destructive play of the same origin within this
# many seconds of the active session opening is treated as a duplicate (double-
# tapped "Play all", a retried request) and skipped. Beyond it, replaying the
# same album/track is a genuinely new listen and opens a fresh session — so
# re-playing a track minutes later isn't swallowed into the stale one.
_SESSION_DEDUP_WINDOW_SEC = 30


def _rotate_session(
    origin: str,
    *,
    seed_media_file_id: Optional[int] = None,
    origin_album_id: Optional[str] = None,
) -> None:
    """Archive the live queue as a session snapshot, then open a new active
    session. Called at the TOP of every destructive play endpoint, BEFORE
    stop()/clear(), so the OLD queue is captured intact.

    Reads the old queue fresh from HQPlayer under the STATUS lock — a
    different connection + lock from the cmd path the caller is about to use,
    so it never nests with the caller's _hqp_lock block (same split `reorder`
    already relies on). A read miss degrades to an empty snapshot; never
    retried/slept on (project rule)."""
    old_media_ids: list[int] = []
    try:
        with _hqp_status_lock:
            hqp = _get_hqp_status()
            hqp_tracks = hqp.get_playlist()
        payload = _build_playlist_payload(hqp_tracks)  # pure transform, no socket I/O
        old_media_ids = [t["id"] for t in payload["tracks"] if t.get("id")]
    except Exception as e:
        # Read miss (HQPlayer stalled): skip rotation entirely. An empty
        # old_media_ids here would otherwise be mistaken for a genuinely empty
        # queue and DELETE the active session — losing the album the user was
        # actually listening to. A stalled HQPlayer means the play will likely
        # fail anyway, so leave session state untouched; the next successful
        # play rotates normally.
        logger.warning(f"_rotate_session: could not read old queue, skipping: {e}")
        return

    archived_mix_id = _archive_and_open_session(
        old_media_ids, origin, seed_media_file_id, origin_album_id,
    )
    if archived_mix_id is not None:
        _schedule_mix_title(archived_mix_id)


def _close_active_session() -> None:
    """Archive the active session on a natural end-of-queue (HQPlayer stopped
    on the last track) WITHOUT opening a new one, so a fully-listened album
    lands in history without a follow-up play. Reads the queue under the
    status lock; a read miss skips (the session stays active and the next play
    rotates it normally)."""
    old_media_ids: list[int] = []
    try:
        with _hqp_status_lock:
            hqp = _get_hqp_status()
            hqp_tracks = hqp.get_playlist()
        payload = _build_playlist_payload(hqp_tracks)
        old_media_ids = [t["id"] for t in payload["tracks"] if t.get("id")]
    except Exception as e:
        logger.warning(f"_close_active_session: could not read queue: {e}")
        return
    archived_mix_id = _archive_and_open_session(
        old_media_ids, "mix", None, None, open_new=False,
    )
    if archived_mix_id is not None:
        _schedule_mix_title(archived_mix_id)


def _archive_and_open_session(
    old_media_ids: list[int],
    origin: str,
    seed_media_file_id: Optional[int],
    origin_album_id: Optional[str],
    *,
    open_new: bool = True,
) -> Optional[str]:
    """Archive the current active session against `old_media_ids` and open a
    new one — atomically. db_pool connections are autocommit, so toggle it
    off for this multi-statement transaction (mirrors
    routers.home._db_query_with_ef_search). With open_new=False (end-of-queue
    completion) the active session is archived but no new one is opened.
    Returns the archived session id IFF it was a mix needing a background
    title, else None."""
    import psycopg2.extras

    archived_mix_id: Optional[str] = None
    with _get_conn() as conn:
        conn.autocommit = False
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id::text AS id, origin, "
                    "origin_album_id::text AS origin_album_id, seed_media_file_id, "
                    "EXTRACT(EPOCH FROM (now() - started_at)) AS age_sec "
                    "FROM listening_sessions WHERE ended_at IS NULL FOR UPDATE"
                )
                active = cur.fetchone()

                # Idempotent re-play: a repeated destructive play of the same
                # thing within _SESSION_DEDUP_WINDOW_SEC (double-tapped "Play
                # all", a retried/duplicated request) must NOT archive the
                # just-opened session against the still-old HQPlayer queue and
                # spawn a duplicate. FOR UPDATE serialises concurrent calls so
                # the second sees the first's freshly-inserted active row. The
                # time window keeps this to genuine duplicates: re-playing the
                # same album/track minutes later is a new listen and opens a
                # fresh session instead of being swallowed into the stale one.
                if open_new and active is not None and (
                    active["age_sec"] is not None
                    and active["age_sec"] < _SESSION_DEDUP_WINDOW_SEC
                    and active["origin"] == origin
                    and active["origin_album_id"] == origin_album_id
                    and active["seed_media_file_id"] == seed_media_file_id
                ):
                    conn.commit()
                    return None

                if active is not None:
                    snapshot_ids = _snapshot_ids_for(cur, active, old_media_ids)
                    if snapshot_ids:
                        psycopg2.extras.execute_values(
                            cur,
                            "INSERT INTO session_tracks "
                            "(session_id, position, media_file_id) VALUES %s",
                            [(active["id"], i, mid)
                             for i, mid in enumerate(snapshot_ids)],
                        )
                        title, subtitle, cover_id = _compute_session_card(
                            cur, active, snapshot_ids,
                        )
                        cur.execute(
                            "UPDATE listening_sessions SET ended_at = now(), "
                            "track_count = %s, title = %s, subtitle = %s, "
                            "cover_id = %s::uuid WHERE id = %s::uuid",
                            (len(snapshot_ids), title, subtitle,
                             cover_id, active["id"]),
                        )
                        if active["origin"] == "mix":
                            archived_mix_id = active["id"]
                    else:
                        # Dangling empty active row — never show an empty card.
                        cur.execute(
                            "DELETE FROM listening_sessions WHERE id = %s::uuid",
                            (active["id"],),
                        )

                if open_new:
                    cur.execute(
                        "INSERT INTO listening_sessions "
                        "(origin, seed_media_file_id, origin_album_id) "
                        "VALUES (%s, %s, %s::uuid)",
                        (origin, seed_media_file_id, origin_album_id),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True
    return archived_mix_id


def _snapshot_ids_for(cur, active: dict, old_media_ids: list[int]) -> list[int]:
    """Resolve which media_file_ids to snapshot into the archived session.

    For album/track sessions the content is deterministic from the origin, so
    guard against the live queue being replaced out-of-band (e.g. HQPlayer
    reopened with a different album, leaving a foreign queue): if the captured
    queue no longer overlaps the origin's own tracks, snapshot the origin's
    tracks so the card's cover/tracks match its title. A queue that still
    overlaps is trusted — it reflects in-app edits (queued/removed tracks).
    radio/mix content is dynamic (similar picks / an explicit list) with no
    origin reference, so the captured queue is the only source."""
    origin = active["origin"]

    if origin == "album" and active["origin_album_id"]:
        cur.execute(
            "SELECT mf.id FROM media_files mf "
            "JOIN album_variants av ON av.id = mf.album_variant_id "
            "WHERE av.album_id = %s::uuid "
            "ORDER BY mf.disc_number, mf.track_number",
            (active["origin_album_id"],),
        )
        album_ids = [r["id"] for r in cur.fetchall()]
        if album_ids and not (set(old_media_ids) & set(album_ids)):
            return album_ids
        return old_media_ids

    if origin == "track" and active["seed_media_file_id"]:
        # A single-track session is exactly its seed; don't let an out-of-band
        # queue swap put a foreign track in it.
        if active["seed_media_file_id"] not in old_media_ids:
            return [active["seed_media_file_id"]]
        return old_media_ids

    return old_media_ids


def _compute_session_card(cur, active: dict, old_media_ids: list[int]):
    """Title / subtitle / cover_id for an archived session, derived from its
    origin (NOT the snapshot's first row — album/track/radio titles come from
    the stored origin columns; only mix uses the snapshot). Per-row label
    formatting — the Python-side case the project allows; the lookups are SQL.
    Uses the cursor already inside the archive transaction."""
    origin = active["origin"]
    first_mid = old_media_ids[0] if old_media_ids else None

    def _cover_of(mid):
        if mid is None:
            return None
        cur.execute(
            "SELECT cover_id::text AS cover_id FROM media_files WHERE id = %s",
            (mid,),
        )
        r = cur.fetchone()
        return r["cover_id"] if r else None

    if origin == "album" and active["origin_album_id"]:
        cur.execute("""
            SELECT al.title,
                   (SELECT a.name
                    FROM artists a
                    JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
                    JOIN tracks t ON t.id = ta.track_id
                    JOIN media_files mf ON mf.track_id = t.id
                    JOIN album_variants av ON av.id = mf.album_variant_id
                    WHERE av.album_id = al.id
                    GROUP BY a.id, a.name
                    ORDER BY COUNT(*) DESC
                    LIMIT 1) AS artist
            FROM albums al WHERE al.id = %s::uuid
        """, (active["origin_album_id"],))
        r = cur.fetchone()
        return (
            r["title"] if r else "Album",
            r["artist"] if r else None,
            _cover_of(first_mid),
        )

    if origin in ("track", "radio") and active["seed_media_file_id"]:
        cur.execute("""
            SELECT t.title, a.name AS artist, mf.cover_id::text AS cover_id
            FROM media_files mf
            JOIN tracks t ON t.id = mf.track_id
            JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
            JOIN artists a ON a.id = ta.artist_id
            WHERE mf.id = %s
        """, (active["seed_media_file_id"],))
        r = cur.fetchone()
        title = r["title"] if r else "Track"
        subtitle = "Radio" if origin == "radio" else (r["artist"] if r else None)
        cover = (r["cover_id"] if r else None) or _cover_of(first_mid)
        return (title, subtitle, cover)

    # mix — or album/track/radio with a NULL seed (degrade gracefully).
    n = len(old_media_ids)
    return ("Mix", f"{n} track{'s' if n != 1 else ''}", _cover_of(first_mid))


def _schedule_mix_title(session_id: str) -> None:
    """Generate an AI title for an archived mix in a background daemon thread
    (the codebase's background-work idiom — chat-stream-worker, status poller).
    Graceful no-op when no AI provider is configured: the card stays 'Mix'."""
    def _worker():
        try:
            from routers.chat import (
                _resolve_provider, _title_via_provider,
                _title_via_claude_code, _clean_title,
            )
            try:
                provider = _resolve_provider(None)
            except Exception:
                return  # no provider → leave 'Mix'

            rows = _db_query("""
                SELECT a.name AS artist, t.title AS title
                FROM session_tracks st
                JOIN media_files mf ON mf.id = st.media_file_id
                JOIN tracks t ON t.id = mf.track_id
                JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
                JOIN artists a ON a.id = ta.artist_id
                WHERE st.session_id = %(sid)s::uuid
                ORDER BY st.position
                LIMIT 30
            """, {"sid": str(session_id)})
            if not rows:
                return

            lines = "\n".join(f"{r['artist']} — {r['title']}" for r in rows)
            prompt = (
                "Below is a playlist of tracks. Produce a short, evocative "
                "playlist name — 2-4 words, max 6 — in the same language as the "
                "track titles. No quotes, no trailing punctuation, do not use "
                "the words 'playlist' or 'mix'. Output ONLY the name.\n\n" + lines
            )
            title = (
                _title_via_claude_code(prompt) if provider == "claude_code"
                else _title_via_provider(provider, prompt)
            )
            title = _clean_title(title) if title else None
            if title:
                # Only overwrite the placeholder — a later user edit wins.
                _db_execute(
                    "UPDATE listening_sessions SET title = %(t)s "
                    "WHERE id = %(id)s::uuid AND title = 'Mix'",
                    {"t": title, "id": str(session_id)},
                )
        except Exception as e:
            logger.warning(f"mix title generation failed for {session_id}: {e}")

    threading.Thread(target=_worker, daemon=True, name="mix-title-worker").start()


# -- Request models -----------------------------------------------------------

class VolumeRequest(BaseModel):
    level: float

class SearchRequest(BaseModel):
    query: str
    limit: int = 30

class PlayTrackRequest(BaseModel):
    track_id: int

class PlayAlbumRequest(BaseModel):
    album_name: str
    artist_name: str = ""

class PlaySimilarRequest(BaseModel):
    track_id: int
    limit: int = 15

class PlayTracksRequest(BaseModel):
    track_ids: list[int]
    # Session origin hint: album "Play all" sends 'album' (+ origin_album_id)
    # so a played-whole-album isn't labelled "Mix". Defaults to 'mix'.
    origin: Optional[str] = None
    origin_album_id: Optional[str] = None

class QueueNextRequest(BaseModel):
    track_id: int
    limit: int = 5
    exclude_ids: list[int] = []

class QueueTracksRequest(BaseModel):
    track_ids: list[int]

class JumpRequest(BaseModel):
    index: int  # 1-based, matches HQPlayer's select_track convention

class RemoveRequest(BaseModel):
    index: int  # 1-based — HQPlayer's PlaylistRemove convention

class ReorderRequest(BaseModel):
    order: list[int]  # full new order of media_file_ids, current track included at its original position


# -- Search -------------------------------------------------------------------

@router.get("/search")
async def search_tracks(q: str = "", limit: int = 20):
    """Two-stage search grouped by albums: exact ILIKE first, fuzzy trigram fallback."""
    limit = min(limit, 100)
    q = q.strip()
    if not q:
        return {"albums": [], "count": 0}

    # Stage 1: Exact ILIKE — split into words, all must match in at least one field
    words = q.split()
    word_conditions = []
    params: dict = {"limit": limit * 10}  # Get more tracks for grouping
    for i, word in enumerate(words):
        key = f"w{i}"
        params[key] = f"%{word}%"
        word_conditions.append(
            f"(a.name ILIKE %({key})s OR al.title ILIKE %({key})s OR t.title ILIKE %({key})s)"
        )
    where_exact = " AND ".join(word_conditions)

    rows = _db_query(f"""
        SELECT mf.id, t.title, mf.track_number, mf.disc_number, mf.duration_seconds,
               a.name as artist, av.id as album_id, al.title as album,
               (SELECT g.name FROM album_genres ag JOIN genres g ON g.id = ag.genre_id
                WHERE ag.album_id = av.album_id ORDER BY ag.count DESC NULLS LAST LIMIT 1) as genre,
               mf.is_lossless, t.id as track_id
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN album_variants av ON mf.album_variant_id = av.id
        JOIN albums al ON av.album_id = al.id
        WHERE {where_exact}
        ORDER BY a.name, al.title, mf.disc_number, mf.track_number
    """, params)

    if not rows:
        # Stage 2: Fuzzy trigram fallback
        params = {"query": q, "query_like": f"%{q}%", "limit": limit * 10}
        rows = _db_query("""
            SELECT mf.id, t.title, mf.track_number, mf.disc_number, mf.duration_seconds,
                   a.name as artist, av.id as album_id, al.title as album,
                   (SELECT g.name FROM album_genres ag JOIN genres g ON g.id = ag.genre_id
                    WHERE ag.album_id = av.album_id ORDER BY ag.count DESC NULLS LAST LIMIT 1) as genre,
                   mf.is_lossless, t.id as track_id,
                   GREATEST(
                       similarity(a.name, %(query)s),
                       similarity(al.title, %(query)s),
                       similarity(t.title, %(query)s)
                   ) as _score
            FROM media_files mf
            JOIN tracks t ON mf.track_id = t.id
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a ON ta.artist_id = a.id
            JOIN album_variants av ON mf.album_variant_id = av.id
            JOIN albums al ON av.album_id = al.id
            WHERE similarity(a.name, %(query)s) > 0.25
               OR similarity(al.title, %(query)s) > 0.25
               OR similarity(t.title, %(query)s) > 0.25
            ORDER BY _score DESC, a.name, al.title, mf.disc_number, mf.track_number
        """, params)

    # Group by album (merge multi-disc albums, but keep lossless/lossy separate)
    # Deduplicate tracks that appear in multiple album_variants of the same album
    albums_dict = {}
    seen_tracks = {}  # key -> set of track_ids already added
    for row in rows:
        key = (row["artist"], row["album"], row["is_lossless"])
        if key not in albums_dict:
            albums_dict[key] = {
                "artist": row["artist"],
                "album": row["album"],
                "album_id": row["album_id"],
                "genre": row["genre"],
                "is_lossless": row["is_lossless"],
                "tracks": [],
            }
            seen_tracks[key] = set()
        track_id = row.get("track_id")
        if track_id and track_id in seen_tracks[key]:
            continue
        if track_id:
            seen_tracks[key].add(track_id)
        albums_dict[key]["tracks"].append({
            "id": row["id"],
            "title": row["title"],
            "track_number": row["track_number"],
            "disc_number": row["disc_number"],
            "duration_seconds": row["duration_seconds"],
        })

    # Calculate totals and limit to requested album count
    albums = []
    for i, album_data in enumerate(list(albums_dict.values())[:limit]):
        album_data["album_id"] = i  # Unique index per group for DOM IDs (DB album_id may collide)
        album_data["track_count"] = len(album_data["tracks"])
        album_data["total_duration"] = sum(t["duration_seconds"] or 0 for t in album_data["tracks"])
        albums.append(album_data)

    return {"albums": albums, "count": len(albums)}


# -- Transport controls -------------------------------------------------------

def _preview_meta(uri: str) -> Optional[dict]:
    """Provider metadata for a phantom-preview URI (http proxy URL), or None.
    Lets the queue/Now-Playing show real artist/title instead of HQPlayer's
    generic 'HTTP stream' label. In-memory lookup, no I/O."""
    if not uri.startswith("http://"):
        return None
    try:
        from streaming import service as streaming_service
        return streaming_service.preview_meta(uri)
    except Exception:
        return None


def _build_playlist_payload(hqp_tracks: list) -> dict:
    """Convert HQPlayer raw playlist into the JSON shape served to the UI.
    Pure transform — no HQPlayer or socket I/O."""
    if not hqp_tracks:
        return {"tracks": [], "count": 0}

    # Convert URIs to DB paths in bulk
    path_to_idx: dict[str, list[int]] = {}
    idx_to_hqp: dict[int, dict] = {}
    for idx, hqp_track in enumerate(hqp_tracks):
        uri = hqp_track["uri"]
        idx_to_hqp[idx] = hqp_track

        if uri.startswith("file:///"):
            db_path = uri[8:]
        elif uri.startswith("file://"):
            db_path = uri[7:]
        else:
            continue

        db_path = db_path.replace("\\", "/")
        db_path = db_path.replace("%5B", "[").replace("%5D", "]")
        path_to_idx.setdefault(db_path, []).append(idx)

    all_paths = list(path_to_idx.keys())
    db_rows_by_path: dict[str, dict] = {}
    if all_paths:
        rows_batch = _db_query("""
            SELECT mf.id, mf.file_path, t.title, mf.track_number,
                   mf.duration_seconds, mf.cover_id::text AS cover_id,
                   a.name as artist
            FROM media_files mf
            JOIN tracks t ON mf.track_id = t.id
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artists a ON ta.artist_id = a.id
            WHERE mf.file_path = ANY(%(paths)s)
        """, {"paths": all_paths})
        for r in rows_batch:
            db_rows_by_path[r["file_path"]] = r

    tracks_with_info = []
    for idx in range(len(hqp_tracks)):
        hqp_track = idx_to_hqp.get(idx)
        if hqp_track is None:
            continue

        row = None
        for path, indices in path_to_idx.items():
            if idx in indices:
                row = db_rows_by_path.get(path)
                break

        if row:
            tracks_with_info.append({
                "id": row["id"],
                "title": row["title"],
                "track_number": row["track_number"],
                "artist": row["artist"],
                "duration_seconds": (
                    float(row["duration_seconds"])
                    if row["duration_seconds"] is not None else None
                ),
                "cover_id": row["cover_id"],
                "index": idx,
            })
        else:
            meta = _preview_meta(hqp_track["uri"])
            if meta:
                tracks_with_info.append({
                    "id": None,
                    "title": meta["title"] or "Unknown",
                    "track_number": None,
                    "artist": meta["artist"] or "Unknown",
                    "album": meta.get("album") or "",
                    "duration_seconds": None,
                    "cover_id": None,
                    "preview": True,
                    "provider": meta["provider"],
                    "index": idx,
                })
            else:
                tracks_with_info.append({
                    "id": None,
                    "title": hqp_track["song"] or "Unknown",
                    "track_number": None,
                    "artist": hqp_track["artist"] or "Unknown",
                    "duration_seconds": None,
                    "cover_id": None,
                    "index": idx,
                })

    return {"tracks": tracks_with_info, "count": len(tracks_with_info)}


def _refresh_playlist_cache():
    """Pull current playlist from HQPlayer (status socket) and update cache.
    Caller must hold _hqp_status_lock. Bumps `_playlist_version` only when
    the new payload differs from the cached one — clients use the version
    bump as a deterministic signal to refetch."""
    global _latest_playlist, _playlist_dirty, _playlist_version
    try:
        hqp = _get_hqp_status()
        hqp_tracks = hqp.get_playlist()
        new_payload = _build_playlist_payload(hqp_tracks)
        if new_payload != _latest_playlist:
            _latest_playlist = new_payload
            _playlist_version += 1
        _playlist_dirty = False
    except (BrokenPipeError, ConnectionError, OSError) as e:
        logger.debug(f"Playlist refresh failed ({e})")
        _reset_hqp_status()


def _invalidate_playlist():
    """Mark playlist cache stale; the poller will pull a fresh copy on its
    next tick. Called from write endpoints (play_track / play_album / etc)
    after the user mutates the queue."""
    global _playlist_dirty
    _playlist_dirty = True
    _status_changed.set()  # wake poller immediately


def _force_refresh_playlist_after_write():
    """Synchronously refresh the playlist cache after a write so the
    response carries the new state. The async invalidate-and-wake-poller
    pattern races with optimistic UI: the frontend issues fetchPlaylist
    immediately on response, hits the still-stale cache, and overwrites
    the optimistic update with old data — a visible revert. Doing the
    refresh here on the status socket (different lock + connection from
    the cmd path) blocks the response by ~50–150ms but guarantees that
    any cache reader after this point sees the post-mutation state."""
    try:
        with _hqp_status_lock:
            _refresh_playlist_cache()
    except Exception as e:
        logger.warning(f"force playlist refresh failed: {e}")
    _status_changed.set()  # propagate the bumped playlist_version via SSE


@router.get("/playlist")
def get_playlist():
    """Return last cached playlist. Refreshed by the status poller every
    PLAYLIST_REFRESH_EVERY ticks and immediately on write-command-driven
    invalidation. Always instant — never blocks on HQPlayer."""
    return _latest_playlist


@router.get("/status/stream")
async def status_stream():
    """SSE endpoint: pushes status updates in real-time."""
    loop = asyncio.get_event_loop()
    evt = asyncio.Event()

    async def event_generator():
        last_version = -1
        try:
            with _sse_clients_lock:
                _sse_clients.append((evt, loop))

            # Send current status immediately
            yield f"data: {json.dumps(_latest_status)}\n\n"
            last_version = _status_version

            while True:
                try:
                    await asyncio.wait_for(evt.wait(), timeout=15.0)
                    evt.clear()
                except asyncio.TimeoutError:
                    # Keepalive
                    yield ": keepalive\n\n"
                    continue

                if _status_version != last_version:
                    last_version = _status_version
                    yield f"data: {json.dumps(_latest_status)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            with _sse_clients_lock:
                _sse_clients[:] = [(e, l) for e, l in _sse_clients if e is not evt]

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/status")
def get_status():
    """Return last cached HQPlayer status. Cache is maintained by the
    background _status_poller (1 s tick) on its own dedicated socket so
    this endpoint never hits HQPlayer in the request path — it's always
    instant regardless of HQPlayer responsiveness."""
    return _latest_status


# Legacy synchronous status (rarely needed; UI uses cached /status above
# and SSE for live updates). Kept for tools that explicitly want a fresh
# read direct from HQPlayer.
@router.get("/status/fresh")
def get_status_fresh():
    """Force a fresh HQPlayer status read. Slower (may hang up to socket
    timeout if HQPlayer is unresponsive)."""
    try:
        with _hqp_lock:
            hqp = _get_hqp()
            status = hqp.get_status()
        if status is None:
            return {"state": "unknown"}

        state_names = {
            PlaybackState.STOPPED: "stopped",
            PlaybackState.PAUSED: "paused",
            PlaybackState.PLAYING: "playing",
            PlaybackState.STOPREQ: "stopping",
        }

        return {
            "state": state_names.get(status.state, "unknown"),
            "artist": status.artist,
            "album": status.album,
            "song": status.song,
            "genre": status.genre,
            "position": status.position,
            "length": status.length,
            "volume": status.volume,
            "process_speed": status.process_speed,
            "track_index": status.track_index,
            "progress_percent": round(status.progress_percent, 1),
            "position_formatted": format_time(status.position),
            "length_formatted": format_time(status.length),
        }
    except Exception:
        return {"state": "disconnected"}


@router.get("/now-playing-detail")
def now_playing_detail(media_file_id: int):
    """Aggregated rich payload for the Now Playing screen.

    Combines media-file metadata (format, sample rate, bit depth, cover),
    track-level audio features (BPM, key, mode, energy, instruments),
    album info, and the top genres into one roundtrip. Called by the
    frontend whenever the playing track changes.
    """
    row = _db_query_one("""
        SELECT mf.id AS media_file_id,
               mf.track_id::text AS track_id,
               mf.cover_id::text AS cover_id,
               mf.file_format,
               mf.is_lossless,
               mf.sample_rate,
               mf.bit_depth,
               mf.duration_seconds,
               t.title,
               af.bpm,
               af.key,
               af.mode,
               af.energy,
               af.energy_db,
               af.danceability,
               af.instruments,
               af.moods,
               av.album_id::text AS album_id,
               al.title AS album_title,
               al.release_year AS year
        FROM media_files mf
        JOIN tracks t ON t.id = mf.track_id
        LEFT JOIN audio_features af ON af.track_id = t.id
        JOIN album_variants av ON av.id = mf.album_variant_id
        JOIN albums al ON al.id = av.album_id
        WHERE mf.id = %(id)s
    """, {"id": media_file_id})

    if not row:
        raise HTTPException(status_code=404, detail="media_file not found")

    row["genres"] = _db_query("""
        SELECT g.id::text, g.name
        FROM album_genres ag
        JOIN genres g ON g.id = ag.genre_id
        WHERE ag.album_id = %(a)s::uuid
        GROUP BY g.id, g.name
        ORDER BY MAX(ag.count) DESC NULLS LAST, g.name
        LIMIT 3
    """, {"a": row["album_id"]})

    row["primary_artist"] = _db_query_one("""
        SELECT a.id::text, a.name
        FROM artists a
        JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
        WHERE ta.track_id = %(t)s::uuid
        LIMIT 1
    """, {"t": row["track_id"]})

    sr = row.get("sample_rate") or 0
    bd = row.get("bit_depth") or 0
    if row.get("is_lossless") and sr >= 48000 and bd >= 24:
        row["quality"] = "hi-res"
    elif row.get("is_lossless"):
        row["quality"] = "lossless"
    else:
        row["quality"] = "lossy"

    return row


@router.post("/play")
def play():
    try:
        result = {"ok": _hqp_cmd(lambda h: h.play())}
        _notify_update()
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/pause")
def pause():
    try:
        result = {"ok": _hqp_cmd(lambda h: h.pause())}
        _notify_update()
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/stop")
def stop():
    try:
        result = {"ok": _hqp_cmd(lambda h: h.stop())}
        _notify_update()
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/next")
def next_track():
    try:
        result = {"ok": _hqp_cmd(lambda h: h.next())}
        _notify_update()
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/previous")
def previous_track():
    try:
        result = {"ok": _hqp_cmd(lambda h: h.previous())}
        _notify_update()
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/volume/up")
def volume_up():
    try:
        result = {"ok": _hqp_cmd(lambda h: h.volume_up())}
        _notify_update()
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/volume/down")
def volume_down():
    try:
        result = {"ok": _hqp_cmd(lambda h: h.volume_down())}
        _notify_update()
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/volume")
def set_volume(req: VolumeRequest):
    try:
        result = {"ok": _hqp_cmd(lambda h: h.set_volume(req.level))}
        _notify_update()
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# -- Smart play ----------------------------------------------------------------

@router.post("/remove")
def remove(req: RemoveRequest):
    """Remove a track from the current HQPlayer playlist by index.

    `index` is 1-based to match HQPlayer's `PlaylistRemove`. After
    removal the playlist cache is invalidated so the next status
    poll refreshes it; the playback-tracker mapping naturally
    becomes stale (indices shift) but is rebuilt by the next
    play-track / play-album call. The play_count for the removed
    slot is unaffected — tracker records past plays, not pending
    queue contents.

    Reorder is intentionally not implemented. HQPlayer's Control
    API exposes add (append-only), clear and remove — there is no
    insert-at-index or move primitive. Implementing reorder would
    require a clear+rebuild round-trip that interrupts the
    currently playing track. If the protocol gains a move
    operation, expose it here.
    """
    if req.index < 1:
        raise HTTPException(status_code=400, detail="index must be >= 1")
    try:
        ok = _hqp_cmd(lambda h: h.playlist_remove(req.index))
        _invalidate_playlist()
        return {"ok": ok, "index": req.index}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/reorder")
def reorder(req: ReorderRequest):
    """Reorder the playlist around the currently playing track.

    HQPlayer's Control API only exposes append (PlaylistAdd) and
    remove (PlaylistRemove) — no insert, no move. The trick is to
    never touch the slot HQPlayer is reading from: as long as we
    only append to the end and remove non-current slots, audio
    plays through the rebuild without a glitch.

    Effects of each primitive on the current slot's index K:
      append(uri)         — playlist grows; K unchanged.
      remove(i), i  < K   — playlist shrinks before K; K -= 1.
      remove(i), i == K   — current slot deleted; AUDIO CUTS OFF.
      remove(i), i  > K   — playlist shrinks after K; K unchanged.

    So K can shift LEFT (via removes before it) or stay; it cannot
    shift RIGHT, and tracks cannot be inserted before it. That
    constrains what reorders can be done seamlessly:

      new_before must be a subsequence of old_before (we can only
      drop tracks from the front; we cannot insert or permute) AND
      new_after must equal (old_after ∪ tracks dropped from before),
      with arbitrary ordering.

    When the request fits these constraints we run the zero-
    interrupt path. Anything else (swapping tracks inside before,
    pulling a track from after into before, shifting current right)
    is impossible without an insert primitive, so we fall back to
    a full clear+rebuild + select_track + seek (~300 ms gap).
    """
    if not req.order:
        raise HTTPException(status_code=400, detail="order is empty")

    try:
        with _hqp_status_lock:
            _refresh_playlist_cache()
        current_payload = _latest_playlist
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"playlist read failed: {e}")
    current_tracks = current_payload.get("tracks") or []
    current_ids = [t.get("id") for t in current_tracks]

    status_idx = _latest_status.get("track_index") or 0
    if status_idx < 1 or status_idx > len(current_ids):
        raise HTTPException(
            status_code=409,
            detail="reorder requires a currently playing track; use /play-tracks",
        )

    if len(req.order) != len(current_ids):
        raise HTTPException(
            status_code=400,
            detail="order length must match current playlist length",
        )

    from collections import Counter
    if Counter(req.order) != Counter(current_ids):
        raise HTTPException(
            status_code=400,
            detail="order must be a permutation of the current playlist",
        )

    current_id = current_ids[status_idx - 1]
    new_status_idx = req.order.index(current_id) + 1

    old_before = current_ids[:status_idx - 1]
    old_after = current_ids[status_idx:]
    new_before = req.order[:new_status_idx - 1]
    new_after = req.order[new_status_idx:]

    if old_before == new_before and old_after == new_after:
        return {
            "ok": True, "removed": 0, "added": 0,
            "anchor_index": status_idx, "interrupted": False,
        }

    # Zero-interrupt feasibility: new_before must be a subsequence
    # of old_before (we can only drop tracks, never reorder or
    # add to before), and new_after's multiset must match what we
    # have available — original after-segment plus any tracks the
    # before-shrink drops out.
    def is_subsequence(needle: list, haystack: list) -> bool:
        j = 0
        for h in haystack:
            if j < len(needle) and needle[j] == h:
                j += 1
        return j == len(needle)

    expected_after = Counter(old_after) + Counter(old_before) - Counter(new_before)
    seamless = (
        is_subsequence(new_before, old_before)
        and Counter(new_after) == expected_after
    )

    # Resolve file paths for tracks we may need to append. Seamless
    # path only appends new_after; interrupt path rebuilds everything.
    ids_needing_paths = new_after if seamless else req.order
    rows = _db_query("""
        SELECT mf.id, mf.file_path
        FROM media_files mf
        WHERE mf.id = ANY(%(ids)s)
    """, {"ids": ids_needing_paths})
    path_by_id = {r["id"]: r["file_path"] for r in rows}
    missing = [mid for mid in ids_needing_paths if mid not in path_by_id]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"unknown media_file_ids: {missing[:5]}",
        )

    if seamless:
        # Sequence (current at slot K = status_idx throughout, then
        # shifts left as we shrink the before-segment in step 3):
        #
        # 1. Empty the after-segment by repeatedly removing slot K+1.
        #    K never changes: every remove targets a slot above K.
        # 2. Append new_after in target order. All appends land at the
        #    very end, after the current slot, so K stays put.
        # 3. Walk old_before slot by slot. Tracks present in new_before
        #    (matched in subsequence order) stay; the rest are removed
        #    by remove(slot). Each kept track advances our cursor;
        #    each removal keeps the cursor where it is (since slots
        #    above shift down to take its place). When the last
        #    "drop" track is removed, K has shifted down by exactly
        #    (len(old_before) - len(new_before)), landing on
        #    new_status_idx as required.
        try:
            with _hqp_lock:
                hqp = _get_hqp()
                for _ in range(len(old_after)):
                    hqp.playlist_remove(status_idx + 1)
                for mid in new_after:
                    hqp.playlist_add(file_path_to_uri(path_by_id[mid]))
                cursor = 1
                new_before_remaining = list(new_before)
                for old_track in old_before:
                    if (new_before_remaining
                            and new_before_remaining[0] == old_track):
                        new_before_remaining.pop(0)
                        cursor += 1
                    else:
                        hqp.playlist_remove(cursor)
            _force_refresh_playlist_after_write()
            return {
                "ok": True,
                "removed": len(old_after) + (len(old_before) - len(new_before)),
                "added": len(new_after),
                "anchor_index": new_status_idx,
                "interrupted": False,
            }
        except Exception as e:
            logger.error(f"reorder (seamless) failed: {e}", exc_info=True)
            raise HTTPException(status_code=503, detail=str(e))

    # Fallback: clear+rebuild. Required for cases that need an
    # insert (swap inside before, after→before crossing, current
    # right-shift). hqp.stop() is mandatory — HQPlayer ignores
    # PlaylistAdd(clear=True) while playing, silently appending
    # instead, which would double every track and land
    # select_track on the wrong slot.
    position = int(_latest_status.get("position") or 0)
    prev_state = _latest_status.get("state")
    should_resume = prev_state in ("playing", "paused")
    try:
        with _hqp_lock:
            hqp = _get_hqp()
            hqp.stop()
            hqp.playlist_add(
                file_path_to_uri(path_by_id[req.order[0]]), clear=True)
            for mid in req.order[1:]:
                hqp.playlist_add(file_path_to_uri(path_by_id[mid]))
            hqp.select_track(new_status_idx)
            if position > 0:
                hqp.seek(position)
            if should_resume:
                hqp.play()
        _force_refresh_playlist_after_write()
        return {
            "ok": True,
            "removed": len(current_ids),
            "added": len(req.order),
            "anchor_index": new_status_idx,
            "interrupted": True,
        }
    except Exception as e:
        logger.error(f"reorder (rebuild) failed: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/jump")
def jump(req: JumpRequest):
    """Jump to a specific position in the current HQPlayer playlist.

    `index` is 1-based to match HQPlayer's own `select_track` API and
    the SSE `track_index` field. Used by the Queue sheet to play a
    specific track without rebuilding the playlist."""
    if req.index < 1:
        raise HTTPException(status_code=400, detail="index must be >= 1")
    try:
        ok = _hqp_cmd(lambda h: h.select_track(req.index))
        if ok:
            _hqp_cmd(lambda h: h.play())
        _notify_update()
        return {"ok": ok, "index": req.index}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/play-track")
def play_track(req: PlayTrackRequest):
    """Clear playlist, add single track, play, register with tracker."""
    row = _db_query_one("""
        SELECT mf.file_path, t.title, a.name as artist, al.title as album
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN album_variants av ON mf.album_variant_id = av.id
        JOIN albums al ON av.album_id = al.id
        WHERE mf.id = %(track_id)s
    """, {"track_id": req.track_id})

    if not row:
        raise HTTPException(status_code=404, detail="Track not found")

    _rotate_session('track', seed_media_file_id=req.track_id)

    try:
        uri = file_path_to_uri(row["file_path"])
        with _hqp_lock:
            _hqp_safe(lambda h: h.stop())
            added = _add_uris_with_retry([uri], clear_first=True)
            if added:
                _hqp_safe(lambda h: h.play())
        _invalidate_playlist()
        _exit_radio_mode()
        _notify_update()

        if not added:
            raise HTTPException(
                status_code=503,
                detail="HQPlayer unavailable — track not queued. Try again.",
            )
        return {
            "ok": True,
            "artist": row["artist"],
            "title": row["title"],
            "album": row["album"],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/play-album")
def play_album(req: PlayAlbumRequest):
    """Fuzzy-match album, load all tracks, play."""
    match_conditions = [
        "(similarity(al.title, %(album)s) > 0.15 OR al.title ILIKE %(album_like)s)"
    ]
    match_params: dict = {"album": req.album_name, "album_like": f"%{req.album_name}%"}
    order_parts = ["similarity(al.title, %(album)s)"]

    if req.artist_name:
        match_conditions.append(
            "(similarity(a.name, %(artist)s) > 0.15 OR a.name ILIKE %(artist_like)s)"
        )
        match_params["artist"] = req.artist_name
        match_params["artist_like"] = f"%{req.artist_name}%"
        order_parts.append("similarity(a.name, %(artist)s)")

    match_where = " AND ".join(match_conditions)
    order_expr = " + ".join(order_parts)

    best_album = _db_query_one(f"""
        SELECT al.id, al.title as album, a.name as artist, {order_expr} as _score
        FROM albums al
        JOIN album_variants av ON av.album_id = al.id
        JOIN media_files mf ON mf.album_variant_id = av.id
        JOIN tracks t ON mf.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        WHERE {match_where}
        GROUP BY al.id, al.title, a.name
        ORDER BY _score DESC
        LIMIT 1
    """, match_params)

    if not best_album:
        raise HTTPException(status_code=404, detail=f"Album '{req.album_name}' not found")

    rows = _db_query("""
        SELECT mf.id, mf.file_path, t.title, mf.track_number,
               a.name as artist, al.title as album
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN album_variants av ON mf.album_variant_id = av.id
        JOIN albums al ON av.album_id = al.id
        WHERE al.id = %(album_id)s
        ORDER BY mf.disc_number, mf.track_number
    """, {"album_id": best_album["id"]})

    if not rows:
        raise HTTPException(status_code=404, detail="Album has no tracks")

    _rotate_session(
        'album',
        origin_album_id=str(best_album["id"]),
        seed_media_file_id=rows[0]["id"],
    )

    try:
        uris = [file_path_to_uri(r["file_path"]) for r in rows]
        with _hqp_lock:
            _hqp_safe(lambda h: h.stop())
            added = _add_uris_with_retry(uris, clear_first=True)
            if added:
                _hqp_safe(lambda h: h.play())

        _invalidate_playlist()
        _exit_radio_mode()
        _notify_update()

        if added < len(rows):
            raise HTTPException(
                status_code=503,
                detail=f"HQPlayer added {added} of {len(rows)} tracks "
                       "(connection unstable). Try again.",
            )
        return {
            "ok": True,
            "artist": rows[0]["artist"],
            "album": rows[0]["album"],
            "track_count": len(rows),
            "tracks": [
                {"id": r["id"], "title": r["title"], "track_number": r.get("track_number")}
                for r in rows
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# -- Phantom-album streaming buffer policy ------------------------------------
_PHANTOM_LEAD_CAP_SECONDS = 240.0    # never pre-buffer more than ~4 min of audio
_PHANTOM_LEAD_TRACK_TIMEOUT = 60.0   # cap the per-track wait while pre-buffering


def _phantom_lead_seconds(rtf: float, queries, concurrency: int) -> float:
    """How much contiguous audio to buffer before starting playback, derived
    from the measured real-time factor (wall-fetch seconds per audio second of
    track 0 — the channel-speed signal):

      rtf <= 1       the pipeline outruns playback on ONE stream → start at once
                     on track 0 (lead 0): truly ASAP.
      1 < rtf <= C   a single stream lags, but C concurrent fetches keep up in
                     steady state → a one-track cushion absorbs variance.
      rtf > C        sustained deficit (very slow channel) → buffer the cap, then
                     start; a long album still eventually drains, but the opening
                     stretch plays seamlessly.
    """
    if rtf <= 1.0:
        return 0.0
    durs = [q.duration for q in queries if q.duration]
    d_avg = (sum(durs) / len(durs)) if durs else 60.0
    if rtf <= concurrency:
        return min(_PHANTOM_LEAD_CAP_SECONDS, d_avg)
    return _PHANTOM_LEAD_CAP_SECONDS


def _phantom_filler(proxy, tokens: list, start_index: int, gen: int) -> None:
    """Rolling-append the rest of a phantom album: append each track to
    HQPlayer's native playlist as it finishes fetching, in order. HQPlayer
    HEAD-probes each URI at ADD time, so we only ever hand it a ready buffer.
    Stops the instant another playback session starts (the generation moved on)."""
    for j in range(start_index, len(tokens)):
        try:
            e = proxy.wait_ready(tokens[j])
        except (TimeoutError, KeyError):
            continue   # a track that never fetched — skip it, keep the album going
        if e is None or e.audio is None:
            continue
        with _hqp_lock:
            if _playback_generation != gen:
                return   # user moved to another queue → stop appending
            _add_uris_with_retry([proxy.url_for(tokens[j])], clear_first=False)
        _invalidate_playlist()
        _notify_update()


def _parallel_resolve(provider, queries: list) -> list:
    """Resolve every query to a provider source id concurrently (the fast
    availability pass, no downloads). Returns a list parallel to `queries` of
    source ids / None (None = not found on this provider)."""
    from concurrent.futures import ThreadPoolExecutor

    def one(q):
        try:
            return provider.resolve(q)
        except Exception:
            return None

    if not queries:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(queries)),
                            thread_name_prefix="resolve") as ex:
        return list(ex.map(one, queries))


def _phantom_track_query(track_id: str):
    """Build a TrackQuery for one phantom track from album_tracks, or None."""
    from streaming.base import TrackQuery
    row = _db_query_one("""
        SELECT t.title, atr.length_ms, al.title AS album,
               (SELECT ar.name FROM track_artists ta
                  JOIN artists ar ON ar.id = ta.artist_id
                WHERE ta.track_id = t.id
                ORDER BY (ta.role = 'primary') DESC LIMIT 1) AS artist
        FROM tracks t
        JOIN album_tracks atr ON atr.track_id = t.id
        JOIN albums al ON al.id = atr.album_id
        WHERE t.id = %(tid)s::uuid
        LIMIT 1
    """, {"tid": track_id})
    if not row or not row["title"]:
        return None
    return TrackQuery(
        artist=row["artist"] or "", title=row["title"], album=row["album"],
        duration=(float(row["length_ms"]) / 1000.0 if row["length_ms"] else None),
        track_id=track_id)


def _phantom_album_queries(album_id: str) -> list:
    """Ordered TrackQuery list for a phantom album's tracklist (album_tracks)."""
    from streaming.base import TrackQuery
    rows = _db_query("""
        SELECT t.id::text AS track_id, t.title, al.title AS album, atr.length_ms,
               (SELECT ar.name FROM track_artists ta
                  JOIN artists ar ON ar.id = ta.artist_id
                WHERE ta.track_id = t.id
                ORDER BY (ta.role = 'primary') DESC LIMIT 1) AS artist
        FROM album_tracks atr
        JOIN tracks t ON t.id = atr.track_id
        JOIN albums al ON al.id = atr.album_id
        WHERE atr.album_id = %(album_id)s
        ORDER BY atr.disc, atr.position
    """, {"album_id": album_id})
    return [
        TrackQuery(
            artist=r["artist"] or "", title=r["title"], album=r["album"],
            duration=(float(r["length_ms"]) / 1000.0 if r["length_ms"] else None),
            track_id=r["track_id"])
        for r in rows if r["title"]
    ]


class PlayPhantomAlbumRequest(BaseModel):
    album_id: str   # UUID of a phantom (not-owned) album


@router.post("/play-phantom-album")
def play_phantom_album(req: PlayPhantomAlbumRequest):
    """Stream a phantom (not-in-library) album onto HQPlayer.

    Resolves the album's MusicBrainz tracklist to a streaming provider, serves
    the audio through the in-memory media proxy as plain-http URLs, and feeds
    those into HQPlayer's NATIVE playlist exactly like owned ``file://`` tracks
    (proven: HQPlayer plays http FLAC sustained alongside local files).

    Now-Playing shows HQPlayer's generic stream label for these tracks until
    provider-sourced metadata is wired into the status poller (next step)."""
    from streaming import service as streaming_service
    from streaming.base import TrackQuery

    if not streaming_service.is_enabled():
        raise HTTPException(status_code=503, detail="Streaming preview is disabled")
    provider = streaming_service.get_provider()   # preferred: lossless before lossy
    if provider is None:
        raise HTTPException(status_code=503, detail="No streaming provider available")

    queries = _phantom_album_queries(req.album_id)
    if not queries:
        raise HTTPException(status_code=404, detail="Phantom album not found or has no tracklist")

    proxy = streaming_service.get_proxy()

    # Resolve-first availability pass: which tracks actually exist on this
    # provider, BEFORE the slow downloads — so the UI can grey out the missing
    # ones and disable Listen when the whole album is unavailable. Providers that
    # can't pre-resolve fall back to fetch-and-skip (no miss list).
    if provider.supports_resolve:
        source_ids = _parallel_resolve(provider, queries)
        avail_q = [q for q, s in zip(queries, source_ids) if s]
        avail_sids = [s for s in source_ids if s]
        missing = [q for q, s in zip(queries, source_ids) if not s]
    else:
        avail_q, avail_sids, missing = queries, None, []
    missing_payload = [{"track_id": q.track_id, "title": q.title} for q in missing]

    if not avail_q:
        # Nothing on this provider — leave HQPlayer untouched; the UI disables
        # Listen and greys out every row.
        return {
            "ok": True, "provider": provider.manifest.id,
            "album": queries[0].album, "artist": queries[0].artist,
            "track_count": 0, "requested": len(queries), "missing": missing_payload,
        }

    tokens = proxy.start_session(provider, avail_q, avail_sids)  # priority-fetches track 0

    # Adaptive rolling buffer. Track 0 is fetched alone (full bandwidth) for the
    # fastest safe start; its measured real-time factor sizes how much audio we
    # pre-buffer before playing so transitions don't gap on a slow channel; the
    # tail then streams in via a background filler that appends each track as it
    # lands. The UI shows a buffering state until this returns.
    try:
        proxy.wait_ready(tokens[0])
    except (TimeoutError, KeyError):
        pass
    rtf = proxy.fetch_rtf(tokens[0]) or 1.0
    proxy.prefetch_from(1)                     # fan out the tail concurrently
    lead_target = _phantom_lead_seconds(rtf, avail_q, proxy.fetch_concurrency)

    # Event-driven pre-buffer: block on each boundary track's ready event in
    # order until the contiguous buffered audio covers the lead (or the album
    # ends, or a track is too slow — then start with what we have).
    prefix, buffered, next_index = proxy.ready_lead(0)
    while (not prefix or buffered < lead_target) and next_index < len(tokens):
        try:
            proxy.wait_ready(tokens[next_index], timeout=_PHANTOM_LEAD_TRACK_TIMEOUT)
        except (TimeoutError, KeyError):
            break
        prefix, buffered, next_index = proxy.ready_lead(0)

    logger.info("phantom buffer: rtf=%.2f lead=%.0fs → start %d/%d (avail %d, missing %d)",
                rtf, lead_target, len(prefix), len(queries), len(avail_q), len(missing))

    if not prefix:
        raise HTTPException(status_code=502, detail="No tracks could be fetched from the provider")

    prefix_urls = [proxy.url_for(t) for t in prefix]
    try:
        with _hqp_lock:
            _hqp_safe(lambda h: h.stop())
            added = _add_uris_with_retry(prefix_urls, clear_first=True)
            gen = _playback_generation          # capture under lock, after the clear
            if added:
                _hqp_safe(lambda h: h.play())
        _invalidate_playlist()
        _exit_radio_mode()
        _notify_update()

        if not added:
            raise HTTPException(
                status_code=503,
                detail="HQPlayer unavailable — preview not queued. Try again.",
            )
        # Roll the remaining tracks in as they finish fetching (background).
        if next_index < len(tokens):
            threading.Thread(
                target=_phantom_filler, args=(proxy, list(tokens), next_index, gen),
                daemon=True, name="phantom-filler").start()
        return {
            "ok": True,
            "album": avail_q[0].album,
            "artist": avail_q[0].artist,
            "provider": provider.manifest.id,
            "track_count": added,                        # streaming now
            "buffering": max(0, len(avail_q) - added),   # rolling in via the filler
            "requested": len(queries),                   # tracklist size
            "missing": missing_payload,                  # not found on this provider
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


class PlayPhantomTrackRequest(BaseModel):
    track_id: str   # UUID of a phantom (not-owned) track


@router.post("/play-phantom-track")
def play_phantom_track(req: PlayPhantomTrackRequest):
    """Stream a single phantom track onto HQPlayer (replaces the queue), the
    streaming counterpart of clicking an owned track. Resolves the track via the
    preferred provider; returns track_count=0 + the track in `missing` when the
    provider has no match (the UI greys the row)."""
    from streaming import service as streaming_service
    from streaming.base import TrackQuery

    if not streaming_service.is_enabled():
        raise HTTPException(status_code=503, detail="Streaming preview is disabled")
    provider = streaming_service.get_provider()
    if provider is None:
        raise HTTPException(status_code=503, detail="No streaming provider available")

    q = _phantom_track_query(req.track_id)
    if q is None:
        raise HTTPException(status_code=404, detail="Phantom track not found")
    missing = [{"track_id": req.track_id, "title": q.title}]
    sid = provider.resolve(q) if provider.supports_resolve else None
    if provider.supports_resolve and not sid:
        return {"ok": True, "provider": provider.manifest.id,
                "track_count": 0, "requested": 1, "missing": missing}

    proxy = streaming_service.get_proxy()
    tokens = proxy.start_session(provider, [q], [sid] if sid else None)
    try:
        e = proxy.wait_ready(tokens[0])
    except (TimeoutError, KeyError):
        e = None
    if e is None or e.audio is None:
        raise HTTPException(status_code=502, detail="Track could not be fetched from the provider")

    try:
        with _hqp_lock:
            _hqp_safe(lambda h: h.stop())
            added = _add_uris_with_retry([proxy.url_for(tokens[0])], clear_first=True)
            if added:
                _hqp_safe(lambda h: h.play())
        _invalidate_playlist()
        _exit_radio_mode()
        _notify_update()
        if not added:
            raise HTTPException(
                status_code=503,
                detail="HQPlayer unavailable — preview not queued. Try again.")
        return {"ok": True, "provider": provider.manifest.id, "track_count": 1,
                "requested": 1, "missing": [], "artist": q.artist, "album": q.album}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/queue-phantom-track")
def queue_phantom_track(req: PlayPhantomTrackRequest):
    """Append one phantom track to the HQPlayer queue (streamed, no replace)."""
    from streaming import service as streaming_service
    if not streaming_service.is_enabled():
        raise HTTPException(status_code=503, detail="Streaming preview is disabled")
    provider = streaming_service.get_provider()
    if provider is None:
        raise HTTPException(status_code=503, detail="No streaming provider available")

    q = _phantom_track_query(req.track_id)
    if q is None:
        raise HTTPException(status_code=404, detail="Phantom track not found")
    missing = [{"track_id": req.track_id, "title": q.title}]
    sid = provider.resolve(q) if provider.supports_resolve else None
    if provider.supports_resolve and not sid:
        return {"ok": True, "provider": provider.manifest.id,
                "track_count": 0, "requested": 1, "missing": missing}

    proxy = streaming_service.get_proxy()
    tokens = proxy.add_tracks(provider, [q], [sid] if sid else None)
    try:
        e = proxy.wait_ready(tokens[0])
    except (TimeoutError, KeyError):
        e = None
    if e is None or e.audio is None:
        raise HTTPException(status_code=502, detail="Track could not be fetched from the provider")

    try:
        with _hqp_lock:
            added = _add_uris_with_retry([proxy.url_for(tokens[0])], clear_first=False)
        _invalidate_playlist()
        _notify_update()
        if not added:
            raise HTTPException(status_code=503,
                                detail="HQPlayer unavailable — not queued. Try again.")
        return {"ok": True, "provider": provider.manifest.id, "track_count": 1,
                "requested": 1, "missing": []}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/queue-phantom-album")
def queue_phantom_album(req: PlayPhantomAlbumRequest):
    """Append a phantom album to the HQPlayer queue (streamed, rolling-append)."""
    from streaming import service as streaming_service
    if not streaming_service.is_enabled():
        raise HTTPException(status_code=503, detail="Streaming preview is disabled")
    provider = streaming_service.get_provider()
    if provider is None:
        raise HTTPException(status_code=503, detail="No streaming provider available")

    queries = _phantom_album_queries(req.album_id)
    if not queries:
        raise HTTPException(status_code=404, detail="Phantom album not found or has no tracklist")

    if provider.supports_resolve:
        source_ids = _parallel_resolve(provider, queries)
        avail_q = [q for q, s in zip(queries, source_ids) if s]
        avail_sids = [s for s in source_ids if s]
        missing = [q for q, s in zip(queries, source_ids) if not s]
    else:
        avail_q, avail_sids, missing = queries, None, []
    missing_payload = [{"track_id": q.track_id, "title": q.title} for q in missing]
    if not avail_q:
        return {"ok": True, "provider": provider.manifest.id, "track_count": 0,
                "requested": len(queries), "missing": missing_payload}

    proxy = streaming_service.get_proxy()
    tokens = proxy.add_tracks(provider, avail_q, avail_sids)
    # Append each available track to the queue as it finishes fetching, in order.
    # A queue-append doesn't start a new session, so capture the CURRENT
    # generation; the filler aborts if the user replaces the queue meanwhile.
    with _hqp_lock:
        gen = _playback_generation
    threading.Thread(target=_phantom_filler, args=(proxy, list(tokens), 0, gen),
                     daemon=True, name="phantom-queue").start()
    return {"ok": True, "provider": provider.manifest.id,
            "track_count": len(avail_q), "requested": len(queries),
            "missing": missing_payload}


@router.post("/play-similar")
def play_similar(req: PlaySimilarRequest):
    """Find similar tracks via pgvector cosine search, queue and play."""
    # Get track_id from the media_file
    source = _db_query_one("""
        SELECT mf.id, t.id as db_track_id
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        WHERE mf.id = %(track_id)s
    """, {"track_id": req.track_id})

    if not source:
        raise HTTPException(status_code=404, detail="Track not found")

    rows = _db_query("""
        WITH target AS (
            SELECT e.vector
            FROM embeddings e
            WHERE e.track_id = %(db_track_id)s
        )
        SELECT mf_rep.id, mf_rep.file_path, t.title, a.name as artist,
               mf_rep.album_title as album,
               1 - (e.vector <=> (SELECT vector FROM target)) as similarity
        FROM tracks t
        JOIN embeddings e ON e.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN LATERAL (
            SELECT mf.id, mf.file_path, mf.duration_seconds, mf.track_number,
                   mf.sample_rate, mf.bit_depth, mf.is_lossless,
                   al.title as album_title
            FROM media_files mf
            JOIN album_variants av ON mf.album_variant_id = av.id
            JOIN albums al ON av.album_id = al.id
            WHERE mf.track_id = t.id
            ORDER BY mf.is_analysis_source DESC, mf.id
            LIMIT 1
        ) mf_rep ON true
        WHERE t.id != %(db_track_id)s
        ORDER BY e.vector <=> (SELECT vector FROM target)
        LIMIT %(limit)s
    """, {"db_track_id": source["db_track_id"], "limit": req.limit})

    if not rows:
        raise HTTPException(status_code=404, detail="No similar tracks found")

    _rotate_session('radio', seed_media_file_id=req.track_id)

    try:
        uris = [file_path_to_uri(r["file_path"]) for r in rows]
        with _hqp_lock:
            _hqp_safe(lambda h: h.stop())
            added = _add_uris_with_retry(uris, clear_first=True)
            if added:
                _hqp_safe(lambda h: h.play())

        _invalidate_playlist()
        _exit_radio_mode()
        _notify_update()

        if added < len(rows):
            raise HTTPException(
                status_code=503,
                detail=f"HQPlayer added {added} of {len(rows)} tracks "
                       "(connection unstable). Try again.",
            )
        return {
            "ok": True,
            "count": len(rows),
            "tracks": [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "artist": r["artist"],
                    "album": r["album"],
                    "similarity": round(float(r["similarity"]), 3),
                }
                for r in rows
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/play-tracks")
def play_tracks(req: PlayTracksRequest):
    """Play multiple tracks by IDs."""
    if not req.track_ids:
        raise HTTPException(status_code=400, detail="No track IDs provided")
    if req.origin is not None and req.origin not in _SESSION_ORIGINS:
        raise HTTPException(status_code=400, detail=f"invalid origin: {req.origin}")

    rows = _db_query("""
        SELECT mf.id, mf.file_path, t.title, a.name as artist, al.title as album
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN album_variants av ON mf.album_variant_id = av.id
        JOIN albums al ON av.album_id = al.id
        WHERE mf.id = ANY(%(ids)s)
        ORDER BY array_position(%(ids)s, mf.id)
    """, {"ids": req.track_ids})

    if not rows:
        raise HTTPException(status_code=404, detail="No tracks found")

    _rotate_session(
        req.origin or 'mix',
        origin_album_id=req.origin_album_id,
        seed_media_file_id=rows[0]["id"],
    )

    try:
        uris = [file_path_to_uri(r["file_path"]) for r in rows]
        with _hqp_lock:
            logger.info("play-tracks: stopping playback")
            _hqp_safe(lambda h: h.stop())
            added = _add_uris_with_retry(uris, clear_first=True)
            logger.info(f"play-tracks: added {added} of {len(rows)} tracks")
            if added:
                _hqp_safe(lambda h: h.play())

                # Verify playback started; if the first track fails (e.g. a
                # [Vinyl] path) skip ahead until one plays. Best-effort —
                # the tracks are already queued, so a status-read miss here
                # must not fail the whole call.
                try:
                    hqp = _get_hqp()
                    time.sleep(0.5)
                    status = hqp.get_status()
                    if status and status.state == PlaybackState.STOPPED and added > 1:
                        logger.warning("play-tracks: first track didn't start, skipping ahead")
                        for skip_idx in range(2, min(added + 1, 6)):
                            hqp.select_track(skip_idx)
                            hqp.play()
                            time.sleep(0.5)
                            status = hqp.get_status()
                            if status and status.state != PlaybackState.STOPPED:
                                logger.info(f"play-tracks: track {skip_idx} started")
                                break
                except (BrokenPipeError, ConnectionError, OSError):
                    pass

        _invalidate_playlist()
        _exit_radio_mode()
        _notify_update()

        if added < len(rows):
            raise HTTPException(
                status_code=503,
                detail=f"HQPlayer added {added} of {len(rows)} tracks "
                       "(connection unstable). Try again.",
            )
        return {
            "ok": True,
            "count": len(rows),
            "tracks": [
                {"id": r["id"], "title": r["title"], "artist": r["artist"]}
                for r in rows
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"play-tracks failed: {e}")
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/queue-tracks")
def queue_tracks(req: QueueTracksRequest):
    """Append the given tracks to the current queue, in order, without
    clearing. Used by the per-track "+" button and the "Queue album"
    action — these expect "add exactly these tracks", not the
    similarity-driven Radio Mode that `/queue-next` provides."""
    if not req.track_ids:
        raise HTTPException(status_code=400, detail="No track IDs provided")

    rows = _db_query("""
        SELECT mf.id, mf.file_path, t.title, a.name as artist, al.title as album
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN album_variants av ON mf.album_variant_id = av.id
        JOIN albums al ON av.album_id = al.id
        WHERE mf.id = ANY(%(ids)s)
        ORDER BY array_position(%(ids)s, mf.id)
    """, {"ids": req.track_ids})

    if not rows:
        raise HTTPException(status_code=404, detail="No tracks found")

    try:
        uris = [file_path_to_uri(r["file_path"]) for r in rows]
        with _hqp_lock:
            added = _add_uris_with_retry(uris)
        _invalidate_playlist()
        if added < len(rows):
            raise HTTPException(
                status_code=503,
                detail=f"HQPlayer added {added} of {len(rows)} tracks "
                       "(connection unstable). Try again.",
            )
        return {
            "ok": True,
            "count": added,
            "tracks": [
                {"id": r["id"], "title": r["title"], "artist": r["artist"], "album": r["album"]}
                for r in rows
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/queue-next")
def queue_next(req: QueueNextRequest):
    """Append similar tracks to the current queue (Radio Mode)."""
    source = _db_query_one("""
        SELECT mf.id, t.id as db_track_id
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        WHERE mf.id = %(track_id)s
    """, {"track_id": req.track_id})

    if not source:
        raise HTTPException(status_code=404, detail="Track not found")

    exclude_clause = ""
    params = {"db_track_id": source["db_track_id"], "limit": req.limit}
    if req.exclude_ids:
        exclude_clause = "AND mf_rep.id != ALL(%(exclude_ids)s)"
        params["exclude_ids"] = req.exclude_ids

    rows = _db_query(f"""
        WITH target AS (
            SELECT e.vector
            FROM embeddings e
            WHERE e.track_id = %(db_track_id)s
        )
        SELECT mf_rep.id, mf_rep.file_path, t.title, a.name as artist,
               mf_rep.album_title as album
        FROM tracks t
        JOIN embeddings e ON e.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN LATERAL (
            SELECT mf.id, mf.file_path, al.title as album_title
            FROM media_files mf
            JOIN album_variants av ON mf.album_variant_id = av.id
            JOIN albums al ON av.album_id = al.id
            WHERE mf.track_id = t.id
            ORDER BY mf.is_analysis_source DESC, mf.id
            LIMIT 1
        ) mf_rep ON true
        WHERE t.id != %(db_track_id)s
          {exclude_clause}
        ORDER BY e.vector <=> (SELECT vector FROM target)
        LIMIT %(limit)s
    """, params)

    if not rows:
        return {"ok": True, "count": 0, "tracks": []}

    try:
        uris = [file_path_to_uri(r["file_path"]) for r in rows]
        with _hqp_lock:
            added = _add_uris_with_retry(uris)

        _invalidate_playlist()

        # Best-effort radio fill: report the real count rather than 503 on a
        # short add — the queue still grows and the next track-end re-fills.
        return {
            "ok": added > 0,
            "count": added,
            "tracks": [
                {"id": r["id"], "title": r["title"], "artist": r["artist"], "album": r["album"]}
                for r in rows
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# -- Radio mode ---------------------------------------------------------------

class RadioStartRequest(BaseModel):
    track_id: int           # media_file_id of the seed
    limit: int = 20         # how many similar tracks to append after the seed


@router.post("/radio/start")
def radio_start(req: RadioStartRequest):
    """Replace the queue with `seed + top-N CLAP-similar tracks`, start
    playback, and flip _radio_mode true. Same destructive nature as
    /play-track + /queue-next chained — the caller (Now Playing
    toggle) is responsible for warning the user before invoking."""
    global _radio_mode

    source = _db_query_one("""
        SELECT mf.id, mf.file_path,
               t.id AS db_track_id,
               regexp_replace(LOWER(t.title), '\\s+', ' ', 'g') AS title_norm,
               LOWER(a.name) AS artist_norm
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
        JOIN artists a ON a.id = ta.artist_id
        WHERE mf.id = %(track_id)s
        LIMIT 1
    """, {"track_id": req.track_id})
    if not source:
        raise HTTPException(status_code=404, detail="Track not found")

    logger.info(
        f"radio_start seed: track_id={source['db_track_id']} "
        f"title_norm={source['title_norm']!r} artist_norm={source['artist_norm']!r}"
    )

    # CLAP-similar tracks for the seed. Mirrors /queue-next, with an
    # extra filter: exclude tracks whose (whitespace-normalized
    # lower title, lower artist name) matches the seed.
    #
    # Matching by artist NAME (not id) shields us from duplicate
    # artist rows in the DB — different uuids that both spell
    # "Lionel Richie" wouldn't share an id, but they would share a
    # normalized name. Same idea for title: multiple spaces and
    # case differences across album reissues are smoothed out by
    # regexp_replace+LOWER.
    similar_rows = _db_query("""
        WITH target AS (
            SELECT e.vector
            FROM embeddings e
            WHERE e.track_id = %(db_track_id)s
        )
        SELECT mf_rep.id, mf_rep.file_path,
               t.title AS dbg_title, a.name AS dbg_artist
        FROM tracks t
        JOIN embeddings e ON e.track_id = t.id
        JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
        JOIN artists a ON a.id = ta.artist_id
        JOIN LATERAL (
            SELECT mf.id, mf.file_path
            FROM media_files mf
            WHERE mf.track_id = t.id
            ORDER BY mf.is_analysis_source DESC, mf.id
            LIMIT 1
        ) mf_rep ON true
        WHERE t.id != %(db_track_id)s
          AND NOT (
              regexp_replace(LOWER(t.title), '\\s+', ' ', 'g') = %(title_norm)s
              AND LOWER(a.name) = %(artist_norm)s
          )
        ORDER BY e.vector <=> (SELECT vector FROM target)
        LIMIT %(limit)s
    """, {
        "db_track_id": source["db_track_id"],
        "title_norm":  source["title_norm"],
        "artist_norm": source["artist_norm"],
        "limit":       req.limit,
    })
    logger.info(
        f"radio_start picked {len(similar_rows)} similar tracks; first 3: "
        + ", ".join(
            f"{r['dbg_artist']} — {r['dbg_title']}"
            for r in similar_rows[:3]
        )
    )

    _rotate_session('radio', seed_media_file_id=req.track_id)

    try:
        uris = [file_path_to_uri(r["file_path"]) for r in similar_rows]
        with _hqp_lock:
            # Seamless seed handling. The seed is whatever's playing
            # right now, so we don't re-add it or restart anything —
            # restarting would cut the user's listen mid-song.
            # `PlaylistClear` keeps the active slot intact (HQPlayer
            # can't drop a track that's reading), so the call below
            # erases everything queued AFTER the current track while
            # the seed keeps playing untouched. Then we append CLAP-
            # similar picks; HQPlayer flows into them when the seed
            # ends, no `play()` needed.
            _hqp_safe(lambda h: h.playlist_clear())
            added = _add_uris_with_retry(uris)
        _invalidate_playlist()
        _radio_mode = True
        # Wake SSE so the UI's Radio toggle flips amber without
        # waiting for the next 1s poll tick.
        global _status_version
        _status_version += 1
        _wake_sse_clients()
        return {
            "ok": True,
            "seed_id": source["id"],
            "similar_count": added,
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/radio/stop")
def radio_stop():
    """Flip _radio_mode off. The queue is left alone — radio's
    'replace' behaviour only happens on start, and turning it off
    just means future track-ends won't trigger an append."""
    _exit_radio_mode()
    return {"ok": True, "radio_mode": False}


def _exit_radio_mode() -> None:
    """Drop the radio flag and wake SSE so the Now Playing toggle
    snaps off immediately. Called from every endpoint that
    replaces the queue with explicit user-picked content (play-
    track, play-album, play-tracks, play-similar). Append-only
    paths like queue-tracks and queue-next don't touch the flag —
    they extend the radio rather than ending it."""
    global _radio_mode, _status_version
    if _radio_mode:
        _radio_mode = False
        _status_version += 1
        _wake_sse_clients()


# -- Lyrics -------------------------------------------------------------------

@router.get("/lyrics/{media_file_id}")
def get_lyrics(media_file_id: int):
    """
    Get lyrics for a track by media_file_id. Lazy-fetch from LRCLIB if not cached.

    Returns parsed synced_lyrics (list of {time_ms, text}) or null.
    Never returns an error — gracefully returns null fields.
    """
    # Get track info from DB
    row = _db_query_one("""
        SELECT mf.id, t.id as track_id, t.title, a.name as artist,
               al.title as album, mf.duration_seconds
        FROM media_files mf
        JOIN tracks t ON mf.track_id = t.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        JOIN album_variants av ON mf.album_variant_id = av.id
        JOIN albums al ON av.album_id = al.id
        WHERE mf.id = %(media_file_id)s
    """, {"media_file_id": media_file_id})

    if not row:
        return {
            "track_id": None, "artist": None, "title": None,
            "source": None, "instrumental": False,
            "plain_lyrics": None, "synced_lyrics": None,
        }

    track_id = str(row["track_id"])

    # Check track_lyrics table (fast path)
    lyrics_row = _db_query_one("""
        SELECT source, plain_lyrics, synced_lyrics, instrumental
        FROM track_lyrics
        WHERE track_id = %(track_id)s
        ORDER BY
            CASE WHEN synced_lyrics IS NOT NULL THEN 0 ELSE 1 END,
            created_at DESC
        LIMIT 1
    """, {"track_id": track_id})

    if lyrics_row:
        synced = None
        if lyrics_row["synced_lyrics"]:
            synced = LrclibService.parse_lrc(lyrics_row["synced_lyrics"])
        return {
            "track_id": track_id,
            "artist": row["artist"],
            "title": row["title"],
            "source": lyrics_row["source"],
            "instrumental": lyrics_row["instrumental"],
            "plain_lyrics": lyrics_row["plain_lyrics"],
            "synced_lyrics": synced,
        }

    # Check external_metadata for previous failed fetch
    meta_row = _db_query_one("""
        SELECT fetch_status FROM external_metadata
        WHERE entity_type = 'track' AND entity_id = %(track_id)s
          AND source = 'lrclib' AND metadata_type = 'lyrics'
    """, {"track_id": track_id})

    if meta_row and meta_row["fetch_status"] == "not_found":
        return {
            "track_id": track_id,
            "artist": row["artist"],
            "title": row["title"],
            "source": None, "instrumental": False,
            "plain_lyrics": None, "synced_lyrics": None,
        }

    # On-demand fetch from LRCLIB
    try:
        from database import get_db_context

        duration = int(row["duration_seconds"]) if row["duration_seconds"] else None

        with LrclibService() as service, get_db_context() as db:
            result = service.fetch_and_store(
                db,
                track_id=row["track_id"],
                track_name=row["title"],
                artist_name=row["artist"],
                album_name=row["album"],
                duration=duration,
            )

        if result["status"] == "not_found":
            return {
                "track_id": track_id,
                "artist": row["artist"],
                "title": row["title"],
                "source": None, "instrumental": False,
                "plain_lyrics": None, "synced_lyrics": None,
            }

        data = result.get("data", {})
        synced = None
        if data.get("syncedLyrics"):
            synced = LrclibService.parse_lrc(data["syncedLyrics"])

        return {
            "track_id": track_id,
            "artist": row["artist"],
            "title": row["title"],
            "source": "lrclib",
            "instrumental": data.get("instrumental", False),
            "plain_lyrics": data.get("plainLyrics"),
            "synced_lyrics": synced,
        }

    except Exception as e:
        logger.error(f"Lyrics fetch failed for media_file {media_file_id}: {e}")
        return {
            "track_id": track_id,
            "artist": row["artist"],
            "title": row["title"],
            "source": None, "instrumental": False,
            "plain_lyrics": None, "synced_lyrics": None,
        }
