-- The credit tables' primary keys put role second (2026-09-24). Mirrors the
-- 001 baseline.
--
-- Readers probe a credit by its track or album AND its role
-- (`ta.track_id = … AND ta.role = 'primary'`, ~120 sites). With role last,
-- behind artist_id, PostgreSQL 18 costs that probe as a skip scan over
-- artist_id whenever its sampled distinct-artist estimate stays under the
-- index's page count — on the master's 3.8M-row track_artists 28,732
-- estimated against 301,879 real, however fresh the statistics — so one
-- index probe was priced at 45,000-55,000 instead of 8 and the planner read
-- the whole table instead. With role second nothing is skipped; a probe by
-- (track, artist) skips over role's three values. The same set of columns:
-- uniqueness and every ON CONFLICT are unchanged.

DO $$
DECLARE
    pk record;
BEGIN
    FOR pk IN
        SELECT c.conrelid::regclass AS tbl, c.conname, v.key_columns
        FROM (VALUES ('track_artists'::regclass, 'track_id, role, artist_id'),
                     ('album_artists'::regclass, 'album_id, role, artist_id')) AS v(tbl, key_columns)
        JOIN pg_constraint c ON c.conrelid = v.tbl AND c.contype = 'p'
        WHERE pg_get_constraintdef(c.oid) <> format('PRIMARY KEY (%s)', v.key_columns)
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I, ADD CONSTRAINT %I PRIMARY KEY (%s)',
                       pk.tbl, pk.conname, pk.conname, pk.key_columns);
    END LOOP;
END $$;
