-- When a benchmark run's put-back left the measured mode's own selection
-- unchecked (2026-10-14): State shows only the mode HQPlayer is in, and the
-- mode is entered to read it only on an HQPlayer at rest — stopped, reading
-- no other controller's entry — because SetMode restarts its output. Busy,
-- it is checked at HQPlayer's nearest such rest or a new run's start, before
-- that run reads the owner's selection: the run's last point goes back to
-- the owner's, anything else is theirs. NULL once checked. Mirrors the 001
-- baseline; a no-op on a database 001 already created this way.

ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS measured_unchecked_at TIMESTAMPTZ;
