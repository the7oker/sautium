"""Collaboration splitting (Layer 2 — specialized).

Deterministic split of guaranteed-collaboration names (feat./vs./pres./…) into
primary + featured members, with cascading UUID rewrites. `&`/`,`/`and` forms
are kept whole (real duos). Future whole-match-first + split-fallback logic for
non-entity compounds belongs here.
"""
import logging
import re
from collections import defaultdict
from typing import List, Tuple, Optional, Dict

from sqlalchemy import text
from sqlalchemy.orm import Session

from uuid_utils import artist_uuid, track_uuid, album_uuid
from canon.identity import (
    _clean_artist_name, _filter_parts, _ensure_artist,
    _update_track_uuid, _update_album_uuid, _merge_album_variants,
    recanonicalize_artist,
)

logger = logging.getLogger(__name__)

SAFE_SEPARATORS = [
    (r'\s+featuring\s+', 'featuring'),
    (r'\s+feat\.?\s+',   'feat.'),
    (r'\s+ft\.?\s+',     'ft.'),
    (r'\s+vs\.?\s+',     'vs.'),
    (r'\s+pres\.?\s+',   'pres.'),
    (r'\s+aka\s+',       'aka'),
    (r'\s+meets\s+',     'meets'),
]

# '&', ',', 'and', 'with', '/' are NOT split: a compound under them ("Simon & Garfunkel",
# "Hootie & the Blowfish") is just as often a single band as a collaboration, and the only
# way to tell was a Last.fm lookup — non-deterministic across nodes/time, which silently
# forks the artist (hence the track/album) UUID and breaks P2P convergence. Kept WHOLE.



def detect_compound_type(name: str) -> Optional[Tuple[str, str, List[str]]]:
    """
    Detect if artist name is compound and classify separator type.

    Returns:
        ('safe', separator_label, [parts]) — guaranteed collaboration (feat./vs./…)
        None — not a safe compound (incl. '&'/','/'and' forms, kept whole — see SAFE note)
    """
    # Pre-clean the name
    cleaned = _clean_artist_name(name)

    # Only the ALWAYS-collaboration separators split — deterministic, no external lookup
    for pattern, label in SAFE_SEPARATORS:
        if re.search(pattern, cleaned, re.IGNORECASE):
            parts = re.split(pattern, cleaned, flags=re.IGNORECASE)
            parts = _filter_parts([p.strip() for p in parts if p.strip()])
            if len(parts) > 1:
                return ('safe', label, parts)

    return None


# ─── Low-level DB helpers ─────────────────────────────────────────────────


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

