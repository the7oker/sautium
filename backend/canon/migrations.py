"""One-shot historical migrations + the Pass-1 normalization orchestrator.

Name cleaning, deterministic safe-separator splitting (Pass 1), and the
already-run legacy fixups (unsplit non-deterministic Pass-2 splits, re-derive
per-track primary artist). Kept apart from the live canon path: these are
idempotent sweeps invoked from the CLI / scan hooks, not the background loop.
"""
import logging
import re
from collections import defaultdict
from typing import List, Tuple, Optional, Dict

from sqlalchemy import text
from sqlalchemy.orm import Session

from uuid_utils import (artist_uuid, track_uuid, album_uuid, genre_uuid, tag_uuid,
                        gear_brand_uuid, gear_model_uuid, gear_caveat_uuid, gear_pair_uuid)
from canon.identity import (
    _SEAL_NULL, _clean_artist_name, _filter_parts, _ensure_artist,
    _update_track_uuid, _update_album_uuid, _merge_album_variants,
    delete_orphan_tracks, recanonicalize_artist,
)
from canon.split import detect_compound_type, normalize_compound_artist

logger = logging.getLogger(__name__)


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

            from transliterate import latinize
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
                    "UPDATE artists SET id = :new_id, name = :name, name_latin = :nl WHERE id = :old_id"
                ), {"new_id": new_artist_id, "name": cleaned, "nl": latinize(cleaned), "old_id": artist_id})
            else:
                # UUID unchanged (e.g. only whitespace diff) — just update name
                db.execute(text(
                    "UPDATE artists SET name = :name, name_latin = :nl WHERE id = :id"
                ), {"name": cleaned, "nl": latinize(cleaned), "id": artist_id})

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


# ---------------------------------------------------------------------------
# Identity re-normalization — the one-off after a uuid_utils.normalize change
# ---------------------------------------------------------------------------

_SEALED_TABLES = ("embedding_segments", "audio_features", "artist_bios", "artist_tags",
                  "similar_artists", "track_stats", "genre_descriptions", "albums",
                  "album_tracks", "track_mbids")
_TRACK_BATCH = 100_000


def _collision_groups(plan) -> Dict[str, list]:
    """plan: iterable of (old_id, new_id[, ...]) → {new: [members]} with 2+."""
    groups = defaultdict(list)
    for row in plan:
        groups[row[1]].append(row)
    return {new: members for new, members in groups.items() if len(members) > 1}


def _survivor(new: str, members: list, score) -> tuple:
    """The row that keeps its data in a merge: the one already sitting at the
    final uuid if any (renaming another onto it would collide), else the
    best-scoring one — score() returns a sortable tuple, larger = better."""
    for m in members:
        if m[0] == new:
            return m
    return max(members, key=lambda m: score(m[0]))


def _renormalize_artists(db: Session, dry_run: bool) -> Dict:
    rows = db.execute(text("SELECT id::text, name FROM artists")).fetchall()
    plan = [(i, str(artist_uuid(n)), n) for i, n in rows]
    changed = [r for r in plan if r[0] != r[1]]
    groups = _collision_groups(plan)
    stats = {"total": len(rows), "changing": len(changed),
             "collision_groups": len(groups), "merged": 0, "renamed": 0}
    if dry_run:
        for new, members in list(groups.items())[:30]:
            logger.info("  artist collision: %s", " | ".join(m[2] for m in members))
        return stats

    def score(aid):
        return tuple(db.execute(text("""
            SELECT (SELECT count(*) FROM track_artists ta JOIN media_files mf ON mf.track_id = ta.track_id
                     WHERE ta.artist_id = :a),
                   (SELECT count(*) FROM track_artists ta JOIN embeddings e ON e.track_id = ta.track_id
                     WHERE ta.artist_id = :a),
                   (SELECT count(*) FROM track_artists ta JOIN listening_history lh ON lh.track_id = ta.track_id
                     WHERE ta.artist_id = :a AND lh.completed),
                   (SELECT count(*) FROM artist_mbids WHERE artist_id = :a),
                   (SELECT count(*) FROM track_artists WHERE artist_id = :a),
                   -extract(epoch FROM (SELECT created_at FROM artists WHERE id = :a))
        """), {"a": aid}).fetchone())

    done = set()
    for new, members in groups.items():
        sid, _, sname = _survivor(new, members, score)
        if sid != new:
            db.execute(text("UPDATE artists SET id = :new WHERE id = :old"),
                       {"new": new, "old": sid})
        for mid, _, _ in members:
            if mid != sid:
                # merges tracks/albums under the survivor's name (recomputed
                # on the current rule) and repoints every association
                recanonicalize_artist(db, mid, sname)
                stats["merged"] += 1
        done.update(m[0] for m in members)
    for old, new, _ in changed:
        if old in done:
            continue
        db.execute(text("UPDATE artists SET id = :new WHERE id = :old"),
                   {"new": new, "old": old})
        stats["renamed"] += 1
    db.commit()
    return stats


