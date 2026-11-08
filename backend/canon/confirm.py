"""The slice nominates, MusicBrainz decides.

On a node whose MusicBrainz rows came from other nodes' signed slices
(`required()`), anything the canon does irreversibly — a merge, a rename, a
mint, an imported scrobble placed, a stream chosen — rests on rows any key
may have signed. So each such decision is made twice: the plan, from the
slice, nominates the entities; the gate re-runs the decision's own premise on
MusicBrainz's answers for those same entities, asked by MBID
(backend/mb_answers.py); and the write takes MusicBrainz's values. A premise
that fails is no action; a binding it refutes is withdrawn and remembered
(`withdraw`), so a slice still carrying the claim cannot make it again.

A gate's verdict is True (the premise holds), False (MusicBrainz's answers
refute it) or WAIT (None): an answer is missing or older than
mb_answers.FRESH, the decision's `Asks` holds what to ask, and the decision
waits — its watermark unstamped — until the answers wake its subject.

Checking only how the slice's MBIDs relate inside MusicBrainz would not do: a
real MBID can be paired with a forged alias, and recordings forged to match
the owned titles can be hung on a real release group. Hence the name premise
is re-run on MusicBrainz's names and the content premise on its tracklists.
"""

import re
from typing import Iterable, List, NamedTuple, Optional, Sequence

import mb_answers
from db_pool import db_execute, db_query, db_query_one, get_conn, transaction
from uuid_utils import normalize

WAIT = None
# A binding MusicBrainz's answers neither refute nor bear out: nothing owned
# carries its evidence (no covering release stamped, no recording on the
# files), or what the slice's plan stamped is not borne out by MusicBrainz's
# releases and recordings. No action rests on it, and it is kept: only
# MusicBrainz's word about the bound artist itself refutes a binding — the
# evidence pointers were the slice's to write, so a forged slice could
# otherwise refute a true binding for good.
UNPROVEN = "unproven"

# Owned evidence asked per binding at most: the strongest bound albums, then
# the stamped recordings of the artist's files.
MAX_ALBUMS = 3
MAX_RECORDINGS = 3


def required() -> bool:
    return mb_answers.required()


class Entity(NamedTuple):
    known: bool                 # MusicBrainz has it (found, or forwarded to it)
    mbid: str                   # its own MBID — the merge target of a forwarded one
    data: Optional[dict]


def _entity(kind: str, reader, mbid: str, asks: mb_answers.Asks) -> Optional[Entity]:
    a = asks.need(kind, mbid)
    if a is None:
        return WAIT
    if not a.known:
        return Entity(False, str(mbid), None)
    data = reader(a.target)
    if data is None:
        # The answer row outlived its entity rows (a concurrent re-ask): ask again.
        asks.missing.add((kind, str(mbid)))
        return WAIT
    return Entity(True, a.target, data)


def artist(mbid: str, asks: mb_answers.Asks) -> Optional[Entity]:
    return _entity("artist", mb_answers.artist, mbid, asks)


def release(mbid: str, asks: mb_answers.Asks) -> Optional[Entity]:
    return _entity("release", mb_answers.release, mbid, asks)


def recording(mbid: str, asks: mb_answers.Asks) -> Optional[Entity]:
    return _entity("recording", mb_answers.recording, mbid, asks)


def release_group(mbid: str, asks: mb_answers.Asks) -> Optional[Entity]:
    return _entity("release_group", mb_answers.release_group, mbid, asks)


def release_groups(artist_mbid: str, asks: mb_answers.Asks) -> Optional[List[dict]]:
    """Every group MusicBrainz credits to the artist; [] for an unknown artist;
    WAIT while the browse is not here."""
    a = asks.need("artist_release_groups", artist_mbid)
    if a is None:
        return WAIT
    return mb_answers.artist_release_groups(a.target) if a.known else []


def same(kind: str, mbid: str, other: Optional[str], asks: mb_answers.Asks) -> Optional[bool]:
    """`mbid` names `other`: the same MBID, or one MusicBrainz merged into it."""
    if other is None:
        return False
    if str(mbid) == str(other):
        return True
    a = asks.need(kind, mbid)
    if a is None:
        return WAIT
    return a.known and a.target == str(other)


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------

def _mb_strings(a: dict) -> List[str]:
    return [s for s in [a["name"]] + [x["name"] for x in a["aliases"]] if s]


