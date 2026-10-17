-- A benchmark run cut short because HQPlayer went away under it (it
-- closed, reset or refused the connection as the restore tried it) is put
-- back whole once HQPlayer answers again showing what it booted with —
-- stopped, reading no other controller's entry — never a person's change
-- (2026-10-14). `lost_at` marks such a run; `restored_at` dates the notice
-- that says it was put back, and `volume_left` the level HQPlayer came back
-- at and was left at — the put-back raises only from the run's own level
-- and lowers one louder than the owner's. Mirrors the 001 baseline; a no-op
-- on a database 001 already created this way.

ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS lost_at TIMESTAMPTZ;
ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS restored_at TIMESTAMPTZ;
ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS volume_left REAL;
