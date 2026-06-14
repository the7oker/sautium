"""One-time retrofit: reshape MIX folders into folder albums (album_identity).

A folder is a MIX when its files aren't whole albums — grouped by album tag, some
group's tracks aren't a contiguous per-disc 1..N (fragments of different source
albums, incl. a single-artist "favourites" folder). The scanner collapses each
directory to one variant under the first file's album, so a mix ends up mis-titled
and can even steal a real album's MB release group. This moves every MIX variant
to (Various Artists | the sole artist, folder name), renumbering by filename only
when the disc/track numbers collide. Folders that are real album(s) — every group
a clean sequence — are left untouched (canon owns their titles; a re-titled real
album would be recovered by canon anyway). Per-track artist/title and ``raw_*``
tags are unchanged. A re-scan reuses the cached per-directory variant, so existing
data needs this pass.

Reversible by restoring the ``backup_pre_folderalbums_*.sql`` dump or re-scanning.

    python migrate_folder_albums.py --dry-run     # preview
    python migrate_folder_albums.py               # apply
"""

import argparse
import logging
import os
from collections import defaultdict

from sqlalchemy import text as _sql

from album_identity import (
    VARIOUS_ARTISTS, folder_album_artist, has_duplicate_positions, is_reshapeable_mix,
)
from database import SessionLocal
from db_pool import db_execute, db_query
from uuid_utils import album_uuid, artist_uuid

logger = logging.getLogger(__name__)


def _basename(path: str) -> str:
    return os.path.basename((path or "").replace("\\", "/").rstrip("/"))


def _load_variants() -> dict:
    """Every variant with its media_files' raw tags + filenames, grouped by id."""
    rows = db_query("""
        SELECT av.id AS variant_id, av.album_id::text AS album_id, av.directory_path,
               mf.id AS mf_id, mf.file_path, mf.disc_number, mf.track_number,
               mf.raw_album_artist, mf.raw_artist, mf.raw_album
        FROM album_variants av
        JOIN media_files mf ON mf.album_variant_id = av.id
        ORDER BY av.id
    """)
    variants: dict = defaultdict(lambda: {"files": []})
    for r in rows:
        v = variants[r["variant_id"]]
        v["album_id"] = r["album_id"]
        v["directory_path"] = r["directory_path"]
        v["files"].append(r)
    return variants


def _move_variant(db, variant_id, src_album_id, target_id, artist_name, title,
                  renumber, files) -> None:
    artist_id = str(artist_uuid(artist_name))
    db.execute(_sql("INSERT INTO artists (id, name) VALUES (:i, :n) "
                    "ON CONFLICT (id) DO NOTHING"), {"i": artist_id, "n": artist_name})
    db.execute(_sql("INSERT INTO albums (id, title) VALUES (:i, :t) "
                    "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title"),
               {"i": target_id, "t": title})
    db.execute(_sql("INSERT INTO album_artists (album_id, artist_id, role) "
                    "VALUES (:a, :r, 'primary') ON CONFLICT DO NOTHING"),
               {"a": target_id, "r": artist_id})
    db.execute(_sql("UPDATE album_variants SET album_id = :t WHERE id = :v"),
               {"t": target_id, "v": variant_id})
    if renumber:
        for n, f in enumerate(sorted(files, key=lambda x: _basename(x["file_path"]).lower()), 1):
            db.execute(_sql("UPDATE media_files SET track_number = :n, disc_number = 1 "
                            "WHERE id = :i"), {"n": n, "i": f["mf_id"]})
    db.execute(_sql("DELETE FROM albums WHERE id = :s "
                    "AND NOT EXISTS (SELECT 1 FROM album_variants WHERE album_id = :s)"),
               {"s": src_album_id})


def migrate(dry_run: bool = True) -> dict:
    st = {"variants": 0, "moved": 0, "to_va": 0, "renumbered": 0, "errors": 0}
    variants = _load_variants()
    st["variants"] = len(variants)
    db = SessionLocal()
    try:
        for vid, v in variants.items():
            files = v["files"]
            tracks = [(f["raw_album"], f["disc_number"], f["track_number"]) for f in files]
            akeys = [(f["raw_album_artist"] or f["raw_artist"]) for f in files]
            # Reshape only genuine mixes: fragments AND (multiple artists or album
            # tags). One real album by one artist — even oddly numbered (vinyl,
            # continuous multi-disc) — is left to canon, never churned/renumbered.
            if not is_reshapeable_mix(tracks, akeys):
                continue
            artist = folder_album_artist(akeys)
            title = _basename(v["directory_path"])
            target = str(album_uuid(title, artist))
            if target == v["album_id"]:
                continue   # already a folder album — idempotent
            # Renumber only when the existing disc/track numbers collide; clean
            # (even if gappy) numbers are kept, so a real order is never scrambled.
            renumber = has_duplicate_positions([(f["disc_number"], f["track_number"])
                                                for f in files])
            st["moved"] += 1
            st["to_va"] += artist == VARIOUS_ARTISTS
            st["renumbered"] += renumber
            if dry_run:
                logger.info("[dry] %-28s -> (%s, %r)%s  [%d files]",
                            _basename(v["directory_path"]), artist, title,
                            " +renumber" if renumber else "", len(files))
                continue
            try:
                _move_variant(db, vid, v["album_id"], target, artist, title, renumber, files)
                db.commit()
            except Exception as e:
                db.rollback()
                st["errors"] += 1
                logger.error("move failed for variant %s (%s): %s",
                             vid, v["directory_path"], e)
    finally:
        db.close()
    if not dry_run and st["moved"]:
        db_execute("DELETE FROM similar_albums")   # read-through cache; recomputes on view
        logger.info("flushed similar_albums cache")
    return st


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Retrofit folder-aware album identity")
    parser.add_argument("--dry-run", action="store_true", help="preview without mutating")
    args = parser.parse_args()
    print(migrate(dry_run=args.dry_run))
