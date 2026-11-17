-- 064 — LIBRARY-WATCH (2026-11-17): the same DDL as 001_initial.sql's
-- blocks, for nodes that ran the earlier files. Idempotent: a no-op on the
-- schema 001 just created.
--
-- media_files: the file's stat at its last read and the analysis passes'
-- refusal of a decode (M6); the genre tag as read (album_genres' filetag
-- counts derive from it); a CUE slice's sheet stat; the file's arrival (its
-- birth on this disk, else its first read — here, the row's creation, which a
-- read by this release replaces with the birth where the disk keeps one) and
-- the moment its content last changed what the canon reads.
-- album_variants: the copy's arrival (was file_modified_at, the latest file
-- mtime — a copy keeps its source's mtime, so it never said when an album
-- came in) and the album credit the copy was minted under.

ALTER TABLE media_files ADD COLUMN IF NOT EXISTS stat_mtime_ns BIGINT;
ALTER TABLE media_files ADD COLUMN IF NOT EXISTS stat_ctime_ns BIGINT;
ALTER TABLE media_files ADD COLUMN IF NOT EXISTS decode_refused BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE media_files ADD COLUMN IF NOT EXISTS raw_genre TEXT;
ALTER TABLE media_files ADD COLUMN IF NOT EXISTS track_total INTEGER;
ALTER TABLE media_files ADD COLUMN IF NOT EXISTS disc_total INTEGER;
ALTER TABLE media_files ADD COLUMN IF NOT EXISTS cue_stat BIGINT[];
ALTER TABLE media_files ADD COLUMN IF NOT EXISTS arrived_at TIMESTAMPTZ;
ALTER TABLE media_files ADD COLUMN IF NOT EXISTS content_at TIMESTAMPTZ;
UPDATE media_files SET arrived_at = COALESCE(created_at, now()) WHERE arrived_at IS NULL;
UPDATE media_files SET content_at = COALESCE(created_at, now()) WHERE content_at IS NULL;
ALTER TABLE media_files ALTER COLUMN arrived_at SET DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE media_files ALTER COLUMN arrived_at SET NOT NULL;
ALTER TABLE media_files ALTER COLUMN content_at SET DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE media_files ALTER COLUMN content_at SET NOT NULL;

ALTER TABLE album_variants ADD COLUMN IF NOT EXISTS raw_artist TEXT;

DROP TRIGGER IF EXISTS trg_media_files_av_mtime_ins ON media_files;
DROP TRIGGER IF EXISTS trg_media_files_av_mtime_upd ON media_files;
DROP TRIGGER IF EXISTS trg_media_files_av_mtime_del ON media_files;
DROP FUNCTION IF EXISTS trg_mf_av_mtime_ins();
DROP FUNCTION IF EXISTS trg_mf_av_mtime_upd();
DROP FUNCTION IF EXISTS trg_mf_av_mtime_del();
DROP FUNCTION IF EXISTS refresh_av_file_modified_at(INTEGER[]);

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = 'album_variants'
                  AND column_name = 'file_modified_at') THEN
        ALTER TABLE album_variants RENAME COLUMN file_modified_at TO arrived_at;
        UPDATE album_variants av
           SET arrived_at = (SELECT MIN(mf.arrived_at) FROM media_files mf
                              WHERE mf.album_variant_id = av.id)
         WHERE av.location = 'local';
    END IF;
END $$;
ALTER INDEX IF EXISTS idx_album_variants_file_modified_at_desc RENAME TO idx_album_variants_arrived_at_desc;
CREATE INDEX IF NOT EXISTS idx_album_variants_arrived_at_desc
    ON album_variants(arrived_at DESC NULLS LAST);

CREATE OR REPLACE FUNCTION refresh_av_arrived_at(p_av_ids INTEGER[])
RETURNS VOID AS $$
BEGIN
    UPDATE album_variants av
    SET arrived_at = (
        SELECT MIN(mf.arrived_at)
        FROM media_files mf
        WHERE mf.album_variant_id = av.id
    )
    WHERE av.id = ANY(p_av_ids) AND av.location = 'local';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION trg_mf_av_arrival_ins() RETURNS TRIGGER AS $$
BEGIN
    PERFORM refresh_av_arrived_at(
        ARRAY(SELECT DISTINCT album_variant_id FROM new_table)
    );
    RETURN NULL;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION trg_mf_av_arrival_upd() RETURNS TRIGGER AS $$
BEGIN
    PERFORM refresh_av_arrived_at(
        ARRAY(
            SELECT DISTINCT av_id FROM (
                SELECT n.album_variant_id AS av_id
                FROM new_table n
                JOIN old_table o ON o.id = n.id
                WHERE n.arrived_at IS DISTINCT FROM o.arrived_at
                   OR n.album_variant_id IS DISTINCT FROM o.album_variant_id
                UNION
                SELECT o.album_variant_id
                FROM new_table n
                JOIN old_table o ON o.id = n.id
                WHERE n.album_variant_id IS DISTINCT FROM o.album_variant_id
            ) affected
        )
    );
    RETURN NULL;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION trg_mf_av_arrival_del() RETURNS TRIGGER AS $$
BEGIN
    PERFORM refresh_av_arrived_at(
        ARRAY(SELECT DISTINCT album_variant_id FROM old_table)
    );
    RETURN NULL;
END $$ LANGUAGE plpgsql;

DO $$ BEGIN
    CREATE TRIGGER trg_media_files_av_arrival_ins
    AFTER INSERT ON media_files
    REFERENCING NEW TABLE AS new_table
    FOR EACH STATEMENT EXECUTE FUNCTION trg_mf_av_arrival_ins();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER trg_media_files_av_arrival_upd
    AFTER UPDATE ON media_files
    REFERENCING NEW TABLE AS new_table OLD TABLE AS old_table
    FOR EACH STATEMENT EXECUTE FUNCTION trg_mf_av_arrival_upd();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER trg_media_files_av_arrival_del
    AFTER DELETE ON media_files
    REFERENCING OLD TABLE AS old_table
    FOR EACH STATEMENT EXECUTE FUNCTION trg_mf_av_arrival_del();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = 'owned_files'
                  AND column_name = 'created_at') THEN
        ALTER VIEW owned_files RENAME COLUMN created_at TO arrived_at;
    END IF;
END $$;
CREATE OR REPLACE VIEW owned_files AS
    SELECT track_id, album_variant_id, 'local'::variant_location AS location,
           duration_seconds, recording_mbid, arrived_at,
           is_lossless, sample_rate, bit_depth, content_at
    FROM media_files
    UNION ALL
    SELECT track_id, album_variant_id, 'hqplayer'::variant_location,
           duration_seconds, recording_mbid, first_seen_at,
           is_lossless, sample_rate, bit_depth, first_seen_at
    FROM hqp_library_files;
