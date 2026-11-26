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
from uuid_utils import artist_uuid, normalize

logger = logging.getLogger(__name__)

# Inside one value, `;` and a spaced ` / ` join names at the read. Never `&`
# or "and" (how a team is credited) or a bare `/` ("AC/DC"; ID3v2.3's TCOM
# list is split by its reader). A comma is the derive's (_LIST_COMMA): what
# it means depends on what the node knows.
_JOINERS = re.compile(r"\s*;\s*|\s+/\s+")

# In composer tags a comma joins a list of writers ("Ed Simons, Tom
# Rowlands"; the owner's library: 483 such values, CREDITS-1-DESIGN §12.5,
# Q15), except where the whole value is one name — a band ("Earth, Wind &
# Fire"), an inverted name ("Bach, Johann Sebastian") — which are exactly the
# values some catalogue knows whole. So the split is decided at the derive,
# beside MusicBrainz's turning of "Last, First": one decision about what a
# comma value stands for, made against what the node knows, and made again
# when MB rows arrive or go (rederive_commas, §5.3; LIBRARY-FIXES-1 §9).
_LIST_COMMA = re.compile(r"\s*,\s+")

# A generational suffix is part of the name before it ("Ray Parker, Jr."),
# never a writer of its own.
_SUFFIX = re.compile(r"(?:jr|sr|ii|iii|iv)\.?", re.IGNORECASE)

# Inside a comma list the last part's "&" / "and" is the list's own joiner
# ("Hans Zimmer, Klaus Badelt & Lisa Gerrard" is three writers; B-136): an
# English list ends so, and an inverted personal name never carries one
# after its given name. A value without a comma keeps it ("Lennon &
# McCartney" is how a team is credited), and so does a last part the node
# knows whole ("Simon & Garfunkel").
_LAST_JOINER = re.compile(r"\s+(?:&|and)\s+", re.IGNORECASE)
_OXFORD_JOINER = re.compile(r"(?:&|and)\s+", re.IGNORECASE)

# "Last, First" is a Latin-script habit, among others; a name in these
# scripts is written without spaces, so its one "word" says nothing of an
# inversion ("坂本龍一, 細野晴臣" is two writers).
_UNSPACED_SCRIPT = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]")

# Parts that are no given names: beside another part, a list of writers.
_LIST_WORDS = frozenset({"traditional", "anonymous"})

# A placeholder names nobody, as Various Artists is no artist (Q16); folded
# (normalize), so "[unknown]" and "VARIOUS" are these. "Traditional" and
# "Anonymous" are kept: they say something true, and MusicBrainz credits
# works so.
_PLACEHOLDERS = frozenset({"various", "various composers", "unknown"})

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


def _is_name(name: str) -> bool:
    return bool(name) and normalize(name) not in _PLACEHOLDERS


def split_names(values: Iterable[str]) -> List[str]:
    """The names a composer tag's values hold, in order, each once, no
    placeholder among them; a comma list stays one value until the derive
    names it (_names_of)."""
    out: List[str] = []
    for value in values:
        for name in _JOINERS.split(value):
            name = name.strip()
            if _is_name(name) and name not in out:
                out.append(name)
    return out


def _turned(name: str):
    """'Bach, Johann Sebastian' → 'Johann Sebastian Bach'; None unless the
    name has exactly one comma with a word on each side."""
    parts = name.split(",")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        return None
    return f"{parts[1].strip()} {parts[0].strip()}"


def _dump_node(db: Session) -> bool:
    return db.execute(text(
        "SELECT 1 FROM user_settings WHERE key = 'musicbrainz.db_version'")).first() is not None


def _named_by_mb(db: Session, written: Set[str]) -> Dict[str, str]:
    """{written: MusicBrainz's name} for the one-comma names the MB rows this
    node holds say are an artist's sort form: an MB artist named as the name
    turned around whose sort name is the name (both case- and accent-folded,
    f_unaccent), and only one such name. The dump's rows or a slice's alike
    — a slice brings the composer's row wherever MB's artist credits of an
    engaged artist name him: a row held is evidence however it came, and a
    namesake a partial set lacks writes the same name."""
    pairs = [(w, t) for w in written for t in [_turned(w)] if t]
    if not pairs:
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


