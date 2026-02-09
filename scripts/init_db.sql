-- Music AI DJ Database Schema

-- Enable pgvector extension for embedding storage
CREATE EXTENSION IF NOT EXISTS vector;

-- Create enum for quality sources
CREATE TYPE quality_source_type AS ENUM ('CD', 'Vinyl', 'Hi-Res', 'MP3');

-- Embedding models table (metadata for CLAP and future models)
CREATE TABLE IF NOT EXISTS embedding_models (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    dimension INTEGER NOT NULL,

    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Embeddings table (512-dimensional vectors for CLAP)
CREATE TABLE IF NOT EXISTS embeddings (
    id SERIAL PRIMARY KEY,
    vector vector(512) NOT NULL,
    model_id INTEGER NOT NULL REFERENCES embedding_models(id) ON DELETE CASCADE,

    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Genres table (normalized, descriptions stored in external_metadata table)
CREATE TABLE IF NOT EXISTS genres (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,

    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Artists table (bio and metadata stored in external_metadata table)
CREATE TABLE IF NOT EXISTS artists (
    id SERIAL PRIMARY KEY,
    name VARCHAR(500) NOT NULL,

    -- External service IDs (for Phase 2)
    spotify_id VARCHAR(100),
    lastfm_id VARCHAR(100),
    musicbrainz_id VARCHAR(100),

    -- Basic metadata
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

    -- Album details
    release_year INTEGER,
    label VARCHAR(200),
    catalog_number VARCHAR(100),
    total_tracks INTEGER,

    -- Quality information
    quality_source quality_source_type DEFAULT 'CD',
    sample_rate INTEGER,
    bit_depth INTEGER,

    -- External service IDs (for Phase 2)
    spotify_id VARCHAR(100),
    musicbrainz_id VARCHAR(100),

    -- File system information
    directory_path TEXT NOT NULL UNIQUE,

    -- User data (for Phase 4)
    user_rating DECIMAL(3, 2) CHECK (user_rating >= 0 AND user_rating <= 5),
    user_notes TEXT,

    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tracks table
CREATE TABLE IF NOT EXISTS tracks (
    id SERIAL PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,

    -- Track details
    track_number INTEGER,
    disc_number INTEGER DEFAULT 1,
    duration_seconds DECIMAL(10, 2),

    -- Audio characteristics (from metadata)
    sample_rate INTEGER,
    bit_depth INTEGER,
    bitrate INTEGER,
    channels INTEGER,

    -- File information
    file_path TEXT NOT NULL UNIQUE,
    file_size_bytes BIGINT,
    file_format VARCHAR(10) DEFAULT 'FLAC',

    -- Audio embedding reference
    embedding_id INTEGER REFERENCES embeddings(id) ON DELETE SET NULL,

    -- External service IDs (for Phase 2)
    spotify_id VARCHAR(100),
    isrc VARCHAR(20),
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
    user_tags TEXT[],

    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Track genres junction table (many-to-many: track can have multiple genres)
CREATE TABLE IF NOT EXISTS track_genres (
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,

    PRIMARY KEY (track_id, genre_id)
);

-- Track artists junction table (for features, compilations, multiple artists)
CREATE TABLE IF NOT EXISTS track_artists (
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    role VARCHAR(50) DEFAULT 'primary',

    PRIMARY KEY (track_id, artist_id, role)
);


-- Create indexes for performance

-- Embedding model indexes
CREATE INDEX IF NOT EXISTS idx_embedding_models_name ON embedding_models(name);

-- Embedding indexes
-- Vector similarity search index (using HNSW for efficient similarity queries)
CREATE INDEX IF NOT EXISTS idx_embeddings_vector ON embeddings
    USING hnsw (vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_embeddings_model_id ON embeddings(model_id);

-- Genre indexes
CREATE INDEX IF NOT EXISTS idx_genres_name ON genres(name);

-- Artist indexes
CREATE INDEX IF NOT EXISTS idx_artists_name ON artists(name);
CREATE INDEX IF NOT EXISTS idx_artists_spotify_id ON artists(spotify_id);

-- Album indexes
CREATE INDEX IF NOT EXISTS idx_albums_title ON albums(title);
CREATE INDEX IF NOT EXISTS idx_albums_release_year ON albums(release_year);
CREATE INDEX IF NOT EXISTS idx_albums_quality_source ON albums(quality_source);

-- Track indexes
CREATE INDEX IF NOT EXISTS idx_tracks_title ON tracks(title);
CREATE INDEX IF NOT EXISTS idx_tracks_album_id ON tracks(album_id);
CREATE INDEX IF NOT EXISTS idx_tracks_file_path ON tracks(file_path);
CREATE INDEX IF NOT EXISTS idx_tracks_play_count ON tracks(play_count);
CREATE INDEX IF NOT EXISTS idx_tracks_embedding_id ON tracks(embedding_id);

-- Track genres indexes
CREATE INDEX IF NOT EXISTS idx_track_genres_track_id ON track_genres(track_id);
CREATE INDEX IF NOT EXISTS idx_track_genres_genre_id ON track_genres(genre_id);

-- Track artists indexes
CREATE INDEX IF NOT EXISTS idx_track_artists_track_id ON track_artists(track_id);
CREATE INDEX IF NOT EXISTS idx_track_artists_artist_id ON track_artists(artist_id);


-- Create function to automatically update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create triggers for automatic timestamp updates
CREATE TRIGGER update_embedding_models_updated_at BEFORE UPDATE ON embedding_models
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_embeddings_updated_at BEFORE UPDATE ON embeddings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_genres_updated_at BEFORE UPDATE ON genres
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_artists_updated_at BEFORE UPDATE ON artists
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_albums_updated_at BEFORE UPDATE ON albums
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_tracks_updated_at BEFORE UPDATE ON tracks
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Create view for quick library statistics
CREATE OR REPLACE VIEW library_stats AS
SELECT
    (SELECT COUNT(*) FROM artists) as total_artists,
    (SELECT COUNT(*) FROM albums) as total_albums,
    (SELECT COUNT(*) FROM tracks) as total_tracks,
    (SELECT COUNT(*) FROM tracks WHERE embedding_id IS NOT NULL) as tracks_with_embeddings,
    (SELECT SUM(duration_seconds) FROM tracks) as total_duration_seconds,
    (SELECT SUM(file_size_bytes) FROM tracks) as total_file_size_bytes,
    (SELECT COUNT(*) FROM genres) as unique_genres;

-- Insert initial data verification
SELECT 'Database schema initialized successfully!' as status;
SELECT * FROM library_stats;
