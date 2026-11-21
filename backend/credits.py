"""Who wrote a track's music: the composers its files' own tags name
(CREDITS-1-DESIGN §2, the design session's answers §12).

A composer is a credit (`track_credits`), never a `track_artists` role, so no
reader of track_artists — the engagement gate among them — sees one. Each
file row keeps the names its tags gave (`raw_composers`: split, before
naming); a track's tag credits are DERIVED from its owned files' rows, best
copy first, never written in place: a re-read, a second copy with other tags
and a file moved to another track are then one rule, and a second derive of
the same files is a no-op. MusicBrainz's credits (source 'mb', from its work
relations) are CREDITS-MB's; nothing here touches them."""

import logging
import re
from typing import Dict, Iterable, List, Set

from sqlalchemy import text
from sqlalchemy.orm import Session

from sql_queries import ARTIST_UNCREDITED
from transliterate import latinize
from uuid_utils import artist_uuid

logger = logging.getLogger(__name__)

# Inside one value, `;` and a spaced ` / ` join names. Never a comma ("Bach,
# Johann Sebastian" is one name, "Lennon, McCartney" two, and nothing in the
# string tells them apart: a wrong split mints two false artists where a
# kept value mints one merely unsplit), `&` or "and" (how a team is credited)
# or a bare `/` ("AC/DC"; ID3v2.3's TCOM list is split by its reader).
_JOINERS = re.compile(r"\s*;\s*|\s+/\s+")

# Derives serialise: two writers (a scan, an HQPlayer sync) deriving one
# track from their own snapshots would each delete what it saw and insert
# its rows — the second's insert meeting the first's. A transaction that
# derives takes the lock FIRST (lock_credits), before any row it writes:
# taken at the derive, after a batch's inserts, it closes a cycle with a
# writer that holds it and waits on one of those rows.
CREDITS_LOCK_KEY = 0x636D7073  # "cmps"


def lock_credits(db: Session) -> None:
    """Take the derives' lock for the rest of this transaction (re-entrant
    in a session); call it at the start of every transaction that will
    derive."""
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": CREDITS_LOCK_KEY})


# artists.name is VARCHAR(500): a longer "name" is no artist's (a tag that
# pasted liner notes), and minting it would fail the derive of every track
# its batch holds.
NAME_MAX = 500


def split_names(values: Iterable[str]) -> List[str]:
    """The names a composer tag's values hold, in order, each once."""
    out: List[str] = []
    for value in values:
        for name in _JOINERS.split(value):
            name = name.strip()
            if name and len(name) <= NAME_MAX and name not in out:
                out.append(name)
    return out


def _turned(name: str):
    """'Bach, Johann Sebastian' → 'Johann Sebastian Bach'; None unless the
    name has exactly one comma with a word on each side."""
    parts = name.split(",")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        return None
    return f"{parts[1].strip()} {parts[0].strip()}"


def _named_by_dump(db: Session, written: Set[str]) -> Dict[str, str]:
    """{written: MusicBrainz's name} for the one-comma names the dump says
    are an artist's sort form: an MB artist named as the name turned around
    whose sort name is the name (both case- and accent-folded, f_unaccent),
    and only one such name. Only from the dump: a slice-fed node's mb_*
    rows hold the subtrees of engaged artists, which a composer never is
    (Q4) — so a node without the dump keeps the written form."""
    pairs = [(w, t) for w in written for t in [_turned(w)] if t]
    if not pairs or db.execute(text(
            "SELECT 1 FROM user_settings WHERE key = 'musicbrainz.db_version'")).first() is None:
        return {}
    rows = db.execute(text("""
        SELECT w.v, min(a.name) AS name
          FROM unnest(CAST(:v AS text[]), CAST(:t AS text[])) AS w(v, t)
          JOIN mb_artist a ON f_unaccent(a.name) = f_unaccent(w.t)
                          AND f_unaccent(a.sort_name) = f_unaccent(w.v)
         GROUP BY w.v
        HAVING count(DISTINCT a.name) = 1"""),
        {"v": [w for w, _ in pairs], "t": [t for _, t in pairs]}).fetchall()
    return {r.v: r.name for r in rows}


def derive_composers(db: Session, track_ids: Iterable, *, sweep: bool = True) -> Set[str]:
    """Rewrite the tag composer credits of `track_ids` from their owned
    files' `raw_composers`, best copy first (best_rip_order), then the order
    within the file. A file not yet read by this reader (NULL) contributes
    nothing, and a track none of whose owned files is read keeps its rows —
    no file left included: the credit is of the music (Q5). A name is the
    artist of the existing UUID v5 rule, so a composer who performs is one
    row; a written one-comma name takes MusicBrainz's name on a dump node.
    Returns the artists a credit of these tracks named before and names no
    longer; with `sweep`, those no other row names are deleted (Q3: a
    corrected tag leaves no orphan) — a caller that sweeps them itself (a
    confirmed prune, counting what it removes) passes False.

    A derive that fails (a race no lock covers) rolls back alone — the batch
    it rides keeps its files, and the files keep what they read — and its
    tracks wait in credits_due, which the next derive takes with its own;
    the failure is logged with them."""
    lock_credits(db)
    tids = sorted({str(t) for t in track_ids}
                  | set(db.execute(text("SELECT track_id::text FROM credits_due")).scalars()))
    if not tids:
        return set()
    try:
        with db.begin_nested():
            db.execute(text("DELETE FROM credits_due WHERE track_id = ANY(CAST(:t AS uuid[]))"),
                       {"t": tids})
            return _derive(db, tids, sweep)
    except Exception:
        logger.error("composer credits: the derive of %d track(s) failed; the next derive takes "
                     "them again: %s", len(tids), tids[:20], exc_info=True)
        db.execute(text("""
            INSERT INTO credits_due (track_id)
            SELECT t FROM unnest(CAST(:t AS uuid[])) t
             WHERE EXISTS (SELECT 1 FROM tracks WHERE id = t)
            ON CONFLICT (track_id) DO UPDATE SET failed_at = now()"""), {"t": tids})
        return set()


