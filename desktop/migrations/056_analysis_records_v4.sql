-- 056 — the analysis records change once (2026-11-10): a record version per
-- kind (backend/record_sig.py). The segments stay v3; the features record is
-- v4, without the five fields that were not the features' — moods, the vocal
-- class, the vocal score and danceability are derived from the segments a
-- node holds (zero_shot_scores, delta 054), and the instruments become a v1
-- record of their own in track_instruments. The same DDL as the
-- audio_features and track_instruments blocks of 001_initial.sql, for nodes
-- that ran the earlier files:
--
--   1. track_instruments and its guard;
--   2. the instruments of every audio_features row whose instruments are a
--      map copied into it, unsigned, with the row's source link, methodology
--      version and dates — only while audio_features.instruments exists
--      (018's pattern), so the delta is a no-op on the schema 001 creates. A
--      row without instruments held SQL NULL or, written through the ORM or
--      the importer, a JSON null: neither is a record (chk_ti_map — the
--      record's hash is over a map), and the track's instruments are derived
--      anew;
--   3. the features guard restated without the five columns first, then
--      their checks, their indexes, the columns and the vocal_class type
--      dropped.
--
-- Both guards watch the record's source link and read a value in the form
-- its hash does (001_initial.sql's seal invalidation).
--
-- It touches no seal: which seals are this node's own and which another
-- author's needs the node's keys, so the re-seal is the data step
-- analysis_records_v4 (backend/db_migrate.py), right after the deltas.

CREATE TABLE IF NOT EXISTS track_instruments (
    id SERIAL PRIMARY KEY,
    track_id UUID NOT NULL UNIQUE REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE,
    instruments JSONB NOT NULL,
    analysis_source_id INTEGER REFERENCES analysis_sources(id) ON DELETE SET NULL,
    analysis_version SMALLINT NOT NULL,
    author_pubkey CHAR(64),
    signature     CHAR(128),
    batch_root    CHAR(64) REFERENCES signing_batches(batch_root),
    merkle_proof  JSONB,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_ti_map CHECK (jsonb_typeof(instruments) = 'object')
);
CREATE INDEX IF NOT EXISTS idx_track_instruments_instruments
    ON track_instruments USING gin (instruments);
CREATE INDEX IF NOT EXISTS idx_track_instruments_analysis_source
    ON track_instruments (analysis_source_id);
CREATE INDEX IF NOT EXISTS idx_track_instruments_unstamped
    ON track_instruments (id) INCLUDE (signature)
    WHERE signature IS NOT NULL AND batch_root IS NULL;

CREATE OR REPLACE FUNCTION seal_guard_track_instruments() RETURNS trigger AS $$
BEGIN
    IF (NEW.instruments::text IS DISTINCT FROM OLD.instruments::text
        OR NEW.analysis_version IS DISTINCT FROM OLD.analysis_version
        OR NEW.analysis_source_id IS DISTINCT FROM OLD.analysis_source_id)
       AND NEW.signature IS NOT DISTINCT FROM OLD.signature THEN
        NEW.author_pubkey := NULL;
        NEW.signature     := NULL;
        NEW.batch_root    := NULL;
        NEW.merkle_proof  := NULL;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_track_instruments_seal_guard ON track_instruments;
CREATE TRIGGER trg_track_instruments_seal_guard
BEFORE UPDATE ON track_instruments
FOR EACH ROW EXECUTE FUNCTION seal_guard_track_instruments();

DO $$ BEGIN CREATE TRIGGER trigger_track_instruments_updated_at BEFORE UPDATE ON track_instruments
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- The features row's instruments were the instruments of its methodology
-- version (v1 a single window, v2 the windowed max — the instruments'
-- versions since, ensemble_instruments.INSTRUMENTS_ANALYSIS_VERSION).
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'audio_features'
                 AND column_name = 'instruments') THEN
        INSERT INTO track_instruments (track_id, instruments, analysis_source_id,
                                       analysis_version, created_at, updated_at)
        SELECT track_id, instruments, analysis_source_id, analysis_version,
               created_at, updated_at
          FROM audio_features
         WHERE jsonb_typeof(instruments) = 'object';
    END IF;
END $$;

CREATE OR REPLACE FUNCTION seal_guard_audio_features() RETURNS trigger AS $$
BEGIN
    IF (float8send(NEW.bpm) IS DISTINCT FROM float8send(OLD.bpm)
        OR NEW.key IS DISTINCT FROM OLD.key
        OR NEW.mode IS DISTINCT FROM OLD.mode
        OR float8send(NEW.key_confidence) IS DISTINCT FROM float8send(OLD.key_confidence)
        OR float8send(NEW.energy) IS DISTINCT FROM float8send(OLD.energy)
        OR float8send(NEW.energy_db) IS DISTINCT FROM float8send(OLD.energy_db)
        OR float8send(NEW.brightness) IS DISTINCT FROM float8send(OLD.brightness)
        OR float8send(NEW.dynamic_range_db) IS DISTINCT FROM float8send(OLD.dynamic_range_db)
        OR float8send(NEW.zero_crossing_rate) IS DISTINCT FROM float8send(OLD.zero_crossing_rate)
        OR NEW.analysis_version IS DISTINCT FROM OLD.analysis_version
        OR NEW.analysis_source_id IS DISTINCT FROM OLD.analysis_source_id)
       AND NEW.signature IS NOT DISTINCT FROM OLD.signature THEN
        NEW.author_pubkey := NULL;
        NEW.signature     := NULL;
        NEW.batch_root    := NULL;
        NEW.merkle_proof  := NULL;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

ALTER TABLE audio_features DROP CONSTRAINT IF EXISTS chk_af_danceability;
ALTER TABLE audio_features DROP CONSTRAINT IF EXISTS chk_af_vocal_score;
DROP INDEX IF EXISTS idx_audio_features_danceability;
DROP INDEX IF EXISTS idx_audio_features_vocal;
DROP INDEX IF EXISTS idx_audio_features_instruments;
DROP INDEX IF EXISTS idx_audio_features_moods;
ALTER TABLE audio_features
    DROP COLUMN IF EXISTS instruments,
    DROP COLUMN IF EXISTS moods,
    DROP COLUMN IF EXISTS vocal_instrumental,
    DROP COLUMN IF EXISTS vocal_score,
    DROP COLUMN IF EXISTS danceability;
DROP TYPE IF EXISTS vocal_class;
