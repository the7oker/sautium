-- Migration: Convert genres, tags, embedding_models from SERIAL to UUID v5 PKs
-- ============================================================================
-- Uses the same NAMESPACE and normalization as Python uuid_utils.py so that
-- uuid_generate_v5() in SQL produces identical UUIDs to uuid.uuid5() in Python.

-- Project namespace UUID (must match backend/uuid_utils.py NAMESPACE exactly)
-- Used as literal below since set_config is transaction-scoped.

-- Ensure uuid-ossp extension is available (needed for uuid_generate_v5)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Normalization helper: lower + trim + collapse whitespace (matches Python normalize())
CREATE OR REPLACE FUNCTION _djai_normalize(t text) RETURNS text AS $$
BEGIN
    RETURN regexp_replace(lower(trim(t)), '\s+', ' ', 'g');
END;
$$ LANGUAGE plpgsql IMMUTABLE;


-- ============================================================================
-- Pre-step: Ensure external_metadata.entity_id is text (may be integer in old schema)
-- ============================================================================
ALTER TABLE external_metadata ALTER COLUMN entity_id TYPE text USING entity_id::text;

-- ============================================================================
-- 0. DEDUPLICATE: merge case-variant genres/tags before UUID conversion
-- ============================================================================

-- 0a. Deduplicate genres: keep lowest id per normalized name, remap FKs
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT min(id) AS keep_id, array_agg(id) AS all_ids, _djai_normalize(name) AS norm
        FROM genres
        GROUP BY _djai_normalize(name)
        HAVING count(*) > 1
    LOOP
        -- Remap track_genres FKs to the kept genre
        UPDATE track_genres SET genre_id = r.keep_id
        WHERE genre_id = ANY(r.all_ids) AND genre_id != r.keep_id
          AND NOT EXISTS (
            SELECT 1 FROM track_genres tg2
            WHERE tg2.track_id = track_genres.track_id AND tg2.genre_id = r.keep_id
          );
        -- Delete duplicate track_genres that would violate PK
        DELETE FROM track_genres
        WHERE genre_id = ANY(r.all_ids) AND genre_id != r.keep_id;
        -- Remap genre_descriptions
        UPDATE genre_descriptions SET genre_id = r.keep_id
        WHERE genre_id = ANY(r.all_ids) AND genre_id != r.keep_id
          AND NOT EXISTS (
            SELECT 1 FROM genre_descriptions gd2
            WHERE gd2.genre_id = r.keep_id AND gd2.source = genre_descriptions.source
          );
        DELETE FROM genre_descriptions
        WHERE genre_id = ANY(r.all_ids) AND genre_id != r.keep_id;
        -- Remap external_metadata
        UPDATE external_metadata SET entity_id = r.keep_id::text
        WHERE entity_type = 'genre' AND entity_id::text = ANY(SELECT unnest(r.all_ids)::text) AND entity_id::text != r.keep_id::text
          AND NOT EXISTS (
            SELECT 1 FROM external_metadata em2
            WHERE em2.entity_type = 'genre' AND em2.entity_id = r.keep_id::text
              AND em2.source = external_metadata.source AND em2.metadata_type = external_metadata.metadata_type
          );
        DELETE FROM external_metadata
        WHERE entity_type = 'genre' AND entity_id::text = ANY(SELECT unnest(r.all_ids)::text) AND entity_id::text != r.keep_id::text;
        -- Delete duplicate genres
        DELETE FROM genres WHERE id = ANY(r.all_ids) AND id != r.keep_id;
    END LOOP;
END $$;

-- 0b. Deduplicate tags: keep lowest id per normalized name, remap FKs
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT min(id) AS keep_id, array_agg(id) AS all_ids, _djai_normalize(name) AS norm
        FROM tags
        GROUP BY _djai_normalize(name)
        HAVING count(*) > 1
    LOOP
        -- Remap artist_tags
        UPDATE artist_tags SET tag_id = r.keep_id
        WHERE tag_id = ANY(r.all_ids) AND tag_id != r.keep_id
          AND NOT EXISTS (
            SELECT 1 FROM artist_tags at2
            WHERE at2.artist_id = artist_tags.artist_id AND at2.tag_id = r.keep_id AND at2.source = artist_tags.source
          );
        DELETE FROM artist_tags
        WHERE tag_id = ANY(r.all_ids) AND tag_id != r.keep_id;
        -- Remap album_tags
        UPDATE album_tags SET tag_id = r.keep_id
        WHERE tag_id = ANY(r.all_ids) AND tag_id != r.keep_id
          AND NOT EXISTS (
            SELECT 1 FROM album_tags abt2
            WHERE abt2.album_id = album_tags.album_id AND abt2.tag_id = r.keep_id AND abt2.source = album_tags.source
          );
        DELETE FROM album_tags
        WHERE tag_id = ANY(r.all_ids) AND tag_id != r.keep_id;
        -- Delete duplicate tags
        DELETE FROM tags WHERE id = ANY(r.all_ids) AND id != r.keep_id;
    END LOOP;
