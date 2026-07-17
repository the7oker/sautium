"""
Audio-similar tracks via CLAP segment embeddings — the shared two-tier scorer
behind radio batches and the Now Playing "Similar" shelf.

Two-stage, one SQL statement:

  1. Recall: the POOL nearest track means (HNSW). Mean↔mean cosine is
     CONCENTRATED post-mean-flip — an entire radio batch fits in dist
     0.042–0.070 while the intra-track segment spread is ~0.19 — so mean
     ordering is near-noise and is used only to shortlist candidates. The
     2026-07-13 probe found real matches out at mean-rank 475 (see POOL).
  2. Rank: segment chamfer — for each of the seed's canonical 10s windows,
     the candidate's best-matching window, averaged — plus DJ-continuity
     add-ons: octave-folded BPM distance (CLAP is timbre-dominant and near
     tempo-blind: the probe's #1 mean neighbour halved the seed's tempo),
     energy delta, and a shared album-genre bonus (file tags carry the
     subgenre split CLAP audibly misses).

Hungarian assignment (album_similarity's rerank) and all-pairs averaging were
both measured WORSE here: all-pairs is mathematically ≈ mean↔mean (mean of dot
products = dot of means), and a one-to-one assignment lets a track's odd
intro/outro windows dominate the score. Assignment stays right for ALBUMS,
whose items are whole coherent tracks; chamfer is right for a single track's
autocorrelated windows.
"""

from db_pool import db_query_with_ef_search

POOL = 600              # tier-1 recall horizon. Probe: widening 300→600 pulled
                        # 5 more tracks into the hybrid top-30 (one at mean-rank
                        # 475); 600→1000 added only 2 — diminishing returns.
                        # Chamfer cost is linear in pool size (~0.6s at 600,
                        # background-thread and shelf-async territory).

# Rescoring weights, sized against the probe pool's chamfer span (0.115–0.19):
# each add-on can reorder within a chamfer tier but not across tiers
# (BPM <= .03, energy <= .04, genres <= .045 against a .075 chamfer span).
W_BPM = 0.06            # x octave-folded |log2(bpm ratio)| (0–0.5)
W_ENERGY = 0.15         # x |energy delta| (pool p50 0.07)
W_GENRE = 0.015         # x shared album-genre count, capped below
GENRE_CAP = 3           # shared-genre count saturates here
BPM_NEUTRAL = 0.25      # pool-median penalties for rows with no usable
ENERGY_NEUTRAL = 0.07   # bpm/energy — unknown ranks mid-pack, not first


