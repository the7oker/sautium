-- A benchmark run cut short owes HQPlayer its put-back; a person's DSP change,
-- their switch away or another HQPlayer chosen, or a knob they set in
-- HQPlayer's own window supersedes it (2026-10-13): HQPlayer stays as they set
-- it, its volume never raised for them, and what still holds the run's test
-- values — the mode it switched into, the selection it set last, the volume
-- it lowered to — is said in one notice until they changed it all, or a new
-- run opened its row. `left_at` dates that notice. Mirrors the 001 baseline;
-- a no-op on a database 001 already created this way.

ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS left_at TIMESTAMPTZ;
