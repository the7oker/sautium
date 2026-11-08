"""The queue of MusicBrainz questions, as both runtimes write it and read its
answers (the tables are backend/mb_answers.py's; its consumer asks them).

The launcher's peer surface queues the checks of slices it is asked to pass on
and the backend's consumer answers them — one SQL for both, so the two never
disagree on what "queued" or "answered" means, nor on which node asks at all
(`node_asks`).
"""

from datetime import timedelta
from typing import Iterable, Optional, Tuple

FRESH = timedelta(days=30)
CHANNEL = "sautium_mbws"
# A person's own act (click-to-mint, an opened page): asked now, whatever
# backoff an earlier failure earned the item.
PRIORITY_PERSON = 1
DUMP_MARKER = "musicbrainz.db_version"

DUMP_HELD_SQL = f"SELECT 1 AS held FROM user_settings WHERE key = '{DUMP_MARKER}'"
# The slices' own ledger, never the catalogue tables: a rebuild or a full load
# rewrites those, and a reader of them would wait it out — the peer surface's
# health among them.
NODE_ASKS_SQL = f"""SELECT EXISTS (SELECT 1 FROM mb_slice_fetches WHERE matched_ids > 0)
                       AND NOT EXISTS ({DUMP_HELD_SQL})"""

# The author a queued question is counted under, when its caller names none:
# the newest kept MusicBrainz slice whose subjects carry the MBID — its artists
# for an artist or a browse, its groups for a group. Releases and recordings
# are counted under their caller's author or none.
_AUTHOR_OF = """
    SELECT btrim(si.author) FROM slice_index si
      JOIN mb_slice_blobs b ON b.name_key = si.key AND btrim(b.author_pubkey) = btrim(si.author)
     WHERE si.family = 'mb' AND NOT b.suspect AND si.{column} @> ARRAY[v.mbid::uuid]
     ORDER BY si.imported_at DESC LIMIT 1"""


def node_asks(conn) -> bool:
    """This node asks MusicBrainz, and so it alone checks another author's
    slice before passing it on: it took catalogue slices (a non-empty one is
    in its ledger) and holds no completed full dump. A node with the dump
    serves only its own builds; one with no catalogue has nothing to check a
    slice against. The backend's consumer reads the same rule through its
    cached catalogue flag (backend/mb_answers.required)."""
    with conn.cursor() as cur:
        cur.execute(NODE_ASKS_SQL)
        return bool(cur.fetchone()[0])


def enqueue(conn, items: Iterable[Tuple[str, str]], priority: int,
            subject: Optional[str] = None, key: Optional[str] = None,
            author: Optional[str] = None) -> None:
    """Queue (kind, mbid) items at `priority` — an item queued already keeps the
    more urgent of the two and its first author — with the subject that waits
    on them, and wake the consumer. In the caller's transaction."""
    from psycopg2.extras import execute_values
    items = sorted({(k, str(m)) for k, m in items})
    if not items:
        return
    with conn.cursor() as cur:
        execute_values(cur, f"""
            INSERT INTO mbws_queue (kind, mbid, priority, author)
            SELECT v.kind::mbws_kind, v.mbid::uuid, v.priority,
                   coalesce(v.author,
                            CASE WHEN v.kind IN ('artist', 'artist_release_groups')
                                 THEN ({_AUTHOR_OF.format(column="subject_artists")})
                                 WHEN v.kind = 'release_group'
                                 THEN ({_AUTHOR_OF.format(column="subject_groups")})
                            END)
              FROM (VALUES %s) AS v(kind, mbid, priority, author)
            ON CONFLICT (kind, mbid) DO UPDATE
               SET priority = LEAST(mbws_queue.priority, EXCLUDED.priority),
                   author = COALESCE(mbws_queue.author, EXCLUDED.author),
                   -- A more urgent ask (a person's own act) is asked now, not
                   -- after a backoff an earlier failure earned.
                   not_before = CASE WHEN EXCLUDED.priority < mbws_queue.priority
                                       OR EXCLUDED.priority <= {PRIORITY_PERSON}
                                     THEN NULL ELSE mbws_queue.not_before END""",
                       [(k, m, priority, author) for k, m in items])
        if subject is not None:
            execute_values(cur, """
                INSERT INTO mbws_waiters (kind, mbid, subject, subject_key)
                SELECT v.kind::mbws_kind, v.mbid::uuid, v.subject::mbws_subject, v.subject_key
                  FROM (VALUES %s) AS v(kind, mbid, subject, subject_key)
                ON CONFLICT DO NOTHING""", [(k, m, subject, key) for k, m in items])
        cur.execute(f"NOTIFY {CHANNEL}")


def answer(conn, kind: str, mbid: str) -> Optional[Tuple[str, Optional[str]]]:
    """(status, target) of a fresh answer, or None."""
    with conn.cursor() as cur:
        cur.execute("""SELECT status::text, target::text FROM mbws_answers
                        WHERE kind = %s AND mbid = %s::uuid AND answered_at > now() - %s""",
                    (kind, str(mbid), FRESH))
        row = cur.fetchone()
    return (row[0], row[1]) if row else None
