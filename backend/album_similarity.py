"""
Audio-similar albums via CLAP track embeddings.

Two-stage retrieval, cached in `similar_albums`:

  1. Candidate generation (recall): per-track CLAP KNN over the existing
     `embeddings` HNSW index, aggregated to albums by hit count.
  2. Rerank (precision): treat each album as a *set* of track vectors and
     score a candidate by the optimal one-to-one assignment (Hungarian) of its
     tracks to the source album's tracks. One-to-one matching makes the
     many-to-one "collapse" failure of mean/Chamfer scoring structurally
     impossible (a homogeneous album's tracks cannot all latch onto a single
     track of a diverse candidate).

Filled lazily on first view of an album page (read-through cache in
`routers/albums.py`) and rebuildable in bulk via the CLI. Track CLAP vectors
are L2-normalized at generation time, so `A @ B.T` is the cosine matrix.
"""

import logging

import numpy as np
import psycopg2.extras
from scipy.optimize import linear_sum_assignment

from db_pool import get_conn

logger = logging.getLogger(__name__)

SOURCE = "clap_assignment"

# Album → its track ids, covering BOTH owned albums (physical files via
# media_files/album_variants) AND phantom albums (the album_tracks tracklist, no
# files). Lets audio similarity work for previewed-and-enriched phantom albums
# too — a phantom source finds owned neighbours just like an owned source does.
_ALBUM_TRACKS_SQL = """
    SELECT av.album_id, mf.track_id
    FROM media_files mf
    JOIN album_variants av ON av.id = mf.album_variant_id
    UNION
    SELECT atr.album_id, atr.track_id
    FROM album_tracks atr
"""

# Candidate generation: PER_TRACK_K neighbours per source track, capped to
# TOP_N_CANDIDATES distinct albums by hit count before the assignment rerank.
# TOP_K neighbours are cached per album.
PER_TRACK_K = 30
TOP_N_CANDIDATES = 50
# Cache more than we display so the same-artist toggle and similarity floor
# stay free query-layer knobs (no recompute) even for prolific artists whose
# own albums fill the top of the list.
TOP_K = 40


def _parse_vec(text: str) -> np.ndarray:
    """psycopg2 returns pgvector columns as the literal text '[a,b,...]'."""
    return np.fromstring(text[1:-1], sep=",", dtype=np.float32)


def _load_album_vectors(cur, album_ids) -> dict[str, np.ndarray]:
    """{album_id: ndarray[m,512]} — one row per (album, track). A track has a
    single CLAP embedding, so DISTINCT ON dedupes multi-variant duplicates."""
    cur.execute(
        f"""
        SELECT DISTINCT ON (m.album_id, e.track_id)
               m.album_id::text AS album_id, e.vector AS vector
        FROM embeddings e
        JOIN ({_ALBUM_TRACKS_SQL}) m ON m.track_id = e.track_id
        WHERE m.album_id = ANY(%(ids)s::uuid[])
        ORDER BY m.album_id, e.track_id
        """,
        {"ids": [str(a) for a in album_ids]},
    )
    grouped: dict[str, list[np.ndarray]] = {}
    for row in cur.fetchall():
        grouped.setdefault(row["album_id"], []).append(_parse_vec(row["vector"]))
    return {aid: np.vstack(vecs) for aid, vecs in grouped.items()}


def _candidate_albums(cur, album_id) -> list[str]:
    """Per-track CLAP KNN → candidate albums ranked by hit count (self excluded).

    The LATERAL k-NN is a parameterized `ORDER BY vector <=> :v LIMIT k` per
    source track, which pgvector serves from the HNSW index on `embeddings`.
    """
    cur.execute(
        f"""
        WITH src AS (
            SELECT DISTINCT e.track_id, e.vector
            FROM embeddings e
            JOIN ({_ALBUM_TRACKS_SQL}) m ON m.track_id = e.track_id
            WHERE m.album_id = %(id)s::uuid
        )
        SELECT cm.album_id::text AS album_id, COUNT(*) AS hits
        FROM src
        CROSS JOIN LATERAL (
            SELECT e2.track_id
            FROM embeddings e2
            WHERE e2.track_id <> src.track_id
            ORDER BY e2.vector <=> src.vector
            LIMIT %(k)s
        ) nn
        JOIN ({_ALBUM_TRACKS_SQL}) cm ON cm.track_id = nn.track_id
        WHERE cm.album_id <> %(id)s::uuid
        GROUP BY cm.album_id
        ORDER BY hits DESC
        LIMIT %(n)s
        """,
        {"id": str(album_id), "k": PER_TRACK_K, "n": TOP_N_CANDIDATES},
    )
    return [r["album_id"] for r in cur.fetchall()]


def _assignment_score(a: np.ndarray, b: np.ndarray) -> float:
    """Mean cosine of the optimal one-to-one track pairing (assignment over
    min(m, n) pairs).

    Length-agnostic by design: similarity is how well an album's tracks pair
    with their best counterparts, *not* penalised for the other album having
    more tracks. Normalising by max(m, n) instead crushes catalogues like
    Klaus Schulze's, where albums range from 2 long pieces to many short ones —
    a 2-track album could never score above 2/6 against a 6-track sibling.
    """
    m = a @ b.T
    rows, cols = linear_sum_assignment(-m)
    score = float(m[rows, cols].mean())
    return max(0.0, min(1.0, score))


def compute_similar(album_id) -> list[dict]:
    """Top-K audio-similar albums for `album_id` (computed, not persisted).

    Returns ``[{"similar_album_id", "score"}]`` desc. Empty when the album has
    no CLAP embeddings yet (so the caller can retry once it's analysed).
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            source = _load_album_vectors(cur, [album_id]).get(str(album_id))
            if source is None:
                return []
            candidate_ids = _candidate_albums(cur, album_id)
            if not candidate_ids:
                return []
            candidate_vecs = _load_album_vectors(cur, candidate_ids)

    scored = [
        {"similar_album_id": cid, "score": _assignment_score(source, candidate_vecs[cid])}
        for cid in candidate_ids
        if cid in candidate_vecs
    ]
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:TOP_K]


def compute_and_cache(album_id) -> list[dict]:
    """Compute + persist top-K into `similar_albums`. Returns the stored rows.

    Concurrent first-views may compute twice; the upsert is idempotent.
    """
    rows = compute_similar(album_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM similar_albums WHERE album_id = %s::uuid AND source = %s",
                (str(album_id), SOURCE),
            )
            if rows:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO similar_albums "
                    "(album_id, similar_album_id, match_score, source) VALUES %s "
                    "ON CONFLICT (album_id, similar_album_id, source) "
                    "DO UPDATE SET match_score = EXCLUDED.match_score, updated_at = now()",
                    [
                        (str(album_id), r["similar_album_id"], round(r["score"], 4), SOURCE)
                        for r in rows
                    ],
                )
    return rows
