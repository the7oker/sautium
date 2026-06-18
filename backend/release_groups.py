"""Release-group clustering for owned albums.

After MB canonicalization, one logical album (a MusicBrainz **release group**)
is spread across several `albums` rows — one per *edition* (a distinct
tracklist: Standard / Deluxe / Trilogy / orchestral re-recording). True rips
(CD/Vinyl of the *same* tracklist) stay folded as `album_variants` inside one
edition row; editions do not — they are separate rows so a 12-track soundtrack
and a 36-track box set are never presented as interchangeable variants.

This module groups those edition rows back into the logical release group and
labels each edition with the part of its title that distinguishes it.

Grouping is `musicbrainz_id`-first (the authoritative signal — once
`canon.content` relaxes RG matching, even bootleg editions carry the RG MBID),
with a bounded same-artist fallback so editions MB models as *separate* release
groups (e.g. "Blade Runner (New American Orchestra)") still land together, while
box-set members ("The Complete Pop Albums: Romanza" vs ": Mi Navidad", distinct
RGs/bases) stay apart. The fallback keys on the RG-canonical `albums.title`
(reusing `discography.release_match_key`/`title_residual`), never the raw tag.

The group id is the **primary edition's album id** — a real row, always
resolvable, no synthetic identifiers. `release_group_cluster(any_member)` and
`release_group_cluster(group_id)` return the same ordered component.
"""

import logging
from typing import Dict, List, Optional

from db_pool import db_query, db_query_one
from discography import _plain_key, release_match_key, split_edition, title_residual
from uuid_utils import artist_uuid

logger = logging.getLogger(__name__)

# Compilations credited to "Various Artists" share no logical album — group them
# by RG MBID only, never by the (meaningless) shared artist.
_VARIOUS_ARTISTS_ID = str(artist_uuid("Various Artists"))

# Pathological artist-album counts (mis-attribution) would make the O(n²) edge
# scan blow up; above this, fall back to RG-MBID-only grouping.
_PAIRWISE_CAP = 600


def simplify_edition_name(name: str, group_base: str) -> Optional[str]:
    """The part of an edition title that distinguishes it from the group base.

    Returns ``None`` for the bare base (the primary edition — the caller labels
    it). Prefers the clean MB release name when the caller has one.

        ("Blade Runner Trilogy, 25th Anniversary", "Blade Runner") -> "Trilogy, 25th Anniversary"
        ("Blade Runner: The Complete Score", "Blade Runner")       -> "The Complete Score"
        ("Blade Runner", "Blade Runner")                           -> None
    """
    name = (name or "").strip()
    base = (group_base or "").strip()
    # Bare base = the primary edition (compare without edition-stripping, so an
    # "Esper Definitive Edition" qualifier isn't mistaken for the base itself).
    if not name or _plain_key(name) == _plain_key(base):
        return None
    bk = release_match_key(base)
    # Peel the base off the front, repeatedly — MB sometimes doubles it
    # ("Blade Runner: Blade Runner Trilogy, 25th Anniversary"). Each step removes
    # a trailing marker that leaves exactly the base, or a leading base prefix.
    cur = name
    while bk:
        r = title_residual(cur, base)            # " : <rest>" / " - <rest>" / "(<rest>)"
        if r and _plain_key(r) != _plain_key(cur):
            cur = r
        elif release_match_key(cur).startswith(bk + " "):
            cur = " ".join(cur.split()[len(base.split()):]).strip(" -–—:,([")
        else:
            break
        if _plain_key(cur) == _plain_key(base):
            return None
    if _plain_key(cur) != _plain_key(name):
        return cur or split_edition(name)[1] or name
    # Nothing peeled: a bracket/suffix edition marker on its own ("… (Deluxe Edition)").
    return split_edition(name)[1] or name


def _mk(row: dict) -> dict:
    """Normalize a candidate album row into the internal grouping shape."""
    arts = {a for a in (row.get("arts") or []) if a and a != _VARIOUS_ARTISTS_ID}
    title = row.get("title") or ""
    return {"id": row["id"], "title": title, "mbid": row.get("mbid"),
            "arts": arts, "key": release_match_key(title)}


def _same_group(a: dict, b: dict) -> bool:
    """Edge predicate between two edition rows (symmetric)."""
    if a["mbid"] and a["mbid"] == b["mbid"]:        # authoritative: same RG MBID
        return True
    if not (a["arts"] & b["arts"]):                  # else: real shared primary artist
        return False
    if a["key"] and a["key"] == b["key"]:            # identical base title
        return True
    # one title is the other plus a bracketed/dash edition marker
    return bool(title_residual(a["title"], b["title"])
                or title_residual(b["title"], a["title"]))


def _components(albums: List[dict]) -> List[List[dict]]:
    """Connected components over the `_same_group` relation (union-find)."""
    parent = {a["id"]: a["id"] for a in albums}

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:        # path compression
            parent[x], x = root, parent[x]
        return root

    pairwise = len(albums) <= _PAIRWISE_CAP
    if not pairwise:
        logger.warning("release_group: %d candidates exceed pairwise cap — "
                       "RG-MBID-only grouping", len(albums))
    for i in range(len(albums)):
        for j in range(i + 1, len(albums)):
            a, b = albums[i], albums[j]
            edge = (a["mbid"] and a["mbid"] == b["mbid"]) if not pairwise \
                else _same_group(a, b)
            if edge:
                parent[find(a["id"])] = find(b["id"])

    comps: Dict[str, List[dict]] = {}
    for a in albums:
        comps.setdefault(find(a["id"]), []).append(a)
    return list(comps.values())


