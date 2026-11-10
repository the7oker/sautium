-- 055 — a source row per declaration, copy and origin, and the material
-- groups (2026-11-10). analysis_sources was unique on
-- (track_id, chromaprint_key): one row per address whoever held it and
-- whatever copy it was made from. A first-hand registration of an address
-- flipped an imported row to first-hand — the other author's records linked
-- to it with it, signable here after the next shed of their seals —, a
-- registration of another duration or copy rewrote the row under records
-- sealed with, or made from, the old one, and an import of another author's
-- declaration shared it. Now a row is one declaration (the print and the
-- duration a seal binds) of one copy (copy_rank: the material rank and the
-- quality score; every imported row ranks as imported) of one origin, and
-- nothing rewrites it: UNIQUE NULLS NOT DISTINCT (track_id, chromaprint_key,
-- duration_seconds, imported, copy_rank). material_of groups a track's rows
-- of one material (desktop/p2p/material.py). The same DDL as the
-- analysis_sources block of 001_initial.sql, for nodes that ran the earlier
-- files: every existing row has one address of its track, so it is unique
-- under the new key too; its group is joined by the analysis records'
-- cutover (db_migrate's analysis_records_v4, provenance.join_existing_rows).
--
-- The two-column constraint goes by name, unconditionally: 018's tail adds
-- it again after 001 on a fresh install, and beside the new one every row
-- of a second origin, declaration or copy would fail on it — an ON CONFLICT
-- arbitrates on its own index only. Idempotent: on the schema 001 just
-- created it drops what 018 re-added and finds the rest in place.

ALTER TABLE analysis_sources DROP CONSTRAINT IF EXISTS uq_asrc_track_chromaprint;

ALTER TABLE analysis_sources
    ADD COLUMN IF NOT EXISTS material_of INTEGER REFERENCES analysis_sources(id) ON DELETE SET NULL;
ALTER TABLE analysis_sources
    ADD COLUMN IF NOT EXISTS copy_rank SMALLINT GENERATED ALWAYS AS (
        CASE WHEN imported THEN -1000
             ELSE CASE WHEN provider_id IS NULL THEN 2000
                       WHEN is_lossless THEN 1000 ELSE 0 END
                + CASE WHEN is_lossless AND bit_depth = 16 THEN 100
                       WHEN is_lossless THEN 50 ELSE 10 END
        END) STORED;

DO $$ BEGIN
    ALTER TABLE analysis_sources
        ADD CONSTRAINT uq_asrc_track_declaration_copy
            UNIQUE NULLS NOT DISTINCT (track_id, chromaprint_key, duration_seconds, imported,
                                       copy_rank);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_analysis_sources_material
    ON analysis_sources(material_of) WHERE material_of IS NOT NULL;
