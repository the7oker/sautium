-- 070 — CREDITS-1 step 2b (2026-11-21): the same DDL as 001_initial.sql's
-- blocks, for nodes that ran the earlier files. Idempotent: a no-op on the
-- schema 001 just created.
--
-- media_files / hqp_library_files: the composers a file's tags name, split
-- and before naming (credits.split_names); a track's composer credits
-- (track_credits, source 'tag') derive from them. NULL = read by a reader
-- before this one: such a file contributes nothing until it is read again
-- (scanner.TAGS_READ_REV 2 makes it a changed file to the next scan).
-- hqp_endpoints: the revision of what the HQPlayer sync reads of each file
-- (hqp_library.LIBRARY_READ_REV): a library last read under an older one is
-- read whole by its next sync, its known files included.
-- credits_due: the tracks whose composer derive failed, taken by the next
-- transaction that derives.

ALTER TABLE media_files ADD COLUMN IF NOT EXISTS raw_composers TEXT[];
ALTER TABLE hqp_library_files ADD COLUMN IF NOT EXISTS raw_composers TEXT[];
ALTER TABLE hqp_endpoints ADD COLUMN IF NOT EXISTS library_read_rev SMALLINT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS credits_due (
    track_id UUID PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE,
    failed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