END $$;


-- ============================================================================
-- 1. GENRES: SERIAL -> UUID
-- ============================================================================

-- 1a. Add new UUID column
ALTER TABLE genres ADD COLUMN new_id UUID;

-- 1b. Compute UUID v5 for each genre
UPDATE genres
SET new_id = uuid_generate_v5(
    '5ba7a9d0-1f8c-4c3d-9e7a-2b4f6c8d0e1f'::uuid,
    'genre:' || _djai_normalize(name)
);

-- 1c. Create temp mapping
CREATE TEMP TABLE _genre_map AS
SELECT id AS old_id, new_id FROM genres;

-- 1d. Update FK in track_genres
ALTER TABLE track_genres ADD COLUMN new_genre_id UUID;
UPDATE track_genres tg
SET new_genre_id = gm.new_id
FROM _genre_map gm
WHERE tg.genre_id = gm.old_id;

-- 1e. Update FK in genre_descriptions
ALTER TABLE genre_descriptions ADD COLUMN new_genre_id UUID;
UPDATE genre_descriptions gd
SET new_genre_id = gm.new_id
FROM _genre_map gm
WHERE gd.genre_id = gm.old_id;

-- 1f. Update external_metadata references for genres
UPDATE external_metadata em
SET entity_id = gm.new_id::text
FROM _genre_map gm
WHERE em.entity_type = 'genre'
  AND em.entity_id = gm.old_id::text;

-- 1g. Drop old constraints and columns, swap in new ones
-- Note: constraint names may differ from table names (e.g. song_genres_* from historical rename)
ALTER TABLE track_genres DROP CONSTRAINT track_genres_pkey;
ALTER TABLE track_genres DROP CONSTRAINT IF EXISTS track_genres_genre_id_fkey;
ALTER TABLE track_genres DROP CONSTRAINT IF EXISTS song_genres_genre_id_fkey;
ALTER TABLE track_genres DROP CONSTRAINT IF EXISTS song_genres_song_id_fkey;
ALTER TABLE track_genres DROP CONSTRAINT IF EXISTS track_genres_track_id_fkey;
DROP INDEX IF EXISTS idx_track_genres_genre_id;

ALTER TABLE genre_descriptions DROP CONSTRAINT IF EXISTS genre_descriptions_genre_id_fkey;
ALTER TABLE genre_descriptions DROP CONSTRAINT IF EXISTS uq_genre_descriptions;
DROP INDEX IF EXISTS idx_genre_descriptions_genre;

-- Drop old PK on genres (CASCADE to drop dependent FKs)
ALTER TABLE genres DROP CONSTRAINT genres_pkey CASCADE;
ALTER TABLE genres DROP COLUMN id;
ALTER TABLE genres RENAME COLUMN new_id TO id;
ALTER TABLE genres ADD PRIMARY KEY (id);

-- Swap columns in track_genres
ALTER TABLE track_genres DROP COLUMN genre_id;
ALTER TABLE track_genres RENAME COLUMN new_genre_id TO genre_id;
ALTER TABLE track_genres ALTER COLUMN genre_id SET NOT NULL;
ALTER TABLE track_genres ADD PRIMARY KEY (track_id, genre_id);
ALTER TABLE track_genres ADD CONSTRAINT track_genres_genre_id_fkey
    FOREIGN KEY (genre_id) REFERENCES genres(id) ON DELETE CASCADE;
ALTER TABLE track_genres ADD CONSTRAINT track_genres_track_id_fkey
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE;
CREATE INDEX idx_track_genres_genre_id ON track_genres(genre_id);

-- Swap columns in genre_descriptions
ALTER TABLE genre_descriptions DROP COLUMN genre_id;
ALTER TABLE genre_descriptions RENAME COLUMN new_genre_id TO genre_id;
ALTER TABLE genre_descriptions ALTER COLUMN genre_id SET NOT NULL;
ALTER TABLE genre_descriptions ADD CONSTRAINT genre_descriptions_genre_id_fkey
    FOREIGN KEY (genre_id) REFERENCES genres(id) ON DELETE CASCADE;
ALTER TABLE genre_descriptions ADD CONSTRAINT uq_genre_descriptions UNIQUE (genre_id, source);
CREATE INDEX idx_genre_descriptions_genre ON genre_descriptions(genre_id);

DROP TABLE _genre_map;