def _renormalize_albums(db: Session, dry_run: bool) -> Dict:
    rows = db.execute(text("""
        SELECT al.id::text, al.title, a.name, count(*) OVER (PARTITION BY al.id) AS nprim
        FROM albums al
        JOIN album_artists aa ON aa.album_id = al.id AND aa.role = 'primary'
        JOIN artists a ON a.id = aa.artist_id""")).fetchall()
    # A multi-primary album's uuid was minted from the compound credit
    # string ("A & B") that nothing stores; albums are local-only, canon
    # re-merges any duplicate by release-group, so they keep their id.
    single = [(i, str(album_uuid(t, n)), t) for i, t, n, np in rows if np == 1]
    changed = [r for r in single if r[0] != r[1]]
    groups = _collision_groups(single)
    stats = {"total": len({r[0] for r in rows}), "single_primary": len(single),
             "skipped_multi_primary": len({r[0] for r in rows if r[3] > 1}),
             "changing": len(changed), "collision_groups": len(groups),
             "merged": 0, "renamed": 0}
    if dry_run:
        for new, members in list(groups.items())[:30]:
            logger.info("  album collision: %s", " | ".join(m[2] for m in members))
        return stats

    def score(aid):
        return tuple(db.execute(text("""
            SELECT (SELECT count(*) FROM album_variants WHERE album_id = :a),
                   (SELECT count(*) FROM albums WHERE id = :a AND musicbrainz_id IS NOT NULL),
                   (SELECT count(*) FROM album_tracks WHERE album_id = :a),
                   -extract(epoch FROM (SELECT created_at FROM albums WHERE id = :a))
        """), {"a": aid}).fetchone())

    done = set()
    for new, members in groups.items():
        sid, _, _ = _survivor(new, members, score)
        for mid, _, _ in members:
            if mid != sid:
                _update_album_uuid(db, mid, sid)
                stats["merged"] += 1
        if sid != new:
            _update_album_uuid(db, sid, new)
        done.update(m[0] for m in members)
    for old, new, _ in changed:
        if old in done:
            continue
        _update_album_uuid(db, old, new)
        stats["renamed"] += 1
    db.commit()
    return stats


def _renormalize_named(db: Session, table: str, uuid_fn, merge, dry_run: bool) -> Dict:
    """genres / tags: name-keyed, ON UPDATE CASCADE children."""
    rows = db.execute(text(f"SELECT id::text, name FROM {table}")).fetchall()
    plan = [(i, str(uuid_fn(n)), n) for i, n in rows]
    changed = [r for r in plan if r[0] != r[1]]
    groups = _collision_groups(plan)
    stats = {"total": len(rows), "changing": len(changed),
             "collision_groups": len(groups), "merged": 0, "renamed": 0}
    if dry_run:
        for new, members in list(groups.items())[:20]:
            logger.info("  %s collision: %s", table, " | ".join(m[2] for m in members))
        return stats
    done = set()
    for new, members in groups.items():
        sid, _, _ = _survivor(new, members, lambda i: (0,))
        for mid, _, _ in members:
            if mid != sid:
                merge(db, mid, sid)
                stats["merged"] += 1
        if sid != new:
            db.execute(text(f"UPDATE {table} SET id = :new WHERE id = :old"),
                       {"new": new, "old": sid})
        done.update(m[0] for m in members)
    for old, new, _ in changed:
        if old in done:
            continue
        db.execute(text(f"UPDATE {table} SET id = :new WHERE id = :old"),
                   {"new": new, "old": old})
        stats["renamed"] += 1
    db.commit()
    return stats


