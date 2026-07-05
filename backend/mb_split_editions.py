"""One-time migration: split conflated editions and attach scattered ones.

``canon.content.apply_editions`` is the engine; this runs it across the WHOLE
owned library once, retrofitting data that predates the edition-aware canon
(``canonicalize_pending`` only applies it to freshly-scanned artists). It splits
the differing-tracklist variants wrongly collapsed into one album row onto their
own rows (sharing the release group), attaches NULL-rg editions to their
cluster's RG with clean MB names, and leaves correctly-named single-edition
albums untouched.

Idempotent — a re-run lands on the same edition rows (0 changes). Reverse with
the pre-migration SQL dump (the project's ``backup_pre_*.sql`` convention);
``--revert`` is a code-level escape hatch that resets every owned album to its
scan-time ``raw_title`` identity (un-canon), from which ``canonicalize_pending``
rebuilds deterministically. Never touches media_files / tracks / recording_mbid.

    python mb_split_editions.py --dry-run     # preview the plan
    python mb_split_editions.py               # apply
    python mb_split_editions.py --revert      # reset albums to scan identity
"""

import argparse
import logging

import mb_backend as mb
from database import SessionLocal
from db_pool import db_execute, db_query
from canon.content import apply_editions
from canon.identity import recanonicalize_album_variants
from sqlalchemy import text as _sql
from uuid_utils import album_uuid

logger = logging.getLogger(__name__)


def revert_editions(dry_run: bool = False) -> dict:
    """Reset every owned album to ``album_uuid(raw_title, primary artist)`` — the
    scan-time identity — undoing all canon renames/splits. Deterministic and
    content-preserving; re-run ``canonicalize_pending`` afterwards to rebuild."""
    vids = [r["vid"] for r in db_query(
        "SELECT id AS vid FROM album_variants WHERE raw_title IS NOT NULL ORDER BY id")]
    st = {"scanned": len(vids), "reverted": 0}
    db = SessionLocal()
    try:
        for vid in vids:
            row = db.execute(_sql("""
                SELECT av.raw_title, av.album_id::text, ar.name
                FROM album_variants av
                JOIN album_artists aa ON aa.album_id = av.album_id AND aa.role = 'primary'
                JOIN artists ar ON ar.id = aa.artist_id
                WHERE av.id = :v
            """), {"v": vid}).fetchone()
            if not row:
                continue
            raw, aid, artist = row
            if str(album_uuid(raw, artist)) == aid:
                continue
            st["reverted"] += 1
            if not dry_run:
                recanonicalize_album_variants(db, aid, [vid], raw, None)
                db.commit()
        if dry_run:
            db.rollback()
    finally:
        db.close()
    return st


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Split/attach album editions (one-time retrofit)")
    parser.add_argument("--dry-run", action="store_true", help="preview without mutating")
    parser.add_argument("--revert", action="store_true",
                        help="reset every owned album to its scan-time raw_title identity")
    args = parser.parse_args()

    if not mb.LOCAL_DUMP:
        mb.refresh()
    if not mb.LOCAL_DUMP and not args.revert:
        print("No local MB dump — apply_editions needs it for release matching.")
        raise SystemExit(1)

    if args.revert:
        print(revert_editions(dry_run=args.dry_run))
    else:
        result = apply_editions(dry_run=args.dry_run)
        print(result)