-- ============================================================================
-- 2. TAGS: SERIAL -> UUID
-- ============================================================================

-- 2a. Add new UUID column
ALTER TABLE tags ADD COLUMN new_id UUID;

-- 2b. Compute UUID v5 for each tag
UPDATE tags
SET new_id = uuid_generate_v5(
    '5ba7a9d0-1f8c-4c3d-9e7a-2b4f6c8d0e1f'::uuid,
    'tag:' || _djai_normalize(name)
);

-- 2c. Create temp mapping
CREATE TEMP TABLE _tag_map AS
SELECT id AS old_id, new_id FROM tags;

-- 2d. Update FK in artist_tags
ALTER TABLE artist_tags ADD COLUMN new_tag_id UUID;
UPDATE artist_tags at2
SET new_tag_id = tm.new_id
FROM _tag_map tm
WHERE at2.tag_id = tm.old_id;

-- 2e. Update FK in album_tags
ALTER TABLE album_tags ADD COLUMN new_tag_id UUID;
UPDATE album_tags abt
SET new_tag_id = tm.new_id
FROM _tag_map tm
WHERE abt.tag_id = tm.old_id;

-- 2f. Drop old constraints, swap columns
ALTER TABLE artist_tags DROP CONSTRAINT IF EXISTS artist_tags_tag_id_fkey;
ALTER TABLE artist_tags DROP CONSTRAINT IF EXISTS uq_artist_tags;
DROP INDEX IF EXISTS idx_artist_tags_tag;

ALTER TABLE album_tags DROP CONSTRAINT IF EXISTS album_tags_tag_id_fkey;
ALTER TABLE album_tags DROP CONSTRAINT IF EXISTS uq_album_tags;
DROP INDEX IF EXISTS idx_album_tags_tag;

-- Drop old PK on tags (CASCADE to drop dependent FKs)
ALTER TABLE tags DROP CONSTRAINT tags_pkey CASCADE;
ALTER TABLE tags DROP COLUMN id;
ALTER TABLE tags RENAME COLUMN new_id TO id;
ALTER TABLE tags ADD PRIMARY KEY (id);

-- Swap columns in artist_tags
ALTER TABLE artist_tags DROP COLUMN tag_id;
ALTER TABLE artist_tags RENAME COLUMN new_tag_id TO tag_id;
ALTER TABLE artist_tags ALTER COLUMN tag_id SET NOT NULL;
ALTER TABLE artist_tags ADD CONSTRAINT artist_tags_tag_id_fkey
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE;
ALTER TABLE artist_tags ADD CONSTRAINT uq_artist_tags UNIQUE (artist_id, tag_id, source);
CREATE INDEX idx_artist_tags_tag ON artist_tags(tag_id);

-- Swap columns in album_tags
ALTER TABLE album_tags DROP COLUMN tag_id;
ALTER TABLE album_tags RENAME COLUMN new_tag_id TO tag_id;
ALTER TABLE album_tags ALTER COLUMN tag_id SET NOT NULL;
ALTER TABLE album_tags ADD CONSTRAINT album_tags_tag_id_fkey
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE;
ALTER TABLE album_tags ADD CONSTRAINT uq_album_tags UNIQUE (album_id, tag_id, source);
CREATE INDEX idx_album_tags_tag ON album_tags(tag_id);

DROP TABLE _tag_map;


-- ============================================================================
-- 3. EMBEDDING_MODELS: SERIAL -> UUID
-- ============================================================================

-- 3a. Add new UUID column
ALTER TABLE embedding_models ADD COLUMN new_id UUID;

-- 3b. Compute UUID v5 for each model
UPDATE embedding_models
SET new_id = uuid_generate_v5(
    '5ba7a9d0-1f8c-4c3d-9e7a-2b4f6c8d0e1f'::uuid,
    'embedding_model:' || _djai_normalize(name)
);

-- 3c. Create temp mapping
CREATE TEMP TABLE _model_map AS
SELECT id AS old_id, new_id FROM embedding_models;

-- 3d. Update FK in embeddings
ALTER TABLE embeddings ADD COLUMN new_model_id UUID;
UPDATE embeddings e
SET new_model_id = mm.new_id
FROM _model_map mm
WHERE e.model_id = mm.old_id;

-- 3e. Update FK in text_embeddings
ALTER TABLE text_embeddings ADD COLUMN new_model_id UUID;
UPDATE text_embeddings te
SET new_model_id = mm.new_id
FROM _model_map mm
WHERE te.model_id = mm.old_id;

