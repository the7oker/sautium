-- When HQPlayer at rest did not let a benchmark run's measured mode be read
-- (2026-10-15): it refused the SetMode into it, or showed no State there.
-- HQPlayer's nearest rest tries a mark only while this is NULL — HQPlayer
-- was busy so far; once set, only a new run's start tries it again. NULL
-- once the mode is judged. Mirrors the 001 baseline; a no-op on a database
-- 001 already created this way.

ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS measured_refused_at TIMESTAMPTZ;
