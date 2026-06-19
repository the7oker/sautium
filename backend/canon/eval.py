"""Regression oracle for canon name-matching.

Content-verified MBIDs (confidence='overlap_verified') are ground truth: those
artists were proven by their own audio overlapping MB recordings, so the MBID is
correct independent of how the name was spelled. This harness replays the messy
INPUT names (artists.name + the pre-canon artists.raw_name) through the read-only
name-matching layers and asks: does the correct MBID come back?

It exists so every future matcher change gets a NUMBER (recall + miss buckets)
instead of a spot-check — the Docker labeled set is the authoritative oracle
(see reference_launcher_test_stand). Run:  python -m canon.eval [N] [--fuzzy]
  N        cap the labeled set (default: all)
  --fuzzy  also try _candidates (trgm search) on a deterministic miss
"""
import re
import sys
from collections import Counter

from db_pool import db_query
from canon.match import phantom_candidate_gids, _candidates


def _labeled(limit=None):
    """Non-trivial (raw_name, mbid, mb_name) triples. Truth = a single
    overlap_verified MBID per artist; inputs = the messy ORIGINAL tags
    (media_files.raw_artist) that a fresh scan would feed the matcher. Keep only
    rows where the raw tag DIFFERS from the MB-canonical name (exact = trivial hit)
    and is NOT a collaboration form (splitting a compound into a member is the
    content layer's job, not the name matcher's — those would be false misses)."""
    lim = "LIMIT %(l)s" if limit else ""
    rows = db_query(f"""
        SELECT DISTINCT btrim(mf.raw_artist) AS name, am.mbid::text AS mbid, m.name AS mb_name
        FROM media_files mf
        JOIN track_artists ta ON ta.track_id = mf.track_id AND ta.role = 'primary'
        JOIN artists a ON a.id = ta.artist_id
        JOIN artist_mbids am ON am.artist_id = a.id AND am.confidence = 'overlap_verified'
        JOIN mb_artist m ON m.gid = am.mbid
        WHERE (SELECT count(*) FROM artist_mbids x WHERE x.artist_id = a.id) = 1
          AND nullif(btrim(mf.raw_artist), '') IS NOT NULL
          AND lower(btrim(mf.raw_artist)) <> lower(btrim(m.name))
          AND m.name <> 'Various Artists'   -- VA = a comp-track mislabel, not a matcher truth
          AND btrim(mf.raw_artist) !~* ' & | and | with |/|;|,| feat| ft\\.| vs'
        {lim}
    """, {"l": limit} if limit else {})
    return [(r["name"], r["mbid"], r["mb_name"]) for r in rows]


def _bucket(name: str) -> str:
    if re.search(r'[^\x00-\x7f]', name):                 return "non_ascii"
    if re.search(r'\s*/\s*|;', name):                    return "slash"
    if re.search(r' & | and | with ', name, re.I):       return "ampersand"
    if "," in name:                                      return "comma"
    if re.search(r"['‘’‐–—]", name): return "punct"
    return "ascii_plain"


def run(limit=None, fuzzy=False) -> dict:
    pairs = _labeled(limit)
    tot = len(pairs)
    hit = uniq = miss = recovered_by_fuzzy = 0
    miss_buckets = Counter()
    examples = []
    for nm, mbid, mbname in pairs:
        cands = phantom_candidate_gids(nm)
        if mbid not in cands and fuzzy:
            fc = _candidates(nm)
            if mbid in fc:
                recovered_by_fuzzy += 1
                cands = fc
        if mbid in cands:
            hit += 1
            if len(set(cands)) == 1:
                uniq += 1
        else:
            miss += 1
            miss_buckets[_bucket(nm)] += 1
            if len(examples) < 25:
                examples.append((nm, mbname))

    pct = (100.0 * hit / tot) if tot else 0.0
    print(f"=== canon name-match oracle ({tot} non-trivial labeled pairs) ===")
    print(f"recall (truth MBID in candidates): {hit}/{tot}  ({pct:.1f}%)")
    print(f"  unambiguous (candidates == {{truth}}): {uniq}")
    if fuzzy:
        print(f"  of the hits, recovered only by --fuzzy: {recovered_by_fuzzy}")
    print(f"miss: {miss}")
    print(f"miss buckets: {dict(miss_buckets)}")
    print("sample misses (input name -> truth MB name):")
    for nm, mbname in examples:
        print(f"  {nm!r:36} -> {mbname!r}")
    return {"pairs": tot, "recall": hit, "recall_pct": round(pct, 1),
            "unambiguous": uniq, "miss": miss, "buckets": dict(miss_buckets)}


if __name__ == "__main__":
    fuzzy = "--fuzzy" in sys.argv
    limit = next((int(a) for a in sys.argv[1:] if a.isdigit()), None)
    run(limit=limit, fuzzy=fuzzy)
