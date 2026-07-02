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

A phantom is either canonized or DISCARDED: a phantom that can't be canonized
(not in MB by name, or namesakes the genre couldn't disambiguate — collaborations
like 'John Coltrane & Johnny Hartman' fall here too, being no single MB entity)
carries no identity or discography, and every consumer (artist screen, missing-
albums recommender) already filters to canonized artists — so it is invisible
graph bloat. Each pass deletes the batch's failures (edges/members CASCADE); they
re-mint from Last.fm on the next similar-sync if still relevant, and re-attempt
then. Registered bands keep whole via the &↔and name-match and ARE canonized.
"""
import logging
from collections import defaultdict
from typing import Optional

from db_pool import db_query, get_conn

logger = logging.getLogger(__name__)


def canonize_phantom_similars(limit: Optional[int] = None, dry_run: bool = False) -> dict:
    stats = {"phantoms": 0, "not_in_mb": 0, "unambiguous": 0,
             "disambiguated": 0, "inconclusive": 0, "canonized": 0, "discarded": 0}

    # Candidates: classic similar-artist stubs (track-less by construction) PLUS
    # streaming-minted artists — a Deezer mint has tracks (its minted album) and
    # no similar_artists edge, so neither original condition admits it, leaving
    # it permanently uncanonized. Once resolved here, the regular
    # stale_canonized_artists → missing-albums pipeline fills its MB discography;
    # the MB phantom of an already-minted album lands on the SAME uuid5 row and
    # simply gains its rg MBID.
    lim = "LIMIT %(lim)s" if limit else ""
    phantoms = db_query(f"""
        SELECT a.id::text AS id, a.name
        FROM artists a
        WHERE ((EXISTS (SELECT 1 FROM similar_artists sa WHERE sa.similar_artist_id = a.id)
                AND NOT EXISTS (SELECT 1 FROM track_artists ta WHERE ta.artist_id = a.id))
               OR EXISTS (SELECT 1 FROM streaming_mints sm WHERE sm.artist_id = a.id))
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

    # candidate MB entities per phantom — exact name OR alias, separator-spelling
    # aware so '&'<->'and' band variants whole-match ('Jon & Vangelis' resolves to
    # the registered group 'Jon and Vangelis', not 0 namesakes). One batched
    # ANY-probe over every variant, mapped back to the phantom that produced it.
    from canon.match import _whole_variants
    variant_orig = defaultdict(set)        # variant_lower -> {phantom_lower_name}
    for i in ph_ids:
        if lname(i):
            for v in _whole_variants(ph_name[i]):
                variant_orig[v.strip().lower()].add(lname(i))
    # NAME-only (not alias): a compound's 'and'-form is often an alias/credit-
    # redirect on one member ('Vangelis and Irene Papas' aliases Vangelis), which
    # would collapse a real collaboration onto a member. Registered bands carry the
    # form as their NAME, so name-match is both the safe and the sufficient signal.
    gid_acc = defaultdict(set)              # phantom_lower_name -> {gid}
    if variant_orig:
        for r in db_query("SELECT lower(btrim(name)) AS ln, gid::text AS gid "
                          "FROM mb_artist WHERE lower(btrim(name)) = ANY(%(n)s)",
                          {"n": list(variant_orig)}):
            for orig in variant_orig.get(r["ln"], ()):
                gid_acc[orig].add(r["gid"])
    name_gids = {k: list(v) for k, v in gid_acc.items()}

    # Richer DETERMINISTIC resolution for the residue the batched name-match missed
    # — exact alias (non-compound), MB-punctuation respelling, diacritic/reorder/
    # prefix/mojibake probes, sort_name. Recovers ~30% of would-be not-in-MB
    # (Korean romanizations, O'Day↔O’Day, VC-118A↔VC‐118A) with no fuzzy risk, so
    # fewer real artists are wrongly discarded. Only the residue runs per-phantom;
    # the common case stays the single batched query above.
    from canon.match import phantom_candidate_gids
    for i in ph_ids:
        if not name_gids.get(lname(i)):
            g = phantom_candidate_gids(ph_name[i])
            if g:
                name_gids[lname(i)] = g

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
            examples.append((ph_name[i], len(gids), [s for s, _ in scored[:4]]))

    if dry_run:
        stats["would_canonize"] = len(chosen)
        stats["would_discard"] = stats["not_in_mb"] + stats["inconclusive"]
        for nm, n, ov in examples:
            logger.info("disambiguated e.g. %s (%d namesakes) overlaps=%s", nm, n, ov)
        return stats

    # mbid is the PK (one MB entity -> one artist); dedup so two phantom name
    # variants resolving to the same MBID don't collide in the batch. name + about
    # are denormalized by the fill_artist_mbid_meta trigger, not set here.
    seen, rows = set(), []
    for ph_id, gid in chosen.items():
        if gid in seen:
            continue
        seen.add(gid)
        rows.append((gid, ph_id))

    from psycopg2.extras import execute_values
    with get_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            execute_values(cur,
                "INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES %s "
                "ON CONFLICT (mbid) DO NOTHING",
                rows, template="(%s, %s, 'phantom')")
            # execute_values' rowcount only reflects the last page; rows is the
            # deduped attempt count (ON CONFLICT skips already-mapped MBIDs).
            stats["canonized"] = len(rows)
            # Discard the batch's failures: every phantom this pass could not give
            # an MBID (not in MB, or namesakes genre couldn't split — collabs land
            # here) is invisible bloat, since downstream filters to canonized
            # artists. Edges/members CASCADE; re-minted from Last.fm next sync if
            # still similar. The dedup conflict-losers above are swept here too.
            cur.execute("""
                DELETE FROM artists a
                WHERE a.id = ANY(%s::uuid[])
                  AND NOT EXISTS (SELECT 1 FROM artist_mbids am WHERE am.artist_id = a.id)
                  AND NOT EXISTS (SELECT 1 FROM track_artists ta WHERE ta.artist_id = a.id)
                  -- streaming-minted phantoms carry explicit user intent (a clicked
                  -- provider search tile) — never swept, MB-resolvable or not
                  AND NOT EXISTS (SELECT 1 FROM streaming_mints sm WHERE sm.artist_id = a.id)
            """, (ph_ids,))
            stats["discarded"] = cur.rowcount
    logger.info("phantom canon: %s", stats)
    return stats
