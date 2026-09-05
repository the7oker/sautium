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
    from transliterate import latinize
    uid = artist_uuid(name)
    exists = db.execute(
        text("SELECT 1 FROM artists WHERE id = :id"), {"id": str(uid)}
    ).fetchone()
    if not exists:
        db.execute(text("""
            INSERT INTO artists (id, name, name_latin)
            VALUES (:id, :name, :name_latin)
            ON CONFLICT (id) DO NOTHING
        """), {"id": str(uid), "name": name, "name_latin": latinize(name)})
        logger.info(f"  Created artist: {name} ({uid})")
    return uid


def elect_analysis_source(db: Session, track_id) -> None:
    """Designate the best-quality media_file of a track as its analysis source.

    Priority: CD (16-bit lossless) > other lossless > lossy. Two statements —
    losers cleared before the winner is set — because a single UPDATE that flips
    both rows transiently holds two TRUE rows for one track_id, which the partial
    unique index uq_media_files_analysis_source rejects. When the elected file
    differs from the one the current embedding was analyzed from, the embeddings
    pending predicate re-analyzes on the next pass (media_file_id mismatch).

    The media_files one-source-per-track invariant belongs to this base layer;
    the scanner and the merge path (_update_track_uuid) are both callers.
    """
    ranked = """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       ORDER BY
                           (bit_depth = 16 AND is_lossless) DESC,
                           is_lossless DESC,
                           id
                   ) as rn
            FROM media_files
            WHERE track_id = :tid
        )
    """
    db.execute(text(ranked + """
        UPDATE media_files
        SET is_analysis_source = false
        WHERE track_id = :tid AND is_analysis_source
          AND id <> (SELECT id FROM ranked WHERE rn = 1)
    """), {"tid": track_id})
    db.execute(text(ranked + """
        UPDATE media_files
        SET is_analysis_source = true
        WHERE id = (SELECT id FROM ranked WHERE rn = 1)
          AND NOT is_analysis_source
    """), {"tid": track_id})


# Mirrors provenance.ORIGIN_RANK: a local rip beats a lossless stream beats a
# lossy one; an unlinked (legacy) analysis ranks below all of them.
_ORIGIN_RANK_SQL = ("CASE s.origin::text WHEN 'local' THEN 2 WHEN 'deezer' THEN 1 "
                    "WHEN 'youtube' THEN 0 ELSE -1 END")

_SEAL_NULL = "author_pubkey = NULL, signature = NULL, batch_root = NULL, merkle_proof = NULL"


def _analysis_rank(db: Session, track_id: str) -> int:
    """Best provenance rank among a track's embeddings rows; -2 = none."""
    best = db.execute(text(f"""
        SELECT MAX({_ORIGIN_RANK_SQL}) FROM embeddings e
        LEFT JOIN analysis_sources s ON s.id = e.analysis_source_id
        WHERE e.track_id = :tid"""), {"tid": track_id}).scalar()
    return -2 if best is None else int(best)


def _shed_track_seals(db: Session, track_id: str) -> None:
    """Null the audio/stat seals of a track whose uuid just changed hands.

    Every seal payload binds the track uuid (segment_payload / features_payload
    / the track_stat entity), so after a rename or a merge the stored
    signatures verify against a uuid the row no longer carries — the
    album_tracks / track_mbids guards shed theirs on the cascaded track_id
    change, these tables have no track_id in their guard. sign_audio
    re-seals on its next pass; nothing travels unsigned in between."""
    db.execute(text(f"""
        UPDATE embedding_segments es SET {_SEAL_NULL}
        FROM embeddings e
        WHERE e.id = es.embedding_id AND e.track_id = :tid
          AND es.signature IS NOT NULL"""), {"tid": track_id})
    db.execute(text(f"UPDATE audio_features SET {_SEAL_NULL} "
                    "WHERE track_id = :tid AND signature IS NOT NULL"),
               {"tid": track_id})
    db.execute(text(f"UPDATE track_stats SET {_SEAL_NULL} "
                    "WHERE track_id = :tid AND signature IS NOT NULL"),
               {"tid": track_id})


def _move_analysis(db: Session, old: str, new: str) -> None:
    """Re-home the old track's analysis (sources, embeddings + segments,
    features) onto ``new``, which holds none. A same-pcm_hash source already
    on the target is the same audio: the moving rows re-point at it and the
    duplicate source is dropped (UNIQUE (track_id, pcm_hash))."""
    p = {"old": old, "new": new}
    for tbl in ("embeddings", "audio_features"):
        db.execute(text(f"""
            UPDATE {tbl} x SET analysis_source_id = t.id
            FROM analysis_sources o
            JOIN analysis_sources t ON t.track_id = :new AND t.pcm_hash = o.pcm_hash
            WHERE x.analysis_source_id = o.id AND o.track_id = :old"""), p)
    db.execute(text("""
        DELETE FROM analysis_sources o WHERE o.track_id = :old
          AND EXISTS (SELECT 1 FROM analysis_sources t
                       WHERE t.track_id = :new AND t.pcm_hash = o.pcm_hash)"""), p)
    db.execute(text("UPDATE analysis_sources SET track_id = :new WHERE track_id = :old"), p)
    db.execute(text("UPDATE embeddings SET track_id = :new WHERE track_id = :old"), p)
    db.execute(text("UPDATE audio_features SET track_id = :new WHERE track_id = :old"), p)


