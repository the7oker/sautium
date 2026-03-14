"""
Artist normalization with two-pass algorithm and cascading UUID updates.

Pass 1 (offline, safe): Split on feat./ft./featuring/vs. patterns — never band names.
Pass 2 (Last.fm):       Verify suspicious patterns (&, comma, and, with, /)
                         by checking if compound name exists on Last.fm.

After splitting, track and album UUIDs are recalculated based on the
normalized primary artist name, and cascaded to all FK references via
ON UPDATE CASCADE constraints.

Usage:
    # Inside Docker container:
    python normalize_artists.py                  # Pass 1 only (safe)
    python normalize_artists.py --pass2          # Pass 1 + Last.fm verification
    python normalize_artists.py --dry-run        # Preview only
    python normalize_artists.py --pass2 --dry-run
"""

import logging
import re
from typing import List, Tuple, Optional, Dict

from sqlalchemy import text
from sqlalchemy.orm import Session

from uuid_utils import artist_uuid, track_uuid, album_uuid

logger = logging.getLogger(__name__)

# ─── Pattern definitions ──────────────────────────────────────────────────

# Patterns that are ALWAYS collaborations — nobody names a band "X feat. Y"
SAFE_SEPARATORS = [
    (r'\s+featuring\s+', 'featuring'),
    (r'\s+feat\.?\s+',   'feat.'),
    (r'\s+ft\.?\s+',     'ft.'),
    (r'\s+vs\.?\s+',     'vs.'),
]

# Patterns that MIGHT be band names — need Last.fm verification
SUSPICIOUS_SEPARATORS = [
    (r'\s+&\s+',   '&'),
    (r',\s+',      ','),
    (r'\s+and\s+', 'and'),
    (r'\s+with\s+','with'),
    (r'\s+/\s+',   '/'),
]


def detect_compound_type(name: str) -> Optional[Tuple[str, str, List[str]]]:
    """
    Detect if artist name is compound and classify separator type.

    Returns:
        ('safe', separator_label, [parts]) — guaranteed collaboration
        ('suspicious', separator_label, [parts]) — needs verification
        None — not compound
    """
    # Check safe patterns first (higher priority)
    for pattern, label in SAFE_SEPARATORS:
        if re.search(pattern, name, re.IGNORECASE):
            parts = re.split(pattern, name, flags=re.IGNORECASE)
            parts = [p.strip() for p in parts if p.strip()]
            if len(parts) > 1:
                return ('safe', label, parts)

    # Check suspicious patterns
    for pattern, label in SUSPICIOUS_SEPARATORS:
        if re.search(pattern, name, re.IGNORECASE):
            parts = re.split(pattern, name, flags=re.IGNORECASE)
            parts = [p.strip() for p in parts if p.strip()]
            if len(parts) > 1:
                return ('suspicious', label, parts)

    return None


# ─── Low-level DB helpers ─────────────────────────────────────────────────

def _ensure_artist(db: Session, name: str):
    """Get or create artist with deterministic UUID. Returns UUID."""
    uid = artist_uuid(name)
    exists = db.execute(
        text("SELECT 1 FROM artists WHERE id = :id"), {"id": str(uid)}
    ).fetchone()
    if not exists:
        db.execute(text("""
            INSERT INTO artists (id, name)
            VALUES (:id, :name)
            ON CONFLICT (id) DO NOTHING
        """), {"id": str(uid), "name": name})
        logger.info(f"  Created artist: {name} ({uid})")
    return uid


