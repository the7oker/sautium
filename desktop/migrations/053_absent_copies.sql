-- A local copy away (ROOT-1 fix 2, 2026-11-09): a Rescan that finds none of a
-- copy's files marks it absent instead of deleting it with its album; any walk
-- that sees one of its files marks it present again. The same DDL as the
-- album_variants block of 001_initial.sql, for nodes that ran the earlier
-- files. Idempotent: a no-op on the schema 001 just created.

ALTER TABLE album_variants ADD COLUMN IF NOT EXISTS absent_since TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_album_variants_absent
    ON album_variants(album_id) WHERE absent_since IS NOT NULL;
