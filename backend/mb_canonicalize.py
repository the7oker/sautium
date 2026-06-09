"""Streaming MB canonicalization pass.

For each library artist — priority order, not yet MB-synced, with ≥1 owned
release — resolve on MusicBrainz, verify by owned-album overlap, then apply
the decision: rename/merge (via `recanonicalize_artist`, which merges
automatically when the canonical row already exists, so duplicates converge
without explicit pairing), split (via `normalize_compound_artist`), or stamp
`last_mb_sync` for an unsure result so it isn't re-queried.

Resumable (the `last_mb_sync` gate means a re-launch continues where it left
off) and cooldown-resilient. Paced by the 1 req/s MB client. Designed to run
detached for a long bulk pass; the same `canonicalize_one` can later feed an
incremental loop step for steady-state.

Priority tiers (Valerii): listened → enriched → similar-of-listened → rest by
file mtime.
"""

import logging
import time
from typing import Dict, Optional, Tuple

from sqlalchemy import text

import mb_audit
import mb_backend as mb
from database import get_db_context
from db_pool import db_query
from normalize_artists import (
    detect_compound_type,
    normalize_compound_artist,
    recanonicalize_artist,
)

logger = logging.getLogger(__name__)

_BATCH_SIZE = 50

_PRIORITY_SQL = """
    WITH owned AS (
        SELECT DISTINCT ta.artist_id
        FROM track_artists ta
        JOIN tracks t ON t.id = ta.track_id
        JOIN media_files mf ON mf.track_id = t.id
    ),
    listened AS (
        SELECT ta.artist_id, MAX(lh.started_at) AS last_listen
        FROM listening_history lh
        JOIN track_artists ta ON ta.track_id = lh.track_id
        GROUP BY ta.artist_id
    ),
    sim AS (
        SELECT DISTINCT sa.similar_artist_id AS aid
        FROM similar_artists sa
        JOIN listened l ON l.artist_id = sa.artist_id
    ),
    recency AS (
        SELECT ta.artist_id, MAX(mf.file_modified_at) AS newest
        FROM track_artists ta
        JOIN tracks t ON t.id = ta.track_id
        JOIN media_files mf ON mf.track_id = t.id
        GROUP BY ta.artist_id
    )
    SELECT a.id::text AS id, a.name,
           CASE
               WHEN l.artist_id IS NOT NULL THEN 1
               WHEN EXISTS (SELECT 1 FROM artist_bios ab WHERE ab.artist_id = a.id) THEN 2
               WHEN s.aid IS NOT NULL THEN 3
               ELSE 4
           END AS tier
    FROM artists a
    JOIN owned o ON o.artist_id = a.id
    LEFT JOIN listened l ON l.artist_id = a.id
    LEFT JOIN sim s ON s.aid = a.id
    LEFT JOIN recency r ON r.artist_id = a.id
    WHERE a.last_mb_sync IS NULL
    ORDER BY tier,
             l.last_listen DESC NULLS LAST,
             r.newest DESC NULLS LAST,
             a.name
    LIMIT %(batch)s
"""


def _select_batch(limit: int):
    return db_query(_PRIORITY_SQL, {"batch": int(limit)})


def _stamp(db, artist_id: str) -> None:
    db.execute(text("UPDATE artists SET last_mb_sync = now() WHERE id = CAST(:id AS uuid)"),
               {"id": str(artist_id)})


def canonicalize_one(artist_id: str, name: str) -> Dict:
    """Resolve + apply one artist. MB calls happen outside the DB session;
    the apply is a short transaction. Returns the audit decision."""
    # A prior merge in this run may have consumed this row already.
    if not db_query("SELECT 1 FROM artists WHERE id = CAST(%(id)s AS uuid)",
                    {"id": str(artist_id)}):
        return {"action": "skipped"}

    decision = mb_audit.audit_artist(artist_id, name)
    act = decision["action"]

    with get_db_context() as db:
        if act in ("keep", "rename"):
            recanonicalize_artist(db, artist_id, decision["canonical"])
        elif act == "split":
            ct = detect_compound_type(name)
            if ct:
                normalize_compound_artist(db, artist_id, name, ct[2], "verified_split")
            _stamp(db, artist_id)
        else:  # unsure — stamp so we don't re-query. A cooldown is a global
               # backoff (the artist wasn't really attempted) → leave it for a
               # retry; a per-artist mb-error IS stamped so the tail can't loop
               # forever on a persistently-failing artist.
            if "cooldown" not in decision.get("note", ""):
                _stamp(db, artist_id)
        db.commit()
    return decision


def run(limit: Optional[int] = None, batch_size: int = _BATCH_SIZE) -> Tuple[int, Dict]:
    """Process artists until the priority queue is empty (or `limit`)."""
    processed = 0
    counts: Dict[str, int] = {}
    logger.info(f"MB canonicalization pass starting (limit={limit})")
    while not limit or processed < limit:
        # Wait out an MB cooldown (bounded) before pulling more work.
        waited = 0
        while mb.cooldown_active() and waited < 600:
            time.sleep(5)
            waited += 5

        want = batch_size if not limit else min(batch_size, limit - processed)
        batch = _select_batch(want)
        if not batch:
            break

        for a in batch:
            if limit and processed >= limit:
                break
            # A cooldown armed mid-batch would otherwise make the rest of the
            # batch return 'mb-cooldown' (no real work) and get re-selected next
            # round — pure churn. Break out; the outer loop waits the cooldown.
            if mb.cooldown_active():
                break
            try:
                d = canonicalize_one(a["id"], a["name"])
            except Exception as e:
                logger.error(f"MB canon failed for {a['name']}: {e}")
                counts["error"] = counts.get("error", 0) + 1
                continue
            if "cooldown" in d.get("note", ""):
                break  # this call just armed the cooldown — stop, wait it out
            counts[d["action"]] = counts.get(d["action"], 0) + 1
            processed += 1
            if processed % 25 == 0:
                logger.info(f"MB canon: {processed} processed {counts}")

    logger.info(f"MB canonicalization pass done: {processed} processed {counts}")
    return processed, counts
