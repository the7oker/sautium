"""The owner's Last.fm listening history, imported.

When the owner connects Last.fm — and whenever they press Sync — this module
walks their scrobbles (user.getRecentTracks) from the newest down, one page of
200 per transaction, into the waiting room (`pending_scrobbles`): raw strings,
local-only like the rest of the Last.fm layer, kept only until the canon
(canon/scrobbles.py) binds each one to a canonical track and it becomes a
listen (`listening_history`, source 'lastfm'). Nothing is ever named after
Last.fm's strings: an entity is born canonical or not at all.

Sautium scrobbles what it plays, so the history hands this node's own listens
back. A scrobble with a record of the same listen already here
(play_stats.same_listen) never enters the room. The walk also stops short of
the listen in progress — its scrobble may be on Last.fm already while its
history row is written only when it ends.

The walk moves down by its `to` bound, never by page number (Last.fm's deep
pages are slow and unstable), and its position lives in `lastfm_import`, so a
restart resumes where it committed. A later sync re-reads from the last
watermark minus two weeks: Last.fm takes a late scrobble for that long.

No timer: Last.fm pushes nothing, and polling is not a design. The triggers
are events — the connection (lastfm_auth), the Sync button, and a restart that
finds a walk half done.
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pylast
from psycopg2.extras import execute_values

from config import settings
from db_pool import db_execute, db_query_one, transaction
from play_stats import LISTENS_LOCK_KEY, refresh_play_stats, same_listen

logger = logging.getLogger(__name__)

PAGE_SIZE = 200
# Last.fm accepts a scrobble up to two weeks after it was played (offline
# scrobblers submit late), so a later sync reads that far behind its watermark.
LATE_SCROBBLE_WINDOW = timedelta(days=14)
# The Last.fm API licence covers a small, temporary portion of its data
# (ToS 4.3.4): raw scrobble strings live only while they wait, and past this
# many (~250 B each) the walk pauses until the canon has bound enough of them.
PENDING_ROW_BUDGET = 250_000
RESUME_BELOW = int(PENDING_ROW_BUDGET * 0.8)
# Pages between wakes of the MB slice cycle, which fetches catalogue data for
# the names that wait (the first page wakes it at once).
_MB_WAKE_EVERY = 50
_PRIVATE_PROFILE = "17"

_state: Dict[str, Any] = {"running": False, "phase": "idle", "progress": "",
                          "pct": None, "error": None, "paused": None, "budget": False}
_cancel = threading.Event()
_lock = threading.Lock()
_thread: Optional[threading.Thread] = None


def _notify() -> None:
    from routers.settings import notify_library_subscribers
    notify_library_subscribers()


def _set(**fields) -> None:
    _state.update(fields)
    _notify()


def name_key(artist: str) -> str:
    """The waiting room's per-name key: the head of a safe compound credit
    ("A feat. B" → "A") in the MB slice ledger's own form, so the slice cycle
    can ask for it as it is."""
    from canon.split import detect_compound_type
    from desktop.p2p.mb_slice_queries import name_key as slice_key
    compound = detect_compound_type(artist)
    return slice_key(compound[2][0] if compound else artist)


def _account() -> Optional[str]:
    if not (settings.lastfm_session_key and settings.lastfm_username):
        return None
    return settings.lastfm_username.strip().lower()


def status() -> Dict[str, Any]:
    """The walk's live state plus what the database knows: listens imported,
    scrobbles waiting, the last completed sync."""
    user = _account()
    row = db_query_one("""
        SELECT (SELECT count(*) FROM listening_history WHERE source = 'lastfm') AS imported,
               (SELECT count(*) FROM pending_scrobbles) AS waiting,
               (SELECT last_sync_at FROM lastfm_import WHERE username = %(u)s) AS last_sync_at,
               (SELECT last_error FROM lastfm_import WHERE username = %(u)s) AS last_error
    """, {"u": user})
    waiting = None
    if row["waiting"] and not _state["running"]:
        # Why they wait — read only between walks: during one the room
        # changes every page and the progress line is what matters.
        from discography import _MB_SOURCE_COVERS_SQL
        covered = _MB_SOURCE_COVERS_SQL.format(name="n.name_key")
        waiting = db_query_one(f"""
            SELECT count(*) FILTER (WHERE n.artist_id IS NULL AND NOT ({covered})) AS catalogue,
                   count(*) FILTER (WHERE n.artist_id IS NULL AND ({covered})) AS unknown,
                   count(*) FILTER (WHERE n.artist_id IS NOT NULL) AS unminted
              FROM pending_scrobbles p JOIN pending_scrobble_artists n USING (name_key)""")
    return {
        "waiting_why": waiting,
        "connected": user is not None,
        "running": bool(_state["running"]),
        "phase": _state["phase"],
        "progress": _state["progress"],
        "pct": _state["pct"],
        "paused": _state["paused"],
        "error": _state["error"] or row["last_error"],
        "imported": int(row["imported"]),
        "waiting": int(row["waiting"]),
        "last_sync_at": row["last_sync_at"].isoformat() if row["last_sync_at"] else None,
    }


def start(reason: str) -> bool:
    """Begin (or continue) the walk for the connected account. Single flight:
    False when one is already running or no account is connected."""
    global _thread
    user = _account()
    if user is None:
        return False
    with _lock:
        if _state["running"]:
            return False
        _cancel.clear()
        _state.update(running=True, phase="starting", progress="", pct=None,
                      error=None, paused=None, budget=False)
        _thread = threading.Thread(target=_run, args=(user, reason), daemon=True,
                                   name="lastfm-history")
        _thread.start()
    _notify()
    return True


def resume() -> None:
    """At startup: continue a walk a restart interrupted."""
    user = _account()
    if user and db_query_one("SELECT 1 AS x FROM lastfm_import "
                             "WHERE username = %(u)s AND walk_top_at IS NOT NULL", {"u": user}):
        start("resume")


def resume_below_budget() -> None:
    """After a canon pass: a walk the waiting room's budget paused goes on once
    the canon has placed enough of what waits."""
    if _state["budget"] and not _state["running"] and _pending_rows() < RESUME_BELOW:
        start("budget")


def _open_walk(cur, user: str) -> Dict[str, Any]:
    """The walk's bounds, fixing the top of a new one: now, or just before the
    listen in progress."""
    from playback.tracker import open_listen_started_at
    top = datetime.now(timezone.utc)
    playing = open_listen_started_at()
    if playing is not None:
        top = min(top, playing - timedelta(seconds=1))
    cur.execute("INSERT INTO lastfm_import (username) VALUES (%s) ON CONFLICT DO NOTHING", (user,))
    cur.execute("""
        UPDATE lastfm_import
           SET walk_top_at = COALESCE(walk_top_at, %(top)s),
               walk_cursor_at = CASE WHEN walk_top_at IS NULL THEN NULL ELSE walk_cursor_at END,
               walk_total = CASE WHEN walk_top_at IS NULL THEN NULL ELSE walk_total END,
               walk_fetched = CASE WHEN walk_top_at IS NULL THEN 0 ELSE walk_fetched END,
               last_error = NULL
         WHERE username = %(u)s
     RETURNING watermark_at, walk_top_at, walk_cursor_at, walk_total, walk_fetched
    """, {"top": top, "u": user})
    watermark, top, cursor, total, fetched = cur.fetchone()
    return {"watermark": watermark, "top": top, "cursor": cursor,
            "total": total, "fetched": fetched}


def _store_page(cur, user: str, items: List[Dict[str, Any]], walk: Dict[str, Any],
                before: datetime, oldest: datetime) -> List[str]:
    """One page into the waiting room — names, then the scrobbles no record
    here already holds — and the walk's position after it (`oldest`: the
    page's oldest scrobble). Returns the name keys that received scrobbles."""
    keyed = [dict(i, name_key=name_key(i["artist"])) for i in items]
    names = sorted({i["name_key"] for i in keyed})
    if names:
        execute_values(cur, "INSERT INTO pending_scrobble_artists (name_key) VALUES %s "
                            "ON CONFLICT DO NOTHING", [(n,) for n in names])
        rows = execute_values(cur, f"""
            INSERT INTO pending_scrobbles (played_at, artist, title, album, name_key,
                                           artist_mbid, album_mbid, track_mbid)
            SELECT v.played_at, v.artist, v.title, v.album, v.name_key,
                   v.artist_mbid, v.album_mbid, v.track_mbid
              FROM (VALUES %s) AS v(played_at, artist, title, album, name_key,
                                    artist_mbid, album_mbid, track_mbid)
             WHERE NOT EXISTS (SELECT 1 FROM listening_history h
                                WHERE {same_listen('h', 'v.played_at')})
            ON CONFLICT DO NOTHING
            RETURNING name_key""",
            [(i["played_at"], i["artist"], i["title"], i["album"], i["name_key"],
              i["artist_mbid"], i["album_mbid"], i["track_mbid"]) for i in keyed],
            template="(%s::timestamptz, %s, %s, %s, %s, %s::uuid, %s::uuid, %s::uuid)",
            fetch=True)
        touched = sorted({r[0] for r in rows})
        if touched:
            cur.execute("UPDATE pending_scrobble_artists SET touched_at = now() "
                        "WHERE name_key = ANY(%s)", (touched,))
    else:
        touched = []
    # Next page: below this one's oldest second, re-reading that second (the
    # natural key absorbs it — a second can hold two scrobbles, split by the
    # page) unless the whole page sat in it. The re-read is not counted twice.
    walk["fetched"] += sum(1 for i in items if i["played_at"] != walk.get("reread"))
    cursor = oldest + timedelta(seconds=1)
    walk["cursor"], walk["reread"] = (cursor, oldest) if cursor < before else (oldest, None)
    cur.execute("""UPDATE lastfm_import SET walk_cursor_at = %s, walk_fetched = %s, walk_total = %s
                    WHERE username = %s""", (walk["cursor"], walk["fetched"], walk["total"], user))
    return touched