def _known_whole(db: Session, values: Set[str]) -> Set[str]:
    """The comma values that name an artist this node knows whole (Q15): an
    artists row of that identity (the UUID v5 rule, or the name itself — a
    canon rename keeps the id) that something besides a composer credit
    holds — a performer here, the phantom layer's, an MBID's, a similar
    edge's: a composer row an earlier derive minted is no evidence of
    itself — or an MB artist's name or sort name among the rows the node
    holds, the dump's or a slice's, folded as MB's naming folds
    (f_unaccent)."""
    if not values:
        return set()
    vs = sorted(values)
    return set(db.execute(text("""
        SELECT w.v FROM unnest(CAST(:v AS text[]), CAST(:i AS uuid[])) AS w(v, i)
         WHERE EXISTS (SELECT 1 FROM artists a
                        WHERE (a.id = w.i OR a.name = w.v)
                          AND (EXISTS (SELECT 1 FROM track_artists ta WHERE ta.artist_id = a.id)
                               OR EXISTS (SELECT 1 FROM album_artists aa WHERE aa.artist_id = a.id)
                               OR EXISTS (SELECT 1 FROM artist_mbids am WHERE am.artist_id = a.id)
                               OR EXISTS (SELECT 1 FROM similar_artists sa WHERE sa.artist_id = a.id)
                               OR EXISTS (SELECT 1 FROM similar_artists sa
                                           WHERE sa.similar_artist_id = a.id)))
            OR EXISTS (SELECT 1 FROM mb_artist m WHERE f_unaccent(m.name) = f_unaccent(w.v))
            OR EXISTS (SELECT 1 FROM mb_artist m WHERE f_unaccent(m.sort_name) = f_unaccent(w.v))"""),
        {"v": vs, "i": [str(artist_uuid(v)) for v in vs]}).scalars())


def _last_part(value: str) -> str:
    """A comma value's last part, an Oxford comma's joiner taken off."""
    last = _LIST_COMMA.split(value)[-1].strip()
    m = _OXFORD_JOINER.match(last)
    return last[m.end():] if m else last


def _parts(value: str, known: Set[str] = frozenset()) -> List[str]:
    """A comma value's parts: the last one split at its "&" / "and" unless
    it is `known` whole, a generational suffix joined to the part before
    it."""
    split = [p.strip() for p in _LIST_COMMA.split(value)]
    last = _last_part(value)
    split[-1:] = [last] if last in known else [p.strip() for p in _LAST_JOINER.split(last)]
    parts: List[str] = []
    for p in split:
        if parts and _SUFFIX.fullmatch(p):
            parts[-1] = f"{parts[-1]}, {p}"
        else:
            parts.append(p)
    return parts


def _inverted_shape(parts: List[str]) -> bool:
    """Two parts, the first one word of a spaced script, neither a word no
    given name is: "Debussy, Claude" or "Loewe, Lerner", which no shape tells
    apart."""
    return (len(parts) == 2 and not re.search(r"\s", parts[0])
            and not _UNSPACED_SCRIPT.search(parts[0])
            and not {normalize(p) for p in parts} & _LIST_WORDS)


