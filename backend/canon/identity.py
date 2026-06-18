"""Canonical-identity primitives (Layer 2 — shared base).

Deterministic UUID (re)writes and merges for artists / albums / album-variants
via ON UPDATE CASCADE, plus name cleaning. These are the low-level operations
every higher canon layer (split, content, migrations) builds on; this module
imports nothing from the other canon modules (it is the base of the DAG).
"""
import logging
import re
from collections import defaultdict
from typing import List, Tuple, Optional, Dict

from sqlalchemy import text
from sqlalchemy.orm import Session

from uuid_utils import artist_uuid, track_uuid, album_uuid

logger = logging.getLogger(__name__)


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


def _merge_album_variants(db: Session, new_album_id: str, *,
                          from_album: str = None, variant_ids: List[int] = None,
                          edition: str = None) -> None:
    """Repoint variants onto ``new_album_id`` honouring the
    UNIQUE(directory_path, album_id) key. When a variant's directory already
    holds a variant of the target album (a box-split sibling, or a folder whose
    files canon now unifies), fold the moving variant's media_files into that
    keeper and drop it instead of colliding; otherwise repoint in place.

    Source is either every variant of ``from_album`` or an explicit
    ``variant_ids`` list. ``edition`` is stamped on the repoint path."""
    if from_album is not None:
        rows = db.execute(text(
            "SELECT id, directory_path FROM album_variants WHERE album_id = :a ORDER BY id"
        ), {"a": from_album}).fetchall()
    else:
        rows = db.execute(text(
            "SELECT id, directory_path FROM album_variants WHERE id = ANY(:v) ORDER BY id"
        ), {"v": list(variant_ids)}).fetchall()
    for vid, dir_path in rows:
        keeper = db.execute(text(
            "SELECT id FROM album_variants "
            "WHERE directory_path = :d AND album_id = :a AND id <> :v"
        ), {"d": dir_path, "a": new_album_id, "v": vid}).fetchone()
        if keeper:
            db.execute(text("UPDATE media_files SET album_variant_id = :k WHERE album_variant_id = :v"),
                       {"k": keeper[0], "v": vid})
            db.execute(text("DELETE FROM album_variants WHERE id = :v"), {"v": vid})
        elif edition is None:
            db.execute(text("UPDATE album_variants SET album_id = :a WHERE id = :v"),
                       {"a": new_album_id, "v": vid})
        else:
            db.execute(text("UPDATE album_variants SET album_id = :a, edition = :e WHERE id = :v"),
                       {"a": new_album_id, "e": edition, "v": vid})


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
        # Merge: move album variants to target (folding same-directory siblings),
        # delete old.
        _merge_album_variants(db, new_str, from_album=old_str)
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


def recanonicalize_album_variants(db: Session, src_album_id, variant_ids: List[int],
                                  canonical_title: str,
                                  edition: Optional[str] = None) -> str:
    """Move a SUBSET of an album's variants onto their own edition row.

    The per-variant sibling of recanonicalize_album. When one album row holds
    several *editions* (different tracklists wrongly collapsed as variants), this
    splits the named variants off onto `album_uuid(canonical_title, primary
    artist)` — a fresh row that inherits the source's release group — while the
    rest stay put. Each edition keeps its own tracklist; true rips (the variants
    sharing one tracklist) are exactly what's left together. Returns the
    destination album id; it EQUALS the source when `canonical_title` is already
    the source's identity (the primary edition stays in place, only relabelled).
    `album_variants.raw_title` is the immutable scan-time title, so the split
    reverses; albums are local-only, never synced.
    """
    src = str(src_album_id)
    vids = [int(v) for v in variant_ids]
    if not vids:
        return src
    row = db.execute(text("""
        SELECT ar.name FROM album_artists aa JOIN artists ar ON ar.id = aa.artist_id
        WHERE aa.album_id = :a AND aa.role = 'primary'
        ORDER BY ar.id LIMIT 1
    """), {"a": src}).fetchone()
    if not row:
        return src  # no primary artist → album UUID isn't recomputable; leave as-is
    new_id = str(album_uuid(canonical_title, row[0]))

    if new_id == src:
        # Primary edition stays in place — just (re)label its variants and pin the title.
        db.execute(text("UPDATE album_variants SET edition = :ed WHERE id = ANY(:v)"),
                   {"ed": edition, "v": vids})
        db.execute(text("UPDATE albums SET title = :t WHERE id = :a"),
                   {"t": canonical_title, "a": src})
        return src

    # Materialize the destination edition row, inheriting the release group.
    db.execute(text("""
        INSERT INTO albums (id, title, release_year, musicbrainz_id, mb_match_confidence)
        SELECT :nid, :t, a.release_year, a.musicbrainz_id, a.mb_match_confidence
        FROM albums a WHERE a.id = :a
        ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title
    """), {"nid": new_id, "t": canonical_title, "a": src})
    db.execute(text("""
        INSERT INTO album_artists (album_id, artist_id, role, mbid)
        SELECT :nid, artist_id, role, mbid FROM album_artists WHERE album_id = :a
        ON CONFLICT DO NOTHING
    """), {"nid": new_id, "a": src})
    # Move the edition's variants onto the destination album, folding any that
    # would collide with an existing variant of it in the same directory.
    _merge_album_variants(db, new_id, variant_ids=vids, edition=edition)
    # GC the source if every edition moved out (no bare primary edition remained).
    db.execute(text("""
        DELETE FROM albums WHERE id = :a
          AND NOT EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = :a)
    """), {"a": src})
    return new_id


# ─── Core normalization logic ─────────────────────────────────────────────

