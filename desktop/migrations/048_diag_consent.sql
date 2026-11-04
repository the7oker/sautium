-- The person's own decision on a support request (2026-11-04): a warrant that
-- names a content scope (conversations, logs, playback traces) waits until the
-- person sends it from the request sheet; `decided_at` is when they did,
-- `granted` the content scopes they ticked (empty: only the structural part
-- goes). The same DDL as the diag_warrants block of 001_initial.sql, for nodes
-- that ran the earlier files. Idempotent: a no-op on the schema 001 just
-- created.
ALTER TABLE diag_warrants ADD COLUMN IF NOT EXISTS decided_at TIMESTAMPTZ;
ALTER TABLE diag_warrants ADD COLUMN IF NOT EXISTS granted TEXT[];
