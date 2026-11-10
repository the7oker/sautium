-- 055 — a source row per origin, and the material groups (ANALYSIS-3 body 2,
-- 2026-11-10). analysis_sources was unique on (track_id, chromaprint_key):
-- one row per address whoever held it, and a first-hand registration of an
-- address flipped an imported row to first-hand, the other author's records
-- linked to it with it — signable here after the next shed of their seals.
-- Now the origin is part of the key: this node's registrations and the sync
-- import each keep a row of their own. material_of groups a track's rows of
-- one material whose digests differ (desktop/p2p/material.py). The same DDL
-- as the analysis_sources block of 001_initial.sql, for nodes that ran the
-- earlier files.
--
-- The two-column constraint goes by name, unconditionally: 018's tail adds
-- it again after 001 on a fresh install, and beside the per-origin one every
-- twin insert would fail on it — an ON CONFLICT arbitrates on its own index
-- only. Idempotent: on the schema 001 just created it drops what 018 re-added
-- and finds the rest in place.

ALTER TABLE analysis_sources DROP CONSTRAINT IF EXISTS uq_asrc_track_chromaprint;

DO $$ BEGIN
    ALTER TABLE analysis_sources
        ADD CONSTRAINT uq_asrc_track_chromaprint_origin UNIQUE (track_id, chromaprint_key, imported);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL;
END $$;

ALTER TABLE analysis_sources
    ADD COLUMN IF NOT EXISTS material_of INTEGER REFERENCES analysis_sources(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_analysis_sources_material
    ON analysis_sources(material_of) WHERE material_of IS NOT NULL;
