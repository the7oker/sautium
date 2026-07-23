"""
Audio-similar tracks via CLAP segment embeddings — the shared two-tier scorer
behind radio batches and the Now Playing "Similar" shelf.

Two-stage, one SQL statement:

  1. Recall, two arms: the POOL nearest track means (HNSW) UNION the "kin"
     arm — every analyzed track by the seed's primary artist(s) and their
     Last.fm similar artists (capped at KIN_ARM_CAP by exact mean distance).
     Mean↔mean cosine is CONCENTRATED post-mean-flip — an entire radio batch
     fits in dist 0.042–0.070 while the intra-track segment spread is ~0.19 —
     so mean ordering is near-noise and is used only to shortlist candidates,
     AND its recall misses the perceptual neighbourhood entirely: for a
     Smooth Operator seed, Sade's own The Sweetest Taboo (the best-sounding
     match of every probe) sat at mean-rank 678, outside any sane pool. The
     kin arm is what brings the human-adjacent catalog in.
  2. Rank: segment chamfer — for each of the seed's canonical 10s windows,
     the candidate's best-matching window, averaged — minus a Last.fm
     artist-tag bonus: W_TAG × weighted Jaccard between the seed's and the
     candidate's artist tag profiles. Crowd tag profiles are the one
     measured signal that hits GENRE (Sade↔Winehouse .66, ↔Baker .51 vs
     ↔Huey Lewis/Queen .02/.006 — a 20–30× separation); they demote
     same-era-production rock that chamfer can't hear apart from soul
     without banishing it.

Artist bio embeddings (BGE) were measured as the tag bonus's alternative and
rejected: the bio space is concentrated (half the corpus within 0.44–0.49)
and confounded by career shape — Queen's bio sits closer to Sade's (0.378)
than Anita Baker's (0.455), so at working weights it pulls British legends
into a Sade radio instead of quiet storm.

Add-ons tried and REMOVED after listening probes:
- Album-genre bonus (2026-07-23, Smooth Operator): file-tag overlap is
  anti-signal at this grain — broad tags ("Pop") gave the max bonus to Bee
  Gees/Santana-class rock against a Sade seed while the truly similar
  artists (Anita Baker, Erykah Badu) carried adjacent-but-disjoint tags
  (R&B) and got nothing; the bonus actively inverted the perceptual order.
- Octave-folded BPM distance (2026-07-23, same probe): the beat tracker
  locks on wrong metrical levels in NON-octave ratios the fold can't absorb
  — Smooth Operator detected at 63 (real ~104) penalized Sade's own
  correctly-detected Kiss of Life (99.4 → fold .34) while rewarding Huey
  Lewis (123 ≈ 2×63 → fold .04); 85% of the pair inversion was this term.
  A bad SEED detection poisons the whole pool's BPM column.
- Energy delta (2026-07-23, same probe): energy is an honest RMS measurement
  but it encodes MASTER loudness, not musical drive — at weight 0.15 it
  moved 19 of the top-20 positions, pulling in same-loudness disco
  ("Stayin' Alive") and pushing out closer-vibe quieter masters (Morcheeba).

Hungarian assignment (album_similarity's rerank) and all-pairs averaging were
both measured WORSE here: all-pairs is mathematically ≈ mean↔mean (mean of dot
products = dot of means), and a one-to-one assignment lets a track's odd
intro/outro windows dominate the score. Assignment stays right for ALBUMS,
whose items are whole coherent tracks; chamfer is right for a single track's
autocorrelated windows.
"""

from db_pool import db_query_with_ef_search

POOL = 600              # mean-KNN recall horizon. Probe: widening 300→600 pulled
                        # 5 more tracks into the hybrid top-30 (one at mean-rank
                        # 475); 600→1000 added only 2 — diminishing returns.
                        # Chamfer cost is linear in candidate count (~0.6s at
                        # 600, background-thread and shelf-async territory).
KIN_ARM_CAP = 1200      # kin recall arm bound: a seed whose similar artists
                        # own huge catalogs (Queen-class) would otherwise pour
                        # thousands of rows into the chamfer rerank; the cap
                        # keeps the mean-closest kin tracks