def _merge_genre(db: Session, old: str, new: str) -> None:
    p = {"old": old, "new": new}
    db.execute(text("""
        INSERT INTO album_genres (album_id, genre_id, source, count)
        SELECT album_id, :new, source, count FROM album_genres WHERE genre_id = :old
        ON CONFLICT DO NOTHING"""), p)
    db.execute(text("""
        UPDATE genre_descriptions gd SET genre_id = :new WHERE gd.genre_id = :old
          AND NOT EXISTS (SELECT 1 FROM genre_descriptions x
                           WHERE x.genre_id = :new AND x.source = gd.source)"""), p)
    db.execute(text("""
        UPDATE genre_desc_embeddings ge SET genre_id = :new WHERE ge.genre_id = :old
          AND NOT EXISTS (SELECT 1 FROM genre_desc_embeddings x
                           WHERE x.genre_id = :new AND x.model_id = ge.model_id
                             AND x.chunk_index = ge.chunk_index)"""), p)
    db.execute(text("DELETE FROM genres WHERE id = :old"), p)


def _merge_tag(db: Session, old: str, new: str) -> None:
    p = {"old": old, "new": new}
    db.execute(text("""
        UPDATE artist_tags at2 SET tag_id = :new WHERE at2.tag_id = :old
          AND NOT EXISTS (SELECT 1 FROM artist_tags x
                           WHERE x.artist_id = at2.artist_id AND x.tag_id = :new
                             AND x.source = at2.source)"""), p)
    db.execute(text("DELETE FROM tags WHERE id = :old"), p)


def _cascade_gear_fks(db: Session) -> int:
    """Every FK onto gear_brands / gear_models gets ON UPDATE CASCADE (the
    rule every other uuid5 entity already follows; 001_initial.sql carries
    the same change). Idempotent — constraints already cascading are left."""
    rows = db.execute(text("""
        SELECT conrelid::regclass::text AS child, conname, pg_get_constraintdef(oid) AS def
        FROM pg_constraint
        WHERE contype = 'f'
          AND confrelid IN ('gear_brands'::regclass, 'gear_models'::regclass)""")).fetchall()
    n = 0
    for child, conname, definition in rows:
        if "ON UPDATE CASCADE" in definition:
            continue
        new_def = definition.replace(" ON DELETE", " ON UPDATE CASCADE ON DELETE") \
            if " ON DELETE" in definition else definition + " ON UPDATE CASCADE"
        db.execute(text(f'ALTER TABLE {child} DROP CONSTRAINT "{conname}"'))
        db.execute(text(f'ALTER TABLE {child} ADD CONSTRAINT "{conname}" {new_def}'))
        n += 1
    return n