def _spelled(a: dict, name_eq: Sequence[str] = (), both_eq: Sequence[str] = (),
             unaccent_eq: Sequence[str] = (), sort_eq: Optional[str] = None,
             trigram: Optional[str] = None) -> bool:
    """MusicBrainz's strings for `a` against the spellings a nomination
    probed, in the plan's own SQL forms (canon.match): its name equal (case
    aside) to one of `name_eq`; its name or an alias equal to one of
    `both_eq`, or unaccent-equal to one of `unaccent_eq`, or scoring
    `trigram` at the search's candidate floor; its sort name equal to
    `sort_eq`."""
    from canon.match import _CAND_SCORE
    names = [a["name"]] if a.get("name") else []
    both = _mb_strings(a)
    if not both:
        return False
    row = db_query_one("""
        SELECT EXISTS (SELECT 1 FROM unnest(CAST(%(names)s AS text[])) n,
                                     unnest(CAST(%(name_eq)s AS text[])) v
                        WHERE lower(n) = lower(v))
            OR EXISTS (SELECT 1 FROM unnest(CAST(%(both)s AS text[])) b,
                                     unnest(CAST(%(both_eq)s AS text[])) v
                        WHERE lower(b) = lower(v))
            OR EXISTS (SELECT 1 FROM unnest(CAST(%(both)s AS text[])) b,
                                     unnest(CAST(%(unaccent_eq)s AS text[])) v
                        WHERE public.f_unaccent(b) = public.f_unaccent(v))
            OR (CAST(%(trigram)s AS text) IS NOT NULL AND EXISTS (
                    SELECT 1 FROM unnest(CAST(%(both)s AS text[])) b
                     WHERE round(100 * similarity(lower(b), lower(%(trigram)s))) >= %(floor)s))
            OR (CAST(%(sort_eq)s AS text) IS NOT NULL
                AND lower(CAST(%(sort)s AS text)) = lower(%(sort_eq)s)) AS ok
    """, {"names": names, "both": both, "name_eq": list(name_eq), "both_eq": list(both_eq),
          "unaccent_eq": list(unaccent_eq), "trigram": trigram, "floor": _CAND_SCORE,
          "sort_eq": sort_eq, "sort": a.get("sort_name")})
    return bool(row and row["ok"])


def name_premise(local_name: str, a: dict) -> bool:
    """MusicBrainz's own strings for the artist `a` name the owned artist
    `local_name` the way the owned canon's nomination does (canon.match.
    _candidates): the name or an alias equal or at the search's trigram
    floor, a probe variant equal to the name or an alias, an &↔and spelling
    equal to the name, the unaccented name, a comma form equal to the sort
    name — or both fold to one identity (uuid_utils.normalize). Not the
    search's top-candidate fallback: a node without the dump never nominates
    by it (canon.match._candidates), for no name of MusicBrainz's would
    confirm it."""
    from canon.match import _probe_variants, _whole_variants
    q = (local_name or "").strip()
    if not q or not _mb_strings(a):
        return False
    if any(normalize(s) == normalize(q) for s in _mb_strings(a)):
        return True
    return _spelled(a, name_eq=_whole_variants(q), both_eq=[q] + list(_probe_variants(q)),
                    unaccent_eq=[local_name], sort_eq=q if "," in q else None, trigram=q)


def phantom_premise(name: str, a: dict) -> bool:
    """MusicBrainz's own strings for `a` name a credit, a stub or a heard
    artist `name` with no owned content behind it the way the phantom canon
    nominates (canon.match.phantom_candidate_gids): no fuzzy score, and an
    alias only for a name that is no compound — a collaboration's "X and Y"
    alias is a credit-redirect onto a member."""
    from canon.match import _PHANTOM_SEP, _mb_spelling, _probe_variants, _whole_variants
    if not (name or "").strip():
        return False
    forms = sorted(set(_whole_variants(name)) | {_mb_spelling(name)})
    probes = list(_probe_variants(name))
    sort_eq = name.strip() if "," in name else None
    if re.search(_PHANTOM_SEP, name, re.IGNORECASE):
        return _spelled(a, name_eq=forms, both_eq=probes, sort_eq=sort_eq)
    return _spelled(a, both_eq=forms + [name] + probes, unaccent_eq=[name], sort_eq=sort_eq)


