-- 080 — CANON-TRACKS-1 (2026-11-26): the same DDL as 001_initial.sql's
-- tracks block, for nodes that ran the earlier files. Idempotent: a no-op on
-- the schema 001 just created.
--
-- tracks.occurrence: the place of a track's title among the positions of
-- one tracklist that carry it — an input of its id (uuid_utils.track_uuid,
-- `song:…#k` for the k-th). Every row before it is a first occurrence; the
-- tracks two positions of one copy or one release shared are split by
-- db_migrate's track_occurrences_v1 (canon.occurrence).

ALTER TABLE tracks ADD COLUMN IF NOT EXISTS occurrence SMALLINT NOT NULL DEFAULT 1;
DO $$ BEGIN
    ALTER TABLE tracks ADD CONSTRAINT tracks_occurrence_check CHECK (occurrence >= 1);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
