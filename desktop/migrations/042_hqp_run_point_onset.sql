-- When a benchmark run's return point was kept (2026-10-15): the onset of
-- the notice that says HQPlayer stayed in the mode a check left it in, its
-- own and not the run's mark's — a new condition, said anew over a "not
-- checked yet" the owner has already seen. Written with the point, NULL with
-- it. Mirrors the 001 baseline; a no-op on a database 001 already created
-- this way.

ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS measured_from_at TIMESTAMPTZ;
