-- Track listening statistics move from Last.fm to ListenBrainz (2026-09-20).
-- Last.fm's terms kept track_stats node-local (019); the ListenBrainz
-- statistics dump is CC0, so the replacement layer (lb_*) is redistributable
-- between nodes as signed per-artist slices. Mirrors the 001 baseline.
--
-- track_stats goes away with its constraints, indexes and trigger; the
-- Last.fm negative-cache rows that only the deleted fetch step consulted go
-- with it.
DROP TABLE IF EXISTS track_stats;
DELETE FROM external_metadata
 WHERE entity_type = 'track' AND source = 'lastfm' AND metadata_type = 'stats';

-- ListenBrainz listening statistics (CC0, the listenbrainz.org statistics
-- dump — backend/lb_dump_load.py). Per recording: SUM/COUNT over every LB
-- user's all-time TOP-1000 list, so both counts are LOWER BOUNDS of the true
-- totals (a listen outside a user's top 1000 is not in the dump). A dump node
-- holds the whole table (TRUNCATE + reload per dump version); every other
-- node holds per-artist slices (desktop/p2p/lb_slice_queries.py) and
-- lb_slice_fetches is its closed-world ledger per artist MBID, versioned.
-- dump_version rides on every row because a slice node mixes versions:
-- imports move a row forward only, never back.
CREATE TABLE IF NOT EXISTS lb_recording (
    recording_mbid UUID    PRIMARY KEY,
    listen_count   BIGINT  NOT NULL,
    user_count     INTEGER NOT NULL,
    artist_mbids   UUID[]  NOT NULL DEFAULT '{}',
    dump_version   TEXT    NOT NULL            -- YYYYMMDD-HHMMSS: lexical order = chronological
);
CREATE INDEX IF NOT EXISTS idx_lb_recording_artists ON lb_recording USING gin (artist_mbids);

CREATE TABLE IF NOT EXISTS lb_artist (
    artist_mbid  UUID    PRIMARY KEY,
    listen_count BIGINT  NOT NULL,
    user_count   INTEGER NOT NULL,
    dump_version TEXT    NOT NULL
);

-- One row per artist MBID ever asked of the network: the version INSIDE the
-- signed blob, the author (verified before import) and the peer that relayed
-- it. recordings = 0 is a signed zero-match — "unknown to ListenBrainz at
-- that version" — and is re-asked like any row once a reachable source
-- advertises a newer dump.
CREATE TABLE IF NOT EXISTS lb_slice_fetches (
    artist_mbid    UUID PRIMARY KEY,
    dump_version   TEXT NOT NULL,
    recordings     INTEGER NOT NULL DEFAULT 0,
    source_node    TEXT,
    source_pubkey  TEXT,
    receipt        TEXT,
    payload_sha256 TEXT,
    source_addr    UUID,
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Verified per-artist blobs kept verbatim with the ORIGINAL dump node's
-- signature (the mb_slice_blobs pattern): a dump node's cache, a replica's
-- re-serve inventory and the wire payload itself.
CREATE TABLE IF NOT EXISTS lb_slice_blobs (
    artist_mbid   UUID PRIMARY KEY,
    dump_version  TEXT NOT NULL,
    author_pubkey CHAR(64) NOT NULL,
    sig           CHAR(128) NOT NULL,
    blob_gz       BYTEA NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The on-demand lane: an artist page opened on a slice node. The row is the
-- durable request (served first by the slice cycle); the NOTIFY that
-- accompanies it is only the wake.
CREATE TABLE IF NOT EXISTS lb_slice_requests (
    artist_id    UUID PRIMARY KEY REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The MBID set grew (canon, AI canon, mint, seed import, carry — a dozen
-- insert sites): one wake for the slice cycle from the table itself.
CREATE OR REPLACE FUNCTION notify_lb_pending() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('sautium_lb_pending', '');
    RETURN NULL;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_artist_mbids_lb_pending ON artist_mbids;
CREATE TRIGGER trg_artist_mbids_lb_pending AFTER INSERT ON artist_mbids
    FOR EACH STATEMENT EXECUTE FUNCTION notify_lb_pending();
