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
    return strong


