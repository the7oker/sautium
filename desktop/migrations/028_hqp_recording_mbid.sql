-- A file the HQPlayer holds gets the same materialised MB recording as a
-- file here (2026-09-27): the canon compares an album's variants by their
-- recording sets, and a variant with none looked like another edition of
-- the same rip — the local rip and its copy at the HQPlayer ended up as
-- twin albums "(Alt)" inside one release group. Mirrors the 001 baseline.

ALTER TABLE hqp_library_files ADD COLUMN IF NOT EXISTS recording_mbid UUID;
CREATE INDEX IF NOT EXISTS idx_hqp_library_files_recording_mbid
    ON hqp_library_files(recording_mbid) WHERE recording_mbid IS NOT NULL;

CREATE OR REPLACE VIEW owned_files AS
    SELECT track_id, album_variant_id, 'local'::variant_location AS location,
           duration_seconds, recording_mbid, created_at
    FROM media_files
    UNION ALL
    SELECT track_id, album_variant_id, 'hqplayer'::variant_location,
           duration_seconds, recording_mbid, first_seen_at
    FROM hqp_library_files;