def _close_walk(cur, user: str) -> None:
    cur.execute("""UPDATE lastfm_import
                      SET watermark_at = walk_top_at, walk_top_at = NULL, walk_cursor_at = NULL,
                          last_sync_at = now(), last_error = NULL
                    WHERE username = %s""", (user,))


def _pending_rows() -> int:
    return int(db_query_one("SELECT count(*) AS n FROM pending_scrobbles")["n"])


def _run(user: str, reason: str) -> None:
    from api_cooldown import cooling_down
    from lastfm import LastFmService, SourceUnavailable
    pages = 0
    try:
        if cooling_down('lastfm'):
            _set(paused="Last.fm is rate-limiting this node — Sync again later.")
            return
        with transaction() as cur:
            walk = _open_walk(cur, user)
        svc = LastFmService(session_key=settings.lastfm_session_key)
        after = walk["watermark"] - LATE_SCROBBLE_WINDOW if walk["watermark"] else None
        logger.info("Last.fm history: walk for %s (%s) from %s", user, reason,
                    (walk["cursor"] or walk["top"]).isoformat())
        _set(phase="fetching")
        while not _cancel.is_set():
            if _pending_rows() >= PENDING_ROW_BUDGET:
                _set(paused="Waiting for the catalogue to place the scrobbles already fetched.",
                     budget=True)
                break
            before = walk["cursor"] or walk["top"] + timedelta(seconds=1)
            page = svc.recent_tracks_page(settings.lastfm_username, before, after)
            if walk["total"] is None:
                walk["total"] = page["total"]
            items = [i for i in page["items"] if i["played_at"] <= walk["top"]]
            oldest = min((i["played_at"] for i in page["items"]), default=before)
            with transaction() as cur:
                touched = _store_page(cur, user, items, walk, before, oldest)
                done = len(page["items"]) < PAGE_SIZE
                if done:
                    _close_walk(cur, user)
            pages += 1
            if touched:
                from canon import scrobbles
                scrobbles.wake(names=touched)
            if pages == 1 or pages % _MB_WAKE_EVERY == 0 or done:
                db_execute("NOTIFY sautium_mb_pending")
            total = max(walk["total"] or 0, walk["fetched"])
            _set(progress=f"{walk['fetched']:,} of {total:,} scrobbles read",
                 pct=int(walk["fetched"] * 100 / total) if total else None)
            if done:
                logger.info("Last.fm history: walk for %s complete — %d scrobbles read in %d pages",
                            user, walk["fetched"], pages)
                _set(phase="done")
                break
    except SourceUnavailable as e:
        logger.info("Last.fm history: source unavailable after %d pages (%s) — the walk resumes "
                    "on the next sync", pages, e)
        _set(paused="Last.fm is not answering — Sync again later; the import resumes where it stopped.")
    except pylast.WSError as e:
        message = ("Your Last.fm profile hides its recent listening — allow it in Last.fm's "
                   "privacy settings, then Sync again." if str(e.status) == _PRIVATE_PROFILE
                   else f"Last.fm refused the history: {e}")
        logger.warning("Last.fm history: %s", message)
        db_execute("UPDATE lastfm_import SET last_error = %s WHERE username = %s", (message, user))
        _set(error=message)
    except Exception as e:
        logger.error("Last.fm history walk failed: %s", e, exc_info=True)
        _set(error=f"The import failed: {e}")
    finally:
        _state["running"] = False
        if _state["phase"] not in ("done",):
            _state["phase"] = "idle"
        _notify()


def remove_imported() -> Dict[str, int]:
    """The owner's explicit "Remove imported history": stop the walk, then
    delete every imported listen and generated card, the waiting room and the
    cursor. Native plays are untouched; a later Sync brings the history back."""
    _cancel.set()
    if _thread is not None:
        _thread.join(timeout=120)
    with transaction() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (LISTENS_LOCK_KEY,))
        cur.execute("DELETE FROM listening_sessions WHERE source = 'lastfm'")
        sessions = cur.rowcount
        cur.execute("DELETE FROM listening_history WHERE source = 'lastfm' RETURNING track_id::text")
        deleted = [r[0] for r in cur.fetchall()]
        tracks = sorted(set(deleted))
        refresh_play_stats(cur, tracks)
        cur.execute("DELETE FROM pending_scrobbles")
        waiting = cur.rowcount
        cur.execute("DELETE FROM pending_scrobble_artists")
        cur.execute("DELETE FROM lastfm_import")
    logger.info("Last.fm history removed: %d listens over %d tracks, %d cards, %d waiting",
                len(deleted), len(tracks), sessions, waiting)
    _set(phase="idle", progress="", pct=None, error=None, paused=None)
    return {"listens": len(deleted), "sessions": sessions, "waiting": waiting}