def exact_premise(name: str, a: dict) -> bool:
    """MusicBrainz's own strings for `a` spell `name` the way the unique-name
    tier nominates (canon.match._name_exact_gids): the name or an alias,
    exactly, in MusicBrainz's punctuation, or unaccented."""
    from canon.match import _mb_spelling
    if not (name or "").strip():
        return False
    return _spelled(a, both_eq=[name, _mb_spelling(name)], unaccent_eq=[name])


def part_premise(part: str, a: dict) -> bool:
    """`part` of a compound name is MusicBrainz's name or an alias of `a`, as
    the name path of a collaboration split nominates its members."""
    return bool((part or "").strip()) and _spelled(a, both_eq=[part])


def credit_premise(local_name: str, credit: Sequence[dict]) -> bool:
    """MusicBrainz's credit `credit` is the owned compound `local_name`: the
    credit's own string (each member as credited, or under its name, with
    the join phrases) folds to the name or one of its &↔and spellings, or
    the name's parts are the credit's members, in any order."""
    from canon.match import _PHANTOM_SEP, _whole_variants
    if len(credit) < 2 or not (local_name or "").strip():
        return False
    keys = {normalize(v) for v in _whole_variants(local_name)}
    for field in ("credited_as", "name"):
        if normalize("".join((c.get(field) or c["name"]) + (c.get("join") or "")
                             for c in credit)) in keys:
            return True
    parts = sorted(normalize(p) for p in re.split(_PHANTOM_SEP, local_name, flags=re.IGNORECASE)
                   if p.strip())
    for field in ("credited_as", "name"):
        if parts == sorted(normalize(c.get(field) or c["name"]) for c in credit):
            return True
    return False


def credited_as(artist_mbid: str, name: str) -> bool:
    """MusicBrainz's answers credit the artist under `name` somewhere — the
    credit-head tier's own rule: it names a head as the recording credits it."""
    return bool((name or "").strip()) and db_query_one("""
        SELECT 1 AS x FROM mbws_credits
         WHERE artist = %(a)s::uuid AND lower(btrim(credited_as)) = lower(btrim(%(n)s))
         LIMIT 1""", {"a": str(artist_mbid), "n": name}) is not None


def name_equals(name: str, a: dict) -> bool:
    """`name` is MusicBrainz's name for `a` or one of its aliases (case and
    spacing aside) — what a person clicked or asked for must be the artist."""
    key = " ".join((name or "").split()).lower()
    return bool(key) and any(" ".join(s.split()).lower() == key for s in _mb_strings(a))


# --------------------------------------------------------------------------
# Credits and recordings
# --------------------------------------------------------------------------

def credited(credit: Sequence[dict], mbids: Iterable[str]) -> bool:
    wanted = {str(m) for m in mbids}
    return any(c["artist"] in wanted for c in credit or ())


def heads(credit: Sequence[dict], mbids: Iterable[str]) -> bool:
    return bool(credit) and credit[0]["artist"] in {str(m) for m in mbids}


def recording_premise(track_id: str, rec: dict, artist_mbids: Iterable[str]) -> bool:
    """The owned track is MusicBrainz's recording `rec` of the artist: credited
    to it, and matched as the canon's own cascade matches a track (its
    "wide-dur" tier, canon.content._CASCADE — the widest that times a match):
    an equal title once folded within its exact band, a trigram match at its
    threshold within its fuzzy band, wherever both lengths are known."""
    from canon.content import _CASCADE, _TRACK_STRIP, _norm
    if not credited(rec["credit"], artist_mbids):
        return False
    _label, exact_tol, fuzzy_tol, sim, *_rest = next(t for t in _CASCADE if t[0] == "wide-dur")
    owned = _norm("regexp_replace(t.title, %(strip)s, '', 'i')")
    theirs = _norm("CAST(%(title)s AS text)")
    within = "(t.dur IS NULL OR %(len)s::int IS NULL OR abs(%(len)s::int / 1000.0 - t.dur) <= {})"
    row = db_query_one(f"""
        WITH t AS (
            SELECT t.title, (SELECT min(f.duration_seconds) FROM owned_files f
                              WHERE f.track_id = t.id) AS dur
              FROM tracks t WHERE t.id = %(t)s::uuid)
        SELECT ({owned} = {theirs} AND {within.format('%(exact)s')})
            OR (similarity({owned}, {theirs}) >= %(sim)s AND {within.format('%(fuzzy)s')}) AS ok
          FROM t""", {"t": track_id, "title": rec["title"], "len": rec["length_ms"],
                      "strip": _TRACK_STRIP, "sim": sim, "exact": exact_tol, "fuzzy": fuzzy_tol})
    return bool(row and row["ok"])


