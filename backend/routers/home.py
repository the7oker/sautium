"""
Home screen endpoints.

Each Home section is its own endpoint so the frontend can render
on-readiness instead of waiting for the slowest block to load, and so
"New in my collection" can paginate independently via infinite scroll.

Favourite artists rank by total listening time (not play count): a
single 90-minute ambient track should outweigh ten 5-minute pop plays.
Artists of the curated seed picks (seed_picks) trail the listened ones
so a fresh install is never an empty shelf.

Recommendations are CLAP-similarity-driven from recent listening
(see get_recommendations for the full pipeline), folding to owned AND
phantom albums — a node can live entirely on streamed phantoms. Cold
start (no listening_history) leads with the seed-pick rotation, then
newest-by-file_modified_at.
"""

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from db_pool import db_query, db_query_one, db_query_with_ef_search

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/home", tags=["home"])


# ─── Recommendation tuning ────────────────────────────────────────────────
# Recency decay τ: weight(t) = exp(-hours_since_play / τ).
# 168h → today=1.0, week-ago=0.37, two-weeks=0.14. Matches "session every
# week or two" listening rhythm; raise to 336 for slower drift, lower to 72
# to make recommendations track today's mood more aggressively.
RECENCY_TAU_HOURS = 168
# Hard cap on listening_history events that may seed the centroid.
# A play from 4 months ago has tiny exp-weight anyway, but the cap keeps
# the seed-pool query bounded and skips cold-cache reads.
SEED_WINDOW_DAYS = 60
# Seed selection: top release groups by recency-weighted listening — one
# slot per record (see the query), each standing in the kNN as its heaviest
# embedded track — then a diversity prune: a seed is dropped when it sits
# within SEED_DIVERSITY_CEIL cosine of a heavier one, which is also how one
# work under two release groups (a coupling, a compilation) folds to one
# taste. What survives (~5-8 seeds) spans the week's distinct records.
# No centroid: averaging the seeds (means of means) drifts into the manifold
# centre and recommends the grey middle of the library; each taste gets its
# own kNN instead and albums merge across tastes.
SEED_CANDIDATES = 16
SEED_KEPT = 8
SEED_DIVERSITY_CEIL = 0.95
# Per-seed HNSW pull. 8 seeds x 150 ~ 1200 hit rows before album folding.
KNN_PER_SEED = 150
# Session-local hnsw.ef_search override for the kNN LATERALs (pgvector's
# default 40 is "single nearest match" tuning); iterative_scan keeps the
# graph walk going when the self-exclusion filter eats into the first wave.
HNSW_EF_SEARCH = 500
# Tier 1 ("forgotten") threshold: albums whose last play was longer than
# this ago — eligible to resurface once tier 0 (never played) is exhausted.
FORGOTTEN_THRESHOLD_DAYS = 90
# Seed-pick rotation quotas per curation tier (list A bridge gems / list B
# palette / honourable mentions / rotation pool) out of the 20-slot shelf:
# the shelf leads with the bridges yet every tier surfaces daily. Once a
# tier's unplayed pool runs dry the overflow refills from the rest; a
# played pick leaves the rotation for good and competes only organically.
SEED_TIER_QUOTAS = {1: 12, 2: 5, 3: 2, 4: 1}


