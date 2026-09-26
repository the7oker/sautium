-- A write to the listening history wakes the backend (2026-09-26). Mirrors
-- the 001 baseline.
--
-- The Home Recommendations ranking is a function of the listening history
-- alone (every clock runs from the newest listen), so the backend rebuilds it
-- when the history changes and Home reads the stored result
-- (backend/routers/home.py) — computed per visit, it cost eight HNSW walks
-- each time Home opened. The trigger makes every writer announce itself: the
-- play tracker, the scrobble canon, a Last.fm history removal, a life-data
-- merge run from the CLI, a track id rewrite cascading in. Per statement, so
-- an import batch or a merge is one wake.

CREATE OR REPLACE FUNCTION notify_listens_changed() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify('sautium_listens', '');
    RETURN NULL;
END $$ LANGUAGE plpgsql;

DO $$ BEGIN
    CREATE TRIGGER trg_listening_history_notify
    AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON listening_history
    FOR EACH STATEMENT EXECUTE FUNCTION notify_listens_changed();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
