"""Canonicalize phantom similar artists (out-of-catalog, 0 owned tracks).

Content-overlap canon (mb_normalize) can't reach them — there are no owned tracks
to verify against. Resolve by exact name against MusicBrainz instead:

  * 0 exact-name namesakes  -> leave it (not in MB by exact name; a tile, no shelf)
  * 1 namesake              -> take that MBID
  * >=2 namesakes           -> disambiguate deterministically: first by the
                              phantom's own linked album titles (an album a
                              phantom is credited on belongs to exactly one
                              namesake's discography), then by MB-genre overlap with the phantom's
                              library seeds (the artists it is "similar to").
                              A unique non-zero winner wins, otherwise leave it.

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

On a node whose MB rows came from other nodes' slices (canon/confirm.py) the
slice only nominates: every candidate is asked of MusicBrainz by MBID, the
choice is re-made on its answers (names, then the albums' and the genres'
channels on MusicBrainz's data), and nothing is discarded — a name the
answers could not settle proves nothing, and a deleted stub never comes back.
A stub kept unbound is taken again only when something could change its
answer (_STUB_DUE_SQL): last_mb_sync is the attempt's watermark.
"""
import logging
from collections import defaultdict
from typing import Optional, Set, Tuple

from db_pool import db_execute, db_query, get_conn

logger = logging.getLogger(__name__)

# When a stub left unbound is due another try: never tried, its MusicBrainz
# answers gone stale (mb_answers.FRESH), a slice for its name fetched since —
# a zero-match included — or a dump loaded since. Without it the kept stubs
# would head every batch for good and starve the rest.
_STUB_DUE_SQL = """(a.last_mb_sync IS NULL
                    OR a.last_mb_sync < now() - %(fresh)s
                    OR EXISTS (SELECT 1 FROM mb_slice_fetches f
                                WHERE f.name_key = lower(btrim(a.name))
                                  AND f.fetched_at > a.last_mb_sync)
                    OR a.last_mb_sync < (SELECT updated_at FROM user_settings
                                          WHERE key = 'musicbrainz.db_version'))"""


def canonize_phantom_similars(limit: Optional[int] = None, dry_run: bool = False,
                              artist_ids: Optional[list] = None) -> dict:
    """`artist_ids` narrows the pass to those stubs, due or not — the ones
    MusicBrainz's answers just woke; None sweeps every due stub."""
    import mb_answers
    from canon import confirm
    checked = confirm.required()
    stats = {"phantoms": 0, "not_in_mb": 0, "unambiguous": 0, "album_matched": 0,
             "disambiguated": 0, "inconclusive": 0, "canonized": 0, "discarded": 0}

    # Candidates: similar-artist stubs (track-less by construction) with no MB
    # anchor yet. Once resolved here, the regular stale_canonized_artists →
    # missing-albums pipeline fills the artist's MB discography. A stub that
    # waits for MusicBrainz's answers is taken again when they wake it.
    lim = "LIMIT %(lim)s" if limit else ""
    scope = "a.id::text = ANY(%(ids)s)" if artist_ids is not None else _STUB_DUE_SQL
    phantoms = db_query(f"""
        SELECT a.id::text AS id, a.name
        FROM artists a
        WHERE EXISTS (SELECT 1 FROM similar_artists sa WHERE sa.similar_artist_id = a.id)
          AND NOT EXISTS (SELECT 1 FROM track_artists ta WHERE ta.artist_id = a.id)
          AND NOT EXISTS (SELECT 1 FROM artist_mbids am WHERE am.artist_id = a.id)
          AND NOT {mb_answers.WAITING_SQL.format(subject='phantom', key='a.id::text')}
          AND {scope}
        ORDER BY a.last_mb_sync NULLS FIRST, a.name
        {lim}
    """, {"lim": limit, "fresh": mb_answers.FRESH,
          "ids": [str(i) for i in artist_ids or ()]})
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

    if checked:
        chosen, waiting = _confirmed_choices(
            ph_ids, ph_name, {i: name_gids.get(lname(i), []) for i in ph_ids}, stats,
            ask=not dry_run)
        return _bind_chosen(chosen, [i for i in ph_ids if i not in waiting], stats, dry_run,
                            discard=False)

    amb_ids = [i for i in ph_ids if len(name_gids.get(lname(i), [])) >= 2]

    # Channel 1 — linked-album titles: an album a phantom is credited on is
    # the strongest signal there is, its release_match_key against each
    # namesake's release-group/release titles picks the namesake whose
    # discography contains it. Only phantoms that HAVE linked albums pay for
    # the title fetch — a similar-artist stub has none, so today this channel
    # mostly idles; it stays for any phantom source that links albums.
    album_keys = defaultdict(set)           # ph_id -> linked-album match keys
    rg_keys = {}                            # gid -> namesake's release title keys
    if amb_ids:
        from discography import release_match_key
        from canon.match import release_title_keys
        for r in db_query("""
            SELECT aa.artist_id::text AS ph_id, al.title
            FROM album_artists aa
            JOIN albums al ON al.id = aa.album_id
            WHERE aa.artist_id = ANY(%(ids)s::uuid[])
        """, {"ids": amb_ids}):
            k = release_match_key(r["title"])
            if k:
                album_keys[r["ph_id"]].add(k)
        rg_keys = release_title_keys(list({g for i in amb_ids if album_keys.get(i)
                                           for g in name_gids[lname(i)]}))

    # Channel 2 — seed-genre overlap (similar-artist stubs).
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
        # Channels in strength order; each needs a unique non-zero winner,
        # a tie or all-zero falls through to the next.
        winner = None
        for channel, have, per_gid in (
                ("album_matched", album_keys.get(i, set()), rg_keys),
                ("disambiguated", seed_genres.get(i, set()), nm_genres)):
            if not have:
                continue
            scored = sorted(((len(have & per_gid.get(g, set())), g)
                             for g in gids), reverse=True)
            top = scored[0][0]
            if top == 0 or (len(scored) > 1 and scored[1][0] == top):
                continue
            winner = (channel, scored)
            break
        if winner is None:
            stats["inconclusive"] += 1
            continue
        channel, scored = winner
        chosen[i] = scored[0][1]
        stats[channel] += 1
        if dry_run and len(examples) < 15:
            examples.append((ph_name[i], len(gids), [s for s, _ in scored[:4]]))

    if dry_run:
        for nm, n, ov in examples:
            logger.info("disambiguated e.g. %s (%d namesakes) overlaps=%s", nm, n, ov)
    return _bind_chosen(chosen, ph_ids, stats, dry_run, discard=True)


