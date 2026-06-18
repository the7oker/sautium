"""Canonicalize phantom similar artists (out-of-catalog, 0 owned tracks).

Content-overlap canon (mb_normalize) can't reach them — there are no owned tracks
to verify against. Resolve by exact name against MusicBrainz instead:

  * 0 exact-name namesakes  -> leave it (not in MB by exact name; a tile, no shelf)
  * 1 namesake              -> take that MBID
  * >=2 namesakes           -> pick the one whose MB genres overlap the phantom's
                              library seeds (the artists it is "similar to"); a
                              unique non-zero winner wins, otherwise leave it.

Writes artist_mbids with confidence='phantom' (name+genre derived, NOT content-
verified -> re-verify before trusting over P2P) plus an `about` disambiguation
caption (mb_artist.comment, else "type · area · year"). Phantom-only: never
touches an owned artist's content-verified MBIDs. The existing missing-albums
pipeline (stale_canonized_artists) then derives discographies for the canonized.
"""
import logging
from collections import defaultdict
from typing import Optional

from db_pool import db_query, get_conn

logger = logging.getLogger(__name__)

_ARTIST_TYPE = {1: "Person", 2: "Group", 3: "Other", 4: "Character",
                5: "Orchestra", 6: "Choir"}


def _caption(comment, atype, area, year) -> Optional[str]:
    """Namesake label: MB disambiguation comment if present, else type/area/year."""
    if comment and comment.strip():
        return comment.strip()
    parts = [p for p in (_ARTIST_TYPE.get(atype), area, str(year) if year else None) if p]
    return " · ".join(parts) or None


def canonize_phantom_similars(limit: Optional[int] = None, dry_run: bool = False) -> dict:
    stats = {"phantoms": 0, "not_in_mb": 0, "unambiguous": 0,
             "disambiguated": 0, "inconclusive": 0, "canonized": 0}

    lim = "LIMIT %(lim)s" if limit else ""
    phantoms = db_query(f"""
        SELECT a.id::text AS id, a.name
        FROM artists a
        WHERE EXISTS (SELECT 1 FROM similar_artists sa WHERE sa.similar_artist_id = a.id)
          AND NOT EXISTS (SELECT 1 FROM track_artists ta WHERE ta.artist_id = a.id)
          AND NOT EXISTS (SELECT 1 FROM artist_mbids am WHERE am.artist_id = a.id)
        ORDER BY a.name
        {lim}
    """, {"lim": limit} if limit else {})
    stats["phantoms"] = len(phantoms)
    if not phantoms:
        return stats
    ph_name = {p["id"]: p["name"] for p in phantoms}
    ph_ids = list(ph_name)

    def lname(i):
        return (ph_name[i] or "").strip().lower()

    # exact-name namesakes
    names = list({lname(i) for i in ph_ids if lname(i)})
    name_gids = defaultdict(list)
    for r in db_query("SELECT lower(btrim(name)) AS ln, gid::text AS gid "
                      "FROM mb_artist WHERE lower(btrim(name)) = ANY(%(n)s)", {"n": names}):
        name_gids[r["ln"]].append(r["gid"])

    amb_ids = [i for i in ph_ids if len(name_gids.get(lname(i), [])) >= 2]

    seed_genres = defaultdict(set)
    nm_genres = defaultdict(set)
    if amb_ids:
        # genres of the canonized library artists each ambiguous phantom is
        # similar to (both stored edge directions), in the MB-genre vocabulary.
        for r in db_query("""
            WITH ph AS (SELECT unnest(%(ids)s::uuid[]) AS id),
            edges AS (
                SELECT ph.id AS ph_id, sa.artist_id AS seed
                FROM ph JOIN similar_artists sa ON sa.similar_artist_id = ph.id
                UNION
                SELECT ph.id, sa.similar_artist_id
                FROM ph JOIN similar_artists sa ON sa.artist_id = ph.id
            )
            SELECT e.ph_id::text AS ph_id, lower(t.name) AS genre
            FROM edges e
            JOIN artist_mbids am ON am.artist_id = e.seed
            JOIN mb_artist a ON a.gid = am.mbid
            JOIN mb_artist_tag at ON at.artist = a.id AND at.count > 0
            JOIN mb_tag t ON t.id = at.tag
            JOIN mb_genre g ON lower(g.name) = lower(t.name)
        """, {"ids": amb_ids}):
            seed_genres[r["ph_id"]].add(r["genre"])

        amb_gids = list({g for i in amb_ids for g in name_gids[lname(i)]})
        for r in db_query("""
            SELECT a.gid::text AS gid, lower(t.name) AS genre
            FROM mb_artist a
            JOIN mb_artist_tag at ON at.artist = a.id AND at.count > 0
            JOIN mb_tag t ON t.id = at.tag
            JOIN mb_genre g ON lower(g.name) = lower(t.name)
            WHERE a.gid = ANY(%(g)s::uuid[])
        """, {"g": amb_gids}):
            nm_genres[r["gid"]].add(r["genre"])

    chosen = {}        # ph_id -> gid
    examples = []
    for i in ph_ids:
        gids = name_gids.get(lname(i), [])
        if not gids:
            stats["not_in_mb"] += 1
            continue
        if len(gids) == 1:
            chosen[i] = gids[0]
            stats["unambiguous"] += 1
            continue
        sg = seed_genres.get(i, set())
        if not sg:
            stats["inconclusive"] += 1
            continue
        scored = sorted(((len(sg & nm_genres.get(g, set())), g) for g in gids), reverse=True)
        top = scored[0][0]
        if top == 0 or (len(scored) > 1 and scored[1][0] == top):
            stats["inconclusive"] += 1
            continue
        chosen[i] = scored[0][1]
        stats["disambiguated"] += 1
        if dry_run and len(examples) < 15:
            examples.append((ph_name[i], len(gids), [s for s, _ in scored[:4]], scored[0][1]))

    cgids = list(set(chosen.values()))
    cap = {}
    if cgids:
        for r in db_query("""
            SELECT a.gid::text AS gid, a.comment, a.type, ar.name AS area, a.begin_date_year AS yr
            FROM mb_artist a LEFT JOIN mb_area ar ON ar.id = a.area
            WHERE a.gid = ANY(%(g)s::uuid[])
        """, {"g": cgids}):
            cap[r["gid"]] = _caption(r["comment"], r["type"], r["area"], r["yr"])

    if dry_run:
        stats["would_canonize"] = len(chosen)
        for nm, n, ov, gid in examples:
            logger.info("disambiguated e.g. %s (%d namesakes) overlaps=%s -> %r",
                        nm, n, ov, cap.get(gid))
        return stats

    # mbid is the PK (one MB entity -> one artist); dedup so two phantom name
    # variants resolving to the same MBID don't collide in the batch.
    seen, rows = set(), []
    for ph_id, gid in chosen.items():
        if gid in seen:
            continue
        seen.add(gid)
        rows.append((gid, ph_id, cap.get(gid)))

    from psycopg2.extras import execute_values
    with get_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            execute_values(cur,
                "INSERT INTO artist_mbids (mbid, artist_id, confidence, about) VALUES %s "
                "ON CONFLICT (mbid) DO NOTHING",
                rows, template="(%s, %s, 'phantom', %s)")
            # execute_values' rowcount only reflects the last page; rows is the
            # deduped attempt count (ON CONFLICT skips already-mapped MBIDs).
            stats["canonized"] = len(rows)
    logger.info("phantom canon: %s", stats)
    return stats