# Subqueries shared by the album shelves to fetch the album-tile-row
# contract: {artist, cover_url, cover_id, media_file_id}. A phantom album
# has no files, so the artist falls back to the album credit and its art
# is the CAA cover_url (the frontend's coverUrl() prefers it).
_ALBUM_TILE_SUBQUERIES = """
    COALESCE(
        (SELECT a.name
         FROM artists a
         JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
         JOIN tracks t ON t.id = ta.track_id
         JOIN media_files mf2 ON mf2.track_id = t.id
         JOIN album_variants av2 ON av2.id = mf2.album_variant_id
         WHERE av2.album_id = al.id
         GROUP BY a.id, a.name
         ORDER BY COUNT(*) DESC
         LIMIT 1),
        (SELECT a.name
         FROM artists a
         JOIN album_artists aa ON aa.artist_id = a.id AND aa.role = 'primary'
         WHERE aa.album_id = al.id
         ORDER BY a.name
         LIMIT 1)
    ) AS artist,
    al.cover_url,
    (SELECT mf3.cover_id::text
     FROM media_files mf3
     JOIN album_variants av3 ON av3.id = mf3.album_variant_id
     WHERE av3.album_id = al.id AND mf3.cover_id IS NOT NULL
     LIMIT 1) AS cover_id,
    (SELECT mf4.id
     FROM media_files mf4
     JOIN album_variants av4 ON av4.id = mf4.album_variant_id
     WHERE av4.album_id = al.id
     ORDER BY mf4.disc_number, mf4.track_number
     LIMIT 1) AS media_file_id
"""


@router.get("/favourite-artists")
def get_favourite_artists(
    limit: int = Query(100, ge=1, le=200),
) -> dict[str, list[dict[str, Any]]]:
    """Top primary artists by listening time, then unlistened seed-pick
    artists at the tail."""

    # Count time only against the primary artist of each track — featured /
    # composer / conductor rows in track_artists would otherwise hoist
    # soundtrack composers into Favourites for any film-score listener, but
    # the artist page lists albums only where they are primary, so the tile
    # would lead to an empty detail screen. Tie-break by play_count so two
    # artists with identical (rare) total_seconds order stably.
    #
    # Bucket 1 is the curated seed layer's artists with no listens yet —
    # never hidden by the zero-plays gate, ordered by curation tier/rank.
    # One completed listen promotes an artist into bucket 0 naturally
    # (local_play_stats keys on track_id, so streamed phantom plays count);
    # past the limit on a mature node the tail falls off by ranking, which
    # is the designed behavior, not hiding. is_owned is resolved only for
    # the emitted rows and drives the phantom tile styling.
    artists = db_query("""
        WITH listened AS (
            SELECT a.id, a.name,
                   SUM(lps.play_count)::int AS play_count,
                   FLOOR(SUM(lps.total_listen_time))::bigint AS total_seconds
            FROM artists a
            JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
            JOIN local_play_stats lps ON lps.track_id = ta.track_id
            GROUP BY a.id, a.name
            HAVING SUM(lps.total_listen_time) > 0
        ),
        seed_tail AS (
            SELECT DISTINCT ON (a.id) a.id, a.name, sp.tier, sp.rank
            FROM seed_picks sp
            JOIN album_artists aa ON aa.album_id = sp.album_id AND aa.role = 'primary'
            JOIN artists a ON a.id = aa.artist_id
            WHERE NOT EXISTS (SELECT 1 FROM listened l WHERE l.id = a.id)
            ORDER BY a.id, sp.tier, sp.rank
        )
        SELECT u.id::text AS id, u.name, u.play_count, u.total_seconds,
               EXISTS (SELECT 1 FROM track_artists ta2
                       JOIN media_files mf ON mf.track_id = ta2.track_id
                       WHERE ta2.artist_id = u.id) AS is_owned
        FROM (
            SELECT id, name, play_count, total_seconds,
                   0 AS bucket, 0 AS tier, 0 AS rank
            FROM listened
            UNION ALL
            SELECT id, name, 0, 0, 1, tier, rank
            FROM seed_tail
        ) u
        ORDER BY u.bucket, u.total_seconds DESC, u.play_count DESC, u.tier, u.rank
        LIMIT %(limit)s
    """, {"limit": limit})

    return {"artists": artists}


