-- Music AI DJ Database Schema
-- Normalized schema for music library with audio embeddings

-- Enable pgvector extension for embedding storage
CREATE EXTENSION IF NOT EXISTS vector;

-- Create enum for quality sources
CREATE TYPE quality_source_type AS ENUM ('CD', 'Vinyl', 'Hi-Res', 'MP3');

-- Artists table
CREATE TABLE IF NOT EXISTS artists (
    id SERIAL PRIMARY KEY,
    name VARCHAR(500) NOT NULL,
    sort_name VARCHAR(500),  -- For proper alphabetical sorting

    -- External service IDs (for Phase 2)
    spotify_id VARCHAR(100),
    lastfm_id VARCHAR(100),
    musicbrainz_id VARCHAR(100),

    -- Metadata
    bio TEXT,
    genre VARCHAR(200),
    country VARCHAR(100),

    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Constraints
    UNIQUE(name)
);

-- Albums table
CREATE TABLE IF NOT EXISTS albums (
    id SERIAL PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,

    -- Album details
    release_year INTEGER,
    genre VARCHAR(200),
    label VARCHAR(200),
    catalog_number VARCHAR(100),
    total_tracks INTEGER,

    -- Quality information
    quality_source quality_source_type DEFAULT 'CD',
    sample_rate INTEGER,  -- Hz (e.g., 44100, 96000, 192000)
    bit_depth INTEGER,    -- bits (e.g., 16, 24)

    -- External service IDs (for Phase 2)
    spotify_id VARCHAR(100),
    musicbrainz_id VARCHAR(100),

    -- File system information
    directory_path TEXT NOT NULL,

    -- User data (for Phase 4)
    user_rating DECIMAL(3, 2) CHECK (user_rating >= 0 AND user_rating <= 5),
    user_notes TEXT,

    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Constraints
    UNIQUE(title, artist_id)
);