def _derive(db: Session, tids: List[str], sweep: bool) -> Set[str]:
    # the best copy first, as best_rip_order ranks rips; a copy here before
    # the HQPlayer's of the same quality, ids last (two tables' serials)
    files = db.execute(text("""
        SELECT f.track_id::text AS track_id, f.raw_composers
          FROM (SELECT track_id, raw_composers, is_lossless, sample_rate, bit_depth, id, 0 AS at_hqp
                  FROM media_files
                 WHERE track_id = ANY(CAST(:t AS uuid[])) AND raw_composers IS NOT NULL
                UNION ALL
                SELECT track_id, raw_composers, is_lossless, sample_rate, bit_depth, id, 1
                  FROM hqp_library_files
                 WHERE track_id = ANY(CAST(:t AS uuid[])) AND raw_composers IS NOT NULL) f
         ORDER BY f.track_id, f.is_lossless DESC NULLS LAST, f.sample_rate DESC NULLS LAST,
                  f.bit_depth DESC NULLS LAST, f.at_hqp, f.id"""), {"t": tids}).fetchall()
    if not files:
        return set()
    written = {w for f in files for w in f.raw_composers}
    named = _named_by_dump(db, written)
    names = {named.get(w, w) for w in written}
    # artists.name is UNIQUE: a row already holding the name is that artist,
    # whatever name its id was minted from (a canon rename keeps the id)
    held = dict(db.execute(text("SELECT name, id::text FROM artists WHERE name = ANY(:n)"),
                           {"n": sorted(names)}).fetchall())
    ids = {n: held.get(n) or str(artist_uuid(n)) for n in names}
    credits: Dict[str, List[str]] = {}
    for f in files:
        order = credits.setdefault(f.track_id, [])
        for w in f.raw_composers:
            aid = ids[named.get(w, w)]
            if aid not in order:
                order.append(aid)
    before = set(db.execute(text("""
        DELETE FROM track_credits
         WHERE track_id = ANY(CAST(:t AS uuid[])) AND role = 'composer' AND source = 'tag'
        RETURNING artist_id::text"""), {"t": list(credits)}).scalars())
    mint = {ids[n]: n for n in names if n not in held}
    if mint:
        db.execute(text("""
            INSERT INTO artists (id, name, name_latin)
            SELECT * FROM unnest(CAST(:i AS uuid[]), CAST(:n AS text[]), CAST(:l AS text[]))
            ON CONFLICT DO NOTHING"""),
            {"i": list(mint), "n": list(mint.values()), "l": [latinize(n) for n in mint.values()]})
    rows = [(t, a, p) for t, order in credits.items() for p, a in enumerate(order)]
    if rows:
        db.execute(text("""
            INSERT INTO track_credits (track_id, role, artist_id, source, position)
            SELECT t, 'composer', a, 'tag', p
              FROM unnest(CAST(:t AS uuid[]), CAST(:a AS uuid[]), CAST(:p AS smallint[])) AS x(t, a, p)"""),
            {"t": [r[0] for r in rows], "a": [r[1] for r in rows], "p": [r[2] for r in rows]})
    dropped = before - {a for _t, a, _p in rows}
    if sweep and dropped:
        # The derive is no confirmed act of the owner — it follows every
        # re-read: it removes only what its own credit alone made, never a
        # row an MBID or a Last.fm similar edge names (a stub the tag
        # happened to name keeps its row, with its bio and its edges).
        gone = db.execute(text(f"""
            DELETE FROM artists a WHERE a.id = ANY(CAST(:a AS uuid[])) AND {ARTIST_UNCREDITED.format(a='a')}
              AND NOT EXISTS (SELECT 1 FROM artist_mbids am WHERE am.artist_id = a.id)
              AND NOT EXISTS (SELECT 1 FROM similar_artists sa
                              WHERE sa.artist_id = a.id OR sa.similar_artist_id = a.id)"""),
            {"a": sorted(dropped)}).rowcount
        if gone:
            logger.info("composer credits: %d composer(s) no file names any more removed", gone)
    return dropped


def rename_inverted(db: Session) -> int:
    """After a dump load: the tracks a written one-comma composer names are
    derived again, so 'Bach, Johann Sebastian' takes the dump's name
    (§5.3). Only those can change. Returns how many tracks."""
    tids = list(db.execute(text("""
        SELECT DISTINCT f.track_id FROM (
            SELECT track_id, raw_composers FROM media_files WHERE raw_composers IS NOT NULL
            UNION ALL
            SELECT track_id, raw_composers FROM hqp_library_files WHERE raw_composers IS NOT NULL) f
         WHERE EXISTS (SELECT 1 FROM unnest(f.raw_composers) n
                       WHERE n LIKE '%,%' AND n NOT LIKE '%,%,%')""")).scalars())
    for i in range(0, len(tids), 500):
        derive_composers(db, tids[i:i + 500])
    return len(tids)
