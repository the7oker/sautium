-- 078 — PLAYBACK-UI-1 (2026-11-25): the same DDL as 001_initial.sql's
-- blocks, for nodes that ran the earlier files. Idempotent: a no-op on the
-- schema 001 just created.
--
-- session_tracks.stop_seconds: where an archived session's listen stopped —
-- the second of the slot that last sounded (playback.tracker.stop_in), kept
-- on that slot, one per session at most. Listening history draws it as the
-- blue dot and its Resume starts there (B-158). Sessions archived before
-- this have none: nothing recorded where they stopped.

ALTER TABLE session_tracks ADD COLUMN IF NOT EXISTS stop_seconds NUMERIC(10, 2);
DO $$ BEGIN
    ALTER TABLE session_tracks ADD CONSTRAINT session_tracks_stop_seconds_check
        CHECK (stop_seconds >= 0);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE UNIQUE INDEX IF NOT EXISTS uq_session_tracks_one_stop
    ON session_tracks(session_id) WHERE stop_seconds IS NOT NULL;
