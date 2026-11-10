-- 055 — a source row per declaration and origin, and the material groups
-- (ANALYSIS-3 body 2, 2026-11-10). analysis_sources was unique on
-- (track_id, chromaprint_key): one row per address whoever held it, a
-- first-hand registration of an address flipped an imported row to
-- first-hand — the other author's records linked to it with it, signable
-- here after the next shed of their seals — and a registration of another
-- duration rewrote the row's declaration under the records sealed with the
-- old one, as an import of another author's declaration shared it. Now a
-- row is one declaration (the print and the duration a seal binds) of one
-- origin: UNIQUE NULLS NOT DISTINCT (track_id, chromaprint_key, imported,
-- duration_seconds). material_of groups a track's rows of one material
-- (desktop/p2p/material.py). The same DDL as the analysis_sources block of
-- 001_initial.sql, for nodes that ran the earlier files: every existing row
-- already has a declaration and an origin of its own.
--
-- The two-column constraint goes by name, unconditionally: 018's tail adds
-- it again after 001 on a fresh install, and beside the new one every row
-- of a second origin or declaration would fail on it — an ON CONFLICT
-- arbitrates on its own index only. Idempotent: on the schema 001 just
-- created it drops what 018 re-added and finds the rest in place.

ALTER TABLE analysis_sources DROP CONSTRAINT IF EXISTS uq_asrc_track_chromaprint;

DO $$ BEGIN
    ALTER TABLE analysis_sources
        ADD CONSTRAINT uq_asrc_track_declaration_origin
            UNIQUE NULLS NOT DISTINCT (track_id, chromaprint_key, imported, duration_seconds);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL;
END $$;

ALTER TABLE analysis_sources
    ADD COLUMN IF NOT EXISTS material_of INTEGER REFERENCES analysis_sources(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_analysis_sources_material
    ON analysis_sources(material_of) WHERE material_of IS NOT NULL;