def _update_track_uuid(db: Session, old_id, new_id) -> str:
    """
    Update track UUID via ON UPDATE CASCADE, or merge if target exists.
    Returns the final track UUID.

    Merge moves EVERYTHING the old row holds that the target lacks — files,
    listens, play stats, tracklist slots, recording bindings, stats, lyrics,
    text embeddings, session rows — and the better-provenance analysis of
    the two (a phantom's sealed lossless-stream analysis merging into an
    owned track that was never analyzed moves rather than dies: GPU work
    nobody re-derives without the audio). Seals of whatever changed uuid are
    shed; sign_audio re-seals.
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
        _shed_track_seals(db, new_str)
    else:
        p = {"new": new_str, "old": old_str}
        # Analysis: keep the better provenance, move it if it is the old
        # row's (its seals are shed below — the payload binds the uuid).
        if _analysis_rank(db, old_str) > _analysis_rank(db, new_str):
            db.execute(text("DELETE FROM embeddings WHERE track_id = :new"), p)
            db.execute(text("DELETE FROM audio_features WHERE track_id = :new"), p)
            _move_analysis(db, old_str, new_str)
            _shed_track_seals(db, new_str)
        # Structure. Slots: the (album, disc, position) PK can't collide —
        # two tracks never share a slot. Recording bindings: keyed by the
        # recording itself, so they simply follow. Both guards shed the seal
        # on the track_id change.
        db.execute(text("UPDATE album_tracks SET track_id = :new WHERE track_id = :old"), p)
        db.execute(text("UPDATE track_mbids SET track_id = :new WHERE track_id = :old"), p)
        db.execute(text("""
            INSERT INTO track_artists (track_id, artist_id, role)
            SELECT :new, artist_id, role FROM track_artists WHERE track_id = :old
            ON CONFLICT DO NOTHING"""), p)
        # One-per-(track, key) tables: move what the target lacks; a moved
        # stats row carries the old uuid in its seal payload — shed it.
        db.execute(text(f"""
            UPDATE track_stats ts SET track_id = :new, {_SEAL_NULL}
            WHERE ts.track_id = :old
              AND NOT EXISTS (SELECT 1 FROM track_stats x
                               WHERE x.track_id = :new AND x.source = ts.source)"""), p)
        db.execute(text("""
            UPDATE track_lyrics tl SET track_id = :new
            WHERE tl.track_id = :old
              AND NOT EXISTS (SELECT 1 FROM track_lyrics x
                               WHERE x.track_id = :new AND x.source = tl.source)"""), p)
        db.execute(text("""
            UPDATE text_embeddings te SET track_id = :new
            WHERE te.track_id = :old
              AND NOT EXISTS (SELECT 1 FROM text_embeddings x
                               WHERE x.track_id = :new AND x.model_id = te.model_id)"""), p)
        db.execute(text("""
            UPDATE lyrics_embeddings le SET track_id = :new
            WHERE le.track_id = :old
              AND NOT EXISTS (SELECT 1 FROM lyrics_embeddings x
                               WHERE x.track_id = :new AND x.model_id = le.model_id
                                 AND x.chunk_index = le.chunk_index)"""), p)
        db.execute(text("UPDATE session_tracks SET track_id = :new WHERE track_id = :old"), p)
        db.execute(text("UPDATE listening_sessions SET seed_track_id = :new "
                        "WHERE seed_track_id = :old"), p)
        # Per-user data. Drop the moving files' analysis-source flag first —
        # the target keeps its own single source, so the repoint can't trip
        # uq_media_files_analysis_source with two TRUE rows on one track_id
        # (the crash this guards against).
        db.execute(
            text("UPDATE media_files SET is_analysis_source = false "
                 "WHERE track_id = :old AND is_analysis_source"),
            {"old": old_str},
        )
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
        # CASCADE takes whatever lost the merge: the weaker analysis, duplicate
        # stats/lyrics/embeddings, the old associations.
        db.execute(text("DELETE FROM tracks WHERE id = :old"), {"old": old_str})
        # Re-elect one analysis source across the merged files (best quality
        # wins; a better rip that moved in schedules re-analysis via the
        # embeddings pending predicate — asrc.media_file_id no longer matches).
        elect_analysis_source(db, new_str)

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
        # The album seal's entity is the uuid; the tracklist rows shed theirs
        # on the cascaded album_id change (guard), this one has no guard for id.
        db.execute(text(f"UPDATE albums SET {_SEAL_NULL} "
                        "WHERE id = :id AND signature IS NOT NULL"), {"id": new_str})
    else:
        # Merge: move album variants to target (folding same-directory
        # siblings), then everything else the target lacks — tracklist slots
        # (a slot the target already fills is the target's), artist links,
        # genres, descriptions, streaming mints, session origins and slots;
        # fill blank canon fields from the old row — then delete old.
        _merge_album_variants(db, new_str, from_album=old_str)
        p = {"new": new_str, "old": old_str}
        db.execute(text("""
            UPDATE album_tracks at2 SET album_id = :new
            WHERE at2.album_id = :old
              AND NOT EXISTS (SELECT 1 FROM album_tracks x
                               WHERE x.album_id = :new AND x.disc = at2.disc
                                 AND x.position = at2.position)"""), p)
        db.execute(text("""
            INSERT INTO album_artists (album_id, artist_id, role, mbid)
            SELECT :new, artist_id, role, mbid FROM album_artists WHERE album_id = :old
            ON CONFLICT DO NOTHING"""), p)
        db.execute(text("""
            INSERT INTO album_genres (album_id, genre_id, source, count)
            SELECT :new, genre_id, source, count FROM album_genres WHERE album_id = :old
            ON CONFLICT DO NOTHING"""), p)
        db.execute(text("""
            UPDATE album_descriptions ad SET album_id = :new
            WHERE ad.album_id = :old
              AND NOT EXISTS (SELECT 1 FROM album_descriptions x
                               WHERE x.album_id = :new AND x.source = ad.source)"""), p)
        db.execute(text("""
            UPDATE seed_picks sp SET album_id = :new
            WHERE sp.album_id = :old
              AND NOT EXISTS (SELECT 1 FROM seed_picks x WHERE x.album_id = :new)"""), p)
        db.execute(text("UPDATE listening_sessions SET origin_album_id = :new "
                        "WHERE origin_album_id = :old"), p)
        db.execute(text("UPDATE session_tracks SET album_id = :new "
                        "WHERE album_id = :old"), p)
        db.execute(text("""
            UPDATE albums n SET
                musicbrainz_id = COALESCE(n.musicbrainz_id, o.musicbrainz_id),
                mb_match_confidence = COALESCE(n.mb_match_confidence, o.mb_match_confidence),
                release_year = COALESCE(n.release_year, o.release_year),
                cover_url = COALESCE(n.cover_url, o.cover_url)
            FROM albums o WHERE n.id = :new AND o.id = :old"""), p)
        db.execute(text("DELETE FROM albums WHERE id = :old"), {"old": old_str})

    return new_str


def delete_orphan_tracks(db: Session) -> int:
    """Drop track rows nothing refers to any more — no file, no tracklist
    slot, no analysis, no listen, no lyrics, no session. A re-keyed phantom
    slot (the mint moved it to another uuid) leaves such a row behind; a
    track that carries anything at all is not an orphan and stays."""
    res = db.execute(text("""
        DELETE FROM tracks t
         WHERE NOT EXISTS (SELECT 1 FROM media_files mf WHERE mf.track_id = t.id)
           AND NOT EXISTS (SELECT 1 FROM album_tracks at2 WHERE at2.track_id = t.id)
           AND NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.track_id = t.id)
           AND NOT EXISTS (SELECT 1 FROM audio_features af WHERE af.track_id = t.id)
           AND NOT EXISTS (SELECT 1 FROM listening_history lh WHERE lh.track_id = t.id)
           AND NOT EXISTS (SELECT 1 FROM local_play_stats lp WHERE lp.track_id = t.id)
           AND NOT EXISTS (SELECT 1 FROM session_tracks st WHERE st.track_id = t.id)
           AND NOT EXISTS (SELECT 1 FROM listening_sessions ls WHERE ls.seed_track_id = t.id)
           AND NOT EXISTS (SELECT 1 FROM track_lyrics tl WHERE tl.track_id = t.id)
    """))
    return res.rowcount


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
    from transliterate import latinize
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
    db.execute(text("UPDATE albums SET title = :t, title_latin = :tl WHERE id = :id"),
               {"t": canonical_title, "tl": latinize(canonical_title), "id": final_id})
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
    from transliterate import latinize
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
        db.execute(text("UPDATE albums SET title = :t, title_latin = :tl WHERE id = :a"),
                   {"t": canonical_title, "tl": latinize(canonical_title), "a": src})
        return src

    # Materialize the destination edition row, inheriting the release group.
    db.execute(text("""
        INSERT INTO albums (id, title, title_latin, release_year, musicbrainz_id, mb_match_confidence)
        SELECT :nid, :t, :tl, a.release_year, a.musicbrainz_id, a.mb_match_confidence
        FROM albums a WHERE a.id = :a
        ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, title_latin = EXCLUDED.title_latin
    """), {"nid": new_id, "t": canonical_title, "tl": latinize(canonical_title), "a": src})
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

