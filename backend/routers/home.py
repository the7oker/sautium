"""
Home screen endpoints.

Each Home section is its own endpoint so the frontend can render
on-readiness instead of waiting for the slowest block to load, and so
"New in library" can paginate independently via infinite scroll.

Favourite artists rank by total listening time (not play count): a
single 90-minute ambient track should outweigh ten 5-minute pop plays.

Recommendations are CLAP-similarity-driven from recent listening
(see get_recommendations for the full pipeline). Cold start
(no listening_history) falls back to newest-by-file_modified_at.
"""

import logging
from datetime import datetime
from typing import Any

import psycopg2.extras
from fastapi import APIRouter, HTTPException, Query

from db_pool import db_query, db_query_one, get_conn

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
# Top-N seed tracks by recency-weight. Beyond ~50 tracks every additional
# seed dilutes the centroid more than it adds signal.
SEED_TOP_N = 50
# HNSW kNN pull: how many nearest tracks to the centroid we examine.
# Stays in lockstep with HNSW_EF_SEARCH below — pgvector's index only
# returns up to ef_search candidates regardless of LIMIT, so a higher
# LIMIT without raising ef_search yields zero benefit. 500 over 35k
# embeddings hits ~80-150 unique albums, plenty for a 20-item row;
# going above 500 climbs into 200ms+ territory for marginal gains.
KNN_CANDIDATE_TRACKS = 500
# Session-local hnsw.ef_search override for the kNN query. pgvector's
# default (40) is calibrated for "find the single nearest match" use
# cases; for recommendation aggregation we need a wider net.
HNSW_EF_SEARCH = 500
# Per-album score = avg cosine over the top-K closest tracks of that
# album. Top-3 favours albums where multiple tracks (not just one outlier)
# match the seed direction.
ALBUM_SCORE_TOP_K = 3
# Tier 1 ("forgotten") threshold: albums whose last play was longer than
# this ago — eligible to resurface once tier 0 (never played) is exhausted.
FORGOTTEN_THRESHOLD_DAYS = 90


def _vec_literal(vec) -> str:
    """Format a Python iterable of floats as a pgvector text literal."""
    return "[" + ",".join(str(float(x)) for x in vec) + "]"


def _db_query_with_ef_search(sql: str, params: dict, ef_search: int) -> list[dict]:
    """
    Run a SELECT with hnsw.ef_search raised for the statement.

    db_pool's connections are autocommit; SET LOCAL only takes effect
    inside an explicit transaction, so we toggle autocommit off, set
    the GUC, run the query, commit, and restore the pooled connection
    to its expected autocommit state for the next caller.
    """
    with get_conn() as conn:
        conn.autocommit = False
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SET LOCAL hnsw.ef_search = %s", (ef_search,))
                cur.execute(sql, params)
                rows = [dict(r) for r in cur.fetchall()]
            conn.commit()
            return rows
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True


