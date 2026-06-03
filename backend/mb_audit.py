"""Read-only MusicBrainz canonicalization audit.

For each artist: resolve on MB, verify the match against the artist's
OWNED albums (release-group overlap), and propose an action — without
mutating anything. The output calibrates the confidence gates before the
background pass is allowed to write aliases / MBIDs / merges / splits.

Proposed actions:
  keep    — MB confirms the current name (overlap-verified); store MBID.
  rename  — MB canonical differs (diacritics/abbrev/translit), overlap-
            verified; rename + store MBID.
  split   — compound name has no MB entity but components resolve → collab.
  unsure  — no confident MB match or overlap couldn't confirm → leave as-is.
"""

import logging
from typing import Dict, List, Optional

import mb_backend as mb
from db_pool import db_query, db_query_one
from discography import release_match_key
from normalize_artists import detect_compound_type
from uuid_utils import normalize

logger = logging.getLogger(__name__)

# MB secondary-types that disqualify a release-group as a clean first-party
# release for the overlap check — these are cross-artist-noisy / generic
# (a "Greatest Hits", a live set, a remix package).
_NOISY_SECONDARY = {"Compilation", "DJ-mix", "Mixtape/Street", "Live", "Remix",
                    "Soundtrack", "Demo", "Interview", "Audiobook", "Spokenword"}

_SCORE_OK = 88        # MB top-candidate score considered a strong name match
_FUZZY_FLOOR = 60     # overlap-scan floor: typos/encoding-corruption score lower
                      # than _SCORE_OK ("Davis Gilmour"→"David Gilmour" ~75), but
                      # owned-album OVERLAP is the real gate, so it's safe to
                      # check them — overlap rejects the wrong ones for free.


def _prefix_related(a: str, b: str) -> bool:
    """True if one name is a token-prefix of the other — i.e. MB's canonical
    name EXTENDS the library tag (or vice-versa): "Bob Marley" ↔ "Bob Marley &
    The Wailers", "Percy Faith" ↔ "Percy Faith and His Orchestra". Such a suffix
    dilutes the trigram score below the gate even though it's the same entity,
    so this bridges the gap for the overlap scan. Token-wise (not substring) so
    "Bob Marley" does not match "Not Bob Marley" / "Bo Marley".
    """
    ta, tb = normalize(a).split(), normalize(b).split()
    if not ta or not tb:
        return False
    n = min(len(ta), len(tb))
    return ta[:n] == tb[:n]


def _owned_match_keys(artist_id: str) -> set:
    rows = db_query("""
        SELECT DISTINCT al.title
        FROM albums al
        JOIN album_variants av ON av.album_id = al.id
        JOIN media_files mf ON mf.album_variant_id = av.id
        JOIN tracks t ON t.id = mf.track_id
        JOIN track_artists ta ON ta.track_id = t.id
        WHERE ta.artist_id = %(id)s::uuid
    """, {"id": str(artist_id)})
    return {release_match_key(r["title"]) for r in rows}


def _release_keys(mbid: str) -> set:
    """Match-keys for an artist's first-party releases (Album/EP/Single, minus
    noisy secondary types) — the candidate side of the overlap check."""
    keys = set()
    for rg in mb.fetch_album_release_groups(mbid):
        if set(rg["secondary_types"]) & _NOISY_SECONDARY:
            continue
        # RG canonical title + every release variant title (local-only): a dirty
        # owned tag frequently matches a regional/alternate release name, not the
        # group's canonical name.
        for title in (rg["title"], *rg.get("release_titles", ())):
            k = release_match_key(title)
            if k:
                keys.add(k)
    return keys


def _overlap(owned: set, candidate: set) -> set:
    """Candidate release-keys that verify against the owned albums: an exact key
    match, OR — to absorb the catalog/label/region/format cruft libraries append
    to folder names — a candidate key (≥5 chars) that is a token-prefix of an
    owned key ("mirrors" ⊑ "mirrors victor vil 28062 japan"). The clean MB title
    leads, the junk trails, so the asymmetric prefix is safe; the ≥5 guard keeps
    tiny keys ("era", "boy") from prefixing unrelated owned titles, and the whole
    check is already scoped to a name-corroborated candidate."""
    matched = set(owned & candidate)
    owned_tokens = [o.split() for o in owned if o]
    for ck in candidate - matched:
        if len(ck) < 5:
            continue
        ct = ck.split()
        n = len(ct)
        if any(ot[:n] == ct for ot in owned_tokens):
            matched.add(ck)
    return matched


