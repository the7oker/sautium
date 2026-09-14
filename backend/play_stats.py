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
"""

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
    cursor; returns how many rows were written."""
    ids = [str(t) for t in track_ids]
    if not ids:
        return 0
    cur.execute(PLAY_STATS_SQL, {"ids": ids})
    return cur.rowcount
