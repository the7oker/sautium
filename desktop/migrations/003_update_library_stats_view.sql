-- Migration: Update library_stats view (handle optional tables)
-- ============================================================================

-- Drop and recreate potentially broken tables from failed migration attempts
-- (previous run may have left media_files with incompatible FK constraints)
DROP TABLE IF EXISTS media_files CASCADE;
DROP TABLE IF EXISTS track_lyrics CASCADE;

CREATE TABLE media_files (
    id SERIAL PRIMARY KEY,
    file_path TEXT UNIQUE NOT NULL,
    duration_seconds NUMERIC(10, 2),
    file_size_bytes BIGINT
);

CREATE TABLE track_lyrics (
    id SERIAL PRIMARY KEY,
    track_id INTEGER,
    source VARCHAR(50),
    lyrics TEXT
);

-- Drop old view first (column count may differ)
DROP VIEW IF EXISTS library_stats;

CREATE VIEW library_stats AS
SELECT
    (SELECT COUNT(*) FROM artists) as total_artists,
    (SELECT COUNT(*) FROM albums) as total_albums,
    (SELECT COUNT(*) FROM tracks) as total_tracks,
    (SELECT COUNT(*) FROM media_files) as total_media_files,
    (SELECT COUNT(*) FROM embeddings) as tracks_with_embeddings,
    (SELECT COUNT(*) FROM track_lyrics) as tracks_with_lyrics,
    (SELECT SUM(duration_seconds) FROM media_files) as total_duration_seconds,
    (SELECT SUM(file_size_bytes) FROM media_files) as total_file_size_bytes,
    (SELECT COUNT(*) FROM genres) as unique_genres;
