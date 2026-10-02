-- What an HQPlayer's DSP costs on its host is measured, not predicted
-- (2026-10-02). HQPlayer reports process_speed (processing speed over
-- playback speed) in every <Status/> since 5.17.0: the playback poller keeps
-- a sample of it while the owner listens, and a benchmark plays test signals
-- through the settings the owner never used. hqp_dsp_speed is the per-key
-- rollup the filter, modulator and rate pickers badge from; the samples are
-- kept 90 days. Keys hold names, never indices (lists differ per mode and
-- per build), and the build (<GetInfo engine="…"/>) — names and costs are
-- valid for one. hqp_endpoints learns what HQPlayer reports about itself and,
-- for an HQPlayer on this machine, the machine. Mirrors the 001 baseline; a
-- no-op on a database 001 already created this way.

DO $$ BEGIN
    CREATE TYPE hqp_cuda AS ENUM ('off', 'full', 'convolution');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE hqp_sample_source AS ENUM ('listen', 'benchmark');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE hqp_bench_outcome AS ENUM ('done', 'cancelled', 'failed', 'interrupted');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE hqp_endpoints ADD COLUMN IF NOT EXISTS hqp_version TEXT;
ALTER TABLE hqp_endpoints ADD COLUMN IF NOT EXISTS hqp_engine TEXT;
ALTER TABLE hqp_endpoints ADD COLUMN IF NOT EXISTS hqp_platform TEXT;
ALTER TABLE hqp_endpoints ADD COLUMN IF NOT EXISTS cuda hqp_cuda;
ALTER TABLE hqp_endpoints ADD COLUMN IF NOT EXISTS host_cpu TEXT;
ALTER TABLE hqp_endpoints ADD COLUMN IF NOT EXISTS host_cores SMALLINT;
ALTER TABLE hqp_endpoints ADD COLUMN IF NOT EXISTS host_gpu TEXT;
ALTER TABLE hqp_endpoints ADD COLUMN IF NOT EXISTS host_ram_gb REAL;

CREATE TABLE IF NOT EXISTS hqp_benchmark_runs (
    id SERIAL PRIMARY KEY,
    hqp_endpoint_id INTEGER NOT NULL REFERENCES hqp_endpoints(id) ON DELETE CASCADE,
    hqp_engine TEXT NOT NULL,
    mode TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMPTZ,
    outcome hqp_bench_outcome,
    points_planned INTEGER NOT NULL,
    points_measured INTEGER NOT NULL DEFAULT 0,
    dropout_speed REAL,
    first_speed REAL,
    last_speed REAL,
    note TEXT,
    pre_mode INTEGER NOT NULL,
    pre_rate INTEGER NOT NULL,
    pre_filter INTEGER NOT NULL,
    pre_filter1x INTEGER NOT NULL,
    pre_shaper INTEGER NOT NULL,
    pre_matrix_profile TEXT,
    pre_volume REAL NOT NULL,
    mute_volume REAL,
    CONSTRAINT chk_hqp_benchmark_runs_finished CHECK ((finished_at IS NULL) = (outcome IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_hqp_benchmark_runs_endpoint
    ON hqp_benchmark_runs(hqp_endpoint_id, started_at DESC);

CREATE TABLE IF NOT EXISTS hqp_dsp_samples (
    id BIGSERIAL PRIMARY KEY,
    hqp_endpoint_id INTEGER NOT NULL REFERENCES hqp_endpoints(id) ON DELETE CASCADE,
    hqp_engine TEXT NOT NULL,
    cuda hqp_cuda,
    sampled_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source hqp_sample_source NOT NULL,
    run_id INTEGER REFERENCES hqp_benchmark_runs(id) ON DELETE SET NULL,
    mode TEXT NOT NULL,
    rate_out INTEGER NOT NULL,
    filter TEXT NOT NULL,
    shaper TEXT NOT NULL,
    matrix_profile TEXT,
    convolution BOOLEAN NOT NULL,
    adaptive BOOLEAN NOT NULL,
    src_rate INTEGER NOT NULL,
    src_bits SMALLINT,
    src_channels SMALLINT NOT NULL,
    src_sdm BOOLEAN NOT NULL,
    process_speed REAL NOT NULL,
    input_fill REAL,
    output_fill REAL,
    dropout BOOLEAN,
    settled BOOLEAN,
    init_s REAL,
    settle_s REAL,
    CONSTRAINT chk_hqp_dsp_samples_benchmark CHECK (
        source = 'benchmark'
        OR (run_id IS NULL AND dropout IS NULL AND settled IS NULL
            AND init_s IS NULL AND settle_s IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_hqp_dsp_samples_key
    ON hqp_dsp_samples(hqp_endpoint_id, hqp_engine, mode, rate_out, filter, shaper, src_rate);
CREATE INDEX IF NOT EXISTS idx_hqp_dsp_samples_sampled_at ON hqp_dsp_samples(sampled_at);

CREATE TABLE IF NOT EXISTS hqp_dsp_speed (
    id SERIAL PRIMARY KEY,
    hqp_endpoint_id INTEGER NOT NULL REFERENCES hqp_endpoints(id) ON DELETE CASCADE,
    hqp_engine TEXT NOT NULL,
    cuda hqp_cuda,
    mode TEXT NOT NULL,
    rate_out INTEGER NOT NULL,
    filter TEXT NOT NULL,
    shaper TEXT NOT NULL,
    matrix_profile TEXT,
    convolution BOOLEAN NOT NULL,
    src_rate INTEGER NOT NULL,
    src_channels SMALLINT NOT NULL,
    n INTEGER NOT NULL,
    speed_p10 REAL NOT NULL,
    speed_median REAL NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL,
    bench_at TIMESTAMPTZ,
    bench_settled BOOLEAN,
    bench_init_s REAL,
    bench_settle_s REAL,
    dropout BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT uq_hqp_dsp_speed_key UNIQUE NULLS NOT DISTINCT
        (hqp_endpoint_id, hqp_engine, cuda, mode, rate_out, filter, shaper,
         matrix_profile, convolution, src_rate, src_channels)
);
