"""
Listening sessions (queue-lifetime snapshots) — the archival core.

Each destructive play archives the live queue as an immutable snapshot and
opens a new active session. Snapshots are captured at the instant the queue
is replaced; `origin` (how the queue started) is the one fact the player
itself doesn't know, so it rides on the active-session marker. Reading the
live queue is the caller's job (routers/player.py) — this module owns the
transactional archive/open logic and the session card derivation.
"""

import logging
import threading
from typing import Optional

from db_pool import (
    db_query as _db_query,
    db_execute as _db_execute,
    get_conn as _get_conn,
)

logger = logging.getLogger(__name__)

_SESSION_ORIGINS = ("album", "track", "radio", "mix")
# Idempotent-replay window: a destructive play of the same origin within this
# many seconds of the active session opening is treated as a duplicate (double-
# tapped "Play all", a retried request) and skipped. Beyond it, replaying the
# same album/track is a genuinely new listen and opens a fresh session — so
# re-playing a track minutes later isn't swallowed into the stale one.
_SESSION_DEDUP_WINDOW_SEC = 30


def _queue_pairs(queue) -> list:
    """(track_id, media_file_id) pairs from the canonical queue — phantom
    (streamed) tracks carry a track UUID with media_file_id None, so they
    land in the session just like owned files."""
    return [(it.track_id, it.media_file_id)
            for it in queue.snapshot() if it.track_id]


def rotate_session(
    queue,
    origin: str,
    *,
    seed_track_id: Optional[str] = None,
    seed_media_file_id: Optional[int] = None,
    origin_album_id: Optional[str] = None,
) -> None:
    """Archive the live queue as a session snapshot, then open a new active
    session. Called at the TOP of every destructive play endpoint, BEFORE
    the queue is replaced, so the OLD queue is captured intact. Owned play
    endpoints pass only seed_media_file_id; _archive_and_open_session
    derives seed_track_id from it."""
    archived_mix_id = _archive_and_open_session(
        _queue_pairs(queue), origin,
        seed_track_id, seed_media_file_id, origin_album_id,
    )
    if archived_mix_id is not None:
        _schedule_mix_title(archived_mix_id)


def close_active_session(queue) -> None:
    """Archive the active session on a natural end-of-queue (the player
    stopped on the last track) WITHOUT opening a new one, so a fully-
    listened album lands in history without a follow-up play."""
    archived_mix_id = _archive_and_open_session(
        _queue_pairs(queue), "mix", None, None, None, open_new=False,
    )
    if archived_mix_id is not None:
        _schedule_mix_title(archived_mix_id)


