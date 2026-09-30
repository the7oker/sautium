-- A file leaving the library wakes the backend (2026-09-30). Mirrors the 001
-- baseline.
--
-- The canonical play queue binds each owned slot to a media_files row (its
-- id and path), in memory and in the persisted `player.queue` setting. A row
-- deleted under it — a rescan pruning a file that was moved or deleted, a
-- CUE image superseded, a track merge cascading — left the slot naming a row
-- that no longer exists: the next Play archived the old queue into
-- session_tracks and died on the foreign key (a 500 on every Play), Now
-- Playing asked for the file's detail and got a 404, and the path no longer
-- opened. The trigger makes every deleter announce itself, whichever process
-- runs it; the playback manager re-binds such slots to a live copy of their
-- track (backend/playback/manager.py, rebind_files). Per statement, and only
-- when the statement removed rows: a prune of thousands is one wake, a
-- reconcile that matched nothing is none.

CREATE OR REPLACE FUNCTION notify_files_removed() RETURNS TRIGGER AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM gone) THEN
        PERFORM pg_notify('sautium_files_removed', '');
    END IF;
    RETURN NULL;
END $$ LANGUAGE plpgsql;

DO $$ BEGIN
    CREATE TRIGGER trg_media_files_removed_notify
    AFTER DELETE ON media_files
    REFERENCING OLD TABLE AS gone
    FOR EACH STATEMENT EXECUTE FUNCTION notify_files_removed();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
