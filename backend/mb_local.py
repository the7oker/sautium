"""Local MusicBrainz resolver — the offline twin of ``musicbrainz.py``.

Backed by the ``mb_*`` dump tables (see ``mb_dump_load.py``), it exposes the
exact same surface as the API client (``search_artist`` /
``fetch_album_release_groups`` / ``cooldown_active`` / ``MBRateLimited``) so
``mb_audit`` and ``mb_canonicalize`` run against it unchanged — but with no
rate limit, no cooldown, no IP-ban risk. ``mb_backend`` picks this module when
the dump is loaded.

Matching is trigram-based against both ``mb_artist.name`` and
``mb_artist_alias.name`` (the alias table is what bridges diacritics /
transliterations / abbreviations to the canonical entity, since the project's
``normalize`` keeps diacritics). The score is rescaled to the API's 0..100
convention — an exact normalized-name match is forced to 100 — so the existing
confidence gates (``_SCORE_OK``) need no retuning.
"""

import logging
from typing import Dict, List

from db_pool import db_query
from uuid_utils import normalize

logger = logging.getLogger(__name__)

# MB artist.type id -> label (admin/sql/InsertDefaultRows.sql). Only the common
# ones matter to the canonicalization gates; unknown ids fall through to None.
_ARTIST_TYPE = {1: "Person", 2: "Group", 3: "Other", 4: "Character",
                5: "Orchestra", 6: "Choir"}


class MBRateLimited(Exception):
    """Interface parity with the API client — never raised locally."""


def cooldown_active() -> bool:
    return False


def search_artist(name: str, limit: int = 5) -> List[dict]:
    """Resolve a name to canonical MB artists via trigram match on names +
    aliases. Returns ``[{mbid, name, score, type, country, disambiguation},
    …]`` best-first, mirroring ``musicbrainz.search_artist``.

    The score is ``round(100 * best_trgm_similarity)`` over the artist's own
    name and all its aliases, forced to 100 on an exact normalized-name match.

    EXACT-FIRST: most owned names are an MB artist's exact name, so an indexed
    equality on ``lower(name)`` / alias (~1 ms) short-circuits the ~250-1400 ms
    trgm scan, which is kept only for genuinely dirty/variant names.
    """
    q = (name or "").strip()
    if not q:
        return []
    qn = q.lower()

    exact = db_query("""
        SELECT a.gid::text AS mbid, a.name, a.type AS type_id,
               a.comment AS disambiguation, ar.name AS country
        FROM mb_artist a
        LEFT JOIN mb_area ar ON ar.id = a.area
        WHERE a.id IN (
            SELECT id FROM mb_artist WHERE lower(name) = %(qn)s
            UNION
            SELECT artist FROM mb_artist_alias WHERE lower(name) = %(qn)s
        )
        ORDER BY a.id
        LIMIT %(lim)s
    """, {"qn": qn, "lim": int(limit)})
    if exact:
        return [{"mbid": r["mbid"], "name": r["name"], "score": 100,
                 "type": _ARTIST_TYPE.get(r["type_id"]), "country": r["country"],
                 "disambiguation": r["disambiguation"] or ""} for r in exact]

    # Fuzzy fallback: gin_trgm `%` operator (similarity_threshold, default 0.3)
    # so only plausibly-similar names/aliases are scanned; the canonical artist
    # is the join target either way (alias hits resolve to it).
    rows = db_query("""
        WITH hits AS (
            SELECT a.id, similarity(lower(a.name), %(qn)s) AS sim
            FROM mb_artist a
            WHERE lower(a.name) %% %(qn)s
            UNION ALL
            SELECT al.artist AS id, similarity(lower(al.name), %(qn)s) AS sim
            FROM mb_artist_alias al
            WHERE lower(al.name) %% %(qn)s
        )
        SELECT a.gid::text AS mbid, a.name, a.type AS type_id,
               a.comment AS disambiguation, ar.name AS country,
               MAX(h.sim) AS sim
        FROM hits h
        JOIN mb_artist a ON a.id = h.id
        LEFT JOIN mb_area ar ON ar.id = a.area
        GROUP BY a.id, a.gid, a.name, a.type, a.comment, ar.name
        ORDER BY MAX(h.sim) DESC, a.id
        LIMIT %(lim)s
    """, {"qn": qn, "lim": int(limit)})

    out = []
    for r in rows:
        exact = normalize(r["name"]) == normalize(q)
        out.append({
            "mbid": r["mbid"],
            "name": r["name"],
            "score": 100 if exact else round(100 * float(r["sim"])),
            "type": _ARTIST_TYPE.get(r["type_id"]),
            "country": r["country"],
            "disambiguation": r["disambiguation"] or "",
        })
    # An exact match can lose the raw-similarity sort to a longer fuzzy name;
    # re-rank so a forced-100 always leads.
    out.sort(key=lambda c: c["score"], reverse=True)
    return out