def _update_track_uuid(db: Session, old_id, new_id) -> str:
    """
    Update track UUID via ON UPDATE CASCADE, or merge if target exists.
    Returns the final track UUID.
    """
    old_str = str(old_id)
    new_str = str(new_id)

    if old_str == new_str:
        return new_str

    exists = db.execute(
        text("SELECT 1 FROM tracks WHERE id = :id"), {"id": new_str}
    ).fetchone()

    if not exists:
        # ON UPDATE CASCADE propagates to all child tables
        db.execute(
            text("UPDATE tracks SET id = :new_id WHERE id = :old_id"),
            {"new_id": new_str, "old_id": old_str},
        )
    else:
        # Merge: move per-user data to target track, delete old
        db.execute(
            text("UPDATE media_files SET track_id = :new WHERE track_id = :old"),
            {"new": new_str, "old": old_str},
        )
        db.execute(
            text("UPDATE listening_history SET track_id = :new WHERE track_id = :old"),
            {"new": new_str, "old": old_str},
        )
        # Merge local_play_stats: sum counts from old into new
        db.execute(text("""
            INSERT INTO local_play_stats (track_id, play_count, skip_count,
                total_listen_time, avg_percent_listened, last_played_at)
            SELECT :new, play_count, skip_count, total_listen_time,
                   avg_percent_listened, last_played_at
            FROM local_play_stats WHERE track_id = :old
            ON CONFLICT (track_id) DO UPDATE SET
                play_count = local_play_stats.play_count + EXCLUDED.play_count,
                skip_count = local_play_stats.skip_count + EXCLUDED.skip_count,
                total_listen_time = local_play_stats.total_listen_time + EXCLUDED.total_listen_time,
                avg_percent_listened = CASE
                    WHEN (local_play_stats.play_count + EXCLUDED.play_count) > 0 THEN
                        (local_play_stats.avg_percent_listened * local_play_stats.play_count
                         + EXCLUDED.avg_percent_listened * EXCLUDED.play_count)
                        / (local_play_stats.play_count + EXCLUDED.play_count)
                    ELSE 0
                END,
                last_played_at = GREATEST(local_play_stats.last_played_at, EXCLUDED.last_played_at),
                updated_at = CURRENT_TIMESTAMP
        """), {"new": new_str, "old": old_str})
        db.execute(text("DELETE FROM local_play_stats WHERE track_id = :old"), {"old": old_str})
        # CASCADE deletes old associations, embeddings, features, stats, lyrics
        db.execute(text("DELETE FROM tracks WHERE id = :old"), {"old": old_str})

    return new_str


def _update_album_uuid(db: Session, old_id, new_id) -> str:
    """
    Update album UUID via ON UPDATE CASCADE, or merge if target exists.
    Returns the final album UUID.
    """
    old_str = str(old_id)
    new_str = str(new_id)

    if old_str == new_str:
        return new_str

    exists = db.execute(
        text("SELECT 1 FROM albums WHERE id = :id"), {"id": new_str}
    ).fetchone()

    if not exists:
        db.execute(
            text("UPDATE albums SET id = :new_id WHERE id = :old_id"),
            {"new_id": new_str, "old_id": old_str},
        )
    else:
        # Merge: move album variants to target, delete old
        db.execute(
            text("UPDATE album_variants SET album_id = :new WHERE album_id = :old"),
            {"new": new_str, "old": old_str},
        )
        db.execute(text("DELETE FROM albums WHERE id = :old"), {"old": old_str})

    return new_str


# ─── Core normalization logic ─────────────────────────────────────────────

