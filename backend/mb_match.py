"""Fill ``artists.musicbrainz_id`` from the local MB dump, with a confidence level.

The overlap-verified canon (``mb_canonicalize``) only assigns an MBID when an
owned album overlap-confirms it — so artists with a sparse/empty MB discography
(e.g. a Bandcamp act MB knows only by name) stay NULL even when their identity
is obvious. This pass fills those, graded by how it matched:

  overlap_verified — already overlap-confirmed by the canon (strongest);
  name_exact       — normalized tag matches an mb_artist by NAME, unambiguous;
  alias_exact      — matches by ALIAS only (no name hit), unambiguous;
  lastfm           — Last.fm's id (= an MBID) as an UNVERIFIED hint, last resort.

The robustness anchor is **unambiguity across names AND aliases together**: the
normalized tag must resolve to exactly ONE distinct mb_artist over the union of
``mb_artist.name`` and ``mb_artist_alias.name``. This is what makes it safe —
e.g. "Helios" is the NAME of 14 entities yet a unique ALIAS of one ("Lizard"),
so an alias-only check would mis-assign it; requiring one distinct artist across
both correctly rejects it. ``via_name`` then grades the survivor name vs alias.

SET-BASED (all normalization pushed into SQL), idempotent, touches only NULL
rows. It records the MBID as a confidence-graded HINT — no rename/merge;
structural identity changes stay with the overlap-verified canon. Weaker tiers
(``name_fuzzy`` typo matching, lastfm-corroboration) are deferred until the
robust residue is measured.
"""

import logging

from db_pool import get_conn

logger = logging.getLogger(__name__)


# SQL-side twin of uuid_utils.normalize(): NFC + lower + trim + whitespace-collapse.
def _norm(col: str) -> str:
    return f"regexp_replace(normalize(lower(btrim({col})), NFC), '\\s+', ' ', 'g')"


# Every (normalized string -> artist) the dump knows, from names and aliases.
_NAME_UNION = f"""
    SELECT {_norm('name')} AS n, id AS aid, gid, true AS is_name FROM mb_artist
    UNION ALL
    SELECT {_norm('al.name')} AS n, al.artist AS aid, ar.gid, false AS is_name
    FROM mb_artist_alias al JOIN mb_artist ar ON ar.id = al.artist
"""

# Strings that resolve to exactly one distinct artist across names+aliases.
_UNAMBIGUOUS = f"""
    SELECT n, min(gid::text) AS gid, bool_or(is_name) AS via_name
    FROM ({_NAME_UNION}) x
    GROUP BY n HAVING count(DISTINCT aid) = 1
"""


def backfill_mb_match(dry_run: bool = False) -> dict:
    """Fill MBIDs + confidence. Returns ``{confidence/metric: count}``."""
    stats: dict = {}
    with get_conn() as conn:  # db_pool autocommit — each statement commits
        with conn.cursor() as cur:
            if not dry_run:
                cur.execute("""
                    UPDATE artists SET mb_match_confidence = 'overlap_verified'
                    WHERE musicbrainz_id IS NOT NULL AND mb_match_confidence IS NULL""")
                stats["overlap_verified"] = cur.rowcount

            if dry_run:
                cur.execute(f"""
                    WITH u AS ({_UNAMBIGUOUS})
                    SELECT count(*) FILTER (WHERE u.via_name)     AS name_exact,
                           count(*) FILTER (WHERE NOT u.via_name) AS alias_exact
                    FROM artists a JOIN u ON {_norm('a.name')} = u.n
                    WHERE a.musicbrainz_id IS NULL""")
                ne, ae = cur.fetchone()
                stats["name_exact"], stats["alias_exact"] = ne, ae
            else:
                cur.execute(f"""
                    WITH u AS ({_UNAMBIGUOUS})
                    UPDATE artists a
                    SET musicbrainz_id = CAST(u.gid AS uuid),
                        mb_match_confidence = CASE WHEN u.via_name
                            THEN 'name_exact'::mb_match_confidence
                            ELSE 'alias_exact'::mb_match_confidence END
                    FROM u
                    WHERE a.musicbrainz_id IS NULL AND {_norm('a.name')} = u.n""")
                stats["assigned"] = cur.rowcount

            # lastfm fallback: Last.fm's id IS an MBID, but it's an UNVERIFIED
            # popularity-disambiguated guess (~97%; picks the most-listened of N
            # namesakes — our exact tiers skip those as ambiguous). Store it as a
            # confidence-graded HINT only — NEVER drive rename/merge from it (that
            # is reserved for overlap-verified / user-confirmed). The local-MB
            # EXISTS check drops dead gids; in a no-local-MB deployment (the tier-3
            # fallback) that filter is simply absent and the gid is taken as-is.
            lastfm_sql = """
                FROM artists a WHERE a.musicbrainz_id IS NULL AND a.lastfm_id IS NOT NULL
                  AND EXISTS (SELECT 1 FROM mb_artist m WHERE m.gid = a.lastfm_id)"""
            if dry_run:
                cur.execute("SELECT count(*) " + lastfm_sql)
                stats["lastfm"] = cur.fetchone()[0]
            else:
                cur.execute(
                    "UPDATE artists a SET musicbrainz_id = a.lastfm_id, "
                    "mb_match_confidence = 'lastfm' "
                    "WHERE a.id IN (SELECT a.id " + lastfm_sql + ")")
                stats["lastfm"] = cur.rowcount

            cur.execute("SELECT count(*) FROM artists WHERE musicbrainz_id IS NULL")
            stats["still_null"] = cur.fetchone()[0]
    logger.info(f"mb_match backfill ({'dry-run' if dry_run else 'applied'}): {stats}")
    return stats


if __name__ == "__main__":
    import sys
    print(backfill_mb_match(dry_run="--dry-run" in sys.argv))