def _bind_chosen(chosen: dict, ph_ids: list, stats: dict, dry_run: bool, discard: bool) -> dict:
    """Write the batch's choices and stamp the stubs decided (`ph_ids`: the
    batch less those still waiting for MusicBrainz). `discard`: the batch's
    failures go too — on a node with the dump. A node without it keeps them,
    unbound: a name that MusicBrainz's answers could not settle is no proof it
    names nothing (a zero-match cannot be refuted by an MBID), and a deleted
    stub never comes back — similars are fetched again only for artists that
    never had them."""
    if dry_run:
        stats["would_canonize"] = len(chosen)
        stats["would_discard"] = (stats["not_in_mb"] + stats["inconclusive"]) if discard else 0
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
            if rows:
                execute_values(cur,
                    "INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES %s "
                    "ON CONFLICT (mbid) DO NOTHING",
                    rows, template="(%s, %s, 'phantom')")
            # execute_values' rowcount only reflects the last page; rows is the
            # deduped attempt count (ON CONFLICT skips already-mapped MBIDs).
            stats["canonized"] = len(rows)
            if discard:
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
                """, (ph_ids,))
                stats["discarded"] = cur.rowcount
            cur.execute("UPDATE artists SET last_mb_sync = now() WHERE id = ANY(%s::uuid[])",
                        (ph_ids,))
    logger.info("phantom canon: %s", stats)
    return stats


def _seed_mbids(ph_ids: list) -> dict:
    """phantom id → the MBIDs of the library artists it is similar to (both
    stored edge directions)."""
    out = defaultdict(set)
    for r in db_query("""
        WITH ph AS (SELECT unnest(%(ids)s::uuid[]) AS id),
        edges AS (
            SELECT ph.id AS ph_id, sa.artist_id AS seed
            FROM ph JOIN similar_artists sa ON sa.similar_artist_id = ph.id
            UNION
            SELECT ph.id, sa.similar_artist_id
            FROM ph JOIN similar_artists sa ON sa.artist_id = ph.id
        )
        SELECT e.ph_id::text AS ph_id, am.mbid::text AS mbid
        FROM edges e JOIN artist_mbids am ON am.artist_id = e.seed
    """, {"ids": ph_ids}):
        out[r["ph_id"]].add(r["mbid"])
    return out


def _unique_winner(have: set, per_gid: dict, gids, channel: str):
    """(channel, gid) of the one candidate whose keys overlap `have` the most,
    or None for no overlap or a tie."""
    if not have:
        return None
    scored = sorted(((len(have & per_gid.get(g, set())), g) for g in gids), reverse=True)
    top = scored[0][0]
    if top == 0 or (len(scored) > 1 and scored[1][0] == top):
        return None
    return channel, scored[0][1]


def _confirmed_choices(ph_ids: list, ph_name: dict, candidates: dict, stats: dict,
                       ask: bool = True) -> Tuple[dict, Set[str]]:
    """The choice of each phantom's MBID re-made on MusicBrainz's answers: a
    slice's candidate counts only when MusicBrainz knows it and names the
    phantom by it (its merge target is what binds); one left is the choice;
    several are told apart on MusicBrainz's data — the phantom's linked albums
    against each one's groups, then the genres of the artists it is similar to
    against each one's. A phantom whose answers are not all here waits (and,
    unless `ask` is off — a dry run —, its answers are asked for). Returns
    the choices and the phantoms that wait."""
    import mb_answers
    from canon import confirm
    from discography import release_match_key
    chosen = {}
    waiting: Set[str] = set()
    seeds = None
    album_keys: dict = {}
    for r in db_query("""
        SELECT aa.artist_id::text AS ph_id, al.title
        FROM album_artists aa JOIN albums al ON al.id = aa.album_id
        WHERE aa.artist_id = ANY(%(ids)s::uuid[])
    """, {"ids": ph_ids}):
        k = release_match_key(r["title"])
        if k:
            album_keys.setdefault(r["ph_id"], set()).add(k)
    for i in ph_ids:
        gids = candidates.get(i) or []
        if not gids:
            stats["not_in_mb"] += 1
            continue
        asks = mb_answers.Asks("phantom", i, mb_answers.PRIORITY_DISCOVERY)
        known = {}
        for g in gids:
            a = confirm.artist(g, asks)
            if a is not confirm.WAIT and a.known and confirm.phantom_premise(ph_name[i], a.data):
                known[a.mbid] = a.data
        if asks.waiting:
            if ask:
                asks.flush()
            stats["waiting"] = stats.get("waiting", 0) + 1
            waiting.add(i)
            continue
        if not known:
            stats["not_in_mb"] += 1
            continue
        if len(known) == 1:
            chosen[i] = next(iter(known))
            stats["unambiguous"] += 1
            continue
        winner = None
        if album_keys.get(i):
            per = {}
            for g in known:
                groups = confirm.release_groups(g, asks)
                if groups is not confirm.WAIT:
                    per[g] = {release_match_key(x["title"]) for x in groups}
            if asks.waiting:
                if ask:
                    asks.flush()
                stats["waiting"] = stats.get("waiting", 0) + 1
                waiting.add(i)
                continue
            winner = _unique_winner(album_keys[i], per, known, "album_matched")
        if winner is None:
            if seeds is None:
                seeds = _seed_mbids(ph_ids)
            have = set()
            for m in seeds.get(i, ()):
                a = confirm.artist(m, asks)
                if a is not confirm.WAIT and a.known:
                    have |= set(a.data["genres"])
            if asks.waiting:
                if ask:
                    asks.flush()
                stats["waiting"] = stats.get("waiting", 0) + 1
                waiting.add(i)
                continue
            winner = _unique_winner(have, {g: set(d["genres"]) for g, d in known.items()},
                                    known, "disambiguated")
        if winner is None:
            stats["inconclusive"] += 1
            continue
        chosen[i] = winner[1]
        stats[winner[0]] += 1
    return chosen, waiting


def canonize_engaged_credit_heads(limit: Optional[int] = None) -> dict:
    """Anchor the credit heads of MB-minted tracklists once the listener has
    engaged with them.

    A phantom tracklist credits each slot to its MB artist-credit head; a head
    that is not the album artist gets a name-only artists row and, on purpose,
    no artist_mbids — an anchor puts the row into stale_canonized_artists'
    discography derive, and a compilation's fifteen credits times their
    discographies is the fan-out the engagement rule forbids
    (discography._persist_phantom_tracklist). The mint promised the anchor
    "the day the user engages": this is that day. Candidates are file-less,
    uncanonized artists with track credits and a completed, unskipped listen
    (ARTIST_ENGAGED — the similars backfill's gate, linear in listening).

    The MBID is not a name guess: the slot's recording_mbid names the MB
    recording, its artist credit names the head by MB artist id, and the
    credited name (or the artist's own) equal to the row's name IS the head
    — hence confidence 'name_exact' (MB-structural, not content-verified),
    never 'phantom'. A row whose credits resolve to two MB artists is a
    namesake collision the mint's name-keyed rows cannot express; it is left
    for the namesake split. Nothing is discarded: a head whose recordings
    are not in the local mb_* tables yet (a slice replica ahead of its slice)
    keeps its tracks and is retried in turn: every attempt stamps
    last_mb_sync (but one that waits for MusicBrainz's answers, on a node
    without the dump: those wake it) and the queue takes the never-tried, then
    the longest-waiting first — an imported listening history engages hundreds of heads at once,
    and unresolvable ones at the front of a name-ordered queue starved the
    rest."""
    import mb_answers
    from canon import confirm
    from sql_queries import ARTIST_ENGAGED
    checked = confirm.required()
    stats = {"candidates": 0, "canonized": 0, "ambiguous": 0, "unresolved": 0}
    lim = "LIMIT %(lim)s" if limit else ""
    # A head that waits for MusicBrainz's answers is taken again when they wake it.
    cand_sql = f"""
        WITH cand AS (
            SELECT a.id, a.name
            FROM artists a
            WHERE NOT EXISTS (SELECT 1 FROM artist_mbids am WHERE am.artist_id = a.id)
              AND EXISTS (SELECT 1 FROM track_artists ta WHERE ta.artist_id = a.id)
              AND NOT EXISTS (SELECT 1 FROM track_artists ta
                              JOIN media_files mf ON mf.track_id = ta.track_id
                              WHERE ta.artist_id = a.id)
              AND {ARTIST_ENGAGED}
              AND NOT {mb_answers.WAITING_SQL.format(subject='credit_head', key='a.id::text')}
            ORDER BY a.last_mb_sync NULLS FIRST, a.name
            {lim}
        )"""
    if checked:
        rows = db_query(cand_sql + """
            SELECT c.id::text AS artist_id, c.name,
                   array_remove(array_agg(DISTINCT at2.recording_mbid::text), NULL) AS recs
            FROM cand c
            LEFT JOIN track_artists ta ON ta.artist_id = c.id
            LEFT JOIN album_tracks at2 ON at2.track_id = ta.track_id
                                      AND at2.recording_mbid IS NOT NULL
            GROUP BY c.id, c.name
        """, {"lim": limit} if limit else {})
    else:
        rows = db_query(cand_sql + """
            SELECT c.id::text AS artist_id,
                   array_remove(array_agg(DISTINCT ma.gid::text), NULL) AS gids
            FROM cand c
            LEFT JOIN track_artists ta ON ta.artist_id = c.id
            LEFT JOIN album_tracks at2 ON at2.track_id = ta.track_id
                                      AND at2.recording_mbid IS NOT NULL
            LEFT JOIN mb_recording rec ON rec.gid = at2.recording_mbid
            LEFT JOIN mb_artist_credit_name acn ON acn.artist_credit = rec.artist_credit
            LEFT JOIN mb_artist ma ON ma.id = acn.artist
                                  AND (lower(btrim(acn.name)) = lower(btrim(c.name))
                                       OR lower(btrim(ma.name)) = lower(btrim(c.name)))
            GROUP BY c.id
        """, {"lim": limit} if limit else {})
    stats["candidates"] = len(rows)
    anchors = []
    waiting = set()
    for r in rows:
        if checked:
            gids = _credited_heads(r["artist_id"], r["name"], r["recs"] or [])
            if gids is confirm.WAIT:
                waiting.add(r["artist_id"])
                continue
        else:
            gids = r["gids"] or []
        if len(gids) == 1:
            anchors.append((gids[0], r["artist_id"]))
        elif gids:
            stats["ambiguous"] += 1
        else:
            stats["unresolved"] += 1
    if anchors:
        from psycopg2.extras import execute_values
        with get_conn() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                execute_values(cur,
                    "INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES %s "
                    "ON CONFLICT (mbid) DO NOTHING",
                    anchors, template="(%s::uuid, %s::uuid, 'name_exact')")
                cur.execute("""
                    SELECT COUNT(*) FROM artist_mbids
                    WHERE artist_id = ANY(%s::uuid[])""", ([a for _, a in anchors],))
                stats["canonized"] = cur.fetchone()[0]
    tried = [r["artist_id"] for r in rows if r["artist_id"] not in waiting]
    if tried:
        db_execute("UPDATE artists SET last_mb_sync = now() WHERE id = ANY(%(ids)s::uuid[])",
                   {"ids": tried})
    if waiting:
        stats["waiting"] = len(waiting)
    if stats["candidates"]:
        logger.info("credit-head canon: %s", stats)
    return stats


# Slot recordings asked per credit head at most.
_HEAD_RECORDINGS = 3


def _credited_heads(artist_id: str, name: str, recordings: list):
    """The MBIDs MusicBrainz's own credits of the head's slot recordings give
    the name — the credited name or the credited artist's name equal to it —
    asked a recording at a time until one answers; WAIT while none has."""
    import mb_answers
    from canon import confirm
    key = (name or "").strip().lower()
    asks = mb_answers.Asks("credit_head", artist_id, mb_answers.PRIORITY_LISTENING)
    for rec in sorted(recordings)[:_HEAD_RECORDINGS]:
        r = confirm.recording(rec, asks)
        if r is confirm.WAIT:
            asks.flush()
            return confirm.WAIT
        if not r.known:
            continue
        gids = sorted({c["artist"] for c in r.data["credit"]
                       if key in ((c["credited_as"] or "").strip().lower(),
                                  (c["name"] or "").strip().lower())})
        if gids:
            return gids
    return []