def normalize_compound_artist(
    db: Session,
    compound_id,
    compound_name: str,
    parts: List[str],
    status: str = 'verified_split',
) -> Dict:
    """
    Split compound artist into primary + featured artists.
    Recalculates track/album UUIDs and cascades all FK references.

    Args:
        db: Database session
        compound_id: UUID of the compound artist
        compound_name: Full compound name (e.g. "Beth Hart & Joe Bonamassa")
        parts: Individual names (e.g. ["Beth Hart", "Joe Bonamassa"])
        status: verification_status to set on compound artist

    Returns:
        Stats dict with counts of updated/merged tracks and albums.
    """
    stats = {
        'tracks_updated': 0,
        'albums_updated': 0,
        'tracks_merged': 0,
        'albums_merged': 0,
    }

    compound_str = str(compound_id)
    primary_name = parts[0]
    featured_names = parts[1:]

    # Create individual artists
    primary_id = str(_ensure_artist(db, primary_name))
    featured_ids = [str(_ensure_artist(db, name)) for name in featured_names]

    # Create artist_members entries
    db.execute(text("""
        INSERT INTO artist_members (compound_artist_id, member_artist_id, role)
        VALUES (:cid, :mid, 'primary')
        ON CONFLICT DO NOTHING
    """), {"cid": compound_str, "mid": primary_id})

    for fid, fname in zip(featured_ids, featured_names):
        db.execute(text("""
            INSERT INTO artist_members (compound_artist_id, member_artist_id, role)
            VALUES (:cid, :mid, 'featured')
            ON CONFLICT DO NOTHING
        """), {"cid": compound_str, "mid": fid})

    # ── Process tracks ────────────────────────────────────────────────────
    tracks = db.execute(text("""
        SELECT t.id::text, t.title
        FROM tracks t
        JOIN track_artists ta ON ta.track_id = t.id
        WHERE ta.artist_id = :aid AND ta.role = 'primary'
    """), {"aid": compound_str}).fetchall()

    for old_track_str, title in tracks:
        new_track_id = track_uuid(title, primary_name)
        new_track_str = str(new_track_id)

        will_merge = (
            old_track_str != new_track_str
            and db.execute(
                text("SELECT 1 FROM tracks WHERE id = :id"), {"id": new_track_str}
            ).fetchone() is not None
        )

        final_id = _update_track_uuid(db, old_track_str, new_track_str)

        if will_merge:
            stats['tracks_merged'] += 1
            logger.debug(f"    Merged track: {title} ({old_track_str} -> {final_id})")
        elif old_track_str != new_track_str:
            stats['tracks_updated'] += 1
            logger.debug(f"    Updated track UUID: {title} ({old_track_str} -> {final_id})")

        # Update artist associations on the final track
        db.execute(text("""
            DELETE FROM track_artists
            WHERE track_id = :tid AND artist_id = :aid
        """), {"tid": final_id, "aid": compound_str})

        db.execute(text("""
            INSERT INTO track_artists (track_id, artist_id, role)
            VALUES (:tid, :aid, 'primary')
            ON CONFLICT DO NOTHING
        """), {"tid": final_id, "aid": primary_id})

        for fid in featured_ids:
            db.execute(text("""
                INSERT INTO track_artists (track_id, artist_id, role)
                VALUES (:tid, :aid, 'featured')
                ON CONFLICT DO NOTHING
            """), {"tid": final_id, "aid": fid})

    # ── Process albums ────────────────────────────────────────────────────
    albums = db.execute(text("""
        SELECT a.id::text, a.title
        FROM albums a
        JOIN album_artists aa ON aa.album_id = a.id
        WHERE aa.artist_id = :aid AND aa.role = 'primary'
    """), {"aid": compound_str}).fetchall()

    for old_album_str, title in albums:
        new_album_id = album_uuid(title, primary_name)
        new_album_str = str(new_album_id)

        will_merge = (
            old_album_str != new_album_str
            and db.execute(
                text("SELECT 1 FROM albums WHERE id = :id"), {"id": new_album_str}
            ).fetchone() is not None
        )

        final_id = _update_album_uuid(db, old_album_str, new_album_str)

        if will_merge:
            stats['albums_merged'] += 1
            logger.debug(f"    Merged album: {title} ({old_album_str} -> {final_id})")
        elif old_album_str != new_album_str:
            stats['albums_updated'] += 1
            logger.debug(f"    Updated album UUID: {title} ({old_album_str} -> {final_id})")

        # Update artist associations on the final album
        db.execute(text("""
            DELETE FROM album_artists
            WHERE album_id = :aid AND artist_id = :cid
        """), {"aid": final_id, "cid": compound_str})

        db.execute(text("""
            INSERT INTO album_artists (album_id, artist_id, role)
            VALUES (:aid, :pid, 'primary')
            ON CONFLICT DO NOTHING
        """), {"aid": final_id, "pid": primary_id})

        for fid in featured_ids:
            db.execute(text("""
                INSERT INTO album_artists (album_id, artist_id, role)
                VALUES (:aid, :fid, 'featured')
                ON CONFLICT DO NOTHING
            """), {"aid": final_id, "fid": fid})

    # ── Update compound artist metadata ───────────────────────────────────
    db.execute(text("""
        UPDATE artists SET
            verification_status = :status,
            artist_type = 'collaboration',
            raw_name = COALESCE(raw_name, name)
        WHERE id = :id
    """), {"status": status, "id": compound_str})

    logger.info(
        f"  Split '{compound_name}' -> {parts}: "
        f"{stats['tracks_updated']} tracks updated, {stats['tracks_merged']} merged, "
        f"{stats['albums_updated']} albums updated, {stats['albums_merged']} merged"
    )

    return stats


