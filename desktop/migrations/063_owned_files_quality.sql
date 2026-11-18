-- The owned_files view carries each copy's quality columns (2026-11-17,
-- SEARCH-GATES): Discovery's quality gate and the CLI's --quality take a
-- track's tier from its best copy, here or at the HQPlayer
-- (sql_queries.QUALITY_GATE). Columns appended, so the view is replaced in
-- place. Mirrors the 001 baseline.

-- A no-op on a baseline whose view already carries arrived_at (065,
-- LIBRARY-WATCH, 2026-11-17): 001 has the quality columns too, and replacing
-- the view here would rename its column back, which PostgreSQL refuses.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = current_schema() AND table_name = 'owned_files'
                     AND column_name = 'arrived_at') THEN
        CREATE OR REPLACE VIEW owned_files AS
            SELECT track_id, album_variant_id, 'local'::variant_location AS location,
                   duration_seconds, recording_mbid, created_at,
                   is_lossless, sample_rate, bit_depth
            FROM media_files
            UNION ALL
            SELECT track_id, album_variant_id, 'hqplayer'::variant_location,
                   duration_seconds, recording_mbid, first_seen_at,
                   is_lossless, sample_rate, bit_depth
            FROM hqp_library_files;
    END IF;
END $$;
