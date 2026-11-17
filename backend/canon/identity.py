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

from desktop.p2p import material
from uuid_utils import artist_uuid, track_uuid, album_uuid
from play_stats import refresh_play_stats
from provenance import ANALYSIS_KINDS, MATERIAL_RANK_SQL

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


# Does this file (`mf`) carry the track's analysis — the material its
# embedding was made from?
_CARRIES_ANALYSIS = """EXISTS (SELECT 1 FROM analysis_sources s
                               JOIN embeddings e ON e.analysis_source_id = s.id
                               WHERE s.media_file_id = mf.id)"""

# The analysis-source ranking of a track's files, over media_files `mf`
# joined to its album_variants `av`. A file of a copy that is away
# (album_variants.absent_since) ranks last unless it carries the track's
# analysis: a track with none is analysed from a copy that is here, and one
# analysed already keeps its source while a card is out (nothing reads it,
# nothing is pending, nothing is re-analysed when it goes or comes back).
# Then CD (16-bit lossless) > other lossless > lossy — a better rip is the
# upgrade the passes re-analyse from — then, between equal rips, the file that
# carries the analysis, then the oldest row. The one rule: elect_analysis_source
# and the CLI's bulk recalculation both order by it.
ANALYSIS_SOURCE_ORDER = f"""(av.absent_since IS NULL OR {_CARRIES_ANALYSIS}) DESC,
                           (mf.bit_depth = 16 AND mf.is_lossless) DESC,
                           mf.is_lossless DESC,
                           {_CARRIES_ANALYSIS} DESC,
                           mf.id"""


def elect_analysis_source(db: Session, track_id) -> None:
    """Designate the best-quality media_file of a track as its analysis source.

    Priority: ANALYSIS_SOURCE_ORDER — a copy that is away last unless it
    carries the track's analysis, then CD (16-bit lossless) > other lossless
    > lossy, then the file carrying the analysis. Two statements —
    losers cleared before the winner is set — because a single UPDATE that flips
    both rows transiently holds two TRUE rows for one track_id, which the partial
    unique index uq_media_files_analysis_source rejects. When the elected file
    differs from the one the current embedding was analyzed from, the embeddings
    pending predicate re-analyzes on the next pass (media_file_id mismatch).

    The media_files one-source-per-track invariant belongs to this base layer;
    the scanner and the merge path (_update_track_uuid) are both callers.
    """
    ranked = f"""
        WITH ranked AS (
            SELECT mf.id,
                   ROW_NUMBER() OVER (ORDER BY {ANALYSIS_SOURCE_ORDER}) as rn
            FROM media_files mf
            JOIN album_variants av ON av.id = mf.album_variant_id
            WHERE mf.track_id = :tid
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


_SEAL_NULL = "author_pubkey = NULL, signature = NULL, batch_root = NULL, merkle_proof = NULL"


def _analysis_rank(db: Session, table: str, track_id: str) -> int:
    """Best provenance rank among a track's rows of one analysis kind; -2 =
    none."""
    best = db.execute(text(f"""
        SELECT MAX({MATERIAL_RANK_SQL}) FROM {table} x
        LEFT JOIN analysis_sources s ON s.id = x.analysis_source_id
        WHERE x.track_id = :tid"""), {"tid": track_id}).scalar()
    return -2 if best is None else int(best)


def _shed_track_seals(db: Session, track_id: str, kinds=ANALYSIS_KINDS) -> None:
    """Null the audio seals of a track's analysis of ``kinds``: after a
    rename every kind, after a merge the kinds it moved (_merge_analysis).

    Every seal payload binds the track uuid (segment_payload /
    features_payload / instruments_payload), so after the uuid changed the
    stored signatures verify against a uuid the row no longer carries — the
    album_tracks / track_mbids guards shed theirs on the cascaded track_id
    change, these tables have no track_id in their guard. sign_audio
    re-seals on its next pass; nothing travels unsigned in between."""
    if "embeddings" in kinds:
        db.execute(text(f"""
            UPDATE embedding_segments es SET {_SEAL_NULL}
            FROM embeddings e
            WHERE e.id = es.embedding_id AND e.track_id = :tid
              AND es.signature IS NOT NULL"""), {"tid": track_id})
    for kind in kinds:
        if kind != "embeddings":
            db.execute(text(f"UPDATE {kind} SET {_SEAL_NULL} "
                            "WHERE track_id = :tid AND signature IS NOT NULL"),
                       {"tid": track_id})


def _merge_analysis(db: Session, old: str, new: str) -> None:
    """Move onto ``new`` each analysis kind of ``old`` that ``new`` lacks, or
    holds from worse provenance (the better one wins only where both hold
    the kind, as for lyrics and text embeddings): a merge whose target wins
    on embeddings keeps the old track's features when the target has none.
    The moved rows' seals are shed — every payload binds the track uuid —
    and only theirs: the target's rows of the kinds it kept stay sealed."""
    p = {"old": old, "new": new}
    kinds = [k for k in ANALYSIS_KINDS
             if _analysis_rank(db, k, old) > _analysis_rank(db, k, new)]
    if not kinds:
        return
    _shed_track_seals(db, old, kinds)
    for kind in kinds:
        db.execute(text(f"DELETE FROM {kind} WHERE track_id = :new"), p)
    _move_analysis(db, old, new, kinds)


