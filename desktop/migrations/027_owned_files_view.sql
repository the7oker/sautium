-- One definition of "this node holds the track" for the canon and the gates
-- (2026-09-27): a file on this node's disk (media_files) or a file the
-- HQPlayer the node drives holds in its own library (hqp_library_files).
-- Presence, coverage and "content newer than the artist's last canon" read
-- this view; the analysis source stays on media_files, the only rows with
-- bytes. Mirrors the 001 baseline (028 adds the HQPlayer files' recording_mbid).

-- Guarded since 2026-11-17: 063 appends the quality columns, and a fresh node
-- runs 001 (which has them) before this file — replacing the view here would
-- drop them, which PostgreSQL refuses.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = current_schema() AND table_name = 'owned_files'
                     AND column_name = 'is_lossless') THEN
        CREATE OR REPLACE VIEW owned_files AS
            SELECT track_id, album_variant_id, 'local'::variant_location AS location,
                   duration_seconds, recording_mbid, created_at
            FROM media_files
            UNION ALL
            SELECT track_id, album_variant_id, 'hqplayer'::variant_location,
                   duration_seconds, NULL::uuid, first_seen_at
            FROM hqp_library_files;
    END IF;
END $$;