def artist_alt_names(name: str, limit: int = 3) -> List[str]:
    """Alternate names for a credited artist: the canonical MB name plus its
    aliases, via exact name/alias equality. Streaming catalogs (Deezer, YouTube
    "- Topic" channels) file a band under one canonical name even when a release
    is credited to a lineup/spelling variant (Contemporary Noise Quartet /
    Quintet / Sextet are aliases of Contemporary Noise Ensemble) — providers
    retry these when the credited name finds nothing. Empty when the name is
    ambiguous (several MB artists match): a namesake's alt names would let an
    obscure track resolve to the wrong artist's recording."""
    qn = (name or "").strip().lower()
    if not qn:
        return []
    rows = db_query("""
        WITH hit AS (
            SELECT id FROM mb_artist WHERE lower(name) = %(qn)s
            UNION
            SELECT artist FROM mb_artist_alias WHERE lower(name) = %(qn)s
        )
        SELECT a.name AS nm, al.name AS alias
        FROM mb_artist a
        LEFT JOIN mb_artist_alias al ON al.artist = a.id
        WHERE a.id IN (SELECT id FROM hit)
          AND (SELECT count(*) FROM hit) = 1
        ORDER BY al.id
    """, {"qn": qn})
    out: List[str] = []
    for cand in [r["nm"] for r in rows] + [r["alias"] for r in rows]:
        if cand and cand.lower() != qn and cand not in out:
            out.append(cand)
    return out[:limit]


def fetch_album_release_groups(mbid: str) -> List[dict]:
    """First-party release-groups for an artist MBID via local joins, the
    overlap anchor for canonicalization. Primary types *Album*, *EP* and
    *Single* — EP/Single are kept because a large share of (electronic /
    underground) artists have NO studio album in MB at all, only singles/EPs,
    yet a same-name namesake sharing the exact owned title is near-impossible,
    so they disambiguate just as well. Broadcast/Other/untyped are left out as
    noisier. Secondary-typed groups (Compilation/Live/Remix/…) are filtered by
    the caller via ``secondary_types``. ``first_year`` is the earliest date
    across the group's releases (``mb_release_country`` +
    ``mb_release_unknown_country``); None until a dump refresh loads those
    tables — overlap keys on title, not year, so canon is unaffected.

    ``release_titles`` is the extra local-only signal: every variant title of
    every release under the group. A dirty owned tag often matches a regional/
    alternate RELEASE title, not the RG's canonical name (e.g. owned
    "I Am What I Am" is a release of the RG "I Am Gloria Gaynor") — without this
    those albums never overlap-verify. The MB API can't supply it cheaply, so it
    returns an empty list there.
    """
    rows = db_query("""
        SELECT rg.gid::text AS rg_mbid, rg.name AS title, pt.name AS primary_type,
               array_remove(array_agg(DISTINCT st.name), NULL) AS secondary_types,
               array_remove(array_agg(DISTINCT r.name), NULL) AS release_titles,
               LEAST(MIN(rc.date_year), MIN(ruc.date_year)) AS first_year
        FROM mb_artist a
        JOIN mb_artist_credit_name acn ON acn.artist = a.id
        JOIN mb_release_group rg ON rg.artist_credit = acn.artist_credit
        JOIN mb_release_group_primary_type pt ON pt.id = rg.type
            AND pt.name IN ('Album', 'EP', 'Single')
        LEFT JOIN mb_release_group_secondary_type_join j ON j.release_group = rg.id
        LEFT JOIN mb_release_group_secondary_type st ON st.id = j.secondary_type
        LEFT JOIN mb_release r ON r.release_group = rg.id
        LEFT JOIN mb_release_country rc ON rc.release = r.id
        LEFT JOIN mb_release_unknown_country ruc ON ruc.release = r.id
        WHERE a.gid = %(mbid)s::uuid
        GROUP BY rg.id, rg.gid, rg.name, pt.name
    """, {"mbid": str(mbid)})
    return [{
        "rg_mbid": r["rg_mbid"],
        "title": r["title"] or "",
        "primary_type": r["primary_type"],
        "secondary_types": list(r["secondary_types"] or []),
        "release_titles": list(r["release_titles"] or []),
        "first_year": r["first_year"],
    } for r in rows]


