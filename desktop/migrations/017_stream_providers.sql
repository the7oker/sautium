-- 017 — stream providers registry (2026-09-18). analysis_sources.origin was
-- an ENUM ('local', 'deezer', 'youtube'): a closed bring-your-own module's id
-- hardcoded in the public schema, and the reason the stream enricher refused
-- provenance for any provider it did not know (its analysis stayed unlinked
-- and never signed). The enum also conflated WHAT was analysed (the node's
-- own file vs a stream — the only distinction any rule reads) with WHICH
-- plugin fetched it. Now `provider_id` references stream_providers, the
-- persisted snapshot of every registered manifest that the backend upserts
-- at start; NULL means the node's own file, or a row imported over P2P (the
-- wire withholds file-vs-stream). Overwrite precedence reads the material
-- (own file > lossless stream > lossy stream), never the brand — the rule the
-- sync already applies to peers' sources.
--
-- Idempotent and a no-op on the schema 001 now creates. On an older database
-- the data move (origin → provider_id), the registry seed and the enum drop
-- run inside the runner's transaction together with the DDL.

CREATE TABLE IF NOT EXISTS stream_providers (
    id            VARCHAR(32) PRIMARY KEY,
    name          TEXT NOT NULL,
    lossless      BOOLEAN NOT NULL,
    excerpt       BOOLEAN NOT NULL DEFAULT false,
    demo_limited  BOOLEAN NOT NULL DEFAULT false,
    version       TEXT,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE analysis_sources
    ADD COLUMN IF NOT EXISTS provider_id VARCHAR(32) REFERENCES stream_providers(id);

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'analysis_sources'
                 AND column_name = 'origin') THEN
        -- Seed the registry from the providers this database has analysed
        -- through, so the FK holds on a node whose plugin is gone or was
        -- never installed. Name and tier are placeholders here; the
        -- backend's start-up snapshot corrects them for every provider
        -- still registered.
        INSERT INTO stream_providers (id, name, lossless)
        SELECT origin::text, initcap(origin::text), coalesce(bool_or(is_lossless), false)
          FROM analysis_sources
         WHERE origin IS NOT NULL AND origin <> 'local'
         GROUP BY origin
        ON CONFLICT (id) DO NOTHING;

        UPDATE analysis_sources
           SET provider_id = origin::text
         WHERE origin IS NOT NULL AND origin <> 'local' AND provider_id IS NULL;

        ALTER TABLE analysis_sources DROP CONSTRAINT IF EXISTS chk_asrc_stream_no_file;
        ALTER TABLE analysis_sources DROP COLUMN origin;
    END IF;
END $$;

DROP TYPE IF EXISTS analysis_origin;

ALTER TABLE analysis_sources DROP CONSTRAINT IF EXISTS chk_asrc_stream_no_file;
ALTER TABLE analysis_sources
    ADD CONSTRAINT chk_asrc_stream_no_file CHECK (provider_id IS NULL OR media_file_id IS NULL);
ALTER TABLE analysis_sources DROP CONSTRAINT IF EXISTS chk_asrc_imported_anonymous;
ALTER TABLE analysis_sources
    ADD CONSTRAINT chk_asrc_imported_anonymous CHECK (NOT (imported AND provider_id IS NOT NULL));
