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
from datetime import timedelta
from itertools import groupby
from typing import Optional

from psycopg2.extras import execute_values

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


# The line under a mix card: its artists by share, top three, "+N" for the
# rest. Window functions over the grouped rows rank and count in one pass.
_ARTIST_LINE_SQL = """
    SELECT string_agg(name, ', ' ORDER BY rn) FILTER (WHERE rn <= 3)
           || CASE WHEN max(total) > 3 THEN ' +' || (max(total) - 3) ELSE '' END AS line
    FROM (
        SELECT a.name,
               row_number() OVER (ORDER BY count(*) DESC, min(st.position)) AS rn,
               count(*) OVER () AS total
        FROM session_tracks st
        JOIN track_artists ta ON ta.track_id = st.track_id AND ta.role = 'primary'
        JOIN artists a ON a.id = ta.artist_id
        WHERE st.session_id = %s::uuid
        GROUP BY a.id, a.name
    ) ranked
"""


def _queue_slots(queue) -> list:
    """(track_id, media_file_id, album_id) slots from the canonical queue —
    phantom (streamed) tracks carry a track UUID with media_file_id None, so
    they land in the session just like owned files. album_id is the album the
    slot was queued from (QueueItem.album_id): a track sits on every album
    that lists it, and the snapshot keeps the edition the listener pressed."""
    return [(it.track_id, it.media_file_id, it.album_id)
            for it in queue.snapshot() if it.track_id]


def rotate_session(
    queue,
    origin: str,
    *,
    seed_track_id: Optional[str] = None,
    seed_media_file_id: Optional[int] = None,
    origin_album_id: Optional[str] = None,
    title: Optional[str] = None,
) -> None:
    """Archive the live queue as a session snapshot, then open a new active
    session. Called at the TOP of every destructive play endpoint, BEFORE
    the queue is replaced, so the OLD queue is captured intact. Owned play
    endpoints pass only seed_media_file_id; _archive_and_open_session
    derives seed_track_id from it. `title` is a name the new session keeps —
    a replayed mix's, so the AI does not christen the same tracks anew."""
    archived_mix_id = _archive_and_open_session(
        _queue_slots(queue), origin,
        seed_track_id, seed_media_file_id, origin_album_id, title=title,
    )
    if archived_mix_id is not None:
        _schedule_mix_title(archived_mix_id)


def close_active_session(queue) -> None:
    """Archive the active session on a natural end-of-queue (the player
    stopped on the last track) WITHOUT opening a new one, so a fully-
    listened album lands in history without a follow-up play."""
    archived_mix_id = _archive_and_open_session(
        _queue_slots(queue), "mix", None, None, None, open_new=False,
    )
    if archived_mix_id is not None:
        _schedule_mix_title(archived_mix_id)


