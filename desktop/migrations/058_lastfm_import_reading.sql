-- 058 — an import session that is reading pages, for the MB slice stream
-- (2026-11-14). The names an imported Last.fm history waits on join the one
-- stream of names the MB slice cycle asks about — drawn at random with every
-- other reason the node asks, in one form — once the session that read them
-- stops: the walk reads the newest scrobbles first, and names drawn while it
-- reads would be the most recent listens'. The same DDL as the
-- lastfm_import block of 001_initial.sql, for nodes that ran the earlier
-- files. Idempotent: a no-op on the schema 001 just created.

ALTER TABLE lastfm_import ADD COLUMN IF NOT EXISTS reading_since TIMESTAMPTZ;
