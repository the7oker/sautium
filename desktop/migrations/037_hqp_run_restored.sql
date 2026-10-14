-- A benchmark run cut short because HQPlayer went away under it (its
-- restore could not reach it) is put back whole once HQPlayer answers again:
-- what HQPlayer shows then is what it booted with, never a person's change
-- (2026-10-14). `lost_at` marks such a run; `restored_at` dates the notice
-- that says it was put back, and `volume_left` the level HQPlayer was left
-- at when the put-back did not raise it (it raises only from the run's own
-- level). Mirrors the 001 baseline; a no-op on a database 001 already
-- created this way.

ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS lost_at TIMESTAMPTZ;
ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS restored_at TIMESTAMPTZ;
ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS volume_left REAL;