def fetch_release_tracklists(rg_mbid: str) -> List[dict]:
    """Every release of a release-group with its full tracklist — the content
    fingerprint for release-ID (name-independent) + per-track recording MBID
    (track normalization). Tens of releases × ~10-15 tracks; one indexed join.

    Returns ``[{release_mbid, name, status, tracks: [{title, length_ms,
    recording_mbid, disc, position, credit, credit_artist_mbid,
    credit_names}, …]}, …]``, releases best-tracklist-first is the caller's
    job (``status`` 1 = Official — the canonical-release pick prefers it).
    ``name`` is the MB release title — the clean label an owned edition
    adopts once its tracklist matches this release. ``credit`` is the track's
    own artist credit as printed ("Daft Punk feat. Pharrell Williams" — join
    phrases included, the way a tag would carry it); ``credit_artist_mbid``
    is that artist's gid when the credit names exactly one artist, else
    None. ``credit_names`` is the credit's STRUCTURE — MB keeps a credit as
    an ordered list of artists with join phrases and renders the string from
    it: ``[{name, gid, credited_as, join}, …]`` in credit order, ``name`` the
    artist's canonical MB name (``credited_as`` when the member's artist row
    is not in a partial slice). The first member is the recording's primary
    by the catalog's own account, whatever phrase follows it. Local-only
    (the MB API can't page tracklists of every release cheaply).
    """
    rows = db_query("""
        SELECT r.gid::text AS release_mbid, r.name AS release_name, r.status,
               t.name AS title, t.length AS length_ms,
               rec.gid::text AS recording_mbid, m.position AS disc, t.position AS pos,
               ac.name AS credit,
               CASE WHEN ac.artist_count = 1 THEN ca.gid::text END AS credit_artist_mbid,
               (SELECT json_agg(json_build_object(
                           'name', COALESCE(ma.name, acn2.name), 'gid', ma.gid::text,
                           'credited_as', acn2.name, 'join', acn2.join_phrase)
                       ORDER BY acn2.position)
                  FROM mb_artist_credit_name acn2
                  LEFT JOIN mb_artist ma ON ma.id = acn2.artist
                 WHERE acn2.artist_credit = t.artist_credit) AS credit_names
        FROM mb_release r
        JOIN mb_medium m ON m.release = r.id
        JOIN mb_track t ON t.medium = m.id
        JOIN mb_recording rec ON rec.id = t.recording
        JOIN mb_artist_credit ac ON ac.id = t.artist_credit
        LEFT JOIN mb_artist_credit_name acn
               ON acn.artist_credit = ac.id AND acn.position = 0 AND ac.artist_count = 1
        LEFT JOIN mb_artist ca ON ca.id = acn.artist
        WHERE r.release_group = (SELECT id FROM mb_release_group WHERE gid = %(rg)s::uuid)
        ORDER BY r.id, m.position, t.position
    """, {"rg": str(rg_mbid)})
    rels: Dict[str, dict] = {}
    for r in rows:
        rel = rels.setdefault(r["release_mbid"],
                              {"release_mbid": r["release_mbid"],
                               "name": r["release_name"] or "",
                               "status": r["status"], "tracks": []})
        rel["tracks"].append({
            "title": r["title"] or "",
            "length_ms": r["length_ms"],
            "recording_mbid": r["recording_mbid"],
            "disc": r["disc"],
            "position": r["pos"],
            "credit": r["credit"] or "",
            "credit_artist_mbid": r["credit_artist_mbid"],
            "credit_names": r["credit_names"] or [],
        })
    return list(rels.values())