# --------------------------------------------------------------------------
# An owned album against a release
# --------------------------------------------------------------------------

_COMPILATION_TYPES = {"Compilation", "DJ-mix", "Mixtape/Street"}


def _covers(album_id: str, artist_id: str, rel: dict) -> bool:
    """canon.content._RG_SQL's own gate, on MusicBrainz's tracklist: no
    bootleg or pseudo-release, a group of the album types; the release covers
    at least half the owned album and is not mostly other tracks; and it
    covers the album strongly or its group's title agrees with the album's —
    a compilation always needs the title."""
    from canon.content import (_RG_DURTOL, _RG_MIN_CONTENT, _RG_NAME_MIN, _RG_OWNED_MIN,
                               _RG_OWNED_STRONG, _RG_SIM, _norm)
    from discography import release_match_key
    group = rel.get("group") or {}
    tracks = rel["tracks"]
    if rel.get("status") in ("Bootleg", "Pseudo-Release") or not tracks:
        return False
    if group.get("primary_type") not in ("Album", "EP", "Single"):
        return False
    album = db_query_one("SELECT title FROM albums WHERE id = %(a)s::uuid", {"a": str(album_id)})
    if album is None:
        return False
    rgn = _norm("CAST(%(gtitle)s AS text)")
    row = db_query_one(f"""
        WITH owned AS (
            SELECT t.id, {_norm('t.title')} AS ntitle, f.duration_seconds::float AS dur
              FROM album_variants av
              JOIN owned_files f ON f.album_variant_id = av.id
              JOIN tracks t ON t.id = f.track_id
              JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
                                   AND ta.artist_id = %(artist)s::uuid
             WHERE av.album_id = %(album)s::uuid),
        theirs AS (
            SELECT {_norm('v.title')} AS rname, v.length
              FROM unnest(CAST(%(titles)s AS text[]), CAST(%(lengths)s AS int[])) AS v(title, length))
        SELECT (SELECT count(DISTINCT id) FROM owned) AS total,
               (SELECT count(DISTINCT o.id) FROM owned o JOIN theirs r
                    ON (r.rname = o.ntitle OR similarity(r.rname, o.ntitle) >= %(sim)s)
                   AND (r.length IS NULL OR abs(r.length / 1000.0 - o.dur) <= %(durtol)s)) AS cov,
               GREATEST(similarity({rgn}, {_norm('CAST(%(atitle)s AS text)')}),
                        similarity({rgn}, CAST(%(abase)s AS text))) AS namesim""",
                       {"artist": str(artist_id), "album": str(album_id),
                        "titles": [t["title"] or "" for t in tracks],
                        "lengths": [t["length_ms"] for t in tracks],
                        "sim": _RG_SIM, "durtol": _RG_DURTOL, "gtitle": group.get("title") or "",
                        "atitle": album["title"] or "",
                        "abase": release_match_key(album["title"]) or album["title"] or ""})
    total, cov = row["total"], row["cov"]
    if not total or cov < _RG_OWNED_MIN * total or cov < 0.5 * len(tracks):
        return False
    named = (row["namesim"] or 0) >= _RG_NAME_MIN
    if set(group.get("secondary_types") or ()) & _COMPILATION_TYPES and not named:
        return False
    return named or (cov >= _RG_OWNED_STRONG * total and cov >= _RG_MIN_CONTENT)