def _names_of(db: Session, written: Set[str]) -> Dict[str, List[str]]:
    """{written value: the names it stands for, in order}: a one-comma name
    the MB rows held turn takes MB's name; a comma value the node knows whole
    is that one name; any other comma value is a list of its parts (Q15) —
    except a two-part value whose first part is one word on a node without
    the dump, which is one name as written: there MB's silence says nothing,
    and a list kept whole shows the file's own text where a split would
    invent two names ("Debussy" and "Claude"). On a dump node that silence
    is evidence, and such a value is a list (LIBRARY-FIXES-1 §9 Q1). A name
    too long for artists.name is no artist's (a tag that pasted liner notes)
    and names nobody."""
    dump = _dump_node(db)
    named = _named_by_mb(db, written)
    lists = {w for w in written if w not in named and _LIST_COMMA.search(w)}
    whole = _known_whole(db, lists)
    lasts = {_last_part(w) for w in lists - whole}
    known_lasts = _known_whole(db, {p for p in lasts if _LAST_JOINER.search(p)})
    parts_of = {w: _parts(w, known_lasts) for w in lists - whole}
    out: Dict[str, List[str]] = {}
    for w in written:
        if w in named:
            names = [named[w]]
        elif (w in parts_of and not dump and len(w) <= NAME_MAX
              and _inverted_shape(parts_of[w]) and all(_is_name(p) for p in parts_of[w])):
            names = [w]
        elif w in parts_of:
            # a placeholder among the parts ("Traditional, Unknown") names
            # nobody, and the rest are the list
            names = [n for n in parts_of[w] if _is_name(n)]
        else:
            names = [w]
        out[w] = [n for n in dict.fromkeys(names) if len(n) <= NAME_MAX]
    return out


def derive_composers(db: Session, track_ids: Iterable, *, sweep: bool = True) -> Set[str]:
    """Rewrite the tag composer credits of `track_ids` from their owned
    files' `raw_composers`, best copy first (best_rip_order), then the order
    within the file. A file not yet read by this reader (NULL) contributes
    nothing, and a track none of whose owned files is read keeps its rows —
    no file left included: the credit is of the music (Q5). A name is the
    artist of the existing UUID v5 rule, so a composer who performs is one
    row; a written one-comma name takes MusicBrainz's name where the MB rows
    held say so, and a comma list is its writers unless the node knows it
    whole (_names_of).
    Returns the artists a credit of these tracks named before and names no
    longer; with `sweep`, those no other row names are deleted (Q3: a
    corrected tag leaves no orphan) — a caller that sweeps them itself (a
    confirmed prune, counting what it removes) passes False.

    A derive that fails (a race no lock covers) rolls back alone — the batch
    it rides keeps its files, and the files keep what they read — and its
    tracks wait in credits_due, logged with them. Each derive then also takes
    a few tracks that waited (DUE_RETRY_AFTER at least), each in a
    savepoint of its own and swept: one that keeps failing stays alone and
    is tried again at that pace, never failing another's derive."""
    lock_credits(db)
    tids = sorted({str(t) for t in track_ids})
    dropped = _derive_alone(db, tids, sweep) if tids else set()
    for (due,) in db.execute(text("""
            SELECT track_id::text FROM credits_due
             WHERE failed_at < now() - CAST(:after AS interval) AND track_id <> ALL(CAST(:t AS uuid[]))
             ORDER BY failed_at LIMIT :n"""),
            {"after": DUE_RETRY_AFTER, "t": tids, "n": DUE_PER_DERIVE}).fetchall():
        _derive_alone(db, [due], True)
    return dropped


# A track whose derive failed is taken again by a later derive once this
# long has passed, a few at a time: a failure that persists costs one
# derive and one logged error per track at this pace, never a batch.
DUE_RETRY_AFTER = "10 minutes"
DUE_PER_DERIVE = 20


def _derive_alone(db: Session, tids: List[str], sweep: bool) -> Set[str]:
    try:
        with db.begin_nested():
            db.execute(text("DELETE FROM credits_due WHERE track_id = ANY(CAST(:t AS uuid[]))"),
                       {"t": tids})
            return _derive(db, tids, sweep)
    except Exception:
        logger.error("composer credits: the derive of %d track(s) failed; a later derive takes "
                     "them again: %s", len(tids), tids[:20], exc_info=True)
    # its own savepoint, the tracks key-locked: one a writer that takes no
    # credits lock deleted meanwhile is skipped, and nothing here can fail
    # the transaction the derive rides
    with db.begin_nested():
        db.execute(text("""
            INSERT INTO credits_due (track_id)
            SELECT id FROM tracks WHERE id = ANY(CAST(:t AS uuid[])) FOR KEY SHARE
            ON CONFLICT (track_id) DO UPDATE SET failed_at = now()"""), {"t": tids})
    return set()


