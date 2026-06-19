"""General candidate-finding for canonicalization (Layer 2 — shared).

Name → MusicBrainz candidate MBIDs: exact name/alias probe, fuzzy search
score-gate, and name-variant (surname reorder / prefix strip / diacritic /
mojibake) probes. The one place future match-whole improvements (&↔and,
alias-aware, unaccent) should land — every specialized canon algorithm
(content, phantom) draws its candidates from here.
"""
import re
import unicodedata

import mb_backend as mb
from db_pool import db_query
from uuid_utils import normalize

_CAND_SCORE = 70   # candidate name-score floor — drops generic partial namesakes


def _exact_mbids(q: str) -> list:
    """All MB artists whose name or alias equals q (lower, indexed — ~1-2 ms)."""
    return [r["gid"] for r in db_query("""
        SELECT a.gid::text AS gid FROM mb_artist a WHERE lower(a.name) = lower(%(q)s)
        UNION
        SELECT a.gid::text FROM mb_artist a
        JOIN mb_artist_alias al ON al.artist = a.id WHERE lower(al.name) = lower(%(q)s)
    """, {"q": q})]


def _whole_variants(name: str) -> list:
    """`name` plus separator-spelling variants that denote the SAME entity:
    ' & ' <-> ' and '. MB registers many bands under the 'and' spelling
    ('Jon and Vangelis', 'Gerry and the Pacemakers', 'Nelson Riddle and His
    Orchestra') while libraries and Last.fm tag them with '&'. Whole-entity
    matching must see through that so a REGISTERED band is kept whole, never
    mis-split into its tokens."""
    n = " ".join(name.split())
    out = [n]
    amp = re.sub(r"\s+&\s+", " and ", n)
    if amp != n:
        out.append(amp)
    a_nd = re.sub(r"\s+and\s+", " & ", n, flags=re.IGNORECASE)
    if a_nd != n:
        out.append(a_nd)
    return out


def match_whole_gids(name: str) -> list:
    """Distinct exact whole-entity MBIDs for `name` by mb_artist.NAME (not alias),
    separator-spelling aware (see _whole_variants). A non-empty result means MB
    holds this as a SINGLE registered entity (band/group/orchestra), so it must be
    kept whole rather than split into members.

    NAME-only on purpose: a collaboration 'X & Y' is frequently an ALIAS / credit-
    redirect on member X ('Vangelis and Irene Papas' aliases the Vangelis person),
    and matching that would collapse the collaboration onto one member. Registered
    bands carry the '&'/'and' form as their NAME (Jon and Vangelis, Gerry and the
    Pacemakers), so name-match keeps them whole without the trap. Shared by the
    content `_collab_members` guard and the phantom canon — the &↔and home."""
    seen = []
    for v in _whole_variants(name):
        for r in db_query("SELECT gid::text AS gid FROM mb_artist "
                          "WHERE lower(name) = lower(%(q)s)", {"q": v}):
            if r["gid"] not in seen:
                seen.append(r["gid"])
    return seen


def _probe_variants(name: str) -> list:
    """Cheap deterministic query transforms — the candidate-generation layer.
    Each output is exact-probed against mb_artist/alias; hits only ADD candidates,
    content verification still gates everything, so recall grows while precision
    stays content-bound. Covers: "Surname, First" reorder, DJ/The/ВИА prefix
    strip & add, diacritic fold (owned-accented → MB-plain), cp1251↔latin1
    mojibake round-trips ('Andrй Sobota' → 'André Sobota')."""
    n = name.strip()
    out = []
    if n.count(",") == 1 and not re.search(r" & | and | with | / ", n):
        a, b = (p.strip() for p in n.split(","))
        if a and b:
            out.append(f"{b} {a}")
    m = re.match(r"(?i)^(dj|the|via|виа|віа)\s+(.+)$", n)
    if m:
        out.append(m.group(2))
    else:
        out.extend((f"DJ {n}", f"The {n}"))
    folded = "".join(c for c in unicodedata.normalize("NFKD", n)
                     if not unicodedata.combining(c))
    if folded != n:
        out.append(folded)
    for enc, dec in (("cp1251", "latin1"), ("latin1", "cp1251")):
        try:
            v = n.encode(enc).decode(dec)
            if v != n and v.isprintable():
                out.append(v)
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return out


