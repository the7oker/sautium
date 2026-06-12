"""
Artist normalization (deterministic, offline) with cascading UUID updates.

Splits a compound artist ONLY on always-collaboration separators (feat./ft./
featuring/vs./pres./aka/meets) — never band names. '&', ',', 'and', 'with', '/'
are kept WHOLE: telling "Simon & Garfunkel" (band) from "Beth Hart & Joe Bonamassa"
(collab) needed a Last.fm lookup, which is non-deterministic across nodes/time and
silently forks the artist (hence track/album) UUID, breaking P2P convergence.

After splitting, track and album UUIDs are recalculated from the normalized primary
artist name and cascaded to all FK references via ON UPDATE CASCADE constraints.

Usage:
    python normalize_artists.py                  # run
    python normalize_artists.py --dry-run        # preview only
"""

import logging
import re
from collections import defaultdict
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
    (r'\s+pres\.?\s+',   'pres.'),
    (r'\s+aka\s+',       'aka'),
    (r'\s+meets\s+',     'meets'),
]

# '&', ',', 'and', 'with', '/' are NOT split: a compound under them ("Simon & Garfunkel",
# "Hootie & the Blowfish") is just as often a single band as a collaboration, and the only
# way to tell was a Last.fm lookup — non-deterministic across nodes/time, which silently
# forks the artist (hence the track/album) UUID and breaks P2P convergence. Kept WHOLE.