def _renormalize_gear(db: Session, dry_run: bool) -> Dict:
    """Brands and models are names; caveats and pair notes derive from the
    model ids and follow. Spec attributes, technologies and registry rows
    are keys (normalize_key) — untouched by the rule. Collisions here would
    be a data error, not typography: the run aborts before mutating."""
    brands = db.execute(text("SELECT id::text, name FROM gear_brands")).fetchall()
    b_plan = [(i, str(gear_brand_uuid(n)), n) for i, n in brands]
    models = db.execute(text("""
        SELECT m.id::text, b.name, m.model, m.category::text
        FROM gear_models m JOIN gear_brands b ON b.id = m.brand_id""")).fetchall()
    m_plan = [(i, str(gear_model_uuid(b, m, c)), f"{b} {m}") for i, b, m, c in models]
    stats = {"brands_changing": sum(1 for r in b_plan if r[0] != r[1]),
             "models_changing": sum(1 for r in m_plan if r[0] != r[1]),
             "collision_groups": len(_collision_groups(b_plan)) + len(_collision_groups(m_plan))}
    if stats["collision_groups"]:
        raise RuntimeError(f"gear identity collision — resolve by hand first: "
                           f"{_collision_groups(b_plan)} {_collision_groups(m_plan)}")
    if dry_run:
        return stats
    stats["fks_cascaded"] = _cascade_gear_fks(db)
    for old, new, _ in b_plan:
        if old != new:
            db.execute(text("UPDATE gear_brands SET id = :new WHERE id = :old"),
                       {"new": new, "old": old})
    # A pair note stores (model_a < model_b); a cascaded model id can land
    # on either side of that order, so the check rests while the ids move
    # and the pairs are re-canonicalized before it is put back.
    db.execute(text("ALTER TABLE gear_pair_notes DROP CONSTRAINT gear_pair_notes_check"))
    for old, new, _ in m_plan:
        if old != new:
            db.execute(text("UPDATE gear_models SET id = :new WHERE id = :old"),
                       {"new": new, "old": old})
    db.execute(text("UPDATE gear_pair_notes SET model_a = model_b, model_b = model_a "
                    "WHERE model_a > model_b"))
    db.execute(text("ALTER TABLE gear_pair_notes ADD CONSTRAINT gear_pair_notes_check "
                    "CHECK (model_a < model_b)"))
    caveats = db.execute(text(
        "SELECT id::text, gear_model_id::text, text FROM gear_measured_caveats")).fetchall()
    for old, mid, txt in caveats:
        new = str(gear_caveat_uuid(mid, txt))
        if new != old:
            db.execute(text("DELETE FROM gear_measured_caveats WHERE id = :new"), {"new": new})
            db.execute(text("UPDATE gear_measured_caveats SET id = :new WHERE id = :old"),
                       {"new": new, "old": old})
    pairs = db.execute(text(
        "SELECT id::text, model_a::text, model_b::text FROM gear_pair_notes")).fetchall()
    for old, a, b in pairs:
        new = str(gear_pair_uuid(a, b))
        if new != old:
            db.execute(text("DELETE FROM gear_pair_notes WHERE id = :new"), {"new": new})
            db.execute(text("UPDATE gear_pair_notes SET id = :new WHERE id = :old"),
                       {"new": new, "old": old})
    db.commit()
    return stats


