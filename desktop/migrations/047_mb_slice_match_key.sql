-- The dump node's slice cache keyed by what a build covers (2026-11-03): the
-- same DDL as the mb_slice_blobs block of 001_initial.sql, for nodes that
-- ran the earlier files. Idempotent: a no-op on the schema 001 just created.
-- Existing rows keep a NULL key; a dump node rebuilds and keys a name the
-- next time it is asked.
ALTER TABLE mb_slice_blobs ADD COLUMN IF NOT EXISTS match_key TEXT;
CREATE INDEX IF NOT EXISTS idx_mb_slice_blobs_match
    ON mb_slice_blobs (match_key, dump_version) WHERE match_key IS NOT NULL;
