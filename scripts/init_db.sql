-- Music AI DJ Database Schema
-- Canonical entities (UUID PKs) + Physical files (SERIAL PKs)

-- Required extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ─────────────────────────────────────────────────────────────────────────
-- Embedding models (shared metadata)
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS embedding_models (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    dimension INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_embedding_models_name ON embedding_models(name);

-- ─────────────────────────────────────────────────────────────────────────
-- Canonical entities (UUID PKs, shareable across users)
-- ─────────────────────────────────────────────────────────────────────────

-- Artists
CREATE TABLE IF NOT EXISTS artists (
    id UUID PRIMARY KEY,
    name VARCHAR(500) NOT NULL UNIQUE,
    lastfm_id VARCHAR(100),
    musicbrainz_id VARCHAR(100),
    country VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_artists_name ON artists(name);
CREATE INDEX IF NOT EXISTS idx_artists_name_trgm ON artists USING gin (name gin_trgm_ops);

-- Albums (canonical — no physical file info)
CREATE TABLE IF NOT EXISTS albums (
    id UUID PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    release_year INTEGER,
    label VARCHAR(200),
    catalog_number VARCHAR(100),
    total_tracks INTEGER,
    musicbrainz_id VARCHAR(100),
    lastfm_id VARCHAR(100),
    user_rating NUMERIC(3, 2) CHECK (user_rating >= 0 AND user_rating <= 5),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_albums_title ON albums(title);
CREATE INDEX IF NOT EXISTS idx_albums_title_trgm ON albums USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_albums_release_year ON albums(release_year);
CREATE INDEX IF NOT EXISTS idx_albums_lastfm_id ON albums(lastfm_id);

-- Tracks (canonical — one per unique title+artist)
CREATE TABLE IF NOT EXISTS tracks (
    id UUID PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tracks_title ON tracks(title);
CREATE INDEX IF NOT EXISTS idx_tracks_title_trgm ON tracks USING gin (title gin_trgm_ops);

-- ─────────────────────────────────────────────────────────────────────────
-- Association tables (canonical)
-- ─────────────────────────────────────────────────────────────────────────

-- Track-Artist (many-to-many with role)
CREATE TABLE IF NOT EXISTS track_artists (
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    role VARCHAR(50) DEFAULT 'primary',
    PRIMARY KEY (track_id, artist_id, role)
);

CREATE INDEX IF NOT EXISTS idx_track_artists_track_id ON track_artists(track_id);
CREATE INDEX IF NOT EXISTS idx_track_artists_artist_id ON track_artists(artist_id);

-- Album-Artist (many-to-many with role)
CREATE TABLE IF NOT EXISTS album_artists (
    album_id UUID NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    role VARCHAR(50) DEFAULT 'primary',
    PRIMARY KEY (album_id, artist_id, role)
);

CREATE INDEX IF NOT EXISTS idx_album_artists_album_id ON album_artists(album_id);
CREATE INDEX IF NOT EXISTS idx_album_artists_artist_id ON album_artists(artist_id);

-- Genres
CREATE TABLE IF NOT EXISTS genres (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_genres_name ON genres(name);

-- Track-Genre (many-to-many)
CREATE TABLE IF NOT EXISTS track_genres (
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
    PRIMARY KEY (track_id, genre_id)
);

CREATE INDEX IF NOT EXISTS idx_track_genres_track_id ON track_genres(track_id);
CREATE INDEX IF NOT EXISTS idx_track_genres_genre_id ON track_genres(genre_id);

-- ─────────────────────────────────────────────────────────────────────────
-- Physical entities (SERIAL PKs, per-user)
-- ─────────────────────────────────────────────────────────────────────────

-- Album variants (CD, Vinyl, Hi-Res editions)
CREATE TABLE IF NOT EXISTS album_variants (
    id SERIAL PRIMARY KEY,
    album_id UUID NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    directory_path TEXT NOT NULL UNIQUE,
    sample_rate INTEGER,
    bit_depth INTEGER,
    is_lossless BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_album_variants_album_id ON album_variants(album_id);
CREATE INDEX IF NOT EXISTS idx_album_variants_directory ON album_variants(directory_path);

-- Media files (physical audio files on disk)
CREATE TABLE IF NOT EXISTS media_files (
    id SERIAL PRIMARY KEY,
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    album_variant_id INTEGER NOT NULL REFERENCES album_variants(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL UNIQUE,
    file_format VARCHAR(10) DEFAULT 'FLAC',
    is_lossless BOOLEAN DEFAULT TRUE,
    file_size_bytes BIGINT,
    file_modified_at TIMESTAMP,
    sample_rate INTEGER,
    bit_depth INTEGER,
    bitrate INTEGER,
    channels INTEGER,
    duration_seconds NUMERIC(10, 2),
    track_number INTEGER,
    disc_number INTEGER DEFAULT 1,
    is_analysis_source BOOLEAN DEFAULT FALSE,
    play_count INTEGER DEFAULT 0,
    last_played_at TIMESTAMP,
    isrc VARCHAR(20),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_media_files_track_id ON media_files(track_id);
CREATE INDEX IF NOT EXISTS idx_media_files_album_variant_id ON media_files(album_variant_id);
CREATE INDEX IF NOT EXISTS idx_media_files_file_path ON media_files(file_path);
CREATE INDEX IF NOT EXISTS idx_media_files_play_count ON media_files(play_count);
CREATE INDEX IF NOT EXISTS idx_media_files_analysis_source ON media_files(track_id, is_analysis_source)
    WHERE is_analysis_source = true;

-- ─────────────────────────────────────────────────────────────────────────
-- Embeddings & Analysis (linked to tracks, not files)
-- ─────────────────────────────────────────────────────────────────────────

-- Audio embeddings (512-dimensional CLAP vectors)
CREATE TABLE IF NOT EXISTS embeddings (
    id SERIAL PRIMARY KEY,
    vector vector(512) NOT NULL,
    model_id INTEGER NOT NULL REFERENCES embedding_models(id) ON DELETE CASCADE,
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    source_bit_depth INTEGER,
    source_sample_rate INTEGER,
    source_is_lossless BOOLEAN,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (track_id, model_id)
);

CREATE INDEX IF NOT EXISTS idx_embeddings_vector ON embeddings
    USING hnsw (vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_embeddings_model_id ON embeddings(model_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_track_id ON embeddings(track_id);

-- Text embeddings (384-dimensional sentence-transformer vectors)
CREATE TABLE IF NOT EXISTS text_embeddings (
    id SERIAL PRIMARY KEY,
    vector vector(384) NOT NULL,
    model_id INTEGER NOT NULL REFERENCES embedding_models(id) ON DELETE CASCADE,
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (track_id, model_id)
);

CREATE INDEX IF NOT EXISTS idx_text_embeddings_vector ON text_embeddings
    USING hnsw (vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_text_embeddings_model_id ON text_embeddings(model_id);
CREATE INDEX IF NOT EXISTS idx_text_embeddings_track_id ON text_embeddings(track_id);

-- Audio features (librosa DSP + CLAP zero-shot classification)
CREATE TABLE IF NOT EXISTS audio_features (
    id SERIAL PRIMARY KEY,
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE UNIQUE,
    bpm DOUBLE PRECISION,
    key VARCHAR(3),
    mode VARCHAR(5),
    key_confidence DOUBLE PRECISION,
    energy DOUBLE PRECISION,
    energy_db DOUBLE PRECISION,
    brightness DOUBLE PRECISION,
    dynamic_range_db DOUBLE PRECISION,
    zero_crossing_rate DOUBLE PRECISION,
    instruments JSONB,
    moods JSONB,
    vocal_instrumental VARCHAR(20),
    vocal_score DOUBLE PRECISION,
    danceability DOUBLE PRECISION,
    source_bit_depth INTEGER,
    source_sample_rate INTEGER,
    source_is_lossless BOOLEAN,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audio_features_track_id ON audio_features(track_id);
CREATE INDEX IF NOT EXISTS idx_audio_features_bpm ON audio_features(bpm);
CREATE INDEX IF NOT EXISTS idx_audio_features_key ON audio_features(key, mode);
CREATE INDEX IF NOT EXISTS idx_audio_features_energy ON audio_features(energy_db);
CREATE INDEX IF NOT EXISTS idx_audio_features_danceability ON audio_features(danceability);
CREATE INDEX IF NOT EXISTS idx_audio_features_vocal ON audio_features(vocal_instrumental);

-- ─────────────────────────────────────────────────────────────────────────
-- Genre descriptions
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS genre_descriptions (
    id SERIAL PRIMARY KEY,
    genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
    source VARCHAR(50) NOT NULL,
    summary TEXT,
    content TEXT,
    url VARCHAR(500),
    reach INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (genre_id, source),
    CHECK (summary IS NOT NULL OR content IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_genre_descriptions_genre ON genre_descriptions(genre_id);
CREATE INDEX IF NOT EXISTS idx_genre_descriptions_source ON genre_descriptions(source);

-- ─────────────────────────────────────────────────────────────────────────
-- Tags
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tags (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CHECK (LENGTH(TRIM(name)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);
CREATE INDEX IF NOT EXISTS idx_tags_name_lower ON tags(name text_pattern_ops);

-- ─────────────────────────────────────────────────────────────────────────
-- Artist metadata tables
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS artist_tags (
    id SERIAL PRIMARY KEY,
    artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    weight INTEGER NOT NULL,
    source VARCHAR(50) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (artist_id, tag_id, source),
    CHECK (weight >= 0 AND weight <= 100)
);

CREATE INDEX IF NOT EXISTS idx_artist_tags_artist ON artist_tags(artist_id);
CREATE INDEX IF NOT EXISTS idx_artist_tags_tag ON artist_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_artist_tags_source ON artist_tags(source);
CREATE INDEX IF NOT EXISTS idx_artist_tags_weight ON artist_tags(weight);

CREATE TABLE IF NOT EXISTS artist_bios (
    id SERIAL PRIMARY KEY,
    artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    source VARCHAR(50) NOT NULL,
    summary TEXT,
    content TEXT,
    url VARCHAR(500),
    listeners INTEGER,
    playcount BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (artist_id, source),
    CHECK (summary IS NOT NULL OR content IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_artist_bios_artist ON artist_bios(artist_id);
CREATE INDEX IF NOT EXISTS idx_artist_bios_source ON artist_bios(source);
CREATE INDEX IF NOT EXISTS idx_artist_bios_listeners ON artist_bios(listeners) WHERE listeners IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_artist_bios_playcount ON artist_bios(playcount) WHERE playcount IS NOT NULL;

CREATE TABLE IF NOT EXISTS similar_artists (
    id SERIAL PRIMARY KEY,
    artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    similar_artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    match_score NUMERIC(5, 4) NOT NULL,
    source VARCHAR(50) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (artist_id, similar_artist_id, source),
    CHECK (artist_id != similar_artist_id),
    CHECK (match_score >= 0 AND match_score <= 1)
);

CREATE INDEX IF NOT EXISTS idx_similar_artists_artist ON similar_artists(artist_id);
CREATE INDEX IF NOT EXISTS idx_similar_artists_similar ON similar_artists(similar_artist_id);
CREATE INDEX IF NOT EXISTS idx_similar_artists_source ON similar_artists(source);
CREATE INDEX IF NOT EXISTS idx_similar_artists_match ON similar_artists(match_score);

-- ─────────────────────────────────────────────────────────────────────────
-- Album metadata tables
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS album_info (
    id SERIAL PRIMARY KEY,
    album_id UUID NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    source VARCHAR(50) NOT NULL,
    summary TEXT,
    content TEXT,
    url VARCHAR(500),
    listeners INTEGER,
    playcount BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (album_id, source),
    CHECK (summary IS NOT NULL OR content IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_album_info_album ON album_info(album_id);
CREATE INDEX IF NOT EXISTS idx_album_info_source ON album_info(source);
CREATE INDEX IF NOT EXISTS idx_album_info_listeners ON album_info(listeners) WHERE listeners IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_album_info_playcount ON album_info(playcount) WHERE playcount IS NOT NULL;

CREATE TABLE IF NOT EXISTS album_tags (
    id SERIAL PRIMARY KEY,
    album_id UUID NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    weight INTEGER NOT NULL,
    source VARCHAR(50) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (album_id, tag_id, source),
    CHECK (weight >= 0 AND weight <= 100)
);

CREATE INDEX IF NOT EXISTS idx_album_tags_album ON album_tags(album_id);
CREATE INDEX IF NOT EXISTS idx_album_tags_tag ON album_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_album_tags_source ON album_tags(source);
CREATE INDEX IF NOT EXISTS idx_album_tags_weight ON album_tags(weight);

-- ─────────────────────────────────────────────────────────────────────────
-- Statistics & External metadata
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS track_stats (
    id SERIAL PRIMARY KEY,
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    source VARCHAR(50) NOT NULL,
    listeners INTEGER,
    playcount BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (track_id, source),
    CHECK (listeners IS NOT NULL OR playcount IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_track_stats_track_id ON track_stats(track_id);
CREATE INDEX IF NOT EXISTS idx_track_stats_source ON track_stats(source);
CREATE INDEX IF NOT EXISTS idx_track_stats_listeners ON track_stats(listeners);
CREATE INDEX IF NOT EXISTS idx_track_stats_playcount ON track_stats(playcount);

CREATE TABLE IF NOT EXISTS external_metadata (
    id SERIAL PRIMARY KEY,
    entity_type VARCHAR(50) NOT NULL,
    entity_id TEXT NOT NULL,
    source VARCHAR(50) NOT NULL,
    metadata_type VARCHAR(50) NOT NULL,
    data JSONB NOT NULL,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fetch_status VARCHAR(20) DEFAULT 'success',
    error_message TEXT,
    UNIQUE (entity_type, entity_id, source, metadata_type)
);

CREATE INDEX IF NOT EXISTS idx_external_metadata_entity ON external_metadata(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_external_metadata_source ON external_metadata(source);
CREATE INDEX IF NOT EXISTS idx_external_metadata_type ON external_metadata(metadata_type);
CREATE INDEX IF NOT EXISTS idx_external_metadata_status ON external_metadata(fetch_status);
CREATE INDEX IF NOT EXISTS idx_external_metadata_data ON external_metadata USING gin (data);

-- ─────────────────────────────────────────────────────────────────────────
-- Lyrics
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS track_lyrics (
    id SERIAL PRIMARY KEY,
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    source VARCHAR(50) NOT NULL,        -- 'lrclib', 'genius', etc.
    plain_lyrics TEXT,                   -- Lyrics text without timestamps
    synced_lyrics TEXT,                  -- LRC format with timestamps (NULL if source doesn't support)
    instrumental BOOLEAN DEFAULT FALSE,  -- Instrumental track flag
    external_id INTEGER,                 -- ID in external service
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (track_id, source)
);

CREATE INDEX IF NOT EXISTS idx_track_lyrics_track_id ON track_lyrics(track_id);
CREATE INDEX IF NOT EXISTS idx_track_lyrics_source ON track_lyrics(source);

-- ─────────────────────────────────────────────────────────────────────────
-- Lyrics embeddings (384-dimensional, multiple chunks per track)
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS lyrics_embeddings (
    id SERIAL PRIMARY KEY,
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    model_id INTEGER NOT NULL REFERENCES embedding_models(id) ON DELETE CASCADE,
    vector vector(384) NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    chunk_text TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (track_id, model_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_lyrics_embeddings_vector ON lyrics_embeddings
    USING hnsw (vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_lyrics_embeddings_track_id ON lyrics_embeddings(track_id);
CREATE INDEX IF NOT EXISTS idx_lyrics_embeddings_model_id ON lyrics_embeddings(model_id);

-- ─────────────────────────────────────────────────────────────────────────
-- Listening history
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS listening_history (
    id SERIAL PRIMARY KEY,
    media_file_id INTEGER REFERENCES media_files(id) ON DELETE SET NULL,
    track_id UUID REFERENCES tracks(id) ON DELETE SET NULL,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    duration_listened NUMERIC(10, 2),
    percent_listened NUMERIC(5, 2),
    completed BOOLEAN DEFAULT FALSE,
    skipped BOOLEAN DEFAULT FALSE,
    source VARCHAR(50) DEFAULT 'hqplayer'
);

CREATE INDEX IF NOT EXISTS idx_listening_history_media_file ON listening_history(media_file_id);
CREATE INDEX IF NOT EXISTS idx_listening_history_track ON listening_history(track_id);
CREATE INDEX IF NOT EXISTS idx_listening_history_started ON listening_history(started_at);

-- ─────────────────────────────────────────────────────────────────────────
-- Functions & Triggers
-- ─────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Auto-update timestamps
CREATE TRIGGER update_embedding_models_updated_at BEFORE UPDATE ON embedding_models
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_artists_updated_at BEFORE UPDATE ON artists
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_albums_updated_at BEFORE UPDATE ON albums
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_tracks_updated_at BEFORE UPDATE ON tracks
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_album_variants_updated_at BEFORE UPDATE ON album_variants
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_media_files_updated_at BEFORE UPDATE ON media_files
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_embeddings_updated_at BEFORE UPDATE ON embeddings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_genres_updated_at BEFORE UPDATE ON genres
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ─────────────────────────────────────────────────────────────────────────
-- Library stats view
-- ─────────────────────────────────────────────────────────────────────────

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

-- Verification
SELECT 'Database schema initialized successfully!' as status;
SELECT * FROM library_stats;