def _archive_and_open_session(
    old_slots: list[tuple[str, Optional[int], Optional[str]]],
    origin: str,
    seed_track_id: Optional[str],
    seed_media_file_id: Optional[int],
    origin_album_id: Optional[str],
    *,
    title: Optional[str] = None,
    open_new: bool = True,
) -> Optional[str]:
    """Archive the current active session against `old_slots` (track_id,
    media_file_id, album_id) and open a new one — atomically. db_pool connections are
    autocommit, so toggle it off for this multi-statement transaction (mirrors
    db_pool.db_query_with_ef_search). With open_new=False (end-of-queue
    completion) the active session is archived but no new one is opened.
    Returns the archived session id IFF it was a mix needing a background
    title (one opened without a name of its own), else None."""
    import psycopg2.extras

    archived_mix_id: Optional[str] = None
    with _get_conn() as conn:
        conn.autocommit = False
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Owned play endpoints pass only the seed's media_file_id; the
                # session keys on the logical track UUID, so derive it here (one
                # query) — keeps every owned call-site untouched. Phantom callers
                # pass seed_track_id directly (they have no media_file). A track
                # session's origin album is the file's album the same way — the
                # edition the row was played from, so the card's cover is that
                # album's and not the first one that happens to list the track.
                if seed_media_file_id is not None and (
                        seed_track_id is None
                        or (origin == "track" and origin_album_id is None)):
                    cur.execute(
                        "SELECT mf.track_id::text AS tid, av.album_id::text AS aid "
                        "FROM media_files mf "
                        "LEFT JOIN album_variants av ON av.id = mf.album_variant_id "
                        "WHERE mf.id = %s",
                        (seed_media_file_id,))
                    r = cur.fetchone()
                    if r:
                        seed_track_id = seed_track_id or r["tid"]
                        if origin == "track" and origin_album_id is None:
                            origin_album_id = r["aid"]

                cur.execute(
                    "SELECT id::text AS id, origin, title, "
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
                    snapshot = _snapshot_slots_for(cur, active, old_slots)
                    if snapshot:
                        psycopg2.extras.execute_values(
                            cur,
                            "INSERT INTO session_tracks "
                            "(session_id, position, track_id, media_file_id, album_id) "
                            "VALUES %s",
                            [(active["id"], i, tid, mid, aid)
                             for i, (tid, mid, aid) in enumerate(snapshot)],
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
                        if active["origin"] == "mix" and not active["title"]:
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
                        "(origin, title, seed_track_id, seed_media_file_id, origin_album_id) "
                        "VALUES (%s, %s, %s::uuid, %s, %s::uuid)",
                        (origin, title, seed_track_id, seed_media_file_id, origin_album_id),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True
    return archived_mix_id


def _snapshot_slots_for(cur, active: dict,
                        old_slots: list[tuple[str, Optional[int], Optional[str]]]
                        ) -> list[tuple[str, Optional[int], Optional[str]]]:
    """Resolve which (track_id, media_file_id, album_id) slots to snapshot
    into the archived session.

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
    tracks carry media_file_id None. Either fallback's slots belong to the
    origin album by construction."""
    origin = active["origin"]
    old_tids = {tid for tid, _mid, _aid in old_slots}
    album_id = active["origin_album_id"]

    if origin == "album" and album_id:
        cur.execute(
            "SELECT t.id::text AS track_id, mf.id AS media_file_id "
            "FROM album_tracks atk "
            "JOIN tracks t ON t.id = atk.track_id "
            "LEFT JOIN media_files mf ON mf.track_id = t.id "
            "WHERE atk.album_id = %s::uuid "
            "ORDER BY atk.disc, atk.position",
            (album_id,),
        )
        album_slots = [(r["track_id"], r["media_file_id"], album_id) for r in cur.fetchall()]
        album_tids = {tid for tid, _mid, _aid in album_slots}
        if album_tids and not (old_tids & album_tids):
            return album_slots
        return old_slots

    if origin == "track" and active["seed_track_id"]:
        # A single-track session is exactly its seed; don't let an out-of-band
        # queue swap put a foreign track in it.
        if active["seed_track_id"] not in old_tids:
            return [(active["seed_track_id"], active.get("seed_media_file_id"), album_id)]
        return old_slots

    return old_slots


def _cover_for_track(cur, track_id: Optional[str], media_file_id: Optional[int],
                     album_id: Optional[str] = None
                     ) -> tuple[Optional[str], Optional[str]]:
    """(cover_id, cover_url) for a snapshot track — owned art is a covers(id)
    via media_files; a phantom track's art is its album's CAA cover_url. The
    session card renders cover_id (owned) OR cover_url (phantom). `album_id`
    is the session's origin album: a phantom track sits on every album that
    lists it, so its art is that album's, not the first cover-bearing one."""
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
            "WHERE atk.track_id = %s::uuid AND al.cover_url IS NOT NULL "
            "ORDER BY (al.id = %s::uuid) DESC NULLS LAST, al.id LIMIT 1",
            (track_id, album_id))
        r = cur.fetchone()
        if r:
            return (None, r["cover_url"])
    return (None, None)


def _compute_session_card(cur, active: dict,
                          snapshot: list[tuple[str, Optional[int], Optional[str]]]):
    """Title / subtitle / (cover_id, cover_url) for an archived session, derived
    from its origin (NOT the snapshot's first row — album/track/radio titles
    come from the stored origin columns; a mix takes its artists from the
    snapshot). Cover is source-agnostic via _cover_for_track, always with the
    album the slot was queued from. Uses the cursor already inside the archive
    transaction."""
    origin = active["origin"]
    first = snapshot[0] if snapshot else (None, None, None)

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
        cover_id, cover_url = _cover_for_track(cur, first[0], first[1],
                                               active["origin_album_id"])
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
            cur, active["seed_track_id"], active.get("seed_media_file_id"),
            active["origin_album_id"])
        if not cover_id and not cover_url:   # seed missing → fall back to the first row
            cover_id, cover_url = _cover_for_track(cur, first[0], first[1], first[2])
        return (title, subtitle, cover_id, cover_url)

    # mix — or album/track/radio with a NULL seed (degrade gracefully). The
    # title is the one the session was opened with (a replay keeps its
    # name), else the AI's to fill in (_schedule_mix_title); the line under
    # it names the artists, the way an album card names its artist.
    cur.execute(_ARTIST_LINE_SQL, (active["id"],))
    r = cur.fetchone()
    n = len(snapshot)
    subtitle = (r["line"] if r and r["line"] else None) or f"{n} track{'s' if n != 1 else ''}"
    cover_id, cover_url = _cover_for_track(cur, first[0], first[1], first[2])
    return (active["title"] or "Mix", subtitle, cover_id, cover_url)