def _archive_and_open_session(
    old_pairs: list[tuple[str, Optional[int]]],
    origin: str,
    seed_track_id: Optional[str],
    seed_media_file_id: Optional[int],
    origin_album_id: Optional[str],
    *,
    open_new: bool = True,
) -> Optional[str]:
    """Archive the current active session against `old_pairs` (track_id,
    media_file_id) and open a new one — atomically. db_pool connections are
    autocommit, so toggle it off for this multi-statement transaction (mirrors
    db_pool.db_query_with_ef_search). With open_new=False (end-of-queue
    completion) the active session is archived but no new one is opened.
    Returns the archived session id IFF it was a mix needing a background
    title, else None."""
    import psycopg2.extras

    archived_mix_id: Optional[str] = None
    with _get_conn() as conn:
        conn.autocommit = False
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Owned play endpoints pass only the seed's media_file_id; the
                # session keys on the logical track UUID, so derive it here (one
                # query) — keeps every owned call-site untouched. Phantom callers
                # pass seed_track_id directly (they have no media_file).
                if seed_track_id is None and seed_media_file_id is not None:
                    cur.execute(
                        "SELECT track_id::text AS tid FROM media_files WHERE id = %s",
                        (seed_media_file_id,))
                    r = cur.fetchone()
                    seed_track_id = r["tid"] if r else None

                cur.execute(
                    "SELECT id::text AS id, origin, "
                    "origin_album_id::text AS origin_album_id, "
                    "seed_track_id::text AS seed_track_id, seed_media_file_id, "
                    "EXTRACT(EPOCH FROM (now() - started_at)) AS age_sec "
                    "FROM listening_sessions WHERE ended_at IS NULL FOR UPDATE"
                )
                active = cur.fetchone()

                # Idempotent re-play: a repeated destructive play of the same
                # thing within _SESSION_DEDUP_WINDOW_SEC (double-tapped "Play
                # all", a retried/duplicated request) must NOT archive the
                # just-opened session against the still-old queue and spawn a
                # duplicate. FOR UPDATE serialises concurrent calls so the
                # second sees the first's freshly-inserted active row. The
                # time window keeps this to genuine duplicates: re-playing the
                # same album/track minutes later is a new listen and opens a
                # fresh session instead of being swallowed into the stale one.
                if open_new and active is not None and (
                    active["age_sec"] is not None
                    and active["age_sec"] < _SESSION_DEDUP_WINDOW_SEC
                    and active["origin"] == origin
                    and active["origin_album_id"] == origin_album_id
                    and active["seed_track_id"] == seed_track_id
                ):
                    conn.commit()
                    return None

                if active is not None:
                    snapshot = _snapshot_pairs_for(cur, active, old_pairs)
                    if snapshot:
                        psycopg2.extras.execute_values(
                            cur,
                            "INSERT INTO session_tracks "
                            "(session_id, position, track_id, media_file_id) VALUES %s",
                            [(active["id"], i, tid, mid)
                             for i, (tid, mid) in enumerate(snapshot)],
                        )
                        title, subtitle, cover_id, cover_url = _compute_session_card(
                            cur, active, snapshot,
                        )
                        cur.execute(
                            "UPDATE listening_sessions SET ended_at = now(), "
                            "track_count = %s, title = %s, subtitle = %s, "
                            "cover_id = %s::uuid, cover_url = %s WHERE id = %s::uuid",
                            (len(snapshot), title, subtitle,
                             cover_id, cover_url, active["id"]),
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
                        "(origin, seed_track_id, seed_media_file_id, origin_album_id) "
                        "VALUES (%s, %s::uuid, %s, %s::uuid)",
                        (origin, seed_track_id, seed_media_file_id, origin_album_id),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True
    return archived_mix_id


def _snapshot_pairs_for(cur, active: dict, old_pairs: list[tuple[str, Optional[int]]]
                        ) -> list[tuple[str, Optional[int]]]:
    """Resolve which (track_id, media_file_id) pairs to snapshot into the
    archived session.

    For album/track sessions the content is deterministic from the origin, so
    guard against the live queue being replaced out-of-band (e.g. the player
    reopened with a different album, leaving a foreign queue): if the captured
    queue no longer overlaps the origin's own tracks, snapshot the origin's
    tracks so the card's cover/tracks match its title. A queue that still
    overlaps is trusted — it reflects in-app edits (queued/removed tracks).
    radio/mix content is dynamic (similar picks / an explicit list) with no
    origin reference, so the captured queue is the only source.

    Album tracks come from the canonical album_tracks list (owned AND phantom
    albums), LEFT-joined to media_files for the physical id; a phantom album's
    tracks carry media_file_id None."""
    origin = active["origin"]
    old_tids = {tid for tid, _mid in old_pairs}

    if origin == "album" and active["origin_album_id"]:
        cur.execute(
            "SELECT t.id::text AS track_id, mf.id AS media_file_id "
            "FROM album_tracks atk "
            "JOIN tracks t ON t.id = atk.track_id "
            "LEFT JOIN media_files mf ON mf.track_id = t.id "
            "WHERE atk.album_id = %s::uuid "
            "ORDER BY atk.disc, atk.position",
            (active["origin_album_id"],),
        )
        album_pairs = [(r["track_id"], r["media_file_id"]) for r in cur.fetchall()]
        album_tids = {tid for tid, _mid in album_pairs}
        if album_tids and not (old_tids & album_tids):
            return album_pairs
        return old_pairs

    if origin == "track" and active["seed_track_id"]:
        # A single-track session is exactly its seed; don't let an out-of-band
        # queue swap put a foreign track in it.
        if active["seed_track_id"] not in old_tids:
            return [(active["seed_track_id"], active.get("seed_media_file_id"))]
        return old_pairs

    return old_pairs


def _cover_for_track(cur, track_id: Optional[str], media_file_id: Optional[int]
                     ) -> tuple[Optional[str], Optional[str]]:
    """(cover_id, cover_url) for a snapshot track — owned art is a covers(id)
    via media_files; a phantom track's art is its album's CAA cover_url. The
    session card renders cover_id (owned) OR cover_url (phantom)."""
    if media_file_id is not None:
        cur.execute("SELECT cover_id::text AS c FROM media_files WHERE id = %s",
                    (media_file_id,))
        r = cur.fetchone()
        if r and r["c"]:
            return (r["c"], None)
    if track_id is not None:
        cur.execute(
            "SELECT al.cover_url FROM album_tracks atk "
            "JOIN albums al ON al.id = atk.album_id "
            "WHERE atk.track_id = %s::uuid AND al.cover_url IS NOT NULL LIMIT 1",
            (track_id,))
        r = cur.fetchone()
        if r:
            return (None, r["cover_url"])
    return (None, None)


def _compute_session_card(cur, active: dict, snapshot: list[tuple[str, Optional[int]]]):
    """Title / subtitle / (cover_id, cover_url) for an archived session, derived
    from its origin (NOT the snapshot's first row — album/track/radio titles
    come from the stored origin columns; only mix uses the snapshot). Cover is
    source-agnostic via _cover_for_track. Per-row label formatting is the
    Python-side case the project allows; the lookups are SQL. Uses the cursor
    already inside the archive transaction."""
    origin = active["origin"]
    first = snapshot[0] if snapshot else (None, None)

    if origin == "album" and active["origin_album_id"]:
        # Source-agnostic primary artist: owned via media_files, phantom via the
        # canonical album_tracks list (COALESCE picks whichever the album has).
        cur.execute("""
            SELECT al.title,
                   COALESCE(
                     (SELECT a.name FROM artists a
                      JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
                      JOIN tracks t ON t.id = ta.track_id
                      JOIN media_files mf ON mf.track_id = t.id
                      JOIN album_variants av ON av.id = mf.album_variant_id
                      WHERE av.album_id = al.id
                      GROUP BY a.id, a.name ORDER BY COUNT(*) DESC LIMIT 1),
                     (SELECT a.name FROM artists a
                      JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
                      JOIN album_tracks atk ON atk.track_id = ta.track_id
                      WHERE atk.album_id = al.id
                      GROUP BY a.id, a.name ORDER BY COUNT(*) DESC LIMIT 1)
                   ) AS artist
            FROM albums al WHERE al.id = %s::uuid
        """, (active["origin_album_id"],))
        r = cur.fetchone()
        cover_id, cover_url = _cover_for_track(cur, first[0], first[1])
        return (
            r["title"] if r else "Album",
            r["artist"] if r else None,
            cover_id, cover_url,
        )

    if origin in ("track", "radio") and active["seed_track_id"]:
        cur.execute("""
            SELECT t.title, a.name AS artist
            FROM tracks t
            LEFT JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
            LEFT JOIN artists a ON a.id = ta.artist_id
            WHERE t.id = %s::uuid
            LIMIT 1
        """, (active["seed_track_id"],))
        r = cur.fetchone()
        title = r["title"] if r else "Track"
        subtitle = "Radio" if origin == "radio" else (r["artist"] if r else None)
        cover_id, cover_url = _cover_for_track(
            cur, active["seed_track_id"], active.get("seed_media_file_id"))
        if not cover_id and not cover_url:   # seed missing → fall back to the first row
            cover_id, cover_url = _cover_for_track(cur, first[0], first[1])
        return (title, subtitle, cover_id, cover_url)

    # mix — or album/track/radio with a NULL seed (degrade gracefully).
    n = len(snapshot)
    cover_id, cover_url = _cover_for_track(cur, first[0], first[1])
    return ("Mix", f"{n} track{'s' if n != 1 else ''}", cover_id, cover_url)


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
                JOIN tracks t ON t.id = st.track_id
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
