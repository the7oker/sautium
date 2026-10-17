-- The return point of a benchmark run's measured-mode check (2026-10-15):
-- the mode HQPlayer was found in when the check left it for the measured
-- mode — its index, what SetMode takes — and that mode's selection by name,
-- as HQPlayer lists it there (names hold across a restart; the indices are
-- read again in that mode), the rate in Hz. Written before the check's
-- first SetMode; the next check or run start goes back there first. NULL
-- once HQPlayer is back in that mode — the check's own way back, a return,
-- the owner's own switch — or once a person's DSP change or switch away
-- made HQPlayer theirs. Mirrors the 001 baseline; a no-op on a database 001
-- already created this way.

ALTER TABLE hqp_benchmark_runs
    ADD COLUMN IF NOT EXISTS measured_from_mode INTEGER,
    ADD COLUMN IF NOT EXISTS measured_from_mode_name TEXT,
    ADD COLUMN IF NOT EXISTS measured_from_rate INTEGER,
    ADD COLUMN IF NOT EXISTS measured_from_filter TEXT,
    ADD COLUMN IF NOT EXISTS measured_from_filter1x TEXT,
    ADD COLUMN IF NOT EXISTS measured_from_shaper TEXT;