def audit_artist(artist_id: str, name: str) -> Dict:
    """Resolve + overlap-verify one artist. Returns a report row. Read-only."""
    row: Dict = {"name": name, "artist_id": str(artist_id),
                 "action": "unsure", "mbid": None, "canonical": None,
                 "score": None, "overlap": None, "note": ""}

    owned = _owned_match_keys(artist_id)
    compound = detect_compound_type(name)  # ('safe'|'suspicious', sep, parts) | None

    try:
        candidates = mb.search_artist(name, limit=8)
    except mb.MBRateLimited:
        row["note"] = "mb-cooldown"
        return row
    except Exception as e:
        row["note"] = f"mb-error: {e}"
        return row

    strong = [c for c in candidates if (c.get("score") or 0) >= _SCORE_OK]
    top = strong[0] if strong else None
    if top:
        row.update(mbid=top["mbid"], canonical=top["name"], score=top["score"])

    # 1) Overlap-verify candidates best-first. Overlap — not the name-similarity
    #    score — is the trust anchor (it can't be faked), so eligibility is gated
    #    LOOSELY and overlap decides. A candidate is checked if it scores highly
    #    OR if the names are token-prefix related. That second clause is the key
    #    local fix: when MB's canonical name EXTENDS the library tag ("Bob
    #    Marley" → "Bob Marley & The Wailers", "Percy Faith" → "Percy Faith and
    #    His Orchestra"), the suffix dilutes the trigram score below the gate
    #    even though it's the right entity that owns the albums — the MB API
    #    ranked these by relevance, local trigram can't. Overlap ALSO
    #    disambiguates same-name ties (several "Adele"): the candidate whose
    #    studio discography overlaps the owned albums wins. Best-first order
    #    means an exact entity that DOES overlap is taken before any extended
    #    name, so junk namesakes ("Not Bob Marley") never win — and they have no
    #    overlap anyway. (API path: only eligible candidates cost a fetch.)
    #    The floor is _FUZZY_FLOOR (not _SCORE_OK): a typo / encoding corruption
    #    ("Davis Gilmour", "Mel Tormй") scores below _SCORE_OK yet overlap-
    #    verifies to the right entity — overlap, the anchor, makes that safe.
    if owned:
        for c in candidates:
            if (c.get("score") or 0) < _FUZZY_FLOOR and not _prefix_related(name, c["name"]):
                continue
            try:
                releases = _release_keys(c["mbid"])
            except mb.MBRateLimited:
                row["note"] = "mb-cooldown"
                return row
            except Exception as e:
                row["note"] = f"mb-rg-error: {e}"
                return row
            ov = _overlap(owned, releases)
            if ov:
                row.update(mbid=c["mbid"], canonical=c["name"],
                           score=c["score"], overlap=len(ov))
                row["action"] = "keep" if normalize(c["name"]) == normalize(name) else "rename"
                return row
        row["overlap"] = 0
        # Nothing overlap-verified — maybe an ad-hoc compound to split.

    # 2) Compound → split attempt, but ONLY when the whole name has no strong MB
    #    entity. The split gate is "MB-empty whole-name + all components resolve"
    #    (e.g. "GMO & Dense", "DJ Snake & Lil Jon"). A real band that happens to
    #    contain "&" ("Bob Marley & The Wailers", "Simon & Garfunkel") strongly
    #    matches an MB Group, so it must be kept whole, not fragmented — even
    #    when the owned albums didn't overlap-verify it (empty/compilation-only).
    if compound and not top:
        parts = compound[2]
        resolved = []
        for p in parts:
            try:
                c = mb.search_artist(p, limit=1)
            except mb.MBRateLimited:
                row["note"] = "mb-cooldown"
                return row
            except Exception:
                c = []
            ok = bool(c and (c[0]["score"] or 0) >= _SCORE_OK
                      and normalize(c[0]["name"]) == normalize(p))
            resolved.append((p, ok))
        if all(ok for _, ok in resolved):
            row["action"] = "split"
            row["note"] = "→ " + " + ".join(p for p, _ in resolved)
        else:
            row["note"] = "compound, components: " + \
                ", ".join(f"{p}{'✓' if ok else '✗'}" for p, ok in resolved)
        return row

    # 3) Nothing confident / couldn't verify → leave as-is.
    if not top:
        row["note"] = "no confident MB match"
    elif not owned:
        row["note"] = "no owned albums to verify"
    else:
        row["note"] = f"score {top['score']} but 0/{len(owned)} owned overlap — unverified"
    return row


def run_audit(artist_rows: List[dict]) -> List[Dict]:
    """Audit a list of {id, name}. Returns report rows (read-only)."""
    out = []
    for a in artist_rows:
        out.append(audit_artist(a["id"], a["name"]))
    return out


def find_merge_candidates(rows: List[Dict]) -> Dict[str, List[Dict]]:
    """Cross-artist merge detection: ≥2 distinct library artists that
    overlap-verify to the SAME MBID are duplicate rows that should merge
    (e.g. "H. Mancini" + "Henry Mancini"). No extra MB calls — pure
    post-processing over audit rows. Returns {mbid: [rows]}.
    """
    from collections import defaultdict
    by_mbid = defaultdict(list)
    for r in rows:
        if r["action"] in ("keep", "rename") and r.get("mbid"):
            by_mbid[r["mbid"]].append(r)
    return {m: rs for m, rs in by_mbid.items()
            if len({x["artist_id"] for x in rs}) >= 2}


def audit_to_jsonl(artist_rows: List[dict], out_path: str) -> int:
    """Audit artists, appending one JSON row each to out_path. Resumable
    (skips artist_ids already written) and cooldown-resilient (waits out an
    MB 503 cooldown rather than skipping the artist). Returns rows written.
    Built for a long background run that may be interrupted."""
    import json
    import time

    done = set()
    try:
        with open(out_path) as f:
            for line in f:
                try:
                    done.add(json.loads(line)["artist_id"])
                except Exception:
                    pass
    except FileNotFoundError:
        pass

    written = 0
    with open(out_path, "a") as f:
        for a in artist_rows:
            if str(a["id"]) in done:
                continue
            while mb.cooldown_active():
                time.sleep(5)
            row = audit_artist(a["id"], a["name"])
            if row.get("note") == "mb-cooldown":
                while mb.cooldown_active():
                    time.sleep(5)
                row = audit_artist(a["id"], a["name"])
            f.write(json.dumps(row) + "\n")
            f.flush()
            written += 1
    return written