W_TAG = 0.08            # × weighted tag Jaccard. Sized on the Smooth Operator
                        # probe: 0.04 left Huey Lewis at #4; 0.08 yields
                        # Sade/Winehouse/Bassey/Franklin/Baker on top with
                        # Huey demoted to ~#15 (still present — tags rank,
                        # never gate)


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
        seed_seg AS (
            SELECT es.segment_index, es.vector
            FROM embedding_segments es
            JOIN embeddings e ON e.id = es.embedding_id
            WHERE e.track_id = %(seed)s::uuid
        ),
        seed_artists AS (
            SELECT ta.artist_id FROM track_artists ta
            WHERE ta.track_id = %(seed)s::uuid AND ta.role = 'primary'
        ),
        kin AS (
            SELECT artist_id FROM seed_artists
            UNION
            SELECT sa.similar_artist_id FROM similar_artists sa
            JOIN seed_artists s ON s.artist_id = sa.artist_id
        ),
        seed_tags AS (
            -- ::numeric is load-bearing: weight is INTEGER and the Jaccard
            -- division below would silently floor to 0 in integer math.
            SELECT at.tag_id, MAX(COALESCE(at.weight, 1))::numeric AS weight
            FROM artist_tags at
            JOIN seed_artists s ON s.artist_id = at.artist_id
            GROUP BY at.tag_id
        ),
        seed_tag_total AS (SELECT COALESCE(SUM(weight), 0) AS total FROM seed_tags),
        cand AS (
            (SELECT e.track_id FROM embeddings e
             WHERE e.track_id != %(seed)s::uuid
               AND e.track_id <> ALL(%(exclude)s::uuid[])
             ORDER BY e.vector <=> (SELECT vector FROM target)
             LIMIT %(pool)s)
            UNION
            -- Kin arm: `+ 0` defeats the HNSW path on purpose — an
            -- artist-filtered index walk over a sparse set would crawl the
            -- graph and dry up; an exact top-N over the few-k joined rows
            -- is cheap and complete.
            (SELECT e.track_id FROM embeddings e
             JOIN track_artists kta ON kta.track_id = e.track_id
                 AND kta.role = 'primary'
             JOIN kin k ON k.artist_id = kta.artist_id
             WHERE e.track_id != %(seed)s::uuid
               AND e.track_id <> ALL(%(exclude)s::uuid[])
             ORDER BY (e.vector <=> (SELECT vector FROM target)) + 0
             LIMIT %(kin_cap)s)
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
            FROM cand c
            JOIN tracks t ON t.id = c.track_id
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
            WHERE (mf_rep.id IS NOT NULL OR ph_rep.album IS NOT NULL)
              AND EXISTS (SELECT 1 FROM target)
        ),
        rescored AS (
            SELECT pool.*,
                   ch.chamfer
                   - %(w_tag)s * COALESCE(
                         tg.sum_min / NULLIF((SELECT total FROM seed_tag_total)
                                             + tg.cand_total - tg.sum_min, 0),
                       0) AS score
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
            -- Weighted Jaccard pieces: sum over shared tags of min(weight)
            -- and the candidate's full tag mass; union mass = seed_total +
            -- cand_total - sum_min.
            LEFT JOIN LATERAL (
                SELECT COALESCE(SUM(LEAST(st.weight, COALESCE(at2.weight, 1)::numeric)), 0) AS sum_min,
                       (SELECT COALESCE(SUM(COALESCE(weight, 1)), 0)::numeric
                        FROM artist_tags WHERE artist_id = pool.artist_id) AS cand_total
                FROM seed_tags st
                JOIN artist_tags at2 ON at2.tag_id = st.tag_id
                    AND at2.artist_id = pool.artist_id
            ) tg ON true
        )
        SELECT track_id, media_file_id, file_path, file_format, is_owned,
               title, artist, album, year, cover_url, phantom_album, length_ms,
               round((1 - score)::numeric, 4) AS similarity
        FROM (SELECT rescored.*, ROW_NUMBER() OVER (PARTITION BY artist_id
                                                    ORDER BY score) AS artist_rank
              FROM rescored) ranked
        WHERE artist_rank <= %(artist_cap)s
        ORDER BY score * (1 + %(jitter)s * random())
        LIMIT %(limit)s
    """, {"seed": seed_uuid, "exclude": list(exclude), "limit": limit,
          "pool": pool, "artist_cap": artist_cap, "jitter": jitter,
          "kin_cap": KIN_ARM_CAP, "w_tag": W_TAG},
        ef_search=max(pool, 500))
