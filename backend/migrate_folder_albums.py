"""One-time retrofit: reshape each directory into per-album variants (album_identity).

The scanner used to key one variant per directory, so a directory could hold only
one album: a box set or singles collection collapsed under its first file's tag
(the other albums orphaned), and a genre/loose mix stole a real album's identity.
The model is now one variant per ``(directory, album)``; this pass brings existing
data to that shape, via the same ``assign_dir_albums`` the scanner now uses:

  * SPLIT a box set / singles collection into one album per release group (each a
    real release; a sub-album on a single non-1 disc is renormalised to disc 1).
  * FOLD a reshapeable mix or a loose per-track dump to one folder album (Various
    Artists / sole artist), renumbering by filename only when positions collide.
  * Leave plain single-album directories untouched (canon owns their titles).

A directory whose files already sit on their target albums is skipped, so the pass
is idempotent and subsumes the earlier mix-only retrofit. Per-track artist/title
and ``raw_*`` tags are unchanged; reversible by restoring the
``backup_pre_folderalbums_*.sql`` dump or re-scanning.

    python migrate_folder_albums.py --dry-run     # preview
    python migrate_folder_albums.py               # apply
"""

import argparse
import logging
import os
from collections import defaultdict

from sqlalchemy import text as _sql

from album_identity import assign_dir_albums
from database import SessionLocal
from db_pool import db_execute, db_query
from uuid_utils import album_uuid, artist_uuid

logger = logging.getLogger(__name__)


def _basename(path: str) -> str:
    return os.path.basename((path or "").replace("\\", "/").rstrip("/"))


def _load_dirs() -> dict:
    """Every directory's media_files (raw tags + variant + rip specs), grouped
    by directory_path."""
    rows = db_query("""
        SELECT av.directory_path AS dir, av.id AS variant_id, av.album_id::text AS cur_album_id,
               mf.id AS mf_id, mf.file_path, mf.disc_number, mf.track_number,
               mf.raw_album_artist, mf.raw_artist, mf.raw_album,
               mf.sample_rate, mf.bit_depth, mf.is_lossless
        FROM album_variants av JOIN media_files mf ON mf.album_variant_id = av.id
        ORDER BY av.directory_path, mf.id
    """)
    dirs: dict = defaultdict(list)
    for r in rows:
        dirs[r["dir"]].append(r)
    return dirs


def _ensure_variant(db, dir_path, album_id, spec) -> int:
    """Get-or-create the ``(dir_path, album_id)`` variant; return its id."""
    row = db.execute(_sql("SELECT id FROM album_variants WHERE directory_path = :d AND album_id = :a"),
                     {"d": dir_path, "a": album_id}).first()
    if row:
        return row[0]
    return db.execute(_sql(
        "INSERT INTO album_variants (album_id, directory_path, raw_title, sample_rate, bit_depth, is_lossless) "
        "VALUES (:a, :d, :rt, :sr, :bd, :ll) RETURNING id"),
        {"a": album_id, "d": dir_path, "rt": spec["raw_title"], "sr": spec["sample_rate"],
         "bd": spec["bit_depth"], "ll": spec["is_lossless"]}).first()[0]


def _reshape_dir(db, dir_path, files, by_target, st) -> None:
    src_variant_ids = {f["variant_id"] for f in files}
    cur_albums = {f["cur_album_id"] for f in files}
    for aid, grp in by_target.items():
        title, artist = grp[0][1], grp[0][2]
        artist_id = str(artist_uuid(artist))
        db.execute(_sql("INSERT INTO artists (id, name) VALUES (:i, :n) ON CONFLICT (id) DO NOTHING"),
                   {"i": artist_id, "n": artist})
        db.execute(_sql("INSERT INTO albums (id, title) VALUES (:i, :t) "
                        "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title"), {"i": aid, "t": title})
        db.execute(_sql("INSERT INTO album_artists (album_id, artist_id, role) "
                        "VALUES (:a, :r, 'primary') ON CONFLICT DO NOTHING"), {"a": aid, "r": artist_id})
        rep = grp[0][0]
        vid = _ensure_variant(db, dir_path, aid, {
            "raw_title": title, "sample_rate": rep["sample_rate"],
            "bit_depth": rep["bit_depth"], "is_lossless": rep["is_lossless"]})
        for (f, _t, _a, trk, disc) in grp:
            sets, params = ["album_variant_id = :v"], {"v": vid, "i": f["mf_id"]}
            if trk is not None:
                sets.append("track_number = :tn"); params["tn"] = trk
            if disc is not None:
                sets.append("disc_number = :dn"); params["dn"] = disc
            db.execute(_sql(f"UPDATE media_files SET {', '.join(sets)} WHERE id = :i"), params)
            st["files_moved"] += 1
        st["albums_made"] += 1
    # Drop variants emptied by the moves, then albums left with no variants
    # anywhere (a target album keeps its files, so it survives this).
    db.execute(_sql("DELETE FROM album_variants av WHERE av.id = ANY(:ids) "
                    "AND NOT EXISTS (SELECT 1 FROM media_files WHERE album_variant_id = av.id)"),
               {"ids": list(src_variant_ids)})
    db.execute(_sql("DELETE FROM albums al WHERE al.id = ANY(CAST(:aids AS uuid[])) "
                    "AND NOT EXISTS (SELECT 1 FROM album_variants WHERE album_id = al.id)"),
               {"aids": list(cur_albums)})


def migrate(dry_run: bool = True) -> dict:
    st = {"dirs": 0, "reshaped": 0, "albums_made": 0, "files_moved": 0, "errors": 0}
    dirs = _load_dirs()
    st["dirs"] = len(dirs)
    db = SessionLocal()
    try:
        for dir_path, files in dirs.items():
            af = [{"album": f["raw_album"], "disc": f["disc_number"], "track": f["track_number"],
                   "artist_key": f["raw_album_artist"] or f["raw_artist"], "path": f["file_path"]}
                  for f in files]
            assignment = assign_dir_albums(_basename(dir_path), af)
            if assignment is None:
                continue
            by_target: dict = defaultdict(list)
            for f in files:
                title, artist, trk, disc = assignment[f["file_path"]]
                by_target[str(album_uuid(title, artist))].append((f, title, artist, trk, disc))
            if all(f["cur_album_id"] in by_target
                   and f["cur_album_id"] == str(album_uuid(*assignment[f["file_path"]][:2]))
                   for f in files):
                continue   # already on target albums — idempotent
            st["reshaped"] += 1
            if dry_run:
                logger.info("[dry] %-44s -> %d album(s): %s", _basename(dir_path), len(by_target),
                            [grp[0][1] for grp in by_target.values()])
                continue
            try:
                _reshape_dir(db, dir_path, files, by_target, st)
                db.commit()
            except Exception as e:
                db.rollback()
                st["errors"] += 1
                logger.error("reshape failed for %s: %s", dir_path, e)
    finally:
        db.close()
    if not dry_run and st["reshaped"]:
        db_execute("DELETE FROM similar_albums")   # read-through cache; recomputes on view
        logger.info("flushed similar_albums cache")
    return st


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Retrofit folder-aware album identity")
    parser.add_argument("--dry-run", action="store_true", help="preview without mutating")
    args = parser.parse_args()
    print(migrate(dry_run=args.dry_run))