def _move_analysis(db: Session, old: str, new: str, kinds) -> None:
    """Re-home the old track's analysis of ``kinds`` (embeddings carry their
    segments) and the sources it is linked to onto ``new``, which holds none
    of those kinds.

    A source the target already holds — the same declaration (the print and
    the duration the records' seals bind), the same copy and the same origin
    (UNIQUE NULLS NOT DISTINCT (track_id, chromaprint_key, duration_seconds,
    imported, copy_rank)) — is the same row: the moving records re-point at
    it, the source's group members re-point at the target row's root, and
    the duplicate is dropped. Any other row moves as a row of its own and
    joins the target's group: this node's records and another author's never
    share a row, nor do records of two declarations or of two copies — a
    lossy stream's features never stand for the target's CD. Each
    source that moves joins the target's material group by the rule
    (material.root_among: a twin's group with no comparison, else the first
    row that is one material with it), a moving root bringing its members
    along (they re-point at the target's root: members point at roots,
    never at members); one whose root stays behind is a root of its own
    until it joins."""
    p = {"old": old, "new": new}
    linked = " OR ".join(
        f"EXISTS (SELECT 1 FROM {k} x WHERE x.analysis_source_id = s.id AND x.track_id = :old)"
        for k in kinds)
    moving = {r.id: r for r in db.execute(text(f"""
        SELECT s.id, s.material_of FROM analysis_sources s
         WHERE s.track_id = :old AND ({linked})"""), p).fetchall()}
    dups = db.execute(text("""
        SELECT o.id AS dup, t.id AS target, COALESCE(t.material_of, t.id) AS root
          FROM analysis_sources o
          JOIN analysis_sources t ON t.track_id = :new
                                 AND t.chromaprint_key = o.chromaprint_key
                                 AND t.imported = o.imported
                                 AND t.duration_seconds IS NOT DISTINCT FROM o.duration_seconds
                                 AND t.copy_rank = o.copy_rank
         WHERE o.track_id = :old AND o.id = ANY(:ids)"""),
        {**p, "ids": list(moving)}).fetchall()
    for d in dups:
        for k in kinds:
            db.execute(text(f"UPDATE {k} SET analysis_source_id = :target "
                            "WHERE analysis_source_id = :dup AND track_id = :old"),
                       {**p, "dup": d.dup, "target": d.target})
        db.execute(text("UPDATE analysis_sources SET material_of = :root WHERE material_of = :dup"),
                   {"dup": d.dup, "root": d.root})
        db.execute(text("DELETE FROM analysis_sources WHERE id = :dup"), {"dup": d.dup})
        moving.pop(d.dup)
    if moving:
        ids = list(moving)
        db.execute(text("""
            UPDATE analysis_sources s SET material_of = NULL
             WHERE s.id = ANY(:ids)
               AND EXISTS (SELECT 1 FROM analysis_sources r
                            WHERE r.id = s.material_of AND r.track_id = :old
                              AND r.id <> ALL(:ids))"""), {**p, "ids": ids})
        db.execute(text("UPDATE analysis_sources SET track_id = :new WHERE id = ANY(:ids)"),
                   {**p, "ids": ids})
        for sid in sorted(ids):
            _join_target_group(db, sid, new)
    for k in kinds:
        db.execute(text(f"UPDATE {k} SET track_id = :new WHERE track_id = :old"), p)


