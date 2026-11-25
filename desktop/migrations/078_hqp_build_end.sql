-- 078 — BENCH-GUARD-1 (2026-11-25): the same DDL as 001_initial.sql's
-- blocks, for nodes that ran the earlier files. Idempotent: a no-op on the
-- schema 001 just created.
--
-- A slow start's end, once the node saw it: HQPlayer came out of the build
-- by itself ('answered'), or went away or was restarted while it prepared
-- the point ('ended' — too heavy here: on a 16 GB Pi a build across rate
-- families took HQPlayer's memory whole and the kernel ended it); build_s,
-- the seconds from its Play to that end. Planning reads the result alone.

DO $$ BEGIN
    CREATE TYPE hqp_build_end AS ENUM ('answered', 'ended');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE hqp_benchmark_points ADD COLUMN IF NOT EXISTS build_end hqp_build_end;
ALTER TABLE hqp_benchmark_points ADD COLUMN IF NOT EXISTS build_s REAL;

DO $$ BEGIN
    ALTER TABLE hqp_benchmark_points ADD CONSTRAINT chk_hqp_benchmark_points_build_end CHECK (
        build_end IS NULL OR result::text = 'slow_start');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
