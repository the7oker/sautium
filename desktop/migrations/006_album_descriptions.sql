-- 006 — album prose comes back (2026-08-28). The old album_info was dropped in
-- 4248e56 because it also carried listeners/playcount that duplicated
-- track_stats and sat in the sync contour for an entity that never syncs.
-- This is only the description half, and it is LOCAL-ONLY by construction: no
-- seal columns, no sync handler, no embedding. Mirrors the 001 block verbatim,
-- so it is a no-op on a database that just ran 001.

CREATE TABLE IF NOT EXISTS album_descriptions (
    id SERIAL PRIMARY KEY,
    album_id UUID NOT NULL REFERENCES albums(id) ON DELETE CASCADE ON UPDATE CASCADE,
    source VARCHAR(50) NOT NULL,
    summary TEXT,
    content TEXT,
    url TEXT,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_album_descriptions UNIQUE (album_id, source),
    CONSTRAINT chk_has_album_description CHECK (summary IS NOT NULL OR content IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_album_descriptions_source ON album_descriptions(source);

DO $$ BEGIN CREATE TRIGGER trg_album_descriptions_updated_at BEFORE UPDATE ON album_descriptions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