@router.get("/new-in-library")
def get_new_in_library(
    limit: int = Query(20, ge=1, le=50),
    before: str | None = None,
    before_id: str | None = None,
) -> dict[str, Any]:
    """Albums by recency of latest file mtime, cursor-paginated."""

    if (before is None) != (before_id is None):
        raise HTTPException(
            status_code=400,
            detail="before and before_id must be provided together",
        )

    cursor_ts: datetime | None = None
    if before is not None:
        try:
            cursor_ts = datetime.fromisoformat(before)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"invalid before timestamp: {exc}"
            )

    # album_variants.file_modified_at is denormalised from media_files via
    # FOR EACH STATEMENT triggers, so GROUP BY here scans ~3.7k variants
    # against an index on (file_modified_at DESC) instead of 30k+ media_files
    # rows. Cursor comparison uses (newest_added, album_id) as a composite
    # key so ties on identical timestamps still paginate without dupes.
    rows = db_query("""
        WITH album_keys AS (
            SELECT al.id AS album_id,
                   al.title,
                   al.release_year AS year,
                   MAX(av.file_modified_at) AS newest_added
            FROM albums al
            JOIN album_variants av ON av.album_id = al.id
            GROUP BY al.id, al.title, al.release_year
        )
        SELECT ak.album_id::text AS id,
               ak.title,
               ak.year,
               ak.newest_added,
               ak.album_id AS _album_uuid,
               (SELECT a.name
                FROM artists a
                JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
                JOIN tracks t ON t.id = ta.track_id
                JOIN media_files mf2 ON mf2.track_id = t.id
                JOIN album_variants av2 ON av2.id = mf2.album_variant_id
                WHERE av2.album_id = ak.album_id
                GROUP BY a.id, a.name
                ORDER BY COUNT(*) DESC
                LIMIT 1) AS artist,
               (SELECT mf3.cover_id::text
                FROM media_files mf3
                JOIN album_variants av3 ON av3.id = mf3.album_variant_id
                WHERE av3.album_id = ak.album_id AND mf3.cover_id IS NOT NULL
                LIMIT 1) AS cover_id,
               (SELECT mf4.id
                FROM media_files mf4
                JOIN album_variants av4 ON av4.id = mf4.album_variant_id
                WHERE av4.album_id = ak.album_id
                ORDER BY mf4.disc_number, mf4.track_number
                LIMIT 1) AS media_file_id
        FROM album_keys ak
        WHERE %(before)s::timestamptz IS NULL
           OR (ak.newest_added, ak.album_id) < (%(before)s::timestamptz, %(before_id)s::uuid)
        ORDER BY ak.newest_added DESC NULLS LAST, ak.album_id DESC
        LIMIT %(limit)s
    """, {
        "limit": limit,
        "before": cursor_ts,
        "before_id": before_id,
    })

    next_cursor: dict[str, str] | None = None
    if len(rows) == limit:
        last = rows[-1]
        next_cursor = {
            "before": last["newest_added"].isoformat(),
            "before_id": str(last["_album_uuid"]),
        }

    albums = [
        {k: v for k, v in row.items() if k not in {"newest_added", "_album_uuid"}}
        for row in rows
    ]

    return {"albums": albums, "next_cursor": next_cursor}


