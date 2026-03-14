-- Migration 003: Fix listening stats integrity
--
-- Problems fixed:
--   1. 303/328 listening_history records have orphaned media_file_id (old SERIAL IDs after re-index)
--   2. playback_tracker writes to track_stats with wrong columns (track_stats is for Last.fm data)
--   3. No table for local play statistics — create local_play_stats
--   4. listening_history.track_id is nullable but should be NOT NULL (all records have valid track_id)
--   5. 5 artist FK constraints missing ON UPDATE CASCADE (breaks artist normalization)
--
-- Safe to run multiple times (idempotent).

BEGIN;

-- ============================================================
-- 1. Create local_play_stats table
-- ============================================================

CREATE TABLE IF NOT EXISTS local_play_stats (
    track_id UUID PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE,
    play_count INTEGER NOT NULL DEFAULT 0,
    skip_count INTEGER NOT NULL DEFAULT 0,
    total_listen_time NUMERIC(12, 2) NOT NULL DEFAULT 0,
    avg_percent_listened NUMERIC(5, 2) NOT NULL DEFAULT 0,
    last_played_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_local_play_stats_last_played ON local_play_stats(last_played_at);
CREATE INDEX IF NOT EXISTS idx_local_play_stats_play_count ON local_play_stats(play_count);

-- ============================================================
-- 2. Populate local_play_stats from existing listening_history
-- ============================================================

INSERT INTO local_play_stats (track_id, play_count, skip_count, total_listen_time, avg_percent_listened, last_played_at)
SELECT
    track_id,
    COUNT(*) FILTER (WHERE completed = true),
    COUNT(*) FILTER (WHERE skipped = true),
    COALESCE(SUM(duration_listened), 0),
    COALESCE(AVG(percent_listened), 0),
    MAX(started_at)
FROM listening_history
WHERE track_id IS NOT NULL
GROUP BY track_id
ON CONFLICT (track_id) DO NOTHING;

-- ============================================================
-- 3. Fix orphaned listening_history.media_file_id
--    Pick best media_file per track: is_analysis_source first, then highest sample_rate
-- ============================================================

WITH best_files AS (
    SELECT DISTINCT ON (track_id) id, track_id
    FROM media_files
    ORDER BY track_id, is_analysis_source DESC, sample_rate DESC NULLS LAST, id
)
UPDATE listening_history lh
SET media_file_id = bf.id
FROM best_files bf
WHERE lh.track_id = bf.track_id
  AND lh.media_file_id NOT IN (SELECT id FROM media_files);

-- ============================================================
-- 4. Make listening_history.track_id NOT NULL + fix FK
-- ============================================================

-- Change ON DELETE from SET NULL to CASCADE (NOT NULL + SET NULL is contradictory)
ALTER TABLE listening_history DROP CONSTRAINT IF EXISTS listening_history_track_id_fkey;
ALTER TABLE listening_history
    ADD CONSTRAINT listening_history_track_id_fkey
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE;

ALTER TABLE listening_history ALTER COLUMN track_id SET NOT NULL;

-- Composite index for per-track history queries
CREATE INDEX IF NOT EXISTS idx_listening_history_track_started
    ON listening_history(track_id, started_at DESC);

-- ============================================================
-- 5. Fix artist FK cascades: add ON UPDATE CASCADE
--    (needed for artist normalization to cascade UUID changes)
-- ============================================================

-- album_artists
ALTER TABLE album_artists DROP CONSTRAINT IF EXISTS album_artists_artist_id_fkey;
ALTER TABLE album_artists
    ADD CONSTRAINT album_artists_artist_id_fkey
    FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE;

-- track_artists
ALTER TABLE track_artists DROP CONSTRAINT IF EXISTS track_artists_artist_id_fkey;
ALTER TABLE track_artists
    ADD CONSTRAINT track_artists_artist_id_fkey
    FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE;

-- artist_bios
ALTER TABLE artist_bios DROP CONSTRAINT IF EXISTS artist_bios_artist_id_fkey;
ALTER TABLE artist_bios
    ADD CONSTRAINT artist_bios_artist_id_fkey
    FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE;

-- artist_tags
ALTER TABLE artist_tags DROP CONSTRAINT IF EXISTS artist_tags_artist_id_fkey;
ALTER TABLE artist_tags
    ADD CONSTRAINT artist_tags_artist_id_fkey
    FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE;

-- similar_artists (both FK columns)
ALTER TABLE similar_artists DROP CONSTRAINT IF EXISTS similar_artists_artist_id_fkey;
ALTER TABLE similar_artists
    ADD CONSTRAINT similar_artists_artist_id_fkey
    FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE;

ALTER TABLE similar_artists DROP CONSTRAINT IF EXISTS similar_artists_similar_artist_id_fkey;
ALTER TABLE similar_artists
    ADD CONSTRAINT similar_artists_similar_artist_id_fkey
    FOREIGN KEY (similar_artist_id) REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE;

-- artist_bio_embeddings
ALTER TABLE artist_bio_embeddings DROP CONSTRAINT IF EXISTS artist_bio_embeddings_artist_id_fkey;
ALTER TABLE artist_bio_embeddings
    ADD CONSTRAINT artist_bio_embeddings_artist_id_fkey
    FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE;

COMMIT;