def _order(comp: List[dict]) -> List[dict]:
    """Component ordered primary-first: the bare-titled edition (shortest title,
    its qualifier stripped to the RG name) leads, MB-known before NULL-rg, then
    by id for determinism."""
    return sorted(comp, key=lambda a: (len(a["title"]), 0 if a["mbid"] else 1, a["id"]))


def _fetch_candidates(album_id) -> tuple[Optional[dict], List[dict]]:
    """The seed row plus every owned album that could share its release group:
    same RG MBID, or same real primary artist."""
    seed = db_query_one("""
        SELECT a.id::text AS id, a.title, a.musicbrainz_id::text AS mbid,
               array(SELECT aa.artist_id::text FROM album_artists aa
                     WHERE aa.album_id = a.id AND aa.role = 'primary') AS arts
        FROM albums a WHERE a.id = %(id)s::uuid
    """, {"id": str(album_id)})
    if not seed:
        return None, []
    arts = [x for x in (seed["arts"] or []) if x != _VARIOUS_ARTISTS_ID]
    rows = db_query("""
        SELECT a.id::text AS id, a.title, a.musicbrainz_id::text AS mbid,
               array(SELECT aa.artist_id::text FROM album_artists aa
                     WHERE aa.album_id = a.id AND aa.role = 'primary') AS arts
        FROM albums a
        WHERE EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = a.id)
          AND (
            (%(mbid)s IS NOT NULL AND a.musicbrainz_id::text = %(mbid)s)
            OR (%(arts)s::uuid[] <> '{}'::uuid[]
                AND EXISTS (SELECT 1 FROM album_artists aa
                            WHERE aa.album_id = a.id AND aa.role = 'primary'
                              AND aa.artist_id = ANY(%(arts)s::uuid[])))
          )
    """, {"mbid": seed["mbid"], "arts": arts})
    return seed, rows


def _resolve(album_id) -> List[dict]:
    """The seed's release-group component, ordered primary-first. Empty if the
    album does not exist."""
    seed, rows = _fetch_candidates(album_id)
    if not seed:
        return []
    by_id: Dict[str, dict] = {}
    for r in [seed, *rows]:                 # seed may be a phantom (no variants)
        by_id.setdefault(r["id"], _mk(r))
    for comp in _components(list(by_id.values())):
        if any(a["id"] == seed["id"] for a in comp):
            return _order(comp)
    return [by_id[seed["id"]]]


def _group_name(comp: List[dict]) -> str:
    """The release group's display name — the MB RG name when known, else the
    primary edition's title with any edition qualifier stripped."""
    primary = comp[0]
    mbid = primary["mbid"] or next((a["mbid"] for a in comp if a["mbid"]), None)
    if mbid:
        row = db_query_one(
            "SELECT name FROM mb_release_group WHERE gid = %(g)s::uuid", {"g": mbid})
        if row and row["name"]:
            return row["name"]
    return split_edition(primary["title"])[0] or primary["title"]


def release_group_cluster(album_id) -> List[str]:
    """Edition album ids of the release group `album_id` belongs to, primary
    first. ``[album_id]`` for a standalone album; ``[]`` if it doesn't exist.
    Stable: passing any member (or the group id) returns the same list."""
    return [a["id"] for a in _resolve(album_id)]


def release_group_meta(album_id) -> Optional[dict]:
    """``{id, name, edition_count}`` for the album's release group, or ``None``.
    ``id`` is the group id (the primary edition's album id) — what the frontend
    routes to when ``edition_count > 1``."""
    comp = _resolve(album_id)
    if not comp:
        return None
    return {"id": comp[0]["id"], "name": _group_name(comp),
            "edition_count": len(comp)}


def collapse_to_groups(rows: List[dict], artist_id: Optional[str] = None) -> List[dict]:
    """Collapse a shelf of album rows to one representative per release group.

    Each returned row is a copy of the group's primary edition row plus
    ``group_id`` and ``edition_count``; input order is preserved by first
    appearance. Rows must carry ``id``, ``title`` and ``musicbrainz_id`` (or
    ``mbid``). Pass ``artist_id`` when every row shares one primary artist (the
    artist shelf) so the same-artist fallback applies without a per-row lookup.
    """
    arts = [artist_id] if artist_id else None
    albums = [_mk({"id": r["id"], "title": r.get("title"),
                   "mbid": r.get("musicbrainz_id") or r.get("mbid"),
                   "arts": arts if arts is not None else r.get("arts")})
              for r in rows]
    comp_of: Dict[str, int] = {}
    primary_of: Dict[int, str] = {}
    size_of: Dict[int, int] = {}
    for i, comp in enumerate(_components(albums)):
        primary_of[i] = _order(comp)[0]["id"]
        size_of[i] = len(comp)
        for a in comp:
            comp_of[a["id"]] = i

    by_id = {r["id"]: r for r in rows}
    seen: set[int] = set()
    out: List[dict] = []
    for r in rows:
        ci = comp_of[r["id"]]
        if ci in seen:
            continue
        seen.add(ci)
        prow = dict(by_id[primary_of[ci]])
        prow["group_id"] = primary_of[ci]
        prow["edition_count"] = size_of[ci]
        out.append(prow)
    return out