-- 3f. Update FK in lyrics_embeddings (if table exists)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'lyrics_embeddings') THEN
        EXECUTE 'ALTER TABLE lyrics_embeddings ADD COLUMN new_model_id UUID';
        EXECUTE '
            UPDATE lyrics_embeddings le
            SET new_model_id = mm.new_id
            FROM _model_map mm
            WHERE le.model_id = mm.old_id
        ';
    END IF;
END $$;

-- 3g. Drop old constraints and swap

-- embeddings
ALTER TABLE embeddings DROP CONSTRAINT IF EXISTS uq_embeddings_track_model;
ALTER TABLE embeddings DROP CONSTRAINT IF EXISTS embeddings_model_id_fkey;
DROP INDEX IF EXISTS idx_embeddings_model_id;

-- text_embeddings
ALTER TABLE text_embeddings DROP CONSTRAINT IF EXISTS uq_text_embeddings_track_model;
ALTER TABLE text_embeddings DROP CONSTRAINT IF EXISTS text_embeddings_model_id_fkey;
DROP INDEX IF EXISTS idx_text_embeddings_model_id;

-- lyrics_embeddings (conditional)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'lyrics_embeddings') THEN
        EXECUTE 'ALTER TABLE lyrics_embeddings DROP CONSTRAINT IF EXISTS uq_lyrics_embeddings_track_model_chunk';
        EXECUTE 'ALTER TABLE lyrics_embeddings DROP CONSTRAINT IF EXISTS lyrics_embeddings_track_id_model_id_chunk_index_key';
        EXECUTE 'ALTER TABLE lyrics_embeddings DROP CONSTRAINT IF EXISTS lyrics_embeddings_model_id_fkey';
        EXECUTE 'DROP INDEX IF EXISTS idx_lyrics_embeddings_model_id';
    END IF;
END $$;

-- Drop old PK on embedding_models (CASCADE to drop dependent FKs)
ALTER TABLE embedding_models DROP CONSTRAINT embedding_models_pkey CASCADE;
ALTER TABLE embedding_models DROP COLUMN id;
ALTER TABLE embedding_models RENAME COLUMN new_id TO id;
ALTER TABLE embedding_models ADD PRIMARY KEY (id);

-- Swap columns in embeddings
ALTER TABLE embeddings DROP COLUMN model_id;
ALTER TABLE embeddings RENAME COLUMN new_model_id TO model_id;
ALTER TABLE embeddings ALTER COLUMN model_id SET NOT NULL;
ALTER TABLE embeddings ADD CONSTRAINT embeddings_model_id_fkey
    FOREIGN KEY (model_id) REFERENCES embedding_models(id);
ALTER TABLE embeddings ADD CONSTRAINT uq_embeddings_track_model UNIQUE (track_id, model_id);
CREATE INDEX idx_embeddings_model_id ON embeddings(model_id);

-- Swap columns in text_embeddings
ALTER TABLE text_embeddings DROP COLUMN model_id;
ALTER TABLE text_embeddings RENAME COLUMN new_model_id TO model_id;
ALTER TABLE text_embeddings ALTER COLUMN model_id SET NOT NULL;
ALTER TABLE text_embeddings ADD CONSTRAINT text_embeddings_model_id_fkey
    FOREIGN KEY (model_id) REFERENCES embedding_models(id);
ALTER TABLE text_embeddings ADD CONSTRAINT uq_text_embeddings_track_model UNIQUE (track_id, model_id);
CREATE INDEX idx_text_embeddings_model_id ON text_embeddings(model_id);

-- Swap columns in lyrics_embeddings (conditional)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'lyrics_embeddings') THEN
        EXECUTE 'ALTER TABLE lyrics_embeddings DROP COLUMN model_id';
        EXECUTE 'ALTER TABLE lyrics_embeddings RENAME COLUMN new_model_id TO model_id';
        EXECUTE 'ALTER TABLE lyrics_embeddings ALTER COLUMN model_id SET NOT NULL';
        EXECUTE 'ALTER TABLE lyrics_embeddings ADD CONSTRAINT lyrics_embeddings_model_id_fkey
            FOREIGN KEY (model_id) REFERENCES embedding_models(id) ON DELETE CASCADE';
        EXECUTE 'ALTER TABLE lyrics_embeddings ADD CONSTRAINT uq_lyrics_embeddings_track_model_chunk
            UNIQUE (track_id, model_id, chunk_index)';
        EXECUTE 'CREATE INDEX idx_lyrics_embeddings_model_id ON lyrics_embeddings(model_id)';
    END IF;
END $$;

DROP TABLE _model_map;


-- ============================================================================
-- Update library_stats view to include tracks_with_lyrics
-- ============================================================================
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

-- ============================================================================
-- Cleanup: drop helper function
-- ============================================================================
DROP FUNCTION IF EXISTS _djai_normalize(text);
