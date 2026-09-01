-- 008 — curated cold-start picks (2026-09-01). The 52-album seed layer a
-- fresh node imports at first start (backend/seed_import.py) so Home has
-- recommendations before the first listen. LOCAL-ONLY like
-- album_descriptions: albums never sync by UUID, so no seal columns and no
-- sync handler. tier: 1 = list A (bridge gems), 2 = list B (taste palette),
-- 3 = honourable mentions, 4 = rotation pool. rank = global 1..52 curation
-- order. Mirrors the 001 block verbatim, so it is a no-op on a database
-- that just ran 001.

CREATE TABLE IF NOT EXISTS seed_picks (
    album_id UUID PRIMARY KEY REFERENCES albums(id) ON DELETE CASCADE ON UPDATE CASCADE,
    tier SMALLINT NOT NULL CHECK (tier BETWEEN 1 AND 4),
    rank SMALLINT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