def _clean_artist_name(name: str) -> str:
    """Strip trailing separators, placeholders, and fix whitespace artifacts."""
    # Collapse multiple spaces
    name = re.sub(r'\s{2,}', ' ', name)
    # Remove placeholder suffixes: "X/VARIOUS", "X & Various Artists"
    _ph = r'(?:various(?:\s+artists?)?|va|unknown(?:\s+artist)?)'
    name = re.sub(r'\s*[/&,;]\s*' + _ph + r'\s*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'^\s*' + _ph + r'\s*[/&,;]\s*', '', name, flags=re.IGNORECASE)
    # Strip trailing separators: & ; , /
    name = re.sub(r'[\s&;,/]+$', '', name)
    # Strip trailing incomplete patterns: "feat." "feat" "ft." at end
    name = re.sub(r'\s+(?:feat\.?|ft\.?|pres\.?)\s*$', '', name, flags=re.IGNORECASE)
    return name.strip()


# Placeholder names that are not real artists — drop from split results
_PLACEHOLDER_NAMES = {
    'various', 'various artists', 'various artist', 'va',
    'unknown', 'unknown artist',
}


def _filter_parts(parts: List[str]) -> List[str]:
    """Remove placeholder names from split results."""
    return [p for p in parts if p.strip().lower() not in _PLACEHOLDER_NAMES]


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


def recanonicalize_artist(db: Session, source_id, canonical_name: str) -> str:
    """Move an artist (and its primary tracks/albums) onto a canonical name.

    Generalises the clean-pass rename/merge to an arbitrary target name
    (e.g. an MB-canonical "Björk" / "Jean‐Michel Jarre"). Target row absent
    → rename via CASCADE; present → merge into it (track/album UUIDs are
    recomputed and merged, preserving listening_history + play stats via
    _update_track_uuid). Stamps last_mb_sync. Returns the canonical artist id.
    The MB merge primitive (reused by merge-on-MBID-collision).

    Similar/bio/tag rows pointing at the deleted source fall away by
    CASCADE — same as the existing clean-pass merge; they re-derive on
    enrichment of the canonical artist.
    """
    source_str = str(source_id)
    canonical_id = str(_ensure_artist(db, canonical_name))

    def _stamp():
        # MBID lives in artist_mbids now — name-normalization no longer assigns it.
        db.execute(text("UPDATE artists SET last_mb_sync = now() WHERE id = :id"),
                   {"id": canonical_id})

    if source_str == canonical_id:
        _stamp()
        return canonical_id

    # Recompute primary tracks/albums under the canonical name (merges if
    # the canonical-named UUID already exists).
    tracks = db.execute(text("""
        SELECT t.id::text, t.title FROM tracks t
        JOIN track_artists ta ON ta.track_id = t.id
        WHERE ta.artist_id = :aid AND ta.role = 'primary'
    """), {"aid": source_str}).fetchall()
    for tid, title in tracks:
        _update_track_uuid(db, tid, str(track_uuid(title, canonical_name)))

    albums = db.execute(text("""
        SELECT a.id::text, a.title FROM albums a
        JOIN album_artists aa ON aa.album_id = a.id
        WHERE aa.artist_id = :aid AND aa.role = 'primary'
    """), {"aid": source_str}).fetchall()
    for aid_, title in albums:
        _update_album_uuid(db, aid_, str(album_uuid(title, canonical_name)))

    # Repoint every association (any role) source → canonical, skipping
    # rows that would duplicate an existing (entity, canonical, role).
    db.execute(text("""
        UPDATE track_artists SET artist_id = :t WHERE artist_id = :s
        AND NOT EXISTS (SELECT 1 FROM track_artists x
            WHERE x.track_id = track_artists.track_id AND x.artist_id = :t
              AND x.role = track_artists.role)
    """), {"t": canonical_id, "s": source_str})
    db.execute(text("DELETE FROM track_artists WHERE artist_id = :s"), {"s": source_str})
    db.execute(text("""
        UPDATE album_artists SET artist_id = :t WHERE artist_id = :s
        AND NOT EXISTS (SELECT 1 FROM album_artists x
            WHERE x.album_id = album_artists.album_id AND x.artist_id = :t
              AND x.role = album_artists.role)
    """), {"t": canonical_id, "s": source_str})
    db.execute(text("DELETE FROM album_artists WHERE artist_id = :s"), {"s": source_str})
    db.execute(text("DELETE FROM artists WHERE id = :s"), {"s": source_str})

    _stamp()
    return canonical_id


def recanonicalize_album(db: Session, album_id, canonical_title: str,
                         edition: Optional[str] = None) -> str:
    """Move an album onto a canonical title — editions collapse onto one album.

    The album axis of recanonicalize_artist: recompute the album UUID from
    (canonical_title, primary artist) and rename via CASCADE, or merge into the
    canonical album if it already exists (its variants are re-pointed by
    _update_album_uuid). The extracted edition is stamped onto THIS album's
    variant(s) BEFORE the move, so "Super Deluxe" / "Remaster" survive the
    collapse as named variants of the one album. Returns the final album id
    (unchanged when the title was already canonical). MB-independent (Tier-0).
    """
    aid = str(album_id)
    row = db.execute(text("""
        SELECT ar.name FROM album_artists aa
        JOIN artists ar ON ar.id = aa.artist_id
        WHERE aa.album_id = :aid AND aa.role = 'primary'
        LIMIT 1
    """), {"aid": aid}).fetchone()
    if not row:
        return aid  # no primary artist → album UUID isn't recomputable; leave as-is
    new_id = str(album_uuid(canonical_title, row[0]))

    if edition:
        db.execute(text(
            "UPDATE album_variants SET edition = :ed "
            "WHERE album_id = :aid AND edition IS NULL"
        ), {"ed": edition, "aid": aid})

    final_id = _update_album_uuid(db, aid, new_id)  # rename, or merge if canonical exists
    db.execute(text("UPDATE albums SET title = :t WHERE id = :id"),
               {"t": canonical_title, "id": final_id})
    return final_id


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

def _clean_all_artist_names(db: Session, dry_run: bool = False) -> Dict:
    """
    Pre-step: fix malformed artist names across ALL statuses.
    Removes placeholders (VARIOUS), trailing separators, double spaces.
    """
    logger.info("=== Pre-step: cleaning artist names ===")

    all_artists = db.execute(text("""
        SELECT id::text, name FROM artists
        WHERE verification_status NOT IN ('verified_split')
        ORDER BY name
    """)).fetchall()

    cleaned_count = 0
    merged_count = 0
    tracks_updated = 0
    albums_updated = 0
    for artist_id, name in all_artists:
        cleaned = _clean_artist_name(name)
        if cleaned == name or not cleaned:
            continue

        if not dry_run:
            new_artist_id = str(artist_uuid(cleaned))
            # Check if cleaned name already exists as different artist
            existing = db.execute(text(
                "SELECT id::text FROM artists WHERE id = :id"
            ), {"id": new_artist_id}).first()
            if existing and existing[0] != artist_id:
                # Merge: target artist already exists
                target_id = existing[0]

                # Recalculate track UUIDs (primary artist name changed)
                tracks = db.execute(text("""
                    SELECT t.id::text, t.title FROM tracks t
                    JOIN track_artists ta ON ta.track_id = t.id
                    WHERE ta.artist_id = :aid AND ta.role = 'primary'
                """), {"aid": artist_id}).fetchall()
                for old_track_id, title in tracks:
                    new_track_id = str(track_uuid(title, cleaned))
                    _update_track_uuid(db, old_track_id, new_track_id)
                    tracks_updated += 1

                # Recalculate album UUIDs
                albums = db.execute(text("""
                    SELECT a.id::text, a.title FROM albums a
                    JOIN album_artists aa ON aa.album_id = a.id
                    WHERE aa.artist_id = :aid AND aa.role = 'primary'
                """), {"aid": artist_id}).fetchall()
                for old_album_id, title in albums:
                    new_album_id_str = str(album_uuid(title, cleaned))
                    _update_album_uuid(db, old_album_id, new_album_id_str)
                    albums_updated += 1

                # Reassign artist associations to target
                db.execute(text("""
                    UPDATE track_artists SET artist_id = :target
                    WHERE artist_id = :old
                    AND NOT EXISTS (
                        SELECT 1 FROM track_artists ta2
                        WHERE ta2.track_id = track_artists.track_id
                        AND ta2.artist_id = :target
                        AND ta2.role = track_artists.role
                    )
                """), {"target": target_id, "old": artist_id})
                db.execute(text(
                    "DELETE FROM track_artists WHERE artist_id = :old"
                ), {"old": artist_id})
                db.execute(text("""
                    UPDATE album_artists SET artist_id = :target
                    WHERE artist_id = :old
                    AND NOT EXISTS (
                        SELECT 1 FROM album_artists aa2
                        WHERE aa2.album_id = album_artists.album_id
                        AND aa2.artist_id = :target
                        AND aa2.role = album_artists.role
                    )
                """), {"target": target_id, "old": artist_id})
                db.execute(text(
                    "DELETE FROM album_artists WHERE artist_id = :old"
                ), {"old": artist_id})
                # Delete the dirty artist (CASCADE handles remaining FK refs)
                db.execute(text(
                    "DELETE FROM artists WHERE id = :id"
                ), {"id": artist_id})
                merged_count += 1
                logger.info(f"Merged: '{name}' -> existing '{cleaned}' ({len(tracks)} tracks, {len(albums)} albums)")
                continue

            # Rename: update artist ID + name, recalculate track/album UUIDs
            if new_artist_id != artist_id:
                # Recalculate track UUIDs
                tracks = db.execute(text("""
                    SELECT t.id::text, t.title FROM tracks t
                    JOIN track_artists ta ON ta.track_id = t.id
                    WHERE ta.artist_id = :aid AND ta.role = 'primary'
                """), {"aid": artist_id}).fetchall()
                for old_track_id, title in tracks:
                    new_track_id = str(track_uuid(title, cleaned))
                    _update_track_uuid(db, old_track_id, new_track_id)
                    tracks_updated += 1

                # Recalculate album UUIDs
                albums = db.execute(text("""
                    SELECT a.id::text, a.title FROM albums a
                    JOIN album_artists aa ON aa.album_id = a.id
                    WHERE aa.artist_id = :aid AND aa.role = 'primary'
                """), {"aid": artist_id}).fetchall()
                for old_album_id, title in albums:
                    new_album_id_str = str(album_uuid(title, cleaned))
                    _update_album_uuid(db, old_album_id, new_album_id_str)
                    albums_updated += 1

                # Update artist ID (ON UPDATE CASCADE propagates to track_artists, etc.)
                db.execute(text(
                    "UPDATE artists SET id = :new_id, name = :name WHERE id = :old_id"
                ), {"new_id": new_artist_id, "name": cleaned, "old_id": artist_id})
            else:
                # UUID unchanged (e.g. only whitespace diff) — just update name
                db.execute(text(
                    "UPDATE artists SET name = :name WHERE id = :id"
                ), {"name": cleaned, "id": artist_id})

        cleaned_count += 1
        logger.info(f"Cleaned name: '{name}' -> '{cleaned}'")

    if not dry_run and cleaned_count > 0:
        db.commit()

    logger.info(
        f"Pre-step done: {cleaned_count} cleaned, {merged_count} merged, "
        f"{tracks_updated} tracks updated, {albums_updated} albums updated"
    )
    return {
        'cleaned': cleaned_count, 'merged': merged_count,
        'tracks_updated': tracks_updated, 'albums_updated': albums_updated,
        'scanned': len(all_artists),
    }


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

    if not dry_run:
        db.commit()

    logger.info(
        f"Artist split done: scanned {total['artists_scanned']}, safe splits {total['split']}"
    )
    return total


def unsplit_pass2_compounds(db: Session, dry_run: bool = False) -> Dict:
    """Collapse legacy Pass-2 splits back into whole compound artists.

    Pass 2 (Last.fm-verified '&'/','/'and'/'with'/'/' splitting) was removed: it
    was non-deterministic across nodes and silently forked artist (hence track/
    album) UUIDs, breaking P2P convergence. This is its data-migration inverse —
    one-shot on any node that carries pre-removal data, so its identities match
    what a fresh scan now produces (split only on feat./vs./… — never '&').

    Authoritative source is media_files.raw_artist/raw_album_artist (immutable
    scan-time ground truth) re-run through the scanner's exact identity formula
    `album_artist or artist`. That is what disambiguates pairs a member-set
    reverse cannot: '&' vs 'and' duplicates ("Trent Reznor & Atticus Ross" /
    "Trent Reznor and Atticus Ross") and swapped names ("A & B" / "B & A").
    Idempotent: a compound already whole maps current==target and is a no-op.
    """
    stale = {}  # compound_id (== artist_uuid(whole name)) -> display name
    for aid, name, probe in db.execute(text("""
        SELECT id::text, name, COALESCE(raw_name, name) AS probe
        FROM artists WHERE verification_status = 'verified_split'
    """)).fetchall():
        if detect_compound_type(probe) is None:
            stale[aid] = name

    stats = {"compounds": len(stale), "tracks_moved": 0, "tracks_merged": 0,
             "albums_moved": 0, "albums_merged": 0, "members_removed": 0}
    if not stale:
        logger.info("No legacy Pass-2 splits to collapse")
        return stats

    member_ids = {
        mid for (mid,) in db.execute(text("""
            SELECT DISTINCT member_artist_id::text FROM artist_members
            WHERE compound_artist_id::text = ANY(:ids)
        """), {"ids": list(stale)}).fetchall()
    }

    # Match every file to its scanner identity; keep those landing on a stale compound.
    # Tracks never collide (track_uuid embeds the title). Albums in shared
    # compilation directories can map to >1 compound — those are skipped (albums
    # are local-only, never synced, so their UUID needn't converge), leaving the
    # dedicated single-collab albums ("Beth Hart & Joe Bonamassa") to move cleanly.
    track_targets: Dict[str, str] = {}        # current track id -> whole compound name
    album_names: Dict[str, set] = defaultdict(set)
    for cur_track, cur_album, raw_artist, raw_album_artist in db.execute(text("""
        SELECT mf.track_id::text, av.album_id::text, mf.raw_artist, mf.raw_album_artist
        FROM media_files mf
        JOIN album_variants av ON av.id = mf.album_variant_id
        WHERE mf.raw_artist IS NOT NULL OR mf.raw_album_artist IS NOT NULL
    """)).fetchall():
        name = (raw_album_artist or raw_artist or "").strip()
        if name and str(artist_uuid(name)) in stale:
            track_targets[cur_track] = name
            album_names[cur_album].add(name)
    album_targets = {aid: next(iter(names)) for aid, names in album_names.items()
                     if len(names) == 1}
    ambiguous_albums = sum(1 for names in album_names.values() if len(names) > 1)

    if dry_run:
        logger.info(
            f"[dry-run] would collapse {len(stale)} compounds: "
            f"{len(track_targets)} tracks, {len(album_targets)} albums "
            f"({ambiguous_albums} compilation albums skipped), "
            f"up to {len(member_ids)} member artists GC'd"
        )
        return stats

    def _reset_assoc(table: str, entity_col: str, entity_id: str, artist_id: str):
        db.execute(text(f"DELETE FROM {table} WHERE {entity_col} = :e"), {"e": entity_id})
        db.execute(text(f"""
            INSERT INTO {table} ({entity_col}, artist_id, role)
            VALUES (:e, :a, 'primary') ON CONFLICT DO NOTHING
        """), {"e": entity_id, "a": artist_id})

    for cur_track, name in track_targets.items():
        row = db.execute(text("SELECT title FROM tracks WHERE id = :id"),
                         {"id": cur_track}).fetchone()
        if not row:
            continue  # already merged away by an earlier move
        target = str(track_uuid(row[0], name))
        merged = target != cur_track and db.execute(
            text("SELECT 1 FROM tracks WHERE id = :id"), {"id": target}).fetchone() is not None
        final = _update_track_uuid(db, cur_track, target)
        _reset_assoc("track_artists", "track_id", final, str(artist_uuid(name)))
        if merged:
            stats["tracks_merged"] += 1
        elif final != cur_track:
            stats["tracks_moved"] += 1

    for cur_album, name in album_targets.items():
        row = db.execute(text("SELECT title FROM albums WHERE id = :id"),
                         {"id": cur_album}).fetchone()
        if not row:
            continue
        target = str(album_uuid(row[0], name))
        merged = target != cur_album and db.execute(
            text("SELECT 1 FROM albums WHERE id = :id"), {"id": target}).fetchone() is not None
        final = _update_album_uuid(db, cur_album, target)
        _reset_assoc("album_artists", "album_id", final, str(artist_uuid(name)))
        if merged:
            stats["albums_merged"] += 1
        elif final != cur_album:
            stats["albums_moved"] += 1

    # Drop membership links; demote compounds to plain whole artists (local-only
    # metadata — not part of any synced UUID, reset purely so the row matches a
    # fresh scan and a future Pass-1 leaves it alone).
    db.execute(text("DELETE FROM artist_members WHERE compound_artist_id::text = ANY(:ids)"),
               {"ids": list(stale)})
    db.execute(text("""
        UPDATE artists SET verification_status = 'unverified',
                           artist_type = 'unknown', raw_name = NULL
        WHERE id::text = ANY(:ids)
    """), {"ids": list(stale)})

    # GC member artists that existed only because of the split (nothing left now).
    for mid in member_ids - set(stale):
        leftover = db.execute(text("""
            SELECT (SELECT count(*) FROM track_artists WHERE artist_id = :m)
                 + (SELECT count(*) FROM album_artists WHERE artist_id = :m)
                 + (SELECT count(*) FROM artist_members WHERE member_artist_id = :m)
        """), {"m": mid}).scalar()
        if leftover == 0:
            db.execute(text("DELETE FROM artists WHERE id = :m"), {"m": mid})
            stats["members_removed"] += 1

    db.commit()
    stats["albums_skipped"] = ambiguous_albums
    logger.info(
        f"Un-split done: {stats['compounds']} compounds collapsed, "
        f"{stats['tracks_moved']} tracks moved ({stats['tracks_merged']} merged), "
        f"{stats['albums_moved']} albums moved ({stats['albums_merged']} merged, "
        f"{ambiguous_albums} compilation albums left local), "
        f"{stats['members_removed']} orphan members removed"
    )
    return stats


def reidentify_per_track_artists(db: Session, dry_run: bool = False) -> Dict:
    """One-shot migration for the scanner identity fix: TRACK identity now comes
    from the per-track artist tag (the old `album_artist or artist` formula let
    album_artist win, collapsing every compilation cut under 'Various Artists'
    and guest tracks under the album artist — the real artist sat unused in
    media_files.raw_artist).

    Re-derives each affected track's primary artist from raw tags (only files
    where BOTH raw fields are non-empty AND differ — everything else is identical
    under both formulas, so canon renames are untouched), recomputes the track
    UUID and moves/merges via _update_track_uuid. Albums and album_artists stay —
    album identity still follows album_artist, so compilations remain one album.
    Deterministic (raw-driven): every node converges to the same result. Tracks
    whose files disagree on the new identity would need a per-file track split —
    skipped and counted instead (expected ~0).
    """
    rows = db.execute(text("""
        SELECT t.id::text AS tid, t.title, a.id::text AS cur_id,
               count(*) AS nfiles,
               count(*) FILTER (WHERE q.new_name IS NOT NULL) AS nqual,
               array_agg(DISTINCT q.new_name) FILTER (WHERE q.new_name IS NOT NULL) AS new_names
        FROM tracks t
        JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
        JOIN artists a ON a.id = ta.artist_id
        JOIN media_files mf ON mf.track_id = t.id
        CROSS JOIN LATERAL (
            SELECT CASE WHEN nullif(btrim(mf.raw_artist), '') IS NOT NULL
                         AND nullif(btrim(mf.raw_album_artist), '') IS NOT NULL
                         AND lower(btrim(mf.raw_artist)) <> lower(btrim(mf.raw_album_artist))
                        THEN btrim(mf.raw_artist) END AS new_name
        ) q
        GROUP BY t.id, t.title, a.id
        HAVING count(*) FILTER (WHERE q.new_name IS NOT NULL) > 0
    """)).fetchall()

    stats = {"affected": len(rows), "moved": 0, "merged": 0, "noop": 0, "skipped_mixed": 0}
    for tid, title, cur_id, nfiles, nqual, new_names in rows:
        if nqual < nfiles or len(new_names) > 1:
            stats["skipped_mixed"] += 1
            logger.warning(f"  mixed identity, skipping track {title!r}: {new_names}")
            continue
        new_name = new_names[0]
        new_aid = str(artist_uuid(new_name))
        if new_aid == cur_id:
            stats["noop"] += 1
            continue
        if dry_run:
            stats["moved"] += 1
            continue
        _ensure_artist(db, new_name)
        new_tid = str(track_uuid(title, new_name))
        merged = new_tid != tid and db.execute(
            text("SELECT 1 FROM tracks WHERE id = :id"), {"id": new_tid}).fetchone() is not None
        final = _update_track_uuid(db, tid, new_tid)
        db.execute(text(
            "DELETE FROM track_artists WHERE track_id = :t AND artist_id = :a AND role = 'primary'"
        ), {"t": final, "a": cur_id})
        db.execute(text(
            "INSERT INTO track_artists (track_id, artist_id, role) "
            "VALUES (:t, :a, 'primary') ON CONFLICT DO NOTHING"
        ), {"t": final, "a": new_aid})
        stats["merged" if merged else "moved"] += 1

    if not dry_run:
        db.commit()
    logger.info(f"Re-identify per-track artists: {stats}")
    return stats


def normalize_artists(
    db: Session,
    pass1: bool = True,
    dry_run: bool = False,
) -> Dict:
    """
    Run artist normalization — deterministic, offline. Splits only ALWAYS-collaboration
    separators (feat./ft./featuring/vs./pres./aka/meets); '&'/','/'and' are kept whole
    (splitting them needed a non-deterministic Last.fm lookup that forked UUIDs across nodes).

    Args:
        db: Database session
        pass1: Run the safe splitting pass
        dry_run: Only show what would be done, no DB changes
    """
    stats = {}

    # Pre-step: clean malformed names for ALL artists (placeholders, trailing junk)
    clean_stats = _clean_all_artist_names(db, dry_run=dry_run)
    stats['cleaned'] = clean_stats

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

    return stats


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s: %(message)s',
    )

    dry_run = '--dry-run' in sys.argv

    from database import get_db_context

    if '--unsplit' in sys.argv:
        with get_db_context() as db:
            logger.info("=== Collapse legacy Pass-2 splits ===")
            if dry_run:
                logger.info("[DRY RUN MODE]")
            stats = unsplit_pass2_compounds(db, dry_run=dry_run)
            print("\n=== Results ===")
            for key, value in stats.items():
                print(f"  {key}: {value}")
        sys.exit(0)

    if '--reidentify' in sys.argv:
        with get_db_context() as db:
            logger.info("=== Re-identify tracks by per-track artist tag ===")
            if dry_run:
                logger.info("[DRY RUN MODE]")
            stats = reidentify_per_track_artists(db, dry_run=dry_run)
            print("\n=== Results ===")
            for key, value in stats.items():
                print(f"  {key}: {value}")
        sys.exit(0)

    with get_db_context() as db:
        logger.info("=== Artist Normalization ===")
        if dry_run:
            logger.info("[DRY RUN MODE]")

        stats = normalize_artists(db, pass1=True, dry_run=dry_run)

        print("\n=== Results ===")
        for pass_name, pass_stats in stats.items():
            print(f"\n{pass_name}:")
            for key, value in pass_stats.items():
                print(f"  {key}: {value}")
