-- Migration 002: Artist normalization support
-- ==============================================
-- Adds artist_type, verification_status, raw_name to artists table.
-- Creates artist_members junction table for compound→member relationships.
-- Adds ON UPDATE CASCADE to UUID FK constraints for track/album UUID cascading.
-- Drops unused 'country' column from artists.

-- ============================================================
-- 1. Add new columns to artists
-- ============================================================

ALTER TABLE artists ADD COLUMN IF NOT EXISTS artist_type VARCHAR(20) DEFAULT 'unknown';
ALTER TABLE artists ADD COLUMN IF NOT EXISTS verification_status VARCHAR(20) DEFAULT 'unverified';
ALTER TABLE artists ADD COLUMN IF NOT EXISTS raw_name VARCHAR(500);

-- Drop unused country column (drop dependent views first, recreate after)
DROP VIEW IF EXISTS artists_enriched CASCADE;
ALTER TABLE artists DROP COLUMN IF EXISTS country;

-- Recreate artists_enriched view without country column
-- (uses helper functions get_artist_bio, get_artist_tags, get_artist_similar)
DO $$ BEGIN
    CREATE OR REPLACE VIEW artists_enriched AS
    SELECT
        a.id,
        a.name,
        a.artist_type,
        a.verification_status,
        get_artist_bio(a.id) AS bio_combined,
        (SELECT jsonb_agg(
            jsonb_build_object('tag', t.tag_name, 'weight', t.weight, 'sources', t.sources)
            ORDER BY t.weight DESC
        ) FROM get_artist_tags(a.id) t(tag_name, sources, weight) LIMIT 30) AS tags_combined,
        (SELECT jsonb_agg(
            jsonb_build_object('name', s.similar_artist_name, 'match', s.avg_match, 'sources', s.sources)
            ORDER BY s.avg_match DESC
        ) FROM get_artist_similar(a.id) s(similar_artist_name, sources, avg_match) LIMIT 20) AS similar_artists,
        a.created_at,
        a.updated_at
    FROM artists a;
EXCEPTION WHEN undefined_function THEN
    -- Helper functions don't exist yet on this DB, skip view creation
    NULL;
END $$;