def release_premise(album_id: str, artist_id: str, artist_mbids: Iterable[str],
                    rel: dict) -> bool:
    """The owned album is MusicBrainz's release `rel` of the artist: the group
    is credited to it (or, when the answer carried no group credit, the
    release); the release is the album both ways, as the plan chose it
    (_covers); and the canon's own cascade (canon.content._CASCADE) verifies
    the album's tracks against the release's tracklist — its titles and
    lengths, the artist's own tracks only."""
    from canon.content import _CASCADE, _MATCH_SQL, _norm, _owned_counts
    mbids = [str(m) for m in artist_mbids]
    group = rel.get("group") or {}
    credit = group.get("credit") or rel["credit"]
    if not credited(credit, mbids) or not _covers(album_id, artist_id, rel):
        return False
    owned = _owned_counts(artist_id).get(str(album_id))
    if not owned:
        return False
    ntracks = owned[1]
    recs = [(t["recording"], t["title"], t["length_ms"]) for t in rel["tracks"]
            if t["recording"] and (credited(t["credit"], mbids) or credited(rel["credit"], mbids))]
    if not recs:
        return False
    import psycopg2.extras
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("DROP TABLE IF EXISTS _recs")
        cur.execute(f"""
            CREATE TEMP TABLE _recs AS
            SELECT v.rec_mbid, %(artist)s::text AS artist_mbid,
                   {_norm('v.title')} AS rname, v.length
              FROM unnest(CAST(%(r)s AS text[]), CAST(%(n)s AS text[]), CAST(%(l)s AS int[]))
                   AS v(rec_mbid, title, length)""",
                    {"artist": mbids[0], "r": [r[0] for r in recs], "n": [r[1] for r in recs],
                     "l": [r[2] for r in recs]})
        cur.execute("CREATE INDEX ON _recs USING gin (rname gin_trgm_ops)")
        try:
            for _label, dur, fdur, sim, minm, frac, cap in _CASCADE:
                cur.execute("SET pg_trgm.similarity_threshold = %s", (sim,))
                cur.execute(_MATCH_SQL, {"artist": str(artist_id), "albums": [str(album_id)],
                                         "durtol": dur, "fdurtol": fdur})
                matched = len(cur.fetchall())
                floor = min(minm, ntracks) if cap else minm
                if matched >= floor and matched / ntracks >= frac:
                    return True
            return False
        finally:
            cur.execute("RESET pg_trgm.similarity_threshold")
            cur.execute("DROP TABLE IF EXISTS _recs")


# --------------------------------------------------------------------------
# The binding of a local artist to an MBID
# --------------------------------------------------------------------------

def _owns_files(artist_id: str) -> bool:
    return db_query_one("""SELECT 1 AS x FROM track_artists ta
                             JOIN owned_files f ON f.track_id = ta.track_id
                            WHERE ta.artist_id = %(a)s::uuid AND ta.role = 'primary'
                            LIMIT 1""", {"a": str(artist_id)}) is not None


def owned_content(artist_id: str, mbids: List[str], asks: mb_answers.Asks) -> object:
    """The content premise of an owned artist's binding: one of the strongest
    bound albums that names its covering release, against that release, or the
    recordings stamped on the artist's files. Everything it may need is asked
    in one round: WAIT only when nothing answered proves it and an answer is
    missing — and only then does the caller's `asks` take its questions, so a
    verdict one piece of evidence proved never leaves the caller waiting on
    another. UNPROVEN when there is no evidence to weigh at all."""
    mine = mb_answers.Asks(asks.subject, asks.key, asks.priority, asks.author)
    verdict = _owned_content(artist_id, mbids, mine)
    if verdict is WAIT:
        asks.missing |= mine.missing
    return verdict


def _owned_content(artist_id: str, mbids: List[str], asks: mb_answers.Asks) -> object:
    albums = db_query("""
        SELECT a.id::text AS album_id, count(DISTINCT f.track_id) AS n,
               (SELECT av2.release_mbid::text FROM album_variants av2
                 WHERE av2.album_id = a.id AND av2.release_mbid IS NOT NULL
                 ORDER BY av2.id LIMIT 1) AS rel
          FROM albums a
          JOIN album_artists aa ON aa.album_id = a.id AND aa.role = 'primary'
           AND aa.artist_id = %(a)s::uuid AND aa.mbid = ANY(CAST(%(m)s AS uuid[]))
          JOIN album_variants av ON av.album_id = a.id
          JOIN owned_files f ON f.album_variant_id = av.id
         WHERE EXISTS (SELECT 1 FROM album_variants av3
                        WHERE av3.album_id = a.id AND av3.release_mbid IS NOT NULL)
         GROUP BY a.id ORDER BY n DESC, a.id LIMIT %(k)s""",
                      {"a": str(artist_id), "m": mbids, "k": MAX_ALBUMS})
    evidence = waits = False
    for alb in albums:
        evidence = True
        rel = release(alb["rel"], asks)
        if rel is WAIT:
            waits = True
        elif rel.known and release_premise(alb["album_id"], artist_id, mbids, rel.data):
            return True
    recs = db_query("""
        SELECT DISTINCT ON (f.recording_mbid) f.recording_mbid::text AS rec, f.track_id::text AS track
          FROM owned_files f JOIN track_artists ta ON ta.track_id = f.track_id
         WHERE ta.artist_id = %(a)s::uuid AND ta.role = 'primary' AND f.recording_mbid IS NOT NULL
         ORDER BY f.recording_mbid, f.track_id LIMIT %(k)s""",
                    {"a": str(artist_id), "k": MAX_RECORDINGS})
    confirmed = 0
    for r in recs:
        evidence = True
        rec = recording(r["rec"], asks)
        if rec is WAIT:
            waits = True
        elif rec.known and recording_premise(r["track"], rec.data, mbids):
            confirmed += 1
    if recs and confirmed >= min(2, len(recs)):
        return True
    if waits:
        return WAIT
    return False if evidence else UNPROVEN