@router.get("/recommendations")
def get_recommendations(
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, list[dict[str, Any]]]:
    """
    CLAP multi-seed recommendations from recent listening — one SQL pass.

    Pipeline (all in the query, no Python vector math):
      1. Seed candidates: recent completed listens within SEED_WINDOW_DAYS,
         weight = duration x exp(-hours_since_play / RECENCY_TAU_HOURS),
         summed per RELEASE GROUP — one seed slot per record, its heaviest
         embedded track as the vector.
      2. Diversity prune: drop a seed within SEED_DIVERSITY_CEIL cosine of a
         heavier one — the survivors span the week's distinct tastes.
      3. Per-seed HNSW kNN (parameterized LATERAL) — NO centroid: averaging
         the seeds lands between tastes and recommends neither.
      4. Album fold: score = SUM(seed_weight x sim) / (hits + 3) — an album
         similar to SEVERAL of the week's tastes outranks a one-hit match;
         the +3 is the same Bayesian shrink the search engine's roll-ups use.
         Folds to owned albums (via media_files) AND phantom albums (via
         album_tracks) — a node can live entirely on streamed phantoms.
      5. Two-tier ordering — never-played (tier 0) before forgotten albums
         whose last play is older than FORGOTTEN_THRESHOLD_DAYS (tier 1) —
         then one edition per release group and one record per credited
         artist.
      6. Shortfall fill: unplayed seed picks first, then random owned
         albums; cold start (no completed listens in the window) →
         seed-pick rotation, then newest-by-file_modified_at.
    """
    has_seeds = db_query_one(f"""
        SELECT 1 AS x FROM listening_history lh
        JOIN embeddings e ON e.track_id = lh.track_id
        WHERE lh.started_at >= NOW() - INTERVAL '{SEED_WINDOW_DAYS} days'
          AND lh.completed
        LIMIT 1
    """)
    if not has_seeds:
        albums = _seed_fill(limit, exclude=set())
        if len(albums) < limit:
            albums.extend(_cold_start_albums(
                limit - len(albums), exclude={a["id"] for a in albums}))
        return {"albums": albums}

    albums = db_query_with_ef_search(
        f"""
        WITH played AS (
            SELECT lh.track_id,
                   SUM(
                       lh.duration_listened *
                       EXP(-EXTRACT(EPOCH FROM (NOW() - lh.started_at))
                           / %(tau_sec)s)
                   ) AS weight
            FROM listening_history lh
            WHERE lh.started_at >= NOW() - INTERVAL '{SEED_WINDOW_DAYS} days'
              AND lh.completed
            GROUP BY lh.track_id
        ),
        -- A taste is a listening EVENT, and for a collector that is the
        -- record: seed slots are allotted per release group, never per
        -- track. Per-track slots let the weight's unit pick the seeds — by
        -- seconds a 22-minute post-rock track outweighs a whole streamed
        -- album of four-minute songs, by plays one album streamed front to
        -- back fills half the slots — and the shelf froze on the week's few
        -- heaviest tracks. Membership is track identity (files ∪ tracklist),
        -- the rule album_state uses; editions fold on the release group
        -- like the shelf's own dedup; a played track on no album at all (a
        -- phantom without a tracklist) is its own group, so every completed
        -- play stays eligible.
        played_groups AS (
            SELECT DISTINCT p.track_id, p.weight,
                   COALESCE(al.musicbrainz_id::text, al.id::text,
                            p.track_id::text) AS rg,
                   EXISTS (SELECT 1 FROM embeddings e
                           WHERE e.track_id = p.track_id) AS embedded
            FROM played p
            LEFT JOIN LATERAL (
                SELECT av.album_id
                FROM media_files mf
                JOIN album_variants av ON av.id = mf.album_variant_id
                WHERE mf.track_id = p.track_id
                UNION
                SELECT at2.album_id
                FROM album_tracks at2
                WHERE at2.track_id = p.track_id
            ) x ON true
            LEFT JOIN albums al ON al.id = x.album_id
        ),
        raw_seeds AS (
            SELECT rg, SUM(weight) AS weight
            FROM played_groups
            GROUP BY rg
            HAVING bool_or(embedded)
            ORDER BY weight DESC
            LIMIT {SEED_CANDIDATES}
        ),
        -- The group's heaviest embedded track stands for it in the kNN: a
        -- real point on the manifold (no within-record centroid — a mean of
        -- the v2 means drifts to the centre of the space) and the track the
        -- listener dwelt on most. The weight is the whole group's.
        cand_seeds AS (
            SELECT rep.track_id, rep.weight, e.vector,
                   ROW_NUMBER() OVER (ORDER BY rep.weight DESC) AS rk
            FROM (
                SELECT DISTINCT ON (pg.rg) pg.track_id, rs.weight
                FROM raw_seeds rs
                JOIN played_groups pg ON pg.rg = rs.rg AND pg.embedded
                ORDER BY pg.rg, pg.weight DESC
            ) rep
            JOIN embeddings e ON e.track_id = rep.track_id
        ),
        seeds AS (
            -- Diversity prune against every HEAVIER candidate (not only kept
            -- ones): a chain of near-dupes over-prunes slightly, which is fine.
            SELECT cs.track_id, cs.vector,
                   cs.weight / SUM(cs.weight) OVER () AS w
            FROM cand_seeds cs
            WHERE NOT EXISTS (
                SELECT 1 FROM cand_seeds h
                WHERE h.rk < cs.rk
                  AND 1 - (h.vector <=> cs.vector) > {SEED_DIVERSITY_CEIL}
            )
            ORDER BY cs.rk
            LIMIT {SEED_KEPT}
        ),
        hits AS (
            SELECT s.w, nn.track_id, nn.sim
            FROM seeds s
            CROSS JOIN LATERAL (
                SELECT e.track_id, 1 - (e.vector <=> s.vector) AS sim
                FROM embeddings e
                WHERE e.track_id <> s.track_id
                ORDER BY e.vector <=> s.vector
                LIMIT {KNN_PER_SEED}
            ) nn
        ),
        candidate_albums AS (
            -- DISTINCT folds multi-edition variants of one album; the same
            -- track hit from TWO seeds keeps two rows — cross-taste evidence.
            -- Two fold arms: owned albums via media_files, phantom albums via
            -- album_tracks. Both are indexed joins driven by the ≤1200 hit
            -- rows — the phantom layer's size never enters the cost. UNION
            -- also dedups an owned album that carries a canonized tracklist.
            SELECT DISTINCT av.album_id, h.w, h.track_id, h.sim
            FROM hits h
            JOIN media_files mf ON mf.track_id = h.track_id
            JOIN album_variants av ON av.id = mf.album_variant_id
            UNION
            SELECT at2.album_id, h.w, h.track_id, h.sim
            FROM hits h
            JOIN album_tracks at2 ON at2.track_id = h.track_id
        ),
        -- Tier state derives from the SAME source as the seed
        -- (listening_history, completed plays) so an album that seeds the
        -- shelf also counts as "played" and leaves it. Reading play state
        -- from local_play_stats here would split the source of truth.
        -- Membership is judged by TRACK identity (files ∪ tracklist):
        -- phantom plays land with media_file_id NULL and would otherwise
        -- never mark their album as played. Driven FROM the played tracks —
        -- the small side — mapped to albums by indexed lateral probes;
        -- driving from candidate albums instead re-scanned listening_history
        -- once per album (measured 1.3s vs 19ms on the reference library).
        album_state AS (
            SELECT c.album_id, p.last_touch, COALESCE(p.total_plays, 0) AS total_plays
            FROM (SELECT DISTINCT album_id FROM candidate_albums) c
            LEFT JOIN (
                SELECT x.album_id, MAX(tp.last_touch) AS last_touch,
                       SUM(tp.plays) AS total_plays
                FROM (SELECT track_id, MAX(started_at) AS last_touch,
                             COUNT(*) AS plays
                      FROM listening_history WHERE completed
                      GROUP BY track_id) tp
                CROSS JOIN LATERAL (
                    SELECT av.album_id
                    FROM media_files mf
                    JOIN album_variants av ON av.id = mf.album_variant_id
                    WHERE mf.track_id = tp.track_id
                    UNION
                    SELECT at2.album_id
                    FROM album_tracks at2
                    WHERE at2.track_id = tp.track_id
                ) x
                GROUP BY x.album_id
            ) p ON p.album_id = c.album_id
        ),
        scored AS (
            SELECT ca.album_id,
                   SUM(ca.w * ca.sim) / (COUNT(*) + 3.0) AS score,
                   CASE
                       WHEN als.total_plays = 0 THEN 0
                       WHEN als.last_touch < NOW() - INTERVAL '{FORGOTTEN_THRESHOLD_DAYS} days' THEN 1
                       ELSE 2
                   END AS tier
            FROM candidate_albums ca
            JOIN album_state als ON als.album_id = ca.album_id
            GROUP BY ca.album_id, als.total_plays, als.last_touch
        ),
        -- One edition per release group: with phantoms in the fold, a hit
        -- track can sit on several near-identical editions/compilations of
        -- one record.
        editions AS (
            SELECT DISTINCT ON (COALESCE(al2.musicbrainz_id::text, al2.id::text))
                   s.album_id, s.tier, s.score
            FROM scored s
            JOIN albums al2 ON al2.id = s.album_id
            WHERE s.tier <= 1
            ORDER BY COALESCE(al2.musicbrainz_id::text, al2.id::text),
                     s.tier ASC, s.score DESC
        ),
        -- One record per credited artist: a seed pulls a whole discography
        -- of one voice (four Godspeed records, three Kevin Kern) and a
        -- 20-tile shelf would spend itself on it. The key is the album's
        -- primary credit, name-ordered for a stable pick on multi-artist
        -- credits; a credit-less album keys on itself. Both dedups run on
        -- slim rows; the tile subqueries are evaluated only for the ≤limit
        -- emitted rows.
        shelf AS (
            SELECT DISTINCT ON (artist_key) album_id, tier, score
            FROM (
                SELECT ed.album_id, ed.tier, ed.score,
                       COALESCE((SELECT aa.artist_id
                                 FROM album_artists aa
                                 JOIN artists a ON a.id = aa.artist_id
                                 WHERE aa.album_id = ed.album_id
                                   AND aa.role = 'primary'
                                 ORDER BY a.name
                                 LIMIT 1), ed.album_id) AS artist_key
                FROM editions ed
            ) keyed
            ORDER BY artist_key, tier ASC, score DESC
        )
        SELECT al.id::text AS id,
               al.title,
               al.release_year AS year,
               {_ALBUM_TILE_SUBQUERIES}
        FROM shelf d
        JOIN albums al ON al.id = d.album_id
        ORDER BY d.tier ASC, d.score DESC
        LIMIT %(limit)s
        """,
        {"tau_sec": RECENCY_TAU_HOURS * 3600, "limit": limit},
        ef_search=HNSW_EF_SEARCH,
    )

    if len(albums) < limit:
        existing = {a["id"] for a in albums}
        albums.extend(_seed_fill(limit - len(albums), exclude=existing))
    if len(albums) < limit:
        existing = {a["id"] for a in albums}
        albums.extend(_random_fill(limit - len(albums), exclude=existing))

    return {"albums": albums}


