-- 059 — the slice blobs a node keeps for itself alone (2026-11-14). A blob
-- fetched for the owner's imported Last.fm history, or for his own lookup
-- (Discovery's mint, the assistant's resolve, an opened artist page's
-- ListenBrainz statistics), is passed on to no other node: a re-served blob
-- answers "did you fetch this?" to anyone who asks. The origin is read when
-- the blob is fetched (mb_slice_queries.LOCAL_ONLY_SQL,
-- lb_slice_queries.LOCAL_ONLY_SQL); the blobs kept before this delta are
-- marked once by the same rules (backend/db_migrate.py `slice_origins_v1`).
-- The same DDL as the mb_slice_blobs and lb_slice_blobs blocks of
-- 001_initial.sql, for nodes that ran the earlier files. Idempotent: a no-op
-- on the schema 001 just created.

ALTER TABLE mb_slice_blobs ADD COLUMN IF NOT EXISTS local_only BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE lb_slice_blobs ADD COLUMN IF NOT EXISTS local_only BOOLEAN NOT NULL DEFAULT FALSE;