# ─── Two-pass normalization ───────────────────────────────────────────────

def normalize_pass1(db: Session, dry_run: bool = False) -> Dict:
    """
    Pass 1: Split safe patterns (feat./ft./featuring/vs.) without external lookups.
    These separators are NEVER used in band names.
    """
    logger.info("=== Pass 1: Safe pattern splitting ===")

    all_artists = db.execute(text("""
        SELECT id::text, name FROM artists
        WHERE verification_status = 'unverified'
           OR (verification_status = 'verified_split'
               AND NOT EXISTS (
                   SELECT 1 FROM artist_members am
                   WHERE am.compound_artist_id = artists.id))
        ORDER BY name
    """)).fetchall()

    total = {
        'artists_scanned': len(all_artists),
        'safe_found': 0,
        'suspicious_marked': 0,
        'split': 0,
        'tracks_updated': 0,
        'albums_updated': 0,
        'tracks_merged': 0,
        'albums_merged': 0,
    }

    for artist_id, name in all_artists:
        result = detect_compound_type(name)
        if not result:
            continue

        compound_type, separator, parts = result

        if compound_type == 'safe':
            total['safe_found'] += 1
            logger.info(f"Safe split: '{name}' -> {parts} (separator: {separator})")

            if dry_run:
                continue

            stats = normalize_compound_artist(db, artist_id, name, parts, 'verified_split')
            total['split'] += 1
            total['tracks_updated'] += stats['tracks_updated']
            total['albums_updated'] += stats['albums_updated']
            total['tracks_merged'] += stats['tracks_merged']
            total['albums_merged'] += stats['albums_merged']

        elif compound_type == 'suspicious':
            total['suspicious_marked'] += 1
            if not dry_run:
                db.execute(text("""
                    UPDATE artists SET verification_status = 'suspicious'
                    WHERE id = :id AND verification_status = 'unverified'
                """), {"id": artist_id})
            logger.debug(f"Marked suspicious: '{name}' (separator: {separator})")

    if not dry_run:
        db.commit()

    logger.info(
        f"Pass 1 done: scanned {total['artists_scanned']}, "
        f"safe splits {total['split']}, suspicious marked {total['suspicious_marked']}"
    )
    return total