def similar_tracks(seed_uuid: str, exclude=(), limit: int = 20,
                   artist_cap: int = 2, jitter: float = 0.0,
                   pool: int = POOL) -> list:
    """Mixed (owned + phantom) two-tier similar tracks for a seed track UUID.

    Every row carries identity + display fields (album, year, cover_url,
    similarity = 1 - the ranking score, monotonic with the returned order)
    AND the playback fields radio needs (file_path/file_format for owned,
    phantom_album/length_ms for phantom).
    `artist_cap` keeps each artist to its best N rows (raw KNN clusters hard);
    `jitter` reshuffles near-ties for radio so the same seed never yields the
    same station twice — 0 gives a deterministic shelf. Excludes the seed and
    `exclude` UUIDs; empty when the seed has no embedding (a NULL target would
    otherwise order arbitrarily). Every embeddings row has segments (written
    atomically since v2), so the chamfer join never drops a pool row.

    HNSW needs ef_search >= pool plus iterative scan — the default 40-candidate
    wave dries up once radio's exclude list outgrows it and the station would
    silently die mid-session.
    """
    return db_query_with_ef_search("""
        WITH target AS (SELECT vector FROM embeddings WHERE track_id = %(seed)s::uuid),
        seed_af AS (
            SELECT NULLIF(bpm, 0) AS bpm, energy FROM audio_features
            WHERE track_id = %(seed)s::uuid
        ),
        seed_genres AS (
            SELECT DISTINCT ag.genre_id FROM album_genres ag
            WHERE ag.album_id IN (
                SELECT av.album_id FROM media_files mf
                JOIN album_variants av ON av.id = mf.album_variant_id
                WHERE mf.track_id = %(seed)s::uuid
                UNION
                SELECT atr.album_id FROM album_tracks atr
                WHERE atr.track_id = %(seed)s::uuid)
        ),
        seed_seg AS (
            SELECT es.segment_index, es.vector
            FROM embedding_segments es
            JOIN embeddings e ON e.id = es.embedding_id
            WHERE e.track_id = %(seed)s::uuid
        ),
        pool AS (
            SELECT t.id AS track_uuid, t.id::text AS track_id, e.id AS emb_id,
                   mf_rep.id AS media_file_id, mf_rep.file_path, mf_rep.file_format,
                   (mf_rep.id IS NOT NULL) AS is_owned,
                   t.title, a.name AS artist, ta.artist_id,
                   COALESCE(mf_rep.album_title, ph_rep.album) AS album,
                   COALESCE(mf_rep.release_year, ph_rep.release_year) AS year,
                   ph_rep.cover_url,
                   ph_rep.album AS phantom_album, ph_rep.length_ms
            FROM tracks t
            JOIN embeddings e ON e.track_id = t.id
            JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
            JOIN artists a ON a.id = ta.artist_id
            LEFT JOIN LATERAL (
                SELECT mf.id, mf.file_path, mf.file_format,
                       al.title AS album_title, al.release_year
                FROM media_files mf
                JOIN album_variants av ON av.id = mf.album_variant_id
                JOIN albums al ON al.id = av.album_id
                WHERE mf.track_id = t.id
                ORDER BY mf.is_analysis_source DESC, mf.id LIMIT 1
            ) mf_rep ON true
            LEFT JOIN LATERAL (
                SELECT atr.length_ms, al.title AS album, al.release_year, al.cover_url
                FROM album_tracks atr JOIN albums al ON al.id = atr.album_id
                WHERE atr.track_id = t.id
                ORDER BY (al.cover_url IS NOT NULL) DESC, al.id LIMIT 1
            ) ph_rep ON true
            WHERE t.id != %(seed)s::uuid
              AND t.id <> ALL(%(exclude)s::uuid[])
              AND (mf_rep.id IS NOT NULL OR ph_rep.album IS NOT NULL)
              AND EXISTS (SELECT 1 FROM target)
            ORDER BY e.vector <=> (SELECT vector FROM target)
            LIMIT %(pool)s
        ),
        rescored AS (
            SELECT pool.*, ch.chamfer,
                   ch.chamfer
                   + %(w_bpm)s * COALESCE(LEAST(
                         abs(ln(NULLIF(af.bpm, 0) / (SELECT bpm FROM seed_af)) / ln(2)),
                         abs(ln(NULLIF(af.bpm, 0) / (SELECT bpm FROM seed_af)) / ln(2) + 1),
                         abs(ln(NULLIF(af.bpm, 0) / (SELECT bpm FROM seed_af)) / ln(2) - 1)),
                       %(bpm_neutral)s)
                   + %(w_energy)s * COALESCE(
                         abs(af.energy - (SELECT energy FROM seed_af)),
                       %(energy_neutral)s)
                   - %(w_genre)s * LEAST((
                         SELECT count(DISTINCT ag.genre_id) FROM album_genres ag
                         WHERE ag.genre_id IN (SELECT genre_id FROM seed_genres)
                           AND ag.album_id IN (
                               SELECT av.album_id FROM media_files mf2
                               JOIN album_variants av ON av.id = mf2.album_variant_id
                               WHERE mf2.track_id = pool.track_uuid
                               UNION
                               SELECT atr.album_id FROM album_tracks atr
                               WHERE atr.track_id = pool.track_uuid)
                     ), %(genre_cap)s) AS score
            FROM pool
            JOIN LATERAL (
                SELECT avg(best) AS chamfer FROM (
                    SELECT min(s.vector <=> es.vector) AS best
                    FROM seed_seg s
                    CROSS JOIN (SELECT vector FROM embedding_segments
                                WHERE embedding_id = pool.emb_id) es
                    GROUP BY s.segment_index
                ) per_seed_window
            ) ch ON true
            LEFT JOIN audio_features af ON af.track_id = pool.track_uuid
        )
        SELECT track_id, media_file_id, file_path, file_format, is_owned,
               title, artist, album, year, cover_url, phantom_album, length_ms,
               -- The DISPLAYED number must be the RANKED number: 1 - score
               -- (chamfer with the BPM/energy/genre continuity add-ons), not
               -- the bare chamfer cosine — a sound-closer track that loses on
               -- continuity would show a higher number below a lower one.
               round((1 - score)::numeric, 4) AS similarity
        FROM (SELECT rescored.*, ROW_NUMBER() OVER (PARTITION BY artist_id
                                                    ORDER BY score) AS artist_rank
              FROM rescored) ranked
        WHERE artist_rank <= %(artist_cap)s
        ORDER BY score * (1 + %(jitter)s * random())
        LIMIT %(limit)s
    """, {"seed": seed_uuid, "exclude": list(exclude), "limit": limit,
          "pool": pool, "artist_cap": artist_cap, "jitter": jitter,
          "w_bpm": W_BPM, "w_energy": W_ENERGY,
          "w_genre": W_GENRE, "genre_cap": GENRE_CAP,
          "bpm_neutral": BPM_NEUTRAL, "energy_neutral": ENERGY_NEUTRAL},
        ef_search=max(pool, 500))
