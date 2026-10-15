-- A benchmark run's return point kept before 042 has no onset
-- (measured_from_at): the notice that HQPlayer stayed in the mode a check
-- left it in is dated by it, and an undated one broke every notices read
-- (2026-10-15). Dated from the run's mark, else its end, else its start. A
-- no-op on a database 001 created, and on a second run.

UPDATE hqp_benchmark_runs
   SET measured_from_at = coalesce(measured_unchecked_at, finished_at, started_at)
 WHERE measured_from_mode IS NOT NULL AND measured_from_at IS NULL;