-- Add check constraints (idempotent via DO blocks)
DO $$ BEGIN
    ALTER TABLE artists ADD CONSTRAINT chk_artist_type
        CHECK (artist_type IN ('unknown', 'solo', 'band', 'collaboration', 'orchestra', 'other'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE artists ADD CONSTRAINT chk_verification_status
        CHECK (verification_status IN ('unverified', 'suspicious', 'verified_band', 'verified_split', 'verified_collab'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_artists_verification_status ON artists(verification_status);
CREATE INDEX IF NOT EXISTS idx_artists_artist_type ON artists(artist_type);

-- ============================================================
-- 2. Create artist_members junction table
-- ============================================================

CREATE TABLE IF NOT EXISTS artist_members (
    compound_artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE,
    member_artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE,
    role VARCHAR(50) NOT NULL DEFAULT 'member',
    PRIMARY KEY (compound_artist_id, member_artist_id)
);

CREATE INDEX IF NOT EXISTS idx_artist_members_compound ON artist_members(compound_artist_id);
CREATE INDEX IF NOT EXISTS idx_artist_members_member ON artist_members(member_artist_id);

-- ============================================================
-- 3. Add ON UPDATE CASCADE to track UUID FK constraints
--    Enables: UPDATE tracks SET id = new_uuid WHERE id = old_uuid
--    and all child rows automatically follow.
-- ============================================================

-- track_artists.track_id
ALTER TABLE track_artists DROP CONSTRAINT IF EXISTS track_artists_track_id_fkey;
ALTER TABLE track_artists ADD CONSTRAINT track_artists_track_id_fkey
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE;

-- track_genres.track_id
ALTER TABLE track_genres DROP CONSTRAINT IF EXISTS track_genres_track_id_fkey;
ALTER TABLE track_genres ADD CONSTRAINT track_genres_track_id_fkey
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE;

-- media_files.track_id
ALTER TABLE media_files DROP CONSTRAINT IF EXISTS media_files_track_id_fkey;
ALTER TABLE media_files ADD CONSTRAINT media_files_track_id_fkey
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE;

-- embeddings.track_id
ALTER TABLE embeddings DROP CONSTRAINT IF EXISTS embeddings_track_id_fkey;
ALTER TABLE embeddings ADD CONSTRAINT embeddings_track_id_fkey
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE;

-- text_embeddings.track_id
ALTER TABLE text_embeddings DROP CONSTRAINT IF EXISTS text_embeddings_track_id_fkey;
ALTER TABLE text_embeddings ADD CONSTRAINT text_embeddings_track_id_fkey
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE;

-- audio_features.track_id
ALTER TABLE audio_features DROP CONSTRAINT IF EXISTS audio_features_track_id_fkey;
ALTER TABLE audio_features ADD CONSTRAINT audio_features_track_id_fkey
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE;

-- track_stats.track_id
ALTER TABLE track_stats DROP CONSTRAINT IF EXISTS track_stats_track_id_fkey;
ALTER TABLE track_stats ADD CONSTRAINT track_stats_track_id_fkey
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE;

-- track_lyrics.track_id
ALTER TABLE track_lyrics DROP CONSTRAINT IF EXISTS track_lyrics_track_id_fkey;
ALTER TABLE track_lyrics ADD CONSTRAINT track_lyrics_track_id_fkey
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE;

-- lyrics_embeddings.track_id
ALTER TABLE lyrics_embeddings DROP CONSTRAINT IF EXISTS lyrics_embeddings_track_id_fkey;
ALTER TABLE lyrics_embeddings ADD CONSTRAINT lyrics_embeddings_track_id_fkey
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE;

-- listening_history.track_id (ON DELETE SET NULL preserved)
ALTER TABLE listening_history DROP CONSTRAINT IF EXISTS listening_history_track_id_fkey;
ALTER TABLE listening_history ADD CONSTRAINT listening_history_track_id_fkey
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE SET NULL ON UPDATE CASCADE;

-- ============================================================
-- 4. Add ON UPDATE CASCADE to album UUID FK constraints
-- ============================================================

-- album_artists.album_id
ALTER TABLE album_artists DROP CONSTRAINT IF EXISTS album_artists_album_id_fkey;
ALTER TABLE album_artists ADD CONSTRAINT album_artists_album_id_fkey
    FOREIGN KEY (album_id) REFERENCES albums(id) ON DELETE CASCADE ON UPDATE CASCADE;

-- album_variants.album_id
ALTER TABLE album_variants DROP CONSTRAINT IF EXISTS album_variants_album_id_fkey;
ALTER TABLE album_variants ADD CONSTRAINT album_variants_album_id_fkey
    FOREIGN KEY (album_id) REFERENCES albums(id) ON DELETE CASCADE ON UPDATE CASCADE;

-- album_info.album_id
ALTER TABLE album_info DROP CONSTRAINT IF EXISTS album_info_album_id_fkey;
ALTER TABLE album_info ADD CONSTRAINT album_info_album_id_fkey
    FOREIGN KEY (album_id) REFERENCES albums(id) ON DELETE CASCADE ON UPDATE CASCADE;

-- album_tags.album_id
ALTER TABLE album_tags DROP CONSTRAINT IF EXISTS album_tags_album_id_fkey;
ALTER TABLE album_tags ADD CONSTRAINT album_tags_album_id_fkey
    FOREIGN KEY (album_id) REFERENCES albums(id) ON DELETE CASCADE ON UPDATE CASCADE;

-- album_info_embeddings.album_id
ALTER TABLE album_info_embeddings DROP CONSTRAINT IF EXISTS album_info_embeddings_album_id_fkey;
ALTER TABLE album_info_embeddings ADD CONSTRAINT album_info_embeddings_album_id_fkey
    FOREIGN KEY (album_id) REFERENCES albums(id) ON DELETE CASCADE ON UPDATE CASCADE;

-- ============================================================
-- 5. Trigger for artist_members
-- ============================================================

-- No updated_at column, no trigger needed.
