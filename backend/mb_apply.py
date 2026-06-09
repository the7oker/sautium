"""Apply MB canonicalization decisions from an audit JSONL.

Consumes the read-only audit (`mb_audit`) output and performs the
mutations: merge duplicate rows, rename to MB-canonical, split ad-hoc
collaborations. All destructive ops reuse the tested CASCADE machinery in
`normalize_artists`. Decisions live in the JSONL, so a run is reproducible
and revertible.

Precedence: merge groups are applied first; a rename whose artist is a
member of a merge group is skipped (the merge already moves it to the
canonical). Splits use the deterministic compound decomposition.
"""

import json
import logging
from typing import Dict

from sqlalchemy import text

import mb_audit
from database import get_db_context
from normalize_artists import (
    detect_compound_type,
    normalize_compound_artist,
    recanonicalize_artist,
)

logger = logging.getLogger(__name__)


def apply_decisions(jsonl_path: str, do_merge=True, do_rename=True,
                    do_split=True, only_artist_id=None) -> Dict[str, int]:
    """Apply audit decisions. `only_artist_id` restricts to a single artist
    (for a verify-then-rollback test). Returns per-action counts."""
    rows = [json.loads(l) for l in open(jsonl_path)]
    if only_artist_id:
        rows = [r for r in rows if r["artist_id"] == only_artist_id
                or only_artist_id in {x["artist_id"]
                    for g in mb_audit.find_merge_candidates(rows).values() for x in g}]

    merges = mb_audit.find_merge_candidates(rows)
    merged_ids = {r["artist_id"] for rs in merges.values() for r in rs}
    stats = {"merge_groups": 0, "merge_rows": 0, "rename": 0, "split": 0, "errors": 0}

    with get_db_context() as db:
        if do_merge:
            for mbid, rs in merges.items():
                canonical = rs[0]["canonical"]
                for r in rs:
                    recanonicalize_artist(db, r["artist_id"], canonical)
                    stats["merge_rows"] += 1
                stats["merge_groups"] += 1
                logger.info(f"Merged → {canonical}: {[r['name'] for r in rs]}")

        if do_rename:
            for r in rows:
                if r["action"] != "rename" or r["artist_id"] in merged_ids:
                    continue
                recanonicalize_artist(db, r["artist_id"], r["canonical"])
                stats["rename"] += 1
                logger.info(f"Renamed: {r['name']} → {r['canonical']}")

        if do_split:
            for r in rows:
                if r["action"] != "split":
                    continue
                ct = detect_compound_type(r["name"])
                if not ct:
                    stats["errors"] += 1
                    continue
                normalize_compound_artist(db, r["artist_id"], r["name"],
                                          ct[2], "verified_split")
                # Stamp the (now-collaboration) compound so the bg pass skips it.
                db.execute(text("UPDATE artists SET last_mb_sync = now() WHERE id = :id"),
                           {"id": r["artist_id"]})
                stats["split"] += 1

        db.commit()
    return stats
