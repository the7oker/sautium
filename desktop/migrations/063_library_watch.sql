-- 063 — LIBRARY-WATCH (2026-11-17): the file's stat at its last read and
-- the analysis passes' refusal of a decode — the same DDL as the media_files
-- block of 001_initial.sql, for nodes that ran the earlier files. Idempotent:
-- a no-op on the schema 001 just created.

ALTER TABLE media_files ADD COLUMN IF NOT EXISTS stat_mtime_ns BIGINT;
ALTER TABLE media_files ADD COLUMN IF NOT EXISTS stat_ctime_ns BIGINT;
ALTER TABLE media_files ADD COLUMN IF NOT EXISTS decode_refused BOOLEAN NOT NULL DEFAULT FALSE;
