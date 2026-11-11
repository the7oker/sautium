-- 057 — the analysis passes' pace on this machine (2026-11-11). The run's
-- two stages became two passes split by what they make: the sonic pass (one
-- decode: the fingerprint, the segment grid, CLAP, librosa) and the
-- instrument pass (one decode: the AST+PaSST tagger), the second behind the
-- owner's switch (enrichment.instruments). Beside the switch the Library
-- screen says what the instrument pass would cost here, from the pace each
-- pass records after every batch (backend/analysis_pace.py). The same DDL as
-- the "Analysis pace" block of 001_initial.sql, for nodes that ran the
-- earlier files. Idempotent: a no-op on the schema 001 just created.

DO $$ BEGIN
    CREATE TYPE analysis_pass AS ENUM ('sonic', 'instruments');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE compute_device AS ENUM ('cuda', 'mps', 'cpu');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS analysis_pace (
    pass analysis_pass NOT NULL,
    device compute_device NOT NULL,
    cores SMALLINT NOT NULL,
    batches INTEGER NOT NULL,
    audio_seconds DOUBLE PRECISION NOT NULL,
    wall_seconds DOUBLE PRECISION NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (pass, device, cores),
    CONSTRAINT chk_analysis_pace CHECK (cores > 0 AND batches > 0
                                        AND audio_seconds > 0 AND wall_seconds >= 0)
);
