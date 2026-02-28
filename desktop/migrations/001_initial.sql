-- Music AI DJ - Full Schema Migration
-- Migrated from scripts/init_db.sql + ORM models

-- ============================================================
-- Extensions
-- ============================================================
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================
-- Enums
-- ============================================================
DO $$ BEGIN
    CREATE TYPE quality_source_type AS ENUM ('CD', 'Vinyl', 'Hi-Res', 'MP3');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- ============================================================
-- Core tables
-- ============================================================

CREATE TABLE IF NOT EXISTS embedding_models (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    dimension INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS genres (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tags (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_tag_name_not_empty CHECK (LENGTH(TRIM(name)) > 0)
);

CREATE TABLE IF NOT EXISTS artists (
    id SERIAL PRIMARY KEY,
    name VARCHAR(500) NOT NULL UNIQUE,
    lastfm_id VARCHAR(100),
    musicbrainz_id VARCHAR(100),
    country VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS albums (
    id SERIAL PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    release_year INTEGER,
    label VARCHAR(200),
    catalog_number VARCHAR(100),
    total_tracks INTEGER,
    quality_source quality_source_type DEFAULT 'CD',
    sample_rate INTEGER,
    bit_depth INTEGER,
    musicbrainz_id VARCHAR(100),
    lastfm_id VARCHAR(100),
    directory_path TEXT NOT NULL UNIQUE,
    user_rating DECIMAL(3, 2) CHECK (user_rating >= 0 AND user_rating <= 5),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS embeddings (
    id SERIAL PRIMARY KEY,
    vector vector(512) NOT NULL,
    model_id INTEGER NOT NULL REFERENCES embedding_models(id) ON DELETE CASCADE,
    track_id INTEGER UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS text_embeddings (
    id SERIAL PRIMARY KEY,
    vector vector(384) NOT NULL,
    model_id INTEGER NOT NULL REFERENCES embedding_models(id) ON DELETE CASCADE,
    track_id INTEGER UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tracks (
    id SERIAL PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    track_number INTEGER,
    disc_number INTEGER DEFAULT 1,
    duration_seconds DECIMAL(10, 2),
    sample_rate INTEGER,
    bit_depth INTEGER,
    bitrate INTEGER,
    channels INTEGER,
    file_path TEXT NOT NULL UNIQUE,
    file_size_bytes BIGINT,
    file_format VARCHAR(10) DEFAULT 'FLAC',
    file_modified_at TIMESTAMP,
    embedding_id INTEGER REFERENCES embeddings(id) ON DELETE SET NULL,
    text_embedding_id INTEGER REFERENCES text_embeddings(id) ON DELETE SET NULL,
    isrc VARCHAR(20),
    musicbrainz_id VARCHAR(100),
    play_count INTEGER DEFAULT 0,
    last_played_at TIMESTAMP,
    user_rating DECIMAL(3, 2) CHECK (user_rating >= 0 AND user_rating <= 5),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Add FK from embeddings/text_embeddings to tracks (after tracks created)
DO $$ BEGIN
    ALTER TABLE embeddings ADD CONSTRAINT fk_embeddings_track
        FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE;
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE text_embeddings ADD CONSTRAINT fk_text_embeddings_track
        FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE;
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- ============================================================
-- Junction tables
-- ============================================================

CREATE TABLE IF NOT EXISTS track_genres (
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
    PRIMARY KEY (track_id, genre_id)
);

CREATE TABLE IF NOT EXISTS track_artists (
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    role VARCHAR(50) DEFAULT 'primary',
    PRIMARY KEY (track_id, artist_id, role)
);

-- ============================================================
-- Last.fm / external metadata tables
-- ============================================================

CREATE TABLE IF NOT EXISTS artist_bios (
    id SERIAL PRIMARY KEY,
    artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    source VARCHAR(50) NOT NULL,
    summary TEXT,
    content TEXT,
    url VARCHAR(500),
    listeners INTEGER,
    playcount BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_artist_bios UNIQUE (artist_id, source),
    CONSTRAINT chk_has_bio CHECK (summary IS NOT NULL OR content IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS artist_tags (
    id SERIAL PRIMARY KEY,
    artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    weight INTEGER NOT NULL CHECK (weight >= 0 AND weight <= 100),
    source VARCHAR(50) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_artist_tags UNIQUE (artist_id, tag_id, source)
);

CREATE TABLE IF NOT EXISTS similar_artists (
    id SERIAL PRIMARY KEY,
    artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    similar_artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    match_score NUMERIC(5, 4) NOT NULL CHECK (match_score >= 0 AND match_score <= 1),
    source VARCHAR(50) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_similar_artists UNIQUE (artist_id, similar_artist_id, source),
    CONSTRAINT chk_not_self_similar CHECK (artist_id != similar_artist_id)
);

CREATE TABLE IF NOT EXISTS album_info (
    id SERIAL PRIMARY KEY,
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    source VARCHAR(50) NOT NULL,
    summary TEXT,
    content TEXT,
    url VARCHAR(500),
    listeners INTEGER,
    playcount BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_album_info UNIQUE (album_id, source),
    CONSTRAINT chk_has_album_info CHECK (summary IS NOT NULL OR content IS NOT NULL OR listeners IS NOT NULL OR playcount IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS album_tags (
    id SERIAL PRIMARY KEY,
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    weight INTEGER NOT NULL CHECK (weight >= 0 AND weight <= 100),
    source VARCHAR(50) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_album_tags UNIQUE (album_id, tag_id, source)
);

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
    CONSTRAINT uq_genre_descriptions UNIQUE (genre_id, source),
    CONSTRAINT chk_has_description CHECK (summary IS NOT NULL OR content IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS track_stats (
    id SERIAL PRIMARY KEY,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    source VARCHAR(50) NOT NULL,
    listeners INTEGER,
    playcount BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_track_stats UNIQUE (track_id, source),
    CONSTRAINT chk_has_track_stats CHECK (listeners IS NOT NULL OR playcount IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS external_metadata (
    id SERIAL PRIMARY KEY,
    entity_type VARCHAR(50) NOT NULL,
    entity_id INTEGER NOT NULL,
    source VARCHAR(50) NOT NULL,
    metadata_type VARCHAR(50) NOT NULL,
    data JSONB NOT NULL,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    fetch_status VARCHAR(20) DEFAULT 'success',
    error_message TEXT,
    CONSTRAINT uq_external_metadata UNIQUE (entity_type, entity_id, source, metadata_type)
);

-- ============================================================
-- Audio features (librosa + CLAP zero-shot)
-- ============================================================

CREATE TABLE IF NOT EXISTS audio_features (
    id SERIAL PRIMARY KEY,
    track_id INTEGER NOT NULL UNIQUE REFERENCES tracks(id) ON DELETE CASCADE,
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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- Chat / session history
-- ============================================================

CREATE TABLE IF NOT EXISTS chat_sessions (
    id SERIAL PRIMARY KEY,
    title VARCHAR(500),
    claude_session_id VARCHAR(100),
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    tracks_data JSONB,
    model VARCHAR(100),
    filters_detected JSONB,
    retrieval_log JSONB,
    tracks_retrieved INTEGER,
    is_not_relevant BOOLEAN DEFAULT FALSE,
    feedback_comment TEXT,
    feedback_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT now()
);

-- ============================================================
-- Listening history
-- ============================================================

CREATE TABLE IF NOT EXISTS listening_history (
    id SERIAL PRIMARY KEY,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP,
    duration_listened NUMERIC(10, 2) CHECK (duration_listened >= 0),
    percent_listened NUMERIC(5, 2) CHECK (percent_listened >= 0 AND percent_listened <= 100),
    completed BOOLEAN DEFAULT FALSE,
    skipped BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- Indexes
-- ============================================================

-- Embedding indexes
CREATE INDEX IF NOT EXISTS idx_embedding_models_name ON embedding_models(name);
CREATE INDEX IF NOT EXISTS idx_embeddings_model_id ON embeddings(model_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_vector ON embeddings
    USING hnsw (vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Text embedding indexes
CREATE INDEX IF NOT EXISTS idx_text_embeddings_model_id ON text_embeddings(model_id);
CREATE INDEX IF NOT EXISTS idx_text_embeddings_vector ON text_embeddings
    USING hnsw (vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Core table indexes
CREATE INDEX IF NOT EXISTS idx_genres_name ON genres(name);
CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);
CREATE INDEX IF NOT EXISTS idx_artists_name ON artists(name);
CREATE INDEX IF NOT EXISTS idx_albums_title ON albums(title);
CREATE INDEX IF NOT EXISTS idx_albums_release_year ON albums(release_year);
CREATE INDEX IF NOT EXISTS idx_albums_quality_source ON albums(quality_source);
CREATE INDEX IF NOT EXISTS idx_albums_lastfm_id ON albums(lastfm_id);
CREATE INDEX IF NOT EXISTS idx_tracks_title ON tracks(title);
CREATE INDEX IF NOT EXISTS idx_tracks_album_id ON tracks(album_id);
CREATE INDEX IF NOT EXISTS idx_tracks_file_path ON tracks(file_path);
CREATE INDEX IF NOT EXISTS idx_tracks_play_count ON tracks(play_count);
CREATE INDEX IF NOT EXISTS idx_tracks_last_played ON tracks(last_played_at);
CREATE INDEX IF NOT EXISTS idx_tracks_file_modified_at ON tracks(file_modified_at);

-- Trigram indexes for fuzzy search (pg_trgm)
CREATE INDEX IF NOT EXISTS idx_artists_name_trgm ON artists USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_albums_title_trgm ON albums USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_tracks_title_trgm ON tracks USING gin (title gin_trgm_ops);

-- Junction indexes
CREATE INDEX IF NOT EXISTS idx_track_genres_track_id ON track_genres(track_id);
CREATE INDEX IF NOT EXISTS idx_track_genres_genre_id ON track_genres(genre_id);
CREATE INDEX IF NOT EXISTS idx_track_artists_track_id ON track_artists(track_id);
CREATE INDEX IF NOT EXISTS idx_track_artists_artist_id ON track_artists(artist_id);

-- External metadata indexes
CREATE INDEX IF NOT EXISTS idx_external_metadata_entity ON external_metadata(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_external_metadata_source ON external_metadata(source);
CREATE INDEX IF NOT EXISTS idx_external_metadata_type ON external_metadata(metadata_type);
CREATE INDEX IF NOT EXISTS idx_external_metadata_data ON external_metadata USING gin (data);

-- Audio feature indexes
CREATE INDEX IF NOT EXISTS idx_audio_features_track_id ON audio_features(track_id);
CREATE INDEX IF NOT EXISTS idx_audio_features_bpm ON audio_features(bpm);
CREATE INDEX IF NOT EXISTS idx_audio_features_key ON audio_features(key, mode);
CREATE INDEX IF NOT EXISTS idx_audio_features_energy ON audio_features(energy_db);
CREATE INDEX IF NOT EXISTS idx_audio_features_danceability ON audio_features(danceability);
CREATE INDEX IF NOT EXISTS idx_audio_features_vocal ON audio_features(vocal_instrumental);
CREATE INDEX IF NOT EXISTS idx_audio_features_instruments ON audio_features USING gin (instruments);
CREATE INDEX IF NOT EXISTS idx_audio_features_moods ON audio_features USING gin (moods);

-- Artist metadata indexes
CREATE INDEX IF NOT EXISTS idx_artist_bios_artist ON artist_bios(artist_id);
CREATE INDEX IF NOT EXISTS idx_artist_bios_source ON artist_bios(source);
CREATE INDEX IF NOT EXISTS idx_artist_tags_artist ON artist_tags(artist_id);
CREATE INDEX IF NOT EXISTS idx_artist_tags_tag ON artist_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_similar_artists_artist ON similar_artists(artist_id);
CREATE INDEX IF NOT EXISTS idx_similar_artists_similar ON similar_artists(similar_artist_id);

-- Album metadata indexes
CREATE INDEX IF NOT EXISTS idx_album_info_album ON album_info(album_id);
CREATE INDEX IF NOT EXISTS idx_album_tags_album ON album_tags(album_id);

-- Track stats indexes
CREATE INDEX IF NOT EXISTS idx_track_stats_track ON track_stats(track_id);
CREATE INDEX IF NOT EXISTS idx_track_stats_source ON track_stats(source);

-- Chat indexes
CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id);

-- Listening history indexes
CREATE INDEX IF NOT EXISTS idx_listening_history_track ON listening_history(track_id);
CREATE INDEX IF NOT EXISTS idx_listening_history_started ON listening_history(started_at);
CREATE INDEX IF NOT EXISTS idx_listening_history_completed ON listening_history(track_id) WHERE completed = true;

-- Partial indexes
CREATE INDEX IF NOT EXISTS idx_tracks_file_modified_at_null ON tracks(id) WHERE file_modified_at IS NULL;

-- ============================================================
-- Trigger function for updated_at
-- ============================================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create triggers (using DO block to avoid errors on re-run)
DO $$ BEGIN
    CREATE TRIGGER update_embedding_models_updated_at BEFORE UPDATE ON embedding_models
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER update_embeddings_updated_at BEFORE UPDATE ON embeddings
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER update_genres_updated_at BEFORE UPDATE ON genres
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER update_artists_updated_at BEFORE UPDATE ON artists
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER update_albums_updated_at BEFORE UPDATE ON albums
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER update_tracks_updated_at BEFORE UPDATE ON tracks
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER trg_artist_bios_updated_at BEFORE UPDATE ON artist_bios
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER trg_artist_tags_updated_at BEFORE UPDATE ON artist_tags
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER trg_similar_artists_updated_at BEFORE UPDATE ON similar_artists
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER trg_album_info_updated_at BEFORE UPDATE ON album_info
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER trg_album_tags_updated_at BEFORE UPDATE ON album_tags
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER trg_genre_descriptions_updated_at BEFORE UPDATE ON genre_descriptions
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER trg_tags_updated_at BEFORE UPDATE ON tags
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER trigger_audio_features_updated_at BEFORE UPDATE ON audio_features
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER trigger_external_metadata_updated_at BEFORE UPDATE ON external_metadata
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ============================================================
-- Custom functions
-- ============================================================

CREATE OR REPLACE FUNCTION get_artist_bio(artist_id_param INTEGER)
RETURNS TEXT
LANGUAGE plpgsql STABLE
AS $$
DECLARE
    combined_bio TEXT;
BEGIN
    SELECT string_agg(
        COALESCE(data->>'summary', data->>'content', '') ||
        E'\n\n[Source: ' || source || ']',
        E'\n\n---\n\n'
        ORDER BY
            CASE source
                WHEN 'lastfm' THEN 1
                WHEN 'musicbrainz' THEN 2
                WHEN 'spotify' THEN 3
                ELSE 99
            END
    )
    INTO combined_bio
    FROM external_metadata
    WHERE entity_type = 'artist'
      AND entity_id = artist_id_param
      AND metadata_type = 'bio'
      AND fetch_status = 'success'
      AND (data->>'summary' IS NOT NULL OR data->>'content' IS NOT NULL);
    RETURN combined_bio;
END;
$$;

CREATE OR REPLACE FUNCTION get_artist_tags(artist_id_param INTEGER)
RETURNS TABLE(tag_name VARCHAR, sources TEXT[], weight INTEGER)
LANGUAGE sql STABLE
AS $$
    SELECT tag, array_agg(DISTINCT source ORDER BY source) as sources, COUNT(*)::INTEGER as weight
    FROM (
        SELECT entity_id, source, jsonb_array_elements(data->'tags')->>'name' as tag
        FROM external_metadata
        WHERE entity_type = 'artist' AND metadata_type = 'tags' AND fetch_status = 'success' AND data ? 'tags'
        UNION ALL
        SELECT entity_id, source, jsonb_array_elements_text(data->'genres') as tag
        FROM external_metadata
        WHERE entity_type = 'artist' AND metadata_type = 'genres' AND fetch_status = 'success' AND data ? 'genres'
    ) combined
    WHERE entity_id = artist_id_param
    GROUP BY tag
    ORDER BY weight DESC, tag;
$$;

CREATE OR REPLACE FUNCTION get_artist_similar(artist_id_param INTEGER)
RETURNS TABLE(similar_artist_name VARCHAR, sources TEXT[], avg_match NUMERIC)
LANGUAGE sql STABLE
AS $$
    SELECT
        artist_name,
        array_agg(DISTINCT source ORDER BY source) as sources,
        AVG(match_score)::NUMERIC(5,4) as avg_match
    FROM (
        SELECT entity_id, source, elem->>'name' as artist_name,
               COALESCE((elem->>'match')::NUMERIC, 1.0) as match_score
        FROM external_metadata, jsonb_array_elements(data->'similar') as elem
        WHERE entity_type = 'artist' AND metadata_type = 'similar_artists'
          AND fetch_status = 'success' AND data ? 'similar'
    ) combined
    WHERE entity_id = artist_id_param
    GROUP BY artist_name
    ORDER BY avg_match DESC, artist_name;
$$;

CREATE OR REPLACE FUNCTION get_artist_stats(artist_id_param INTEGER)
RETURNS TABLE(source VARCHAR, listeners BIGINT, playcount BIGINT, url TEXT)
LANGUAGE sql STABLE
AS $$
    SELECT source,
        COALESCE((data->'stats'->>'listeners')::BIGINT, 0) as listeners,
        COALESCE((data->'stats'->>'playcount')::BIGINT, 0) as playcount,
        data->>'url' as url
    FROM external_metadata
    WHERE entity_type = 'artist' AND entity_id = artist_id_param
      AND metadata_type = 'bio' AND fetch_status = 'success' AND data ? 'stats'
    ORDER BY CASE source WHEN 'lastfm' THEN 1 WHEN 'spotify' THEN 2 WHEN 'musicbrainz' THEN 3 ELSE 99 END;
$$;

-- ============================================================
-- Views
-- ============================================================

CREATE OR REPLACE VIEW library_stats AS
SELECT
    (SELECT COUNT(*) FROM artists) as total_artists,
    (SELECT COUNT(*) FROM albums) as total_albums,
    (SELECT COUNT(*) FROM tracks) as total_tracks,
    (SELECT COUNT(*) FROM tracks WHERE embedding_id IS NOT NULL) as tracks_with_embeddings,
    (SELECT SUM(duration_seconds) FROM tracks) as total_duration_seconds,
    (SELECT SUM(file_size_bytes) FROM tracks) as total_file_size_bytes,
    (SELECT COUNT(*) FROM genres) as unique_genres;

CREATE OR REPLACE VIEW track_listening_stats AS
SELECT
    t.id,
    t.play_count,
    t.last_played_at,
    COUNT(lh.id) FILTER (WHERE lh.completed) AS completed_plays,
    COUNT(lh.id) FILTER (WHERE lh.skipped) AS skips,
    AVG(lh.percent_listened) FILTER (WHERE lh.completed) AS avg_completion_percent,
    MAX(lh.started_at) AS last_listen_started,
    COUNT(DISTINCT DATE(lh.started_at)) AS days_listened
FROM tracks t
LEFT JOIN listening_history lh ON t.id = lh.track_id
GROUP BY t.id, t.play_count, t.last_played_at;

CREATE OR REPLACE VIEW artists_enriched AS
SELECT
    a.id,
    a.name,
    a.country,
    get_artist_bio(a.id) AS bio_combined,
    (SELECT jsonb_agg(jsonb_build_object('tag', tag_name, 'weight', weight, 'sources', sources)
        ORDER BY weight DESC)
     FROM get_artist_tags(a.id)
     LIMIT 30
    ) AS tags_combined,
    (SELECT jsonb_agg(jsonb_build_object('name', similar_artist_name, 'match', avg_match, 'sources', sources)
        ORDER BY avg_match DESC)
     FROM get_artist_similar(a.id)
     LIMIT 20
    ) AS similar_artists,
    a.created_at,
    a.updated_at
FROM artists a;
