-- 018 — chromaprint is the only content address (2026-09-18). pcm_hash
-- (BLAKE2b of the natively-decoded PCM) keyed analysis_sources on exact bytes,
-- and a seal over it was a possession proof of those bytes — changing with
-- every decoder build and every lossy decode, so two nodes analysing the same
-- stream addressed two materials. The AcoustID fingerprint is a public
-- recording identity that any decode of the material reproduces; it is now
-- the key and the whole material declaration (audio record payload v3;
-- enrichment payloads stay v2, so those seals — own and foreign — survive).
--
-- What that costs, this file pays: every audio seal on every node was made
-- over v2 and verifies no more. Foreign audio analysis (imported sources —
-- sync, carry and the seed alike) is deleted; its authors' v3 seals arrive
-- again over the network. The node's own audio seals are shed and the notary
-- re-signs them at the next backend start (sign() is unconditional on the
-- startup wake). Analysis with no fingerprint (fpcalc has a floor of a few
-- seconds) has no address and goes too — the passes no longer save such
-- rows. The ~3 KB fingerprint is too long for a btree row, so the key is the
-- database's own digest of it, a stored generated column.
--
-- Idempotent and a no-op on the schema 001 now creates: the data block runs
-- only while the pcm_hash column still exists.

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'analysis_sources'
                 AND column_name = 'pcm_hash') THEN

        -- 1. Foreign analysis out: embeddings (segments cascade), features, then
        --    the imported sources themselves.
        DELETE FROM embeddings e USING analysis_sources s
         WHERE s.id = e.analysis_source_id AND s.imported;
        DELETE FROM audio_features a USING analysis_sources s
         WHERE s.id = a.analysis_source_id AND s.imported;
        DELETE FROM analysis_sources WHERE imported;

        -- 2. No fingerprint = no address. ON DELETE SET NULL would leave rows
        --    nothing can sign, which the pending predicates re-derive every run.
        DELETE FROM embeddings e USING analysis_sources s
         WHERE s.id = e.analysis_source_id AND s.chromaprint IS NULL;
        DELETE FROM audio_features a USING analysis_sources s
         WHERE s.id = a.analysis_source_id AND s.chromaprint IS NULL;
        DELETE FROM analysis_sources WHERE chromaprint IS NULL;

        -- 3. One row per (track, fingerprint). Two rips of one master already
        --    share a fingerprint under two pcm_hash rows: keep the row analysis
        --    links to, else the one on the elected analysis-source file, else the
        --    best material, else the newest; re-point the losers' links first.
        CREATE TEMP TABLE asrc_dup ON COMMIT DROP AS
        WITH scored AS (
            SELECT s.id, s.track_id, s.chromaprint,
                   EXISTS (SELECT 1 FROM embeddings e WHERE e.analysis_source_id = s.id)
                   OR EXISTS (SELECT 1 FROM audio_features a WHERE a.analysis_source_id = s.id)
                       AS linked,
                   COALESCE(mf.is_analysis_source, false) AS elected,
                   CASE WHEN s.provider_id IS NULL THEN 2 WHEN s.is_lossless THEN 1 ELSE 0 END
                       AS rank
              FROM analysis_sources s
              LEFT JOIN media_files mf ON mf.id = s.media_file_id)
        SELECT id,
               first_value(id) OVER (PARTITION BY track_id, chromaprint
                                     ORDER BY linked DESC, elected DESC, rank DESC, id DESC)
                   AS keep_id
          FROM scored;
        DELETE FROM asrc_dup WHERE id = keep_id;
        UPDATE embeddings e SET analysis_source_id = d.keep_id
          FROM asrc_dup d WHERE e.analysis_source_id = d.id;
        UPDATE audio_features a SET analysis_source_id = d.keep_id
          FROM asrc_dup d WHERE a.analysis_source_id = d.id;
        DELETE FROM analysis_sources s USING asrc_dup d WHERE s.id = d.id;

        -- 4. Every remaining audio seal is v2: shed it (the payload columns are
        --    untouched, so the seal-guard triggers stay quiet) for the notary to
        --    re-sign, and drop the batches nothing references any more —
        --    enrichment seals keep theirs.
        UPDATE embedding_segments
           SET author_pubkey = NULL, signature = NULL, batch_root = NULL, merkle_proof = NULL
         WHERE signature IS NOT NULL;
        UPDATE audio_features
           SET author_pubkey = NULL, signature = NULL, batch_root = NULL, merkle_proof = NULL
         WHERE signature IS NOT NULL;
        DELETE FROM signing_batches WHERE batch_root NOT IN (
            SELECT batch_root FROM embedding_segments WHERE batch_root IS NOT NULL
            UNION SELECT batch_root FROM audio_features     WHERE batch_root IS NOT NULL
            UNION SELECT batch_root FROM artist_bios        WHERE batch_root IS NOT NULL
            UNION SELECT batch_root FROM artist_tags        WHERE batch_root IS NOT NULL
            UNION SELECT batch_root FROM similar_artists    WHERE batch_root IS NOT NULL
            UNION SELECT batch_root FROM track_stats        WHERE batch_root IS NOT NULL
            UNION SELECT batch_root FROM genre_descriptions WHERE batch_root IS NOT NULL
            UNION SELECT batch_root FROM albums             WHERE batch_root IS NOT NULL
            UNION SELECT batch_root FROM album_tracks       WHERE batch_root IS NOT NULL
            UNION SELECT batch_root FROM track_mbids        WHERE batch_root IS NOT NULL);
    END IF;
END $$;

ALTER TABLE analysis_sources DROP COLUMN IF EXISTS pcm_hash;   -- UNIQUE (track_id, pcm_hash) goes with it
ALTER TABLE analysis_sources ALTER COLUMN chromaprint SET NOT NULL;
-- STORED, explicitly: PG18 defaults a generated column to VIRTUAL, which no
-- index may cover. sha256 and decode are IMMUTABLE; the fingerprint alphabet
-- (record_sig._guard_chromaprint) has no backslash, so decode(…, 'escape')
-- is the bytes of the text.
ALTER TABLE analysis_sources
    ADD COLUMN IF NOT EXISTS chromaprint_key BYTEA
        GENERATED ALWAYS AS (sha256(decode(chromaprint, 'escape'))) STORED;
ALTER TABLE analysis_sources DROP CONSTRAINT IF EXISTS uq_asrc_track_chromaprint;
ALTER TABLE analysis_sources
    ADD CONSTRAINT uq_asrc_track_chromaprint UNIQUE (track_id, chromaprint_key);
