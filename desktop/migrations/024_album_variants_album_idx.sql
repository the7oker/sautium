-- Albums find their variants by album id through an index again (2026-09-26).
-- Mirrors the 001 baseline, which has declared it since the canonical-tracks
-- schema.
--
-- The master's database is older than the unified 001 and never got it, so
-- every album tile's artist, cover and first file (the Home shelves, per
-- album) and every deleted album's FK cascade scanned album_variants whole.
-- A no-op where the index already exists.

CREATE INDEX IF NOT EXISTS idx_album_variants_album_id ON album_variants(album_id);