@router.get("/listening-history")
def get_listening_history(
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, list[dict[str, Any]]]:
    """Archived listening sessions (queue-lifetime snapshots), newest first,
    with consecutive replays of one queue collapsed into a single card.

    Only completed sessions — the active queue lives in Now Playing / Queue,
    not here. The card (title, subtitle, cover) is denormalised onto the
    session row; the live facts are counted over the slots (a PK-prefix scan
    per card): album_count flags a snapshot that spans several albums, and
    the content key — the ordered track ids — is what makes two sessions
    "the same queue", so a run of consecutive ones collapses into its newest
    card, which carries the run's length as repeat_count. A session whose
    slots are gone (its tracks left the catalog) has nothing to open, so it
    stays off the shelf. The scan window is a multiple of the page, and a
    run is only ever as long as the window sees — the right cap for a shelf.
    """
    sessions = db_query("""
        WITH recent AS (
            SELECT ls.id, ls.title, ls.subtitle, ls.cover_id, ls.cover_url,
                   ls.origin, ls.track_count, ls.started_at, ls.ended_at,
                   (SELECT count(DISTINCT st.album_id) FROM session_tracks st
                    WHERE st.session_id = ls.id AND st.album_id IS NOT NULL) AS album_count,
                   (SELECT md5(string_agg(st.track_id::text, ',' ORDER BY st.position))
                    FROM session_tracks st WHERE st.session_id = ls.id) AS content_key
            FROM listening_sessions ls
            WHERE ls.ended_at IS NOT NULL
              AND EXISTS (SELECT 1 FROM session_tracks st WHERE st.session_id = ls.id)
            ORDER BY ls.ended_at DESC, ls.id
            LIMIT %(scan)s
        ),
        runs AS (
            SELECT r.*,
                   CASE WHEN content_key = LAG(content_key) OVER (ORDER BY ended_at DESC, id)
                        THEN 0 ELSE 1 END AS run_start
            FROM recent r
        ),
        numbered AS (
            SELECT r.*,
                   SUM(run_start) OVER (ORDER BY ended_at DESC, id
                                        ROWS UNBOUNDED PRECEDING) AS run_no
            FROM runs r
        ),
        counted AS (
            SELECT n.*, count(*) OVER (PARTITION BY run_no) AS repeat_count
            FROM numbered n
        )
        SELECT id::text AS id,
               title,
               subtitle,
               cover_id::text AS cover_id,
               cover_url,
               origin,
               track_count,
               started_at,
               album_count,
               repeat_count
        FROM counted
        WHERE run_start = 1
        ORDER BY ended_at DESC, id
        LIMIT %(limit)s
    """, {"limit": limit, "scan": limit * 5})

    return {"sessions": sessions}


