-- 067 — CREDITS-1 step 2a (2026-11-18): the same DDL as 001_initial.sql's
-- blocks, for nodes that ran the earlier files. Idempotent: a no-op on the
-- schema 001 just created.
--
-- track_credits: who wrote a track's music, apart from who performs it —
-- a credit, never a track_artists role, so the engagement gate never sees
-- it. Empty until the composer read writes it; artist_id RESTRICTs, so a
-- sweep or merge that forgets the table fails instead of dropping a credit.

DO $$ BEGIN
    CREATE TYPE track_credit_role AS ENUM ('composer');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE track_credit_source AS ENUM ('tag', 'mb');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS track_credits (
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE,
    role track_credit_role NOT NULL,
    artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE RESTRICT ON UPDATE CASCADE,
    source track_credit_source NOT NULL,
    position SMALLINT NOT NULL,
    PRIMARY KEY (track_id, role, artist_id, source)
);
CREATE INDEX IF NOT EXISTS idx_track_credits_artist ON track_credits(artist_id, role);
