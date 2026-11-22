-- 071 — HQP-BENCH-63 (2026-11-22): the same DDL as 001_initial.sql's blocks,
-- for nodes that ran the earlier files. Idempotent: a no-op on the schema
-- 001 just created.
--
-- hqp_point_result: 'slow_start' — HQPlayer answered nothing past the
-- run's build limit after a point's Play (two such on 6.3.3 with the CUDA
-- resampler, 44.1 → 768 kHz, 2026-11-20: between rate families a build can
-- take hours, and a long build and a stopped
-- HQPlayer look the same from outside), a result of its own that a later
-- run on that engine, CUDA state and mode does not ask again; 'implied' — a
-- rung of a ladder a neighbour answers for, written so the sheet and Compare
-- by ear read it as too slow or keeping up here, never as not measured (implied_from: that neighbour's rate; implied_ok:
-- its answer). Neither value is used in this file: a value an ALTER TYPE
-- adds cannot be used in the transaction that adds it (the check compares
-- the text).
-- hqp_dsp_samples.speed_lo/speed_hi, hqp_dsp_speed.bench_lo/bench_hi: a
-- benchmark point's lowest and highest reading — the mark shows the range
-- where the readings swing (6.3.3: ±5–15 %).

ALTER TYPE hqp_point_result ADD VALUE IF NOT EXISTS 'slow_start' BEFORE 'failed';
ALTER TYPE hqp_point_result ADD VALUE IF NOT EXISTS 'implied' BEFORE 'failed';

ALTER TABLE hqp_dsp_samples ADD COLUMN IF NOT EXISTS speed_lo REAL;
ALTER TABLE hqp_dsp_samples ADD COLUMN IF NOT EXISTS speed_hi REAL;
ALTER TABLE hqp_dsp_samples DROP CONSTRAINT IF EXISTS chk_hqp_dsp_samples_benchmark;
ALTER TABLE hqp_dsp_samples ADD CONSTRAINT chk_hqp_dsp_samples_benchmark CHECK (
    source = 'benchmark'
    OR (run_id IS NULL AND dropout IS NULL AND settled IS NULL
        AND init_s IS NULL AND settle_s IS NULL AND speed_lo IS NULL AND speed_hi IS NULL));

ALTER TABLE hqp_dsp_speed ADD COLUMN IF NOT EXISTS bench_lo REAL;
ALTER TABLE hqp_dsp_speed ADD COLUMN IF NOT EXISTS bench_hi REAL;

ALTER TABLE hqp_benchmark_points ADD COLUMN IF NOT EXISTS implied_from INTEGER;
ALTER TABLE hqp_benchmark_points ADD COLUMN IF NOT EXISTS implied_ok BOOLEAN;
ALTER TABLE hqp_benchmark_points DROP CONSTRAINT IF EXISTS chk_hqp_benchmark_points_implied;
ALTER TABLE hqp_benchmark_points ADD CONSTRAINT chk_hqp_benchmark_points_implied CHECK (
    (result::text = 'implied') = (implied_from IS NOT NULL AND implied_ok IS NOT NULL));