-- Tracks table
CREATE TABLE IF NOT EXISTS tracks (
    id SERIAL PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,

    -- Track details
    track_number INTEGER,
    disc_number INTEGER DEFAULT 1,
    duration_seconds DECIMAL(10, 2),  -- Track length in seconds
    genre VARCHAR(200),

    -- Audio characteristics (from metadata)
    sample_rate INTEGER,
    bit_depth INTEGER,
    bitrate INTEGER,  -- kbps
    channels INTEGER,  -- 1 for mono, 2 for stereo, etc.

    -- File information
    file_path TEXT NOT NULL UNIQUE,
    file_size_bytes BIGINT,
    file_format VARCHAR(10) DEFAULT 'FLAC',

    -- Audio embedding (512-dimensional vector for CLAP)
    embedding vector(512),
    embedding_model VARCHAR(100),  -- e.g., 'laion/clap-htsat-unfused'
    embedding_generated_at TIMESTAMP,

    -- External service IDs (for Phase 2)
    spotify_id VARCHAR(100),
    isrc VARCHAR(20),  -- International Standard Recording Code
    musicbrainz_id VARCHAR(100),

    -- Spotify audio features (for Phase 2)
    spotify_tempo DECIMAL(6, 2),
    spotify_energy DECIMAL(3, 2) CHECK (spotify_energy >= 0 AND spotify_energy <= 1),
    spotify_danceability DECIMAL(3, 2) CHECK (spotify_danceability >= 0 AND spotify_danceability <= 1),
    spotify_valence DECIMAL(3, 2) CHECK (spotify_valence >= 0 AND spotify_valence <= 1),
    spotify_acousticness DECIMAL(3, 2) CHECK (spotify_acousticness >= 0 AND spotify_acousticness <= 1),
    spotify_instrumentalness DECIMAL(3, 2) CHECK (spotify_instrumentalness >= 0 AND spotify_instrumentalness <= 1),
    spotify_liveness DECIMAL(3, 2) CHECK (spotify_liveness >= 0 AND spotify_liveness <= 1),
    spotify_speechiness DECIMAL(3, 2) CHECK (spotify_speechiness >= 0 AND spotify_speechiness <= 1),
    spotify_loudness DECIMAL(6, 2),
    spotify_key INTEGER,
    spotify_mode INTEGER,
    spotify_time_signature INTEGER,

    -- User data (for Phase 4)
    play_count INTEGER DEFAULT 0,
    last_played_at TIMESTAMP,
    user_rating DECIMAL(3, 2) CHECK (user_rating >= 0 AND user_rating <= 5),
    user_notes TEXT,
    user_tags TEXT[],  -- Array of custom tags

    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Track artists junction table (for features, compilations, multiple artists)
CREATE TABLE IF NOT EXISTS track_artists (
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    role VARCHAR(50) DEFAULT 'primary',  -- primary, featured, remixer, composer, etc.

    PRIMARY KEY (track_id, artist_id, role)
);

-- Playlists table (for Phase 3/4)
CREATE TABLE IF NOT EXISTS playlists (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    description TEXT,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Playlist tracks junction table (for Phase 3/4)
CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (playlist_id, track_id),
    UNIQUE (playlist_id, position)
);

-- Create indexes for performance

-- Artist indexes
CREATE INDEX IF NOT EXISTS idx_artists_name ON artists(name);
CREATE INDEX IF NOT EXISTS idx_artists_spotify_id ON artists(spotify_id);

-- Album indexes
CREATE INDEX IF NOT EXISTS idx_albums_title ON albums(title);
CREATE INDEX IF NOT EXISTS idx_albums_artist_id ON albums(artist_id);
CREATE INDEX IF NOT EXISTS idx_albums_genre ON albums(genre);
CREATE INDEX IF NOT EXISTS idx_albums_release_year ON albums(release_year);
CREATE INDEX IF NOT EXISTS idx_albums_quality_source ON albums(quality_source);

-- Track indexes
CREATE INDEX IF NOT EXISTS idx_tracks_title ON tracks(title);
CREATE INDEX IF NOT EXISTS idx_tracks_album_id ON tracks(album_id);
CREATE INDEX IF NOT EXISTS idx_tracks_genre ON tracks(genre);
CREATE INDEX IF NOT EXISTS idx_tracks_file_path ON tracks(file_path);
CREATE INDEX IF NOT EXISTS idx_tracks_play_count ON tracks(play_count);

-- Vector similarity search index (using HNSW for efficient similarity queries)
CREATE INDEX IF NOT EXISTS idx_tracks_embedding ON tracks
    USING hnsw (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL;

-- Track artists indexes
CREATE INDEX IF NOT EXISTS idx_track_artists_track_id ON track_artists(track_id);
CREATE INDEX IF NOT EXISTS idx_track_artists_artist_id ON track_artists(artist_id);

-- Playlist indexes
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_playlist_id ON playlist_tracks(playlist_id);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_track_id ON playlist_tracks(track_id);

-- Create function to automatically update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create triggers for automatic timestamp updates
CREATE TRIGGER update_artists_updated_at BEFORE UPDATE ON artists
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_albums_updated_at BEFORE UPDATE ON albums
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_tracks_updated_at BEFORE UPDATE ON tracks
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_playlists_updated_at BEFORE UPDATE ON playlists
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Create view for quick library statistics
CREATE OR REPLACE VIEW library_stats AS
SELECT
    (SELECT COUNT(*) FROM artists) as total_artists,
    (SELECT COUNT(*) FROM albums) as total_albums,
    (SELECT COUNT(*) FROM tracks) as total_tracks,
    (SELECT COUNT(*) FROM tracks WHERE embedding IS NOT NULL) as tracks_with_embeddings,
    (SELECT SUM(duration_seconds) FROM tracks) as total_duration_seconds,
    (SELECT SUM(file_size_bytes) FROM tracks) as total_file_size_bytes,
    (SELECT COUNT(DISTINCT genre) FROM tracks WHERE genre IS NOT NULL) as unique_genres;

-- Insert initial data verification
SELECT 'Database schema initialized successfully!' as status;
SELECT * FROM library_stats;
