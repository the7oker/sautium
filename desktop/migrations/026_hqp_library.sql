-- The HQPlayer library as a source of album variants (2026-09-27).
--
-- A copy of an album that lives in an HQPlayer's own library (a disk on an
-- HQPlayer Embedded box, a share it mounts, a hi-res subset copied there) is
-- one more VARIANT of the album — the same thing a CD rip next to a vinyl rip
-- already is — located at that HQPlayer instead of on this node's disk.
-- album_variants says where a variant lives; hqp_library_files mirrors
-- media_files for the files HQPlayer holds (there are no bytes here: no
-- analysis source, no CUE slices, no cover extraction). Mirrors the 001
-- baseline; a no-op on a database 001 already created with these blocks.

DO $$ BEGIN
    CREATE TYPE variant_location AS ENUM ('local', 'hqplayer');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE album_variants
    ADD COLUMN IF NOT EXISTS location variant_location NOT NULL DEFAULT 'local',
    ADD COLUMN IF NOT EXISTS hqp_endpoint_host TEXT,
    ADD COLUMN IF NOT EXISTS hqp_endpoint_port INTEGER;

-- The same directory string can name a local folder AND the folder an
-- HQPlayer on this very box scans (a Linux node running Embedded on itself),
-- so the endpoint is part of the variant's key; local rows carry NULLs and
-- NULLS NOT DISTINCT keeps them unique among themselves.
ALTER TABLE album_variants DROP CONSTRAINT IF EXISTS album_variants_dir_album_key;
ALTER TABLE album_variants DROP CONSTRAINT IF EXISTS album_variants_dir_album_endpoint_key;
ALTER TABLE album_variants ADD CONSTRAINT album_variants_dir_album_endpoint_key
    UNIQUE NULLS NOT DISTINCT (directory_path, album_id, hqp_endpoint_host, hqp_endpoint_port);
ALTER TABLE album_variants DROP CONSTRAINT IF EXISTS chk_album_variants_location;
ALTER TABLE album_variants ADD CONSTRAINT chk_album_variants_location CHECK (
    (location = 'local' AND hqp_endpoint_host IS NULL AND hqp_endpoint_port IS NULL)
    OR (location = 'hqplayer' AND hqp_endpoint_host IS NOT NULL AND hqp_endpoint_port IS NOT NULL));

CREATE TABLE IF NOT EXISTS hqp_library_files (
    id SERIAL PRIMARY KEY,
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE,
    album_variant_id INTEGER NOT NULL REFERENCES album_variants(id) ON DELETE CASCADE ON UPDATE CASCADE,
    hqp_path TEXT NOT NULL,                 -- LibraryDirectory.path + LibraryFile.name, forward slashes; the file:// URI HQPlayer opens
    hqp_file_hash TEXT,                     -- LibraryFile.hash (an MD5 of the file name) — change detection only
    hqp_dir_hash TEXT,                      -- LibraryDirectory.hash (an MD5 of the directory path)
    file_format audio_file_format,
    is_lossless BOOLEAN DEFAULT TRUE,
    sample_rate INTEGER,
    bit_depth INTEGER,
    bitrate INTEGER,
    channels INTEGER,
    duration_seconds NUMERIC(10, 2),
    track_number INTEGER,
    disc_number INTEGER DEFAULT 1,
    -- Tags as HQPlayer reported them — the same ground truth media_files keeps
    raw_track_name TEXT,
    raw_artist TEXT,
    raw_album_artist TEXT,
    raw_album TEXT,
    raw_year TEXT,
    first_seen_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_hqp_library_files_variant_path UNIQUE (album_variant_id, hqp_path),
    CONSTRAINT chk_hqp_library_files_duration CHECK (duration_seconds IS NULL OR duration_seconds >= 0)
);
CREATE INDEX IF NOT EXISTS idx_hqp_library_files_track_id ON hqp_library_files(track_id);
