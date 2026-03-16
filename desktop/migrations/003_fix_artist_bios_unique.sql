-- Migration 003: Restore missing UNIQUE constraint on artist_bios(artist_id, source)
-- The constraint was defined in 001_initial.sql but got dropped during artist normalization.
-- This caused sync errors: "ON CONFLICT DO UPDATE cannot affect row a second time"

-- Step 1: Remove duplicates, keeping the most recently updated row
DELETE FROM artist_bios
WHERE id NOT IN (
    SELECT DISTINCT ON (artist_id, source) id
    FROM artist_bios
    ORDER BY artist_id, source, updated_at DESC NULLS LAST, id DESC
);

-- Step 2: Restore the UNIQUE constraint
ALTER TABLE artist_bios
    ADD CONSTRAINT uq_artist_bios UNIQUE (artist_id, source);