@router.get("/listening-history/{session_id}")
def get_listening_session(session_id: str) -> dict[str, Any]:
    """Session header, the artists the snapshot spans, and its ordered slots
    for the session-detail view.

    The context is derived from the CONTENT, not the origin: `artists` is
    every primary artist in the snapshot by share (the screen links each),
    and every slot names the album it was queued from, which is what groups
    the tracklist and links a single-album session to its album. Per-slot
    duration and art prefer that album's listing over any other edition."""

    session = db_query_one("""
        SELECT id::text AS id,
               title,
               subtitle,
               cover_id::text AS cover_id,
               cover_url,
               origin,
               origin_album_id::text AS origin_album_id,
               track_count,
               started_at,
               ended_at
        FROM listening_sessions
        WHERE id = %(id)s::uuid AND ended_at IS NOT NULL
    """, {"id": session_id})
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    session["artists"] = db_query("""
        SELECT a.id::text AS id,
               a.name,
               count(*) AS track_count
        FROM session_tracks st
        JOIN track_artists ta ON ta.track_id = st.track_id AND ta.role = 'primary'
        JOIN artists a ON a.id = ta.artist_id
        WHERE st.session_id = %(id)s::uuid
        GROUP BY a.id, a.name
        ORDER BY track_count DESC, min(st.position)
    """, {"id": session_id})

    session["tracks"] = db_query("""
        SELECT st.media_file_id,
               st.track_id::text AS track_id,
               st.album_id::text AS album_id,
               al.title AS album_title,
               al.release_year AS album_year,
               t.title,
               pa.id::text AS artist_id,
               pa.name AS artist,
               COALESCE(
                   mf.duration_seconds,
                   (SELECT atk.length_ms / 1000.0 FROM album_tracks atk
                    WHERE atk.track_id = t.id AND atk.length_ms IS NOT NULL
                    ORDER BY (atk.album_id = st.album_id) DESC NULLS LAST
                    LIMIT 1)
               )::float AS duration_seconds,
               mf.cover_id::text AS cover_id,
               (SELECT al2.cover_url FROM album_tracks atk
                JOIN albums al2 ON al2.id = atk.album_id
                WHERE atk.track_id = t.id AND al2.cover_url IS NOT NULL
                ORDER BY (al2.id = st.album_id) DESC NULLS LAST
                LIMIT 1) AS cover_url,
               (st.media_file_id IS NULL) AS is_phantom,
               af.key,
               af.mode,
               af.bpm::float AS bpm
        FROM session_tracks st
        JOIN tracks t ON t.id = st.track_id
        LEFT JOIN albums al ON al.id = st.album_id
        LEFT JOIN media_files mf ON mf.id = st.media_file_id
        LEFT JOIN LATERAL (
            SELECT a.id, a.name
            FROM track_artists ta
            JOIN artists a ON a.id = ta.artist_id
            WHERE ta.track_id = t.id AND ta.role = 'primary'
            ORDER BY a.name
            LIMIT 1
        ) pa ON TRUE
        LEFT JOIN audio_features af ON af.track_id = t.id
        WHERE st.session_id = %(id)s::uuid
        ORDER BY st.position
    """, {"id": session_id})

    session["total_duration"] = sum(
        t["duration_seconds"] or 0 for t in session["tracks"]
    )

    return session






