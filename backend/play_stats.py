"""`local_play_stats` is a derived table: every column is a function of the
track's `listening_history` rows, and this is the one statement that says
which. The play tracker runs it for the track it just recorded; the life-data
merge (backend/life_merge.py) runs it for every track whose history it added.
Counters are never incremented in place — an incremental upsert drifted from
the history it summarised (measured 2026-09-14: 567 of 2,205 tracks carried a
skip's seconds as listening time because the first row for a track was a
skip), and two nodes' counters cannot be merged at all, while two histories
can.

play_count / total_listen_time / avg_percent_listened / last_played_at
describe COMPLETED listens (the scrobble rule — a play that reached its end);
skip_count is every other row.

The module also owns the repair of listen starts written before 2026-09-24
(`repaired_start`): the history's own facts, read by the startup migration and
by the merge of an old backup.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from psycopg2.extras import execute_values

# A pre-fix row took its start from the process clock and its end from
# PostgreSQL's; the two disagree by up to ~2 s on the master. Past this slack a
# start after the end is not skew but the shift below.
_CLOCK_SLACK = timedelta(seconds=5)

PLAY_STATS_SQL = """
    INSERT INTO local_play_stats (track_id, play_count, skip_count, total_listen_time,
                                  avg_percent_listened, last_played_at, updated_at)
    SELECT track_id,
           count(*) FILTER (WHERE completed),
           count(*) FILTER (WHERE NOT completed),
           COALESCE(sum(duration_listened) FILTER (WHERE completed), 0),
           COALESCE(avg(percent_listened) FILTER (WHERE completed), 0),
           max(COALESCE(ended_at, started_at)) FILTER (WHERE completed),
           now()
      FROM listening_history
     WHERE track_id = ANY(CAST(%(ids)s AS uuid[]))
     GROUP BY track_id
    ON CONFLICT (track_id) DO UPDATE SET
           play_count = EXCLUDED.play_count,
           skip_count = EXCLUDED.skip_count,
           total_listen_time = EXCLUDED.total_listen_time,
           avg_percent_listened = EXCLUDED.avg_percent_listened,
           last_played_at = EXCLUDED.last_played_at,
           updated_at = now()
"""


def refresh_play_stats(cur, track_ids) -> int:
    """Recompute the stats rows of `track_ids` from their history on an open
    cursor; returns how many rows were written. A track whose history is gone
    loses its row too — a stats row with nothing under it would keep counting
    listens that no longer exist (and keep an orphan track alive)."""
    ids = [str(t) for t in track_ids]
    if not ids:
        return 0
    cur.execute(PLAY_STATS_SQL, {"ids": ids})
    written = cur.rowcount
    cur.execute("""
        DELETE FROM local_play_stats lp
         WHERE lp.track_id = ANY(CAST(%(ids)s AS uuid[]))
           AND NOT EXISTS (SELECT 1 FROM listening_history h WHERE h.track_id = lp.track_id)
    """, {"ids": ids})
    return written


def repaired_start(started_at: datetime, ended_at: Optional[datetime],
                   duration_listened) -> Optional[datetime]:
    """The instant a listen written before 2026-09-24 began, or None when the
    stored start stands.

    The tracker stamped the start with the process's NAIVE local clock and
    wrote it through a UTC session, so on a launcher the stored UTC reading is
    the local wall clock: a listen begun at 16:08 in Kyiv was stored as 19:08
    UTC — three hours late, after its own end. Reading that wall clock back in
    this process's zone gives the instant it meant; on Docker (UTC) the two
    readings coincide. A row merged from another node was written in THAT
    node's zone, so the reading is taken only where it fits the row better: a
    start after the end is impossible, and otherwise the start whose distance
    to the end better matches the seconds listened wins. The miss this leaves
    is a west-of-UTC row that was already right and sat paused for hours."""
    if ended_at is None:
        return None
    local = started_at.astimezone(timezone.utc).replace(tzinfo=None).astimezone(timezone.utc)
    if local == started_at or local > ended_at + _CLOCK_SLACK:
        return None
    if started_at > ended_at + _CLOCK_SLACK:
        return local
    listened = float(duration_listened or 0)

    def misfit(start: datetime) -> float:
        return abs((ended_at - start).total_seconds() - listened)

    return local if misfit(local) < misfit(started_at) else None


def repair_naive_listen_times(cur, table: str = "listening_history") -> List[str]:
    """Rewrite the shifted starts of `table` — the history, or the life merge's
    scratch copy of one — on an open cursor; returns the track ids touched.
    The decision needs the process's zone rules, which only Python has (a
    Windows zone has no IANA name to hand PostgreSQL), and it runs once."""
    cur.execute(f"SELECT id, track_id::text, started_at, ended_at, duration_listened FROM {table}")
    fixes, tracks = [], set()
    for row_id, track_id, started_at, ended_at, listened in cur.fetchall():
        start = repaired_start(started_at, ended_at, listened)
        if start is not None:
            fixes.append((row_id, start))
            tracks.add(track_id)
    if fixes:
        execute_values(cur, f"UPDATE {table} h SET started_at = v.started_at "
                            "FROM (VALUES %s) AS v(id, started_at) WHERE h.id = v.id",
                       fixes, template="(%s, %s::timestamptz)", page_size=500)
    return sorted(tracks)