def normalize_pass2(db: Session, dry_run: bool = False) -> Dict:
    """
    Pass 2: Verify suspicious artists via Last.fm.

    If compound name is found on Last.fm -> it's a band, keep as-is.
    If not found -> split into individual artists.
    """
    import time
    logger.info("=== Pass 2: Last.fm verification ===")

    suspicious = db.execute(text("""
        SELECT id::text, name FROM artists
        WHERE verification_status = 'suspicious'
        ORDER BY name
    """)).fetchall()

    total = {
        'suspicious_count': len(suspicious),
        'verified_band': 0,
        'verified_split': 0,
        'errors': 0,
        'tracks_updated': 0,
        'albums_updated': 0,
        'tracks_merged': 0,
        'albums_merged': 0,
    }

    if not suspicious:
        logger.info("No suspicious artists to verify")
        return total

    from lastfm import LastFmService
    try:
        lastfm = LastFmService()
    except Exception as e:
        logger.error(f"Cannot initialize Last.fm service: {e}")
        return total

    for artist_id, name in suspicious:
        result = detect_compound_type(name)
        if not result:
            continue

        _, separator, parts = result
        logger.info(f"Verifying: '{name}' ...")

        try:
            compound_info = lastfm.get_artist_info(name, fetch_similar=False)
            time.sleep(0.25)
        except Exception as e:
            logger.error(f"  Last.fm error for '{name}': {e}")
            total['errors'] += 1
            continue

        if compound_info:
            # Found on Last.fm — treat as a recognized entity (band/project)
            listeners = compound_info.get('stats', {}).get('listeners', 0)
            if isinstance(listeners, str):
                listeners = int(listeners) if listeners.isdigit() else 0

            logger.info(f"  Found on Last.fm: '{name}' ({listeners} listeners) -> verified_band")

            if not dry_run:
                db.execute(text("""
                    UPDATE artists SET
                        verification_status = 'verified_band',
                        artist_type = 'band',
                        raw_name = COALESCE(raw_name, name)
                    WHERE id = :id
                """), {"id": artist_id})

            total['verified_band'] += 1
        else:
            # Not found on Last.fm — split into individual artists
            logger.info(f"  Not on Last.fm: '{name}' -> splitting into {parts}")

            if not dry_run:
                stats = normalize_compound_artist(
                    db, artist_id, name, parts, 'verified_split'
                )
                total['tracks_updated'] += stats['tracks_updated']
                total['albums_updated'] += stats['albums_updated']
                total['tracks_merged'] += stats['tracks_merged']
                total['albums_merged'] += stats['albums_merged']

            total['verified_split'] += 1

    if not dry_run:
        db.commit()

    logger.info(
        f"Pass 2 done: {total['verified_band']} bands, "
        f"{total['verified_split']} splits, {total['errors']} errors"
    )
    return total


# ─── Main entry point ─────────────────────────────────────────────────────

def normalize_artists(
    db: Session,
    pass1: bool = True,
    pass2: bool = False,
    dry_run: bool = False,
) -> Dict:
    """
    Run artist normalization.

    Args:
        db: Database session
        pass1: Run offline safe splitting (feat./ft./featuring/vs.)
        pass2: Run Last.fm verification for suspicious patterns (&, comma, and, with, /)
        dry_run: Only show what would be done, no DB changes

    Returns:
        Dict with pass1/pass2 stats
    """
    stats = {}

    if pass1:
        # Run Pass 1 iteratively — splitting "A vs. B feat. C" first produces
        # "A vs. B" as a new artist, which needs another pass to split on "vs."
        iteration = 0
        while True:
            iteration += 1
            pass1_stats = normalize_pass1(db, dry_run=dry_run)
            if iteration == 1:
                stats['pass1'] = pass1_stats
            else:
                # Merge stats from subsequent iterations
                for key in pass1_stats:
                    if isinstance(pass1_stats[key], int):
                        stats['pass1'][key] = stats['pass1'].get(key, 0) + pass1_stats[key]
                logger.info(f"Pass 1 iteration {iteration}: {pass1_stats['split']} more splits")
            if pass1_stats['split'] == 0 or dry_run:
                break

    if pass2:
        stats['pass2'] = normalize_pass2(db, dry_run=dry_run)

    return stats


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s: %(message)s',
    )

    dry_run = '--dry-run' in sys.argv
    run_pass2 = '--pass2' in sys.argv or '--lastfm' in sys.argv

    from database import get_db_context

    with get_db_context() as db:
        logger.info("=== Artist Normalization ===")
        if dry_run:
            logger.info("[DRY RUN MODE]")

        stats = normalize_artists(db, pass1=True, pass2=run_pass2, dry_run=dry_run)

        print("\n=== Results ===")
        for pass_name, pass_stats in stats.items():
            print(f"\n{pass_name}:")
            for key, value in pass_stats.items():
                print(f"  {key}: {value}")