def _renormalize_tracks(db: Session, dry_run: bool) -> Dict:
    """The bulk. Every track's new uuid goes into a temp map (COPY, a few
    million rows in seconds); collision groups merge through
    _update_track_uuid one by one; everything else is renamed in batched
    UPDATEs whose ON UPDATE CASCADE carries the fifteen child tables."""
    import io
    import psycopg2
    from config import settings
    raw = psycopg2.connect(settings.database_url)
    raw.autocommit = False
    try:
        cur = raw.cursor()
        cur.execute("CREATE TEMP TABLE _tmap (old uuid PRIMARY KEY, new uuid NOT NULL)")
        src = raw.cursor(name="renorm_tracks")
        src.itersize = 50_000
        src.execute("""
            SELECT t.id::text, t.title, a.name
            FROM tracks t
            JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
            JOIN artists a ON a.id = ta.artist_id""")
        buf, n = io.StringIO(), 0
        for tid, title, name in src:
            buf.write(f"{tid}\t{track_uuid(title, name)}\n")
            n += 1
            if n % 250_000 == 0:
                buf.seek(0)
                cur.copy_from(buf, "_tmap", columns=("old", "new"))
                buf = io.StringIO()
                logger.info("  track map: %d rows", n)
        buf.seek(0)
        cur.copy_from(buf, "_tmap", columns=("old", "new"))
        src.close()
        cur.execute("CREATE INDEX ON _tmap (new)")
        cur.execute("ANALYZE _tmap")
        cur.execute("SELECT count(*) FILTER (WHERE old <> new) FROM _tmap")
        changing = cur.fetchone()[0]
        cur.execute("""SELECT new::text, array_agg(old::text) FROM _tmap
                       GROUP BY new HAVING count(*) > 1""")
        groups = cur.fetchall()
        raw.commit()
        stats = {"total": n, "changing": changing, "collision_groups": len(groups),
                 "tracks_in_collisions": sum(len(g[1]) for g in groups),
                 "merged": 0, "renamed": 0}
        if dry_run:
            return stats

        def score(tid):
            return tuple(db.execute(text("""
                SELECT (SELECT count(*) FROM media_files WHERE track_id = :t),
                       (SELECT coalesce(max(CASE s.origin::text WHEN 'local' THEN 2
                                            WHEN 'deezer' THEN 1 WHEN 'youtube' THEN 0
                                            ELSE -1 END), -2)
                          FROM embeddings e LEFT JOIN analysis_sources s ON s.id = e.analysis_source_id
                         WHERE e.track_id = :t),
                       (SELECT count(*) FROM listening_history WHERE track_id = :t),
                       (SELECT count(*) FROM album_tracks WHERE track_id = :t),
                       -extract(epoch FROM (SELECT created_at FROM tracks WHERE id = :t))
            """), {"t": tid}).fetchone())

        for k, (new, olds) in enumerate(groups, 1):
            sid = new if new in olds else max(olds, key=score)
            for old in olds:
                if old != sid:
                    _update_track_uuid(db, old, sid)
                    stats["merged"] += 1
            if sid != new:
                _update_track_uuid(db, sid, new)
            if k % 500 == 0:
                db.commit()
                logger.info("  track collisions: %d/%d groups", k, len(groups))
        db.commit()

        # Two plain statements — an OR'd IN-subquery over the same table
        # planned as a per-row subplan (measured: 3M × aggregate, never
        # finished). Collision rows are done above; unchanged rows stay.
        cur.execute("""CREATE TEMP TABLE _coll AS
                       SELECT new FROM _tmap GROUP BY new HAVING count(*) > 1""")
        cur.execute("DELETE FROM _tmap m USING _coll c WHERE m.new = c.new")
        cur.execute("DELETE FROM _tmap WHERE old = new")
        cur.execute("""CREATE TEMP TABLE _tb AS
                       SELECT old, new, (row_number() OVER (ORDER BY old) - 1) / %s AS batch
                       FROM _tmap""", (_TRACK_BATCH,))
        cur.execute("CREATE INDEX ON _tb (batch)")
        cur.execute("SELECT coalesce(max(batch), -1) + 1 FROM _tb")
        nb = cur.fetchone()[0]
        raw.commit()
        for b in range(nb):
            cur.execute("UPDATE tracks t SET id = m.new FROM _tb m "
                        "WHERE t.id = m.old AND m.batch = %s", (b,))
            stats["renamed"] += cur.rowcount
            raw.commit()
            logger.info("  track rename batch %d/%d (%d rows so far)", b + 1, nb, stats["renamed"])
        return stats
    finally:
        raw.close()


def _shed_all_seals(db: Session) -> Dict:
    """Every seal payload binds an entity uuid; after the rewrite none of
    them verifies. Null them all (imported ones included — a foreign seal on
    a re-keyed row is just as dead, and unsealed rows never travel) and drop
    the now-unreferenced batches; sign_audio re-seals first-hand material in
    one batch. Authorship dates restart at that batch — accepted while the
    network has one live node."""
    out = {}
    for t in _SEALED_TABLES:
        res = db.execute(text(f"UPDATE {t} SET {_SEAL_NULL} WHERE signature IS NOT NULL"))
        out[t] = res.rowcount
    out["signing_batches"] = db.execute(text("DELETE FROM signing_batches")).rowcount
    db.commit()
    return out


