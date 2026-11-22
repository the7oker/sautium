-- 072 — HQP-BENCH-63 (2026-11-22): the points HQPlayer stopped answering in
-- before 071 named that. A run ledgered them 'unstarted' with the note
-- "HQPlayer stayed silent N min building it — …" (a build HQPlayer answered
-- through says "HQPlayer was still starting it …" and stays 'unstarted'):
-- each is 'hung' from here, so the picker says "Stops HQPlayer here" and asks
-- before choosing it. Its own file: 071 adds the value, and a value is used
-- only once the transaction that added it committed. Idempotent; nothing to
-- do on a database 001 just created.

UPDATE hqp_benchmark_points SET result = 'hung'
 WHERE result = 'unstarted' AND note LIKE 'HQPlayer stayed silent % min building it%';
