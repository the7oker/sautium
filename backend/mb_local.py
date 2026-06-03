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
from typing import List

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
    """
    q = (name or "").strip()
    if not q:
        return []
    qn = q.lower()

    # Candidate gen leans on the gin_trgm `%` operator (similarity_threshold,
    # default 0.3) so only plausibly-similar names/aliases are scanned; the
    # canonical artist is the join target either way (alias hits resolve to it).
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


def fetch_album_release_groups(mbid: str) -> List[dict]:
    """First-party release-groups for an artist MBID via local joins, the
    overlap anchor for canonicalization. Primary types *Album*, *EP* and
    *Single* — EP/Single are kept because a large share of (electronic /
    underground) artists have NO studio album in MB at all, only singles/EPs,
    yet a same-name namesake sharing the exact owned title is near-impossible,
    so they disambiguate just as well. Broadcast/Other/untyped are left out as
    noisier. Secondary-typed groups (Compilation/Live/Remix/…) are filtered by
    the caller via ``secondary_types``. ``first_year`` is None — the dump's
    ``release_group_meta`` is not loaded and overlap keys on title, not year.

    ``release_titles`` is the extra local-only signal: every variant title of
    every release under the group. A dirty owned tag often matches a regional/
    alternate RELEASE title, not the RG's canonical name (e.g. owned
    "I Am What I Am" is a release of the RG "I Am Gloria Gaynor") — without this
    those albums never overlap-verify. The MB API can't supply it cheaply, so it
    returns an empty list there.
    """
    rows = db_query("""
        SELECT rg.name AS title, pt.name AS primary_type,
               array_remove(array_agg(DISTINCT st.name), NULL) AS secondary_types,
               array_remove(array_agg(DISTINCT r.name), NULL) AS release_titles
        FROM mb_artist a
        JOIN mb_artist_credit_name acn ON acn.artist = a.id
        JOIN mb_release_group rg ON rg.artist_credit = acn.artist_credit
        JOIN mb_release_group_primary_type pt ON pt.id = rg.type
            AND pt.name IN ('Album', 'EP', 'Single')
        LEFT JOIN mb_release_group_secondary_type_join j ON j.release_group = rg.id
        LEFT JOIN mb_release_group_secondary_type st ON st.id = j.secondary_type
        LEFT JOIN mb_release r ON r.release_group = rg.id
        WHERE a.gid = %(mbid)s::uuid
        GROUP BY rg.id, rg.name, pt.name
    """, {"mbid": str(mbid)})
    return [{
        "title": r["title"] or "",
        "primary_type": r["primary_type"],
        "secondary_types": list(r["secondary_types"] or []),
        "release_titles": list(r["release_titles"] or []),
        "first_year": None,
    } for r in rows]