def _verify_identities(db: Session) -> Dict:
    """Post-check: id == uuid(current rule) for every artist, single-primary
    album, genre, tag and track. Nonzero drift = the run must not be trusted."""
    out = {}
    out["artists"] = sum(1 for i, n in db.execute(text("SELECT id::text, name FROM artists"))
                         if str(artist_uuid(n)) != i)
    out["genres"] = sum(1 for i, n in db.execute(text("SELECT id::text, name FROM genres"))
                        if str(genre_uuid(n)) != i)
    out["tags"] = sum(1 for i, n in db.execute(text("SELECT id::text, name FROM tags"))
                      if str(tag_uuid(n)) != i)
    out["albums_single_primary"] = sum(
        1 for i, t, n, np in db.execute(text("""
            SELECT al.id::text, al.title, a.name, count(*) OVER (PARTITION BY al.id)
            FROM albums al JOIN album_artists aa ON aa.album_id = al.id AND aa.role = 'primary'
            JOIN artists a ON a.id = aa.artist_id"""))
        if np == 1 and str(album_uuid(t, n)) != i)
    drift = 0
    for i, t, n in db.execute(text("""
            SELECT t.id::text, t.title, a.name FROM tracks t
            JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
            JOIN artists a ON a.id = ta.artist_id""").execution_options(yield_per=50_000)):
        if str(track_uuid(t, n)) != i:
            drift += 1
    out["tracks"] = drift
    return out


def renormalize_identities(db: Session, dry_run: bool = False) -> Dict:
    """Rewrite every uuid5 identity under the CURRENT uuid_utils rules — the
    one-off that must follow a normalize() change on a live node (2026-08-25,
    v2: punctuation folding). Order matters: artists first (track and album
    uuids embed the primary artist's name), then albums, genres, tags, gear,
    and tracks last — the bulk. Rows whose new uuid collides merge (the
    survivor is whichever holds more: files, analysis, listens); the rest are
    renamed through ON UPDATE CASCADE. Every seal is shed at the end — the
    payloads bind the uuids — and sign_audio re-seals in one batch.

    Run with the backend STOPPED: a scan or a stream enrich landing mid-way
    mints on one rule next to rows still on the other. Dry-run reports the
    plan (counts, collision samples) and touches nothing."""
    stats: Dict = {}
    logger.info("=== artists ===")
    stats["artists"] = _renormalize_artists(db, dry_run)
    logger.info("%s", stats["artists"])
    logger.info("=== albums ===")
    stats["albums"] = _renormalize_albums(db, dry_run)
    logger.info("%s", stats["albums"])
    logger.info("=== genres ===")
    stats["genres"] = _renormalize_named(db, "genres", genre_uuid, _merge_genre, dry_run)
    logger.info("%s", stats["genres"])
    logger.info("=== tags ===")
    stats["tags"] = _renormalize_named(db, "tags", tag_uuid, _merge_tag, dry_run)
    logger.info("%s", stats["tags"])
    logger.info("=== gear ===")
    stats["gear"] = _renormalize_gear(db, dry_run)
    logger.info("%s", stats["gear"])
    logger.info("=== tracks ===")
    stats["tracks"] = _renormalize_tracks(db, dry_run)
    logger.info("%s", stats["tracks"])
    if dry_run:
        return stats
    # An album merge keeps the survivor's slot where both filled it; the
    # loser's track in that slot is left holding nothing — drop such rows.
    stats["orphan_tracks_deleted"] = delete_orphan_tracks(db)
    db.commit()
    logger.info("orphan tracks deleted: %d", stats["orphan_tracks_deleted"])
    logger.info("=== seals ===")
    stats["seals_shed"] = _shed_all_seals(db)
    logger.info("%s", stats["seals_shed"])
    logger.info("=== verify ===")
    stats["drift"] = _verify_identities(db)
    logger.info("%s", stats["drift"])
    return stats


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s: %(message)s',
    )

    dry_run = '--dry-run' in sys.argv

    from database import get_db_context

    if '--renormalize' in sys.argv:
        with get_db_context() as db:
            logger.info("=== Re-normalize identities (uuid_utils rules) ===")
            if dry_run:
                logger.info("[DRY RUN MODE]")
            stats = renormalize_identities(db, dry_run=dry_run)
            print("\n=== Results ===")
            for key, value in stats.items():
                print(f"  {key}: {value}")
        sys.exit(0)

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