def _schedule_mix_title(session_id: str) -> None:
    """Generate an AI title for an archived mix in a background daemon thread
    (the codebase's background-work idiom — chat-stream-worker, status poller).
    Graceful no-op when no AI provider is configured: the card stays 'Mix'."""
    def _worker():
        try:
            from routers.chat import (
                _resolve_provider, _title_via_provider,
                _title_via_claude_code, _title_via_codex, _clean_title,
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
            # Same dispatch as the chat-session titler: the two CLI providers
            # have their own one-shot subprocess paths (BaseProvider.chat() is
            # a sentinel there), everything else goes through the SDK.
            if provider == "claude_code":
                title = _title_via_claude_code(prompt)
            elif provider == "codex":
                title = _title_via_codex(prompt)
            else:
                title = _title_via_provider(provider, prompt)
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


# --------------------------------------------------------------------------
# Cards from imported listens (the Last.fm history)
# --------------------------------------------------------------------------

# A listen that starts more than this after the previous one ended begins a
# new sitting; three consecutive listens off one album make it an album card.
_RUN_GAP_MIN = 30
_ALBUM_BLOCK = 3
_IMPORTED_WINDOW_DAYS = 30


def rebuild_imported_sessions(cur) -> int:
    """The Listening-history cards of the imported listens, rebuilt with them —
    a projection of `listening_history` rows with source 'lastfm', re-derivable
    like local_play_stats, so replacing them removes nothing the owner did.
    Covers the 30 days before the newest imported listen (an account that went
    quiet still yields cards); older generated cards stay as they were.

    A sitting is a run of listens with no gap over 30 minutes. Inside it,
    three or more consecutive listens sharing an album become that album's
    card (the owned edition, else the smallest album listing them all — the
    record, not a compilation); the rest become a mix, or a track card when
    alone. Card fields come from _compute_session_card; a mix is titled
    "Mix" (no model call per imported card). `cur` is a RealDictCursor inside
    the caller's transaction, which holds the listens lock."""
    import uuid
    from uuid_utils import NAMESPACE
    cur.execute("SELECT max(started_at) AS newest FROM listening_history WHERE source = 'lastfm'")
    newest = cur.fetchone()["newest"]
    if newest is None:
        cur.execute("DELETE FROM listening_sessions WHERE source = 'lastfm'")
        return 0
    since = newest - timedelta(days=_IMPORTED_WINDOW_DAYS)
    cur.execute("DELETE FROM listening_sessions WHERE source = 'lastfm' AND started_at >= %s",
                (since,))
    cur.execute(f"""
        SELECT track_id, media_file_id, started_at, ended_at, albums,
               sum(gap) OVER (ORDER BY started_at) AS sitting
          FROM (SELECT lh.track_id::text AS track_id, lh.media_file_id, lh.started_at, lh.ended_at,
                       CASE WHEN lh.started_at - lag(lh.ended_at) OVER (ORDER BY lh.started_at)
                                 > interval '{_RUN_GAP_MIN} minutes' THEN 1 ELSE 0 END AS gap,
                       ARRAY(SELECT at.album_id::text FROM album_tracks at
                              WHERE at.track_id = lh.track_id
                             UNION
                             SELECT av.album_id::text FROM media_files mf
                               JOIN album_variants av ON av.id = mf.album_variant_id
                              WHERE mf.track_id = lh.track_id) AS albums
                  FROM listening_history lh
                 WHERE lh.source = 'lastfm' AND lh.started_at >= %s) x
         ORDER BY started_at""", (since,))
    listens = cur.fetchall()
    album_ids = sorted({a for l in listens for a in l["albums"]})
    cur.execute("""
        SELECT al.id::text AS id,
               EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = al.id) AS owned,
               (SELECT count(*) FROM album_tracks at WHERE at.album_id = al.id) AS slots
          FROM albums al WHERE al.id = ANY(CAST(%s AS uuid[]))""", (album_ids,))
    rank = {r["id"]: (not r["owned"], r["slots"], r["id"]) for r in cur.fetchall()}

    def best(albums) -> Optional[str]:
        return min(albums, key=lambda a: rank.get(a, (True, 0, a))) if albums else None

    cards = []
    for _, sitting in groupby(listens, key=lambda l: l["sitting"]):
        sitting, loose = list(sitting), []
        i = 0
        while i < len(sitting):
            shared, j = set(sitting[i]["albums"]), i + 1
            while j < len(sitting) and shared & set(sitting[j]["albums"]):
                shared &= set(sitting[j]["albums"])
                j += 1
            if j - i >= _ALBUM_BLOCK:
                if loose:
                    cards.append(("mix", None, loose))
                    loose = []
                cards.append(("album", best(shared), sitting[i:j]))
                i = j
            else:
                loose.append(sitting[i])
                i += 1
        if loose:
            cards.append(("mix", None, loose))

    for origin, album_id, block in cards:
        if origin == "mix" and len(block) == 1:
            origin = "track"
        first = block[0]
        sid = str(uuid.uuid5(NAMESPACE, f"listening_session:lastfm:{int(first['started_at'].timestamp())}"))
        snapshot = [(l["track_id"], l["media_file_id"], album_id or best(l["albums"])) for l in block]
        active = {"id": sid, "origin": origin, "origin_album_id": album_id,
                  "seed_track_id": first["track_id"], "seed_media_file_id": first["media_file_id"],
                  "title": "Mix" if origin == "mix" else None}
        cur.execute("""
            INSERT INTO listening_sessions (id, origin, origin_album_id, seed_track_id,
                                            seed_media_file_id, track_count, started_at,
                                            ended_at, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'lastfm')""",
            (sid, origin, album_id, first["track_id"], first["media_file_id"], len(block),
             first["started_at"], block[-1]["ended_at"]))
        execute_values(cur, """INSERT INTO session_tracks (session_id, position, track_id,
                                                           media_file_id, album_id) VALUES %s""",
                       [(sid, n, t, mf, a) for n, (t, mf, a) in enumerate(snapshot)],
                       template="(%s::uuid, %s, %s::uuid, %s, %s::uuid)")
        title, subtitle, cover_id, cover_url = _compute_session_card(cur, active, snapshot)
        cur.execute("""UPDATE listening_sessions
                          SET title = %s, subtitle = %s, cover_id = %s, cover_url = %s
                        WHERE id = %s""", (title, subtitle, cover_id, cover_url, sid))
    return len(cards)
