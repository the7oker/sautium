-- Migration: Update library_stats view (handle optional tables)
-- ============================================================================

-- Create missing tables if they don't exist (older schema installs)
CREATE TABLE IF NOT EXISTS media_files (
    id SERIAL PRIMARY KEY,
    track_id UUID REFERENCES tracks(id) ON DELETE CASCADE,
    file_path TEXT UNIQUE NOT NULL,
    file_size_bytes BIGINT,
    duration_seconds NUMERIC(10, 2),
    sample_rate INTEGER,
    bit_depth INTEGER,
    channels INTEGER,
    codec VARCHAR(50),
    quality_source VARCHAR(50),
    is_analysis_source BOOLEAN DEFAULT false,
    play_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS track_lyrics (
    id SERIAL PRIMARY KEY,
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    source VARCHAR(50) NOT NULL,
    lyrics TEXT,
    synced_lyrics TEXT,
    is_instrumental BOOLEAN DEFAULT false,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_track_lyrics UNIQUE (track_id, source)
);

CREATE OR REPLACE VIEW library_stats AS
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
