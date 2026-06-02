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

import musicbrainz as mb
from db_pool import db_query, db_query_one
from discography import release_match_key
from normalize_artists import detect_compound_type
from uuid_utils import normalize

logger = logging.getLogger(__name__)

# MB secondary-types that disqualify a release-group as a "studio album"
# for the overlap check (we still want overlap on real albums only).
_NON_STUDIO = {"Compilation", "DJ-mix", "Mixtape/Street", "Live", "Remix",
               "Soundtrack", "Demo", "Interview", "Audiobook", "Spokenword"}

_SCORE_OK = 88        # MB top-candidate score considered a strong name match


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


def _studio_keys(mbid: str) -> set:
    keys = set()
    for rg in mb.fetch_album_release_groups(mbid):
        if set(rg["secondary_types"]) & _NON_STUDIO:
            continue
        k = release_match_key(rg["title"])
        if k:
            keys.add(k)
    return keys


def audit_artist(artist_id: str, name: str) -> Dict:
    """Resolve + overlap-verify one artist. Returns a report row. Read-only."""
    row: Dict = {"name": name, "artist_id": str(artist_id),
                 "action": "unsure", "mbid": None, "canonical": None,
                 "score": None, "overlap": None, "note": ""}

    owned = _owned_match_keys(artist_id)
    compound = detect_compound_type(name)  # ('safe'|'suspicious', sep, parts) | None

    try:
        candidates = mb.search_artist(name, limit=3)
    except mb.MBRateLimited:
        row["note"] = "mb-cooldown"
        return row
    except Exception as e:
        row["note"] = f"mb-error: {e}"
        return row

    top = candidates[0] if candidates and (candidates[0].get("score") or 0) >= _SCORE_OK else None

    # 1) Overlap-verify the top full-name candidate FIRST. Overlap is the
    #    anchor, not an exact name string — this catches real "&"-entities
    #    and canonical variations the name check would miss, e.g. MB stores
    #    "Percy Faith and His Orchestra" while the tag says "… & His
    #    Orchestra" (& vs and), or diacritic/abbreviation/transliteration.
    if top and owned:
        row.update(mbid=top["mbid"], canonical=top["name"], score=top["score"])
        try:
            studio = _studio_keys(top["mbid"])
        except mb.MBRateLimited:
            row["note"] = "mb-cooldown"
            return row
        except Exception as e:
            row["note"] = f"mb-rg-error: {e}"
            return row
        ov = owned & studio
        row["overlap"] = len(ov)
        if ov:
            row["action"] = "keep" if normalize(top["name"]) == normalize(name) else "rename"
            return row
        # Full name didn't overlap-verify — maybe an ad-hoc compound to split.

    # 2) Compound with no overlap-verified full-name entity → split attempt.
    if compound:
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
