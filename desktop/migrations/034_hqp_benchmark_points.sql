-- Every benchmark point leaves its outcome (2026-10-02): what was asked
-- (the selected rate, the filter and modulator by name, the source) and
-- how it ended — measured, unsettled, dropped out, refused by HQPlayer, or
-- failed. A repeat run skips a point by what was asked, not only by what a
-- sample recorded HQPlayer playing: a combination HQPlayer refuses ("…512+fs"
-- below 512×) or an adaptive output rate that plays another rate would
-- otherwise be planned again on every run. A run row keeps the context it
-- measured in (CUDA, matrix profile, convolution) — its points are covered
-- only in that context — and two more things a run cut short is put back
-- from: when it switched HQPlayer to the other mode, that mode's own
-- selection as it found it (HQPlayer keeps one selection per mode), and the
-- selection it set last — an HQPlayer that restarted under the run keeps
-- no signal and maybe not the lowered volume, but still its selection.
-- Mirrors the 001 baseline; a no-op on a database 001 already created this
-- way.

DO $$ BEGIN
    CREATE TYPE hqp_point_result AS ENUM ('measured', 'unsettled', 'dropout', 'refused', 'failed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS cuda hqp_cuda;
ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS matrix_profile TEXT;
ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS convolution BOOLEAN;
ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS own_mode INTEGER;
ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS own_rate INTEGER;
ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS own_filter INTEGER;
ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS own_filter1x INTEGER;
ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS own_shaper INTEGER;
ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS last_rate INTEGER;
ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS last_filter INTEGER;
ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS last_filter1x INTEGER;
ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS last_shaper INTEGER;

CREATE TABLE IF NOT EXISTS hqp_benchmark_points (
    id BIGSERIAL PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES hqp_benchmark_runs(id) ON DELETE CASCADE,
    rate_hz INTEGER NOT NULL,
    filter TEXT NOT NULL,
    shaper TEXT NOT NULL,
    src_rate INTEGER NOT NULL,
    src_channels SMALLINT NOT NULL,
    result hqp_point_result NOT NULL,
    note TEXT,
    sample_id BIGINT REFERENCES hqp_dsp_samples(id) ON DELETE SET NULL,
    at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_hqp_benchmark_points_run ON hqp_benchmark_points(run_id);
