-- The owned_files view carries each copy's quality columns (2026-11-17,
-- SEARCH-GATES): Discovery's quality gate and the CLI's --quality take a
-- track's tier from its best copy, here or at the HQPlayer
-- (sql_queries.QUALITY_GATE). Columns appended, so the view is replaced in
-- place. Mirrors the 001 baseline.

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
