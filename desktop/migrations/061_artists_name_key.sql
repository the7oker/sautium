-- 061 — an artist by the slice key of its name (2026-11-14). Whether a
-- fetched slice blob is kept for this node alone is read at the fetch from the
-- artist its name is (mb_slice_queries.LOCAL_ONLY_SQL: lower(btrim(name))),
-- and the one-time marking of the blobs kept before reads it for every blob;
-- without an index each read scanned the artists. The same DDL as the
-- artists index block of 001_initial.sql, for nodes that ran the earlier
-- files. Idempotent: a no-op on the schema 001 just created.

CREATE INDEX IF NOT EXISTS idx_artists_name_key ON artists (lower(btrim(name)));