def _candidates(name: str) -> list:
    """Candidate mbids for content verification: the primary search (exact-first
    + trgm fuzzy) plus exact hits of the probe variants."""
    cands = mb.search_artist(name, limit=8)
    strong = [c["mbid"] for c in cands if (c.get("score") or 0) >= _CAND_SCORE]
    if not strong and cands:
        strong = [cands[0]["mbid"]]
    seen = set(strong)
    for v in _probe_variants(name):
        for g in _exact_mbids(v):
            if g not in seen:
                seen.add(g)
                strong.append(g)
    # comma-named artists: mb_artist.sort_name IS the "Surname, First" form
    # ("Kaji, Meiko" exactly) — indexed exact probe
    if "," in name:
        for r in db_query(
                "SELECT gid::text AS gid FROM mb_artist WHERE lower(sort_name) = lower(%(q)s)",
                {"q": name.strip()}):
            if r["gid"] not in seen:
                seen.add(r["gid"])
                strong.append(r["gid"])
    # &↔and whole-entity variants (registered bands spelled with the other separator)
    for g in match_whole_gids(name):
        if g not in seen:
            seen.add(g)
            strong.append(g)
    return strong


# MB's style guide stores apostrophes as U+2019 and hyphens as U+2010, while
# library/Last.fm names use ASCII (and assorted apostrophe stand-ins: backtick,
# acute accent) — so 'Anita O'Day' / 'Lab`s Cloud' / 'Aphrodite´s Child' / 'VC-118A'
# miss the exact index against the MB spelling. _mb_spelling re-spells to MB's form
# (+ collapses whitespace) so they match off the existing lower(name) index — no
# fuzzy, no new index. Oracle-justified (canon.eval punct-bucket misses).
_MB_PUNCT = str.maketrans({"'": "’", "`": "’", "´": "’", "-": "‐"})


def _mb_spelling(name: str) -> str:
    return " ".join(name.translate(_MB_PUNCT).split())
# collaboration separators — for phantom matching, alias is trusted ONLY for
# non-compounds (a compound's 'X and Y' alias is a credit-redirect onto a member).
_PHANTOM_SEP = r'\s+&\s+|\s+and\s+|\s+with\s+|\s*/\s*|\s*,\s*|;'


def phantom_candidate_gids(name: str) -> list:
    """Deterministic MB candidate MBIDs for a phantom (no owned content to verify)
    — NO trgm fuzzy, precision over recall like the rest of canon. Gathers: exact
    name + &↔and + MB-punctuation respelling + surname/prefix/diacritic/mojibake
    probes + sort_name; PLUS exact ALIAS for NON-compound names (a non-Latin act's
    romanization is stored as an alias — 윈터플레이→Winterplay — recovering it safely,
    while a collaboration's 'X and Y' alias would be a credit-redirect onto a
    member, so alias is gated to non-compounds). The phantom canon feeds the result
    through the same 1-namesake / genre-disambiguation logic."""
    compound = bool(re.search(_PHANTOM_SEP, name, re.IGNORECASE))
    out, seen = [], set()

    def add(gids):
        for g in gids:
            if g not in seen:
                seen.add(g)
                out.append(g)

    for v in set(_whole_variants(name)) | {_mb_spelling(name)}:
        if compound:   # name-only: a compound's alias is a member credit-redirect
            add(r["gid"] for r in db_query(
                "SELECT gid::text AS gid FROM mb_artist WHERE lower(name) = lower(%(q)s)",
                {"q": v}))
        else:
            add(_exact_mbids(v))                    # name + alias
    if not compound:
        add(_exact_mbids(name))                     # raw-name alias (romanizations)
    for v in _probe_variants(name):
        add(_exact_mbids(v))
    if "," in name:
        add(r["gid"] for r in db_query(
            "SELECT gid::text AS gid FROM mb_artist WHERE lower(sort_name) = lower(%(q)s)",
            {"q": name.strip()}))
    return out