def _join_target_group(db: Session, sid: int, track: str) -> None:
    """A moved source, still a root, joins its new track's group — with its
    members."""
    rows = db.execute(text("""
        SELECT id, material_of, chromaprint, duration_seconds FROM analysis_sources
         WHERE track_id = :t ORDER BY id"""), {"t": track}).fetchall()
    me = next(r for r in rows if r.id == sid)
    if me.material_of is not None:
        return
    root = material.root_among(
        me.chromaprint, me.duration_seconds,
        [(r.id, r.material_of, r.chromaprint, r.duration_seconds)
         for r in rows if r.id != sid and r.material_of != sid])
    if root is not None:
        db.execute(text("UPDATE analysis_sources SET material_of = :root "
                        "WHERE id = :sid OR material_of = :sid"), {"root": root, "sid": sid})


def _update_track_uuid(db: Session, old_id, new_id) -> str:
    """
    Update track UUID via ON UPDATE CASCADE, or merge if target exists.
    Returns the final track UUID.

    Merge moves EVERYTHING the old row holds that the target lacks — files,
    listens, play stats, tracklist slots, recording bindings, stats, lyrics,
    text embeddings, session rows — and, per analysis kind, the
    better-provenance analysis of the two (a phantom's sealed lossless-stream
    analysis merging into an owned track that was never analyzed moves
    rather than dies: GPU work nobody re-derives without the audio). Seals
    of whatever changed uuid are shed; sign_audio re-seals.
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
        # Analysis: per kind, the better provenance — the old row's moves,
        # its seals shed (the payload binds the uuid).
        _merge_analysis(db, old_str, new_str)
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
        # One-per-(track, key) tables: move what the target lacks.
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
            text("UPDATE hqp_library_files SET track_id = :new WHERE track_id = :old"),
            {"new": new_str, "old": old_str},
        )
        db.execute(
            text("UPDATE listening_history SET track_id = :new WHERE track_id = :old"),
            {"new": new_str, "old": old_str},
        )
        # The survivor's stats are a function of the history it now holds
        # (play_stats) — summing two counter rows was the drift that made the
        # table derived in the first place.
        db.execute(text("DELETE FROM local_play_stats WHERE track_id = :old"), {"old": old_str})
        with db.connection().connection.cursor() as cur:
            refresh_play_stats(cur, [new_str])
        # CASCADE takes whatever lost the merge: the weaker analysis, duplicate
        # stats/lyrics/embeddings, the old associations.
        db.execute(text("DELETE FROM tracks WHERE id = :old"), {"old": old_str})
        # Re-elect one analysis source across the merged files (best quality
        # wins; a better rip that moved in schedules re-analysis via the
        # embeddings pending predicate — asrc.media_file_id no longer matches).
        elect_analysis_source(db, new_str)

    return new_str


def _move_file_analysis(db: Session, media_file_id: int, old: str, new: str) -> None:
    """The analysis made from one file's material moves with the file to
    the track its tags now name — the material is the file's, and the
    chromaprint is the only content address. Only onto a track that holds
    no analysis source yet (a track just minted for the file): a track that
    has its own analysis keeps it, and the moved file is then a copy of it
    for the rule to elect. The moved records' seals are shed: every payload
    binds the track uuid; sign_audio makes them again."""
    if db.execute(text("SELECT 1 FROM analysis_sources WHERE track_id = :new LIMIT 1"),
                  {"new": new}).first():
        return
    sources = list(db.execute(text(
        "SELECT id FROM analysis_sources WHERE media_file_id = :mf AND track_id = :old"),
        {"mf": media_file_id, "old": old}).scalars())
    if not sources:
        return
    p = {"old": old, "new": new, "ids": sources}
    # A member whose root stays behind becomes a root of its own; one whose
    # root moves keeps pointing at it.
    db.execute(text("""UPDATE analysis_sources s SET material_of = NULL
                        WHERE s.id = ANY(:ids) AND s.material_of IS NOT NULL
                          AND s.material_of <> ALL(:ids)"""), p)
    db.execute(text("""UPDATE analysis_sources SET material_of = NULL
                        WHERE track_id = :old AND material_of = ANY(:ids)"""), p)
    for kind in ANALYSIS_KINDS:
        db.execute(text(f"UPDATE {kind} SET track_id = :new "
                        "WHERE track_id = :old AND analysis_source_id = ANY(:ids)"), p)
    db.execute(text("UPDATE analysis_sources SET track_id = :new WHERE id = ANY(:ids)"), p)
    _shed_track_seals(db, new)


def rehome_file_track(db: Session, media_file_id: int, old_track: str, title: str,
                      artist_name: str) -> str:
    """A file read again whose tags now name another track (a title or an
    artist fixed in a tag editor): the track a fresh import would place it
    on. When it was its track's last file the track follows it —
    _update_track_uuid renames it, or merges it into a track of the new
    identity that exists (a phantom slot, another rip): listens, sessions,
    lyrics and analysis with it. When the track keeps other files (another
    rip, the HQPlayer's copy) only this file moves, with the analysis made
    from its material (_move_file_analysis); the listens stay — a listen is
    of the track. Either way the track's primary credit is the tags' artist.
    Returns the track the file is on."""
    from transliterate import latinize
    new = str(track_uuid(title, artist_name))
    if new == old_track:
        return new
    artist = str(_ensure_artist(db, artist_name))
    keeps_others = db.execute(text("""
        SELECT EXISTS (SELECT 1 FROM media_files WHERE track_id = :t AND id <> :mf)
            OR EXISTS (SELECT 1 FROM hqp_library_files WHERE track_id = :t)"""),
        {"t": old_track, "mf": media_file_id}).scalar()
    existed = db.execute(text("SELECT 1 FROM tracks WHERE id = :id"), {"id": new}).first()
    if not existed:
        if keeps_others:
            db.execute(text("INSERT INTO tracks (id, title, title_latin) VALUES (:id, :t, :tl)"),
                       {"id": new, "t": title, "tl": latinize(title)})
        else:
            _update_track_uuid(db, old_track, new)
            db.execute(text("UPDATE tracks SET title = :t, title_latin = :tl WHERE id = :id"),
                       {"id": new, "t": title, "tl": latinize(title)})
    elif not keeps_others:
        _update_track_uuid(db, old_track, new)
    if keeps_others:
        db.execute(text("UPDATE media_files SET is_analysis_source = false, track_id = :new "
                        "WHERE id = :mf"), {"new": new, "mf": media_file_id})
        _move_file_analysis(db, media_file_id, old_track, new)
        elect_analysis_source(db, old_track)
    db.execute(text("""DELETE FROM track_artists
                        WHERE track_id = :t AND role = 'primary' AND artist_id <> :a"""),
               {"t": new, "a": artist})
    db.execute(text("""INSERT INTO track_artists (track_id, artist_id, role)
                       VALUES (:t, :a, 'primary') ON CONFLICT DO NOTHING"""), {"t": new, "a": artist})
    elect_analysis_source(db, new)
    return new


def _merge_album_variants(db: Session, new_album_id: str, *,
                          from_album: str = None, variant_ids: List[int] = None,
                          edition: str = None) -> None:
    """Repoint variants onto ``new_album_id`` honouring the
    UNIQUE(directory_path, album_id, endpoint) key. When a variant's directory already
    holds a variant of the target album (a box-split sibling, or a folder whose
    files canon now unifies), fold the moving variant's media_files into that
    keeper and drop it instead of colliding; otherwise repoint in place.

    Source is either every variant of ``from_album`` or an explicit
    ``variant_ids`` list. ``edition`` is stamped on the repoint path."""
    cols = "id, directory_path, hqp_endpoint_id"
    if from_album is not None:
        rows = db.execute(text(
            f"SELECT {cols} FROM album_variants WHERE album_id = :a ORDER BY id"
        ), {"a": from_album}).fetchall()
    else:
        rows = db.execute(text(
            f"SELECT {cols} FROM album_variants WHERE id = ANY(:v) ORDER BY id"
        ), {"v": list(variant_ids)}).fetchall()
    for vid, dir_path, endpoint_id in rows:
        # The key is the directory AT a location: a variant folds only into a
        # keeper of the same endpoint (local rows carry NULL).
        keeper = db.execute(text(
            "SELECT id FROM album_variants "
            "WHERE directory_path = :d AND album_id = :a AND id <> :v "
            "  AND hqp_endpoint_id IS NOT DISTINCT FROM :e"
        ), {"d": dir_path, "a": new_album_id, "v": vid, "e": endpoint_id}).fetchone()
        if keeper:
            for table in ("media_files", "hqp_library_files"):
                db.execute(text(f"UPDATE {table} SET album_variant_id = :k WHERE album_variant_id = :v"),
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


# The one definition of an orphan track, formatted with the alias of the
# `tracks` row: nothing refers to it — no file, no tracklist slot, no
# analysis, none of the owner's life data (a listen, its stats, a session
# slot or seed, the demo channel's spent listen) and no lyrics. Every deleter
# of tracks uses it: the orphan sweep below, the discography reconcile (and
# through it the explicit phantom-layer removal), the scanner's prune of
# vanished files and its cue supersede. A listen cascades with its track,
# and the reconcile's own copy of this rule lacked that line — opening an
# artist page could delete the owner's listens of a streamed phantom
# (2026-09-24). A NOT EXISTS fragment rather than a SQL function, so the
# planner keeps anti-joins over millions of phantom tracks.
ORPHAN_TRACK_SQL = """
    NOT EXISTS (SELECT 1 FROM media_files o_mf WHERE o_mf.track_id = {t}.id)
    AND NOT EXISTS (SELECT 1 FROM hqp_library_files o_hf WHERE o_hf.track_id = {t}.id)
    AND NOT EXISTS (SELECT 1 FROM album_tracks o_at WHERE o_at.track_id = {t}.id)
    AND NOT EXISTS (SELECT 1 FROM embeddings o_e WHERE o_e.track_id = {t}.id)
    AND NOT EXISTS (SELECT 1 FROM audio_features o_af WHERE o_af.track_id = {t}.id)
    AND NOT EXISTS (SELECT 1 FROM track_instruments o_ti WHERE o_ti.track_id = {t}.id)
    AND NOT EXISTS (SELECT 1 FROM listening_history o_lh WHERE o_lh.track_id = {t}.id)
    AND NOT EXISTS (SELECT 1 FROM local_play_stats o_lp WHERE o_lp.track_id = {t}.id)
    AND NOT EXISTS (SELECT 1 FROM session_tracks o_st WHERE o_st.track_id = {t}.id)
    AND NOT EXISTS (SELECT 1 FROM listening_sessions o_ls WHERE o_ls.seed_track_id = {t}.id)
    AND NOT EXISTS (SELECT 1 FROM demo_plays o_dp WHERE o_dp.track_id = {t}.id)
    AND NOT EXISTS (SELECT 1 FROM track_lyrics o_tl WHERE o_tl.track_id = {t}.id)
"""


def delete_orphan_tracks(db: Session) -> int:
    """Drop track rows nothing refers to any more (ORPHAN_TRACK_SQL). A
    re-keyed phantom slot (the mint moved it to another uuid) leaves such a
    row behind; a track that carries anything at all is not an orphan and
    stays."""
    res = db.execute(text(f"DELETE FROM tracks t WHERE {ORPHAN_TRACK_SQL.format(t='t')}"))
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