def _seed_fill(needed: int, exclude: set[str]) -> list[dict[str, Any]]:
    """Curated-pick filler: unplayed seed albums in a deterministic daily
    rotation, tier-weighted by SEED_TIER_QUOTAS with overflow refill.

    md5(album_id || current_date) is the rotation primitive — stable within
    a day, different tomorrow (hashtext is internal API, md5 is documented).
    A pick with any completed listen (by track identity, so streamed
    phantom plays count) leaves the pool for good: from then on it competes
    only organically through the similarity fold."""

    if needed <= 0:
        return []

    quota_case = (f"CASE tier WHEN 1 THEN {SEED_TIER_QUOTAS[1]} "
                  f"WHEN 2 THEN {SEED_TIER_QUOTAS[2]} "
                  f"WHEN 3 THEN {SEED_TIER_QUOTAS[3]} "
                  f"ELSE {SEED_TIER_QUOTAS[4]} END")
    return db_query(
        f"""
        WITH pool AS (
            SELECT sp.album_id, sp.tier,
                   md5(sp.album_id::text || current_date::text) AS shuffle
            FROM seed_picks sp
            WHERE sp.album_id::text != ALL(%(exclude)s::text[])
              AND NOT EXISTS (
                  SELECT 1 FROM album_tracks at2
                  JOIN listening_history lh
                       ON lh.track_id = at2.track_id AND lh.completed
                  WHERE at2.album_id = sp.album_id)
        ),
        ranked AS (
            SELECT album_id, tier, shuffle,
                   ROW_NUMBER() OVER (PARTITION BY tier ORDER BY shuffle) AS rn,
                   {quota_case} AS cap
            FROM pool
        )
        SELECT al.id::text AS id,
               al.title,
               al.release_year AS year,
               {_ALBUM_TILE_SUBQUERIES}
        FROM ranked r
        JOIN albums al ON al.id = r.album_id
        ORDER BY (r.rn > r.cap), r.tier, r.shuffle
        LIMIT %(needed)s
        """,
        {"exclude": list(exclude), "needed": needed},
    )