def binding(artist_id: str, name: str, mbid: str, asks: mb_answers.Asks,
            need_content: bool = False) -> object:
    """Whether the local artist (`artist_id`, `name`) is MusicBrainz's artist
    `mbid`: MusicBrainz knows it, its names carry `name` (the name premise),
    and, for an artist with files here, its releases and recordings carry the
    owned content (the content premise). False — the binding refuted — only
    on MusicBrainz's word about the artist: unknown, or named otherwise.
    Content its answers do not bear out is UNPROVEN; with nothing owned to
    weigh, the name premise decides, unless `need_content` (a rename or a
    merge rests on the binding), which then gets UNPROVEN too.

    A row with no files here holds the binding a phantom tier confirmed on its
    own rule — a stub's choice or a heard name's anchor (phantom_premise), a
    credit head named as MusicBrainz credits the artist on a recording
    (credited_as) — and only a name none of those nor the owned tiers' rule
    carries refutes it."""
    a = artist(mbid, asks)
    owned = _owns_files(artist_id)
    if a is WAIT:
        # The content's answers are asked in the same round, not after.
        if owned:
            owned_content(artist_id, [str(mbid)], asks)
        return WAIT
    if not a.known:
        return False
    if not owned:
        return (name_premise(name, a.data) or phantom_premise(name, a.data)
                or credited_as(a.mbid, name))
    if not name_premise(name, a.data):
        return False
    content = owned_content(artist_id, sorted({str(mbid), a.mbid}), asks)
    if content is UNPROVEN:
        return UNPROVEN if need_content else True
    if content is False:
        return UNPROVEN
    return content


def mb_name(mbid: str) -> Optional[str]:
    """MusicBrainz's current name for the artist `mbid`, from a fresh answer."""
    a = mb_answers.answer("artist", mbid)
    if a is None or not a.known:
        return None
    data = mb_answers.artist(a.target)
    return data["name"] if data else None


def withdraw(artist_id: str, mbid: str) -> None:
    """MusicBrainz's answers refute that the local artist is `mbid`: the
    binding goes, with its albums' attribution, and the pair is remembered —
    no tier binds it again (the artist_mbids trigger refuses it)."""
    with transaction() as cur:
        cur.execute("""INSERT INTO mb_refuted_bindings (artist_id, mbid)
                       SELECT %(a)s::uuid, %(m)s::uuid
                        WHERE EXISTS (SELECT 1 FROM artists WHERE id = %(a)s::uuid)
                       ON CONFLICT DO NOTHING""", {"a": str(artist_id), "m": str(mbid)})
        cur.execute("DELETE FROM artist_mbids WHERE artist_id = %(a)s::uuid AND mbid = %(m)s::uuid",
                    {"a": str(artist_id), "m": str(mbid)})
        cur.execute("""UPDATE album_artists SET mbid = NULL
                        WHERE artist_id = %(a)s::uuid AND mbid = %(m)s::uuid""",
                    {"a": str(artist_id), "m": str(mbid)})


def refuted(artist_id: str, mbid: str) -> bool:
    return db_query_one("""SELECT 1 AS x FROM mb_refuted_bindings
                            WHERE artist_id = %(a)s::uuid AND mbid = %(m)s::uuid""",
                        {"a": str(artist_id), "m": str(mbid)}) is not None


def unstamp(artist_ids: Iterable[str]) -> None:
    """Artists whose decisions wait: their canon watermark goes, so the canon
    takes them again once the answers they wait on are here."""
    ids = sorted({str(a) for a in artist_ids})
    if ids:
        db_execute("UPDATE artists SET last_mb_sync = NULL WHERE id = ANY(CAST(%(i)s AS uuid[]))",
                   {"i": ids})
