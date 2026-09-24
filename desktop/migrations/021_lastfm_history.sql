-- Last.fm listening-history import (2026-09-24): the owner's scrobbles become
-- listens on canonical tracks once the canon has placed them
-- (backend/lastfm_history.py, backend/canon/scrobbles.py). LOCAL-ONLY, like
-- the rest of the Last.fm layer. Mirrors the 001 baseline.

DO $$ BEGIN
    CREATE TYPE listen_source AS ENUM ('sautium', 'lastfm');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Who recorded the listen / the card. A constant default: no table rewrite.
ALTER TABLE listening_history  ADD COLUMN IF NOT EXISTS source listen_source NOT NULL DEFAULT 'sautium';
ALTER TABLE listening_sessions ADD COLUMN IF NOT EXISTS source listen_source NOT NULL DEFAULT 'sautium';

CREATE TABLE IF NOT EXISTS lastfm_import (
    username       TEXT PRIMARY KEY,
    watermark_at   TIMESTAMPTZ,
    walk_top_at    TIMESTAMPTZ,
    walk_cursor_at TIMESTAMPTZ,
    walk_total     INTEGER,
    walk_fetched   INTEGER NOT NULL DEFAULT 0,
    last_sync_at   TIMESTAMPTZ,
    last_error     TEXT
);

CREATE TABLE IF NOT EXISTS pending_scrobble_artists (
    name_key   TEXT PRIMARY KEY,
    artist_id  UUID REFERENCES artists(id) ON DELETE SET NULL ON UPDATE CASCADE,
    touched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    checked_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS pending_scrobbles (
    played_at   TIMESTAMPTZ NOT NULL,
    artist      TEXT NOT NULL,
    title       TEXT NOT NULL,
    album       TEXT,
    name_key    TEXT NOT NULL REFERENCES pending_scrobble_artists(name_key) ON DELETE CASCADE,
    artist_mbid UUID,
    album_mbid  UUID,
    track_mbid  UUID,
    PRIMARY KEY (played_at, artist, title)
);

CREATE INDEX IF NOT EXISTS idx_listening_history_imported ON listening_history(started_at) WHERE source = 'lastfm';
CREATE INDEX IF NOT EXISTS idx_pending_scrobble_artists_artist ON pending_scrobble_artists(artist_id) WHERE artist_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pending_scrobbles_name ON pending_scrobbles(name_key);