# Subqueries shared by new-in-library and recommendations to fetch the
# album-tile-row contract: {artist, cover_id, media_file_id}.
_ALBUM_TILE_SUBQUERIES = """
    (SELECT a.name
     FROM artists a
     JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
     JOIN tracks t ON t.id = ta.track_id
     JOIN media_files mf2 ON mf2.track_id = t.id
     JOIN album_variants av2 ON av2.id = mf2.album_variant_id
     WHERE av2.album_id = al.id
     GROUP BY a.id, a.name
     ORDER BY COUNT(*) DESC
     LIMIT 1) AS artist,
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
    """Top primary artists ranked by total listening time."""

    # Count time only against the primary artist of each track — featured /
    # composer / conductor rows in track_artists would otherwise hoist
    # soundtrack composers into Favourites for any film-score listener, but
    # the artist page lists albums only where they are primary, so the tile
    # would lead to an empty detail screen. Tie-break by play_count so two
    # artists with identical (rare) total_seconds order stably.
    artists = db_query("""
        SELECT a.id::text AS id,
               a.name,
               SUM(lps.play_count)::int AS play_count,
               FLOOR(SUM(lps.total_listen_time))::bigint AS total_seconds
        FROM artists a
        JOIN track_artists ta ON ta.artist_id = a.id AND ta.role = 'primary'
        JOIN local_play_stats lps ON lps.track_id = ta.track_id
        GROUP BY a.id, a.name
        HAVING SUM(lps.total_listen_time) > 0
        ORDER BY total_seconds DESC, play_count DESC
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
    CLAP-similarity recommendations from recent listening.

    Pipeline:
      1. Top-N seed tracks from listening_history within SEED_WINDOW_DAYS,
         weighted by exp(-hours_since_play / RECENCY_TAU_HOURS); only
         events flagged completed by the playback tracker count as seeds.
      2. Weighted centroid in Python (pgvector has no weighted-avg op).
      3. HNSW kNN: top KNN_CANDIDATE_TRACKS embeddings nearest to centroid.
      4. Album aggregation, score = avg cosine over top-K tracks per album.
      5. Two-tier ordering — never-played (tier 0) before forgotten albums
         whose last play is older than FORGOTTEN_THRESHOLD_DAYS (tier 1).
      6. Random fill from the whole library if tier 0+1 came up short
         (every album touched and not yet forgotten).
      7. Cold start (empty seed pool) → newest-by-file_modified_at, which
         on a fresh import degrades to random — acceptable until the first
         listen lands.
    """
    seeds = _fetch_seed_vectors()
    if not seeds:
        return {"albums": _cold_start_albums(limit)}

    centroid = _weighted_centroid(seeds)
    albums = _knn_recommend(centroid, limit)

    if len(albums) < limit:
        existing = {a["id"] for a in albums}
        albums.extend(_random_fill(limit - len(albums), exclude=existing))

    return {"albums": albums}


@router.get("/listening-history")
def get_listening_history(
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, list[dict[str, Any]]]:
    """Archived listening sessions (queue-lifetime snapshots), newest first.

    Only completed sessions — the active queue lives in Now Playing / Queue,
    not here. cover_id is denormalised onto the session row so the shelf
    renders without joining session_tracks.
    """
    sessions = db_query("""
        SELECT id::text AS id,
               title,
               subtitle,
               cover_id::text AS cover_id,
               origin,
               track_count
        FROM listening_sessions
        WHERE ended_at IS NOT NULL
        ORDER BY ended_at DESC
        LIMIT %(limit)s
    """, {"limit": limit})

    return {"sessions": sessions}


@router.get("/listening-history/{session_id}")
def get_listening_session(session_id: str) -> dict[str, Any]:
    """Session header + ordered track list for the session-detail view."""

    session = db_query_one("""
        SELECT id::text AS id,
               title,
               subtitle,
               cover_id::text AS cover_id,
               origin,
               track_count
        FROM listening_sessions
        WHERE id = %(id)s::uuid AND ended_at IS NOT NULL
    """, {"id": session_id})
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    session["tracks"] = db_query("""
        SELECT st.media_file_id,
               t.title,
               a.name AS artist,
               mf.duration_seconds::float AS duration_seconds,
               mf.cover_id::text AS cover_id,
               af.key,
               af.mode,
               af.bpm::float AS bpm
        FROM session_tracks st
        JOIN media_files mf ON mf.id = st.media_file_id
        JOIN tracks t ON t.id = mf.track_id
        LEFT JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
        LEFT JOIN artists a ON a.id = ta.artist_id
        LEFT JOIN audio_features af ON af.track_id = t.id
        WHERE st.session_id = %(id)s::uuid
        ORDER BY st.position
    """, {"id": session_id})

    session["total_duration"] = sum(
        t["duration_seconds"] or 0 for t in session["tracks"]
    )

    return session


def _fetch_seed_vectors() -> list[dict[str, Any]]:
    """Top-N recent completed tracks with their CLAP vectors and recency weight."""

    return db_query(
        f"""
        WITH recent AS (
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
        )
        SELECT r.track_id::text AS track_id,
               r.weight::float AS weight,
               e.vector::text  AS vector
        FROM recent r
        JOIN embeddings e ON e.track_id = r.track_id
        ORDER BY r.weight DESC
        LIMIT {SEED_TOP_N}
        """,
        {"tau_sec": RECENCY_TAU_HOURS * 3600},
    )


def _weighted_centroid(seeds: list[dict[str, Any]]) -> list[float]:
    """Weighted mean of 512-d CLAP vectors keyed by recency weight."""

    total_w = sum(s["weight"] for s in seeds)
    if total_w <= 0:
        return _parse_vector(seeds[0]["vector"])

    dim = 512
    accum = [0.0] * dim
    for s in seeds:
        w = s["weight"]
        v = _parse_vector(s["vector"])
        for i in range(dim):
            accum[i] += w * v[i]
    return [x / total_w for x in accum]


def _parse_vector(v_text: str) -> list[float]:
    """Parse a pgvector text literal '[1.0,2.0,...]' into a list of floats."""

    return [float(x) for x in v_text.strip("[]").split(",")]


def _knn_recommend(centroid: list[float], limit: int) -> list[dict[str, Any]]:
    """HNSW kNN + album aggregation + tiered ordering."""

    qvec = _vec_literal(centroid)

    rows = _db_query_with_ef_search(
        f"""
        WITH similar_tracks AS (
            SELECT e.track_id,
                   1 - (e.vector <=> %(qvec)s::vector) AS sim
            FROM embeddings e
            ORDER BY e.vector <=> %(qvec)s::vector
            LIMIT {KNN_CANDIDATE_TRACKS}
        ),
        candidate_albums AS (
            SELECT av.album_id,
                   st.sim,
                   ROW_NUMBER() OVER (
                       PARTITION BY av.album_id ORDER BY st.sim DESC
                   ) AS rn
            FROM similar_tracks st
            JOIN media_files mf ON mf.track_id = st.track_id
            JOIN album_variants av ON av.id = mf.album_variant_id
        ),
        album_state AS (
            SELECT av.album_id,
                   MAX(lps.last_played_at) AS last_touch,
                   COALESCE(SUM(lps.play_count), 0) AS total_plays
            FROM album_variants av
            JOIN media_files mf ON mf.album_variant_id = av.id
            LEFT JOIN local_play_stats lps ON lps.track_id = mf.track_id
            WHERE av.album_id IN (SELECT album_id FROM candidate_albums)
            GROUP BY av.album_id
        ),
        scored AS (
            SELECT ca.album_id,
                   AVG(ca.sim) AS score,
                   CASE
                       WHEN als.total_plays = 0 THEN 0
                       WHEN als.last_touch < NOW() - INTERVAL '{FORGOTTEN_THRESHOLD_DAYS} days' THEN 1
                       ELSE 2
                   END AS tier
            FROM candidate_albums ca
            JOIN album_state als ON als.album_id = ca.album_id
            WHERE ca.rn <= {ALBUM_SCORE_TOP_K}
            GROUP BY ca.album_id, als.total_plays, als.last_touch
        )
        SELECT al.id::text AS id,
               al.title,
               al.release_year AS year,
               {_ALBUM_TILE_SUBQUERIES}
        FROM scored s
        JOIN albums al ON al.id = s.album_id
        WHERE s.tier <= 1
        ORDER BY s.tier ASC, s.score DESC
        LIMIT %(limit)s
        """,
        {"qvec": qvec, "limit": limit},
        ef_search=HNSW_EF_SEARCH,
    )
    return rows


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
        ORDER BY RANDOM()
        LIMIT %(needed)s
        """,
        {"exclude": list(exclude), "needed": needed},
    )


def _cold_start_albums(limit: int) -> list[dict[str, Any]]:
    """No listening history yet — newest imports first."""

    return db_query(
        f"""
        WITH album_keys AS (
            SELECT al.id AS album_id,
                   MAX(av.file_modified_at) AS newest_added
            FROM albums al
            JOIN album_variants av ON av.album_id = al.id
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
        {"limit": limit},
    )