def _plan(db: Session, tids: List[str]):
    """What a derive of `tids` would write: ({track: [artist ids in
    position order]}, {artist id to mint: name}, {artist id: the name that
    named it}), or None when no owned file of them is read."""
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
        return None
    names_of = _names_of(db, {w for f in files for w in f.raw_composers})
    names = {n for ns in names_of.values() for n in ns}
    # artists.name is UNIQUE: a row already holding the name is that artist,
    # whatever name its id was minted from (a canon rename keeps the id)
    held = dict(db.execute(text("SELECT name, id::text FROM artists WHERE name = ANY(:n)"),
                           {"n": sorted(names)}).fetchall())
    ids = {n: held.get(n) or str(artist_uuid(n)) for n in names}
    credits: Dict[str, List[str]] = {}
    for f in files:
        order = credits.setdefault(f.track_id, [])
        for w in f.raw_composers:
            for n in names_of[w]:
                if ids[n] not in order:
                    order.append(ids[n])
    named: Dict[str, str] = {}
    for n in sorted(names):
        named.setdefault(ids[n], n)
    return credits, {ids[n]: n for n in names if n not in held}, named


def _derive(db: Session, tids: List[str], sweep: bool) -> Set[str]:
    plan = _plan(db, tids)
    if plan is None:
        return set()
    credits, mint, _named = plan
    before = set(db.execute(text("""
        DELETE FROM track_credits
         WHERE track_id = ANY(CAST(:t AS uuid[])) AND role = 'composer' AND source = 'tag'
        RETURNING artist_id::text"""), {"t": list(credits)}).scalars())
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


def rederive_commas(db: Session) -> int:
    """When what the node knows of names changed — the dump loaded or
    deleted, slices landed or dropped (the canon run they wake), this
    release's rule (db_migrate's composer_commas_v1) — the tracks whose
    written comma value now reads otherwise are derived again: 'Bach,
    Johann Sebastian' takes MusicBrainz's name, a list it knows whole is one
    name, and back (§5.3). Only a comma track can change, and only one whose
    composers the rule would now name otherwise is touched — position by
    position the same artist, or a name the same folded as the canon folds
    (f_unaccent, then the identity rule): a composer the canon merged into a
    performer's spelling ("Bjork" into "Björk") is no change, where a derive
    would mint the tag's spelling again. A run after which nothing that
    matters arrived derives nothing. Returns how many tracks."""
    lock_credits(db)
    tids = [str(t) for t in db.execute(text("""
        SELECT DISTINCT f.track_id FROM (
            SELECT track_id, raw_composers FROM media_files WHERE raw_composers IS NOT NULL
            UNION ALL
            SELECT track_id, raw_composers FROM hqp_library_files WHERE raw_composers IS NOT NULL) f
         WHERE EXISTS (SELECT 1 FROM unnest(f.raw_composers) n WHERE n LIKE '%,%')""")).scalars()]
    plan = _plan(db, sorted(tids)) if tids else None
    if plan is None:
        return 0
    credits, _mint, named = plan
    stored = {r.track_id: list(zip(r.ids, r.names)) for r in db.execute(text("""
        SELECT tc.track_id::text AS track_id, array_agg(a.id::text ORDER BY tc.position) AS ids,
               array_agg(a.name ORDER BY tc.position) AS names
          FROM track_credits tc JOIN artists a ON a.id = tc.artist_id
         WHERE tc.track_id = ANY(CAST(:t AS uuid[])) AND tc.role = 'composer' AND tc.source = 'tag'
         GROUP BY tc.track_id"""), {"t": tids})}
    spelt = sorted(set(named.values()) | {n for row in stored.values() for _i, n in row})
    folded = {n: normalize(f) for n, f in db.execute(text(
        "SELECT n, f_unaccent(n) FROM unnest(CAST(:n AS text[])) AS n"), {"n": spelt})}

    def same(order, row):
        return len(order) == len(row) and all(
            a == i or folded[named[a]] == folded[n] for a, (i, n) in zip(order, row))

    changed = sorted(t for t, order in credits.items() if not same(order, stored.get(t, [])))
    for i in range(0, len(changed), 500):
        derive_composers(db, changed[i:i + 500])
    return len(changed)
