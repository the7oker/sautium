-- 074 — PI-NODE-2D item 3 (2026-11-24): the same DDL as 001_initial.sql's
-- blocks, for nodes that ran the earlier files. Idempotent: a no-op on the
-- schema 001 just created.
--
-- album_variants.entered_at: when the copy entered this library — the start
-- of the scan round (or the HQPlayer library's sync) that made its row, one
-- instant for every copy a round makes, written at the insert and never
-- after. Home's "New in my collection" orders by it; the files' birth time
-- (arrived_at) orders the copies of one round. The birth time alone had
-- ordered the shelf: a stick whose file system keeps the day its files were
-- copied, scanned sixteen minutes after another, sorted two days before it.
--
-- The backfill gives every copy made before this its arrival (else its row's
-- creation), so no shelf reshuffles under a person at the update: the new
-- rule governs what enters from then on.
--
-- The index is the shelf's key itself, so a page reads a page; nothing orders
-- by a copy's arrival alone any more, and that index goes.

ALTER TABLE album_variants ADD COLUMN IF NOT EXISTS entered_at TIMESTAMPTZ;
UPDATE album_variants SET entered_at = COALESCE(arrived_at, created_at, now())
 WHERE entered_at IS NULL;
ALTER TABLE album_variants ALTER COLUMN entered_at SET DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE album_variants ALTER COLUMN entered_at SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_album_variants_entered_desc
    ON album_variants (entered_at DESC, (COALESCE(arrived_at, entered_at)) DESC, album_id DESC);
DROP INDEX IF EXISTS idx_album_variants_arrived_at_desc;