def _random_fill(needed: int, exclude: set[str]) -> list[dict[str, Any]]:
    """Tier-2 fallback: random albums from the whole library, minus exclude."""

    if needed <= 0:
        return []

    return db_query(
        f"""
        SELECT al.id::text AS id,
               al.title,
               al.release_year AS year,
               {_ALBUM_TILE_SUBQUERIES}
        FROM albums al
        WHERE al.id::text != ALL(%(exclude)s::text[])
          -- owned only: phantom rows (MB missing-album discovery) have no
          -- variants/files and would fill the shelf with dead tiles
          AND EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = al.id)
        ORDER BY RANDOM()
        LIMIT %(needed)s
        """,
        {"exclude": list(exclude), "needed": needed},
    )


def _cold_start_albums(limit: int, exclude: set[str] = frozenset()) -> list[dict[str, Any]]:
    """No listening history yet — newest imports first, after the seed
    rotation (exclude covers owned pick albums already emitted by it)."""

    return db_query(
        f"""
        WITH album_keys AS (
            SELECT al.id AS album_id,
                   MAX(av.file_modified_at) AS newest_added
            FROM albums al
            JOIN album_variants av ON av.album_id = al.id
            WHERE al.id::text != ALL(%(exclude)s::text[])
            GROUP BY al.id
        )
        SELECT al.id::text AS id,
               al.title,
               al.release_year AS year,
               {_ALBUM_TILE_SUBQUERIES}
        FROM album_keys ak
        JOIN albums al ON al.id = ak.album_id
        ORDER BY ak.newest_added DESC NULLS LAST, RANDOM()
        LIMIT %(limit)s
        """,
        {"limit": limit, "exclude": list(exclude)},
    )
