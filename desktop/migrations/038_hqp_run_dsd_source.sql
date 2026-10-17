-- The URI a benchmark run handed HQPlayer for the owner's DSD file it
-- measured (2026-10-14): a lost run's put-back knows the run's own entry by
-- what the run handed, never by the owner's newest DSD listen at the time of
-- the put-back — a listen, an import or a merge since would make the run's
-- own entry read as another controller's. NULL: no DSD source, or a row from
-- before it was kept (the newest listen is asked, as before). Mirrors the 001
-- baseline; a no-op on a database 001 already created this way.

ALTER TABLE hqp_benchmark_runs ADD COLUMN IF NOT EXISTS dsd_uri TEXT;
