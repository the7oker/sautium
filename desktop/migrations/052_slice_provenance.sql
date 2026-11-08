-- Catalogue slices from any author (2026-11-07): what each kept slice
-- carried and who passed it on, what MusicBrainz's answers proved about it,
-- and the penalties of the slice lanes. The same DDL as the slice_* block of
-- 001_initial.sql, for nodes that ran the earlier files. Idempotent: a no-op
-- on the schema 001 just created.

DO $$ BEGIN
    CREATE TYPE slice_family AS ENUM ('mb', 'lb');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE slice_evidence_kind AS ENUM ('P1', 'P2', 'S1', 'S2');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE slice_entity AS ENUM ('dump_version', 'subtree', 'artist', 'release_group',
                                      'release', 'recording');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE slice_party AS ENUM ('author', 'relay');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS slice_index (
    id              BIGSERIAL PRIMARY KEY,
    family          slice_family NOT NULL,
    key             TEXT         NOT NULL,
    author          CHAR(64)     NOT NULL,
    relay           CHAR(64)     NOT NULL,
    relay_addr      UUID,
    payload_sha256  CHAR(64)     NOT NULL,
    dump_version    TEXT         NOT NULL,
    subject_artists UUID[]       NOT NULL DEFAULT '{}',
    subject_groups  UUID[]       NOT NULL DEFAULT '{}',
    imported_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_slice_index_key ON slice_index (family, key);
CREATE INDEX IF NOT EXISTS idx_slice_index_author ON slice_index (author);
CREATE INDEX IF NOT EXISTS idx_slice_index_relay ON slice_index (relay);
CREATE INDEX IF NOT EXISTS idx_slice_index_payload ON slice_index (payload_sha256);
CREATE INDEX IF NOT EXISTS idx_slice_index_artists ON slice_index USING gin (subject_artists);
CREATE INDEX IF NOT EXISTS idx_slice_index_groups ON slice_index USING gin (subject_groups);

CREATE TABLE IF NOT EXISTS slice_evidence (
    id             BIGSERIAL PRIMARY KEY,
    family         slice_family        NOT NULL,
    key            TEXT                NOT NULL,
    author         CHAR(64)            NOT NULL,
    relay          CHAR(64)            NOT NULL,
    kind           slice_evidence_kind NOT NULL,
    entity         slice_entity        NOT NULL,
    mbid           UUID,
    dump_version   TEXT                NOT NULL,
    payload_sha256 CHAR(64)            NOT NULL,
    seen_at        TIMESTAMPTZ         NOT NULL DEFAULT now(),
    -- Found through one of the blob's subject artists when it has no more
    -- than the check draws (slice_checks.SUBJECTS_PER_CHECK): any node's
    -- check of the blob saw it, the node that passed the blob on included.
    covered        BOOLEAN             NOT NULL DEFAULT FALSE
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_slice_evidence_event
    ON slice_evidence (payload_sha256, kind, entity, mbid) NULLS NOT DISTINCT;
CREATE INDEX IF NOT EXISTS idx_slice_evidence_author ON slice_evidence (author, seen_at);
CREATE INDEX IF NOT EXISTS idx_slice_evidence_relay ON slice_evidence (relay, seen_at);

CREATE TABLE IF NOT EXISTS slice_tally (
    day    DATE         NOT NULL,
    party  slice_party  NOT NULL,
    pubkey CHAR(64)     NOT NULL,
    family slice_family NOT NULL,
    facts  INTEGER      NOT NULL DEFAULT 0,
    events INTEGER      NOT NULL DEFAULT 0,
    PRIMARY KEY (day, party, pubkey, family)
);

CREATE TABLE IF NOT EXISTS slice_penalties (
    pubkey      CHAR(64)    PRIMARY KEY,
    party       slice_party NOT NULL,
    email_token TEXT,
    evidence    BIGINT      REFERENCES slice_evidence(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_slice_penalties_token ON slice_penalties (email_token)
    WHERE email_token IS NOT NULL;
CREATE TABLE IF NOT EXISTS slice_check_plans (
    family     slice_family NOT NULL,
    key        TEXT         NOT NULL,
    subjects   UUID[]       NOT NULL DEFAULT '{}',
    releases   UUID[]       NOT NULL DEFAULT '{}',
    recordings UUID[]       NOT NULL DEFAULT '{}',
    planned_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (family, key)
);
DO $$ BEGIN
    CREATE TYPE mb_slice_table AS ENUM ('mb_artist', 'mb_release_group', 'mb_release', 'mb_recording');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE TABLE IF NOT EXISTS slice_clashes (
    family         slice_family   NOT NULL,
    key            TEXT           NOT NULL,
    payload_sha256 CHAR(64)       NOT NULL,
    entity         mb_slice_table NOT NULL,
    incoming       UUID           NOT NULL,
    held           UUID           NOT NULL,
    -- One MBID under two ids (incoming = held): the two rows' names, which
    -- MusicBrainz's answer for the MBID tells apart.
    incoming_name  TEXT,
    held_name      TEXT,
    noted_at       TIMESTAMPTZ    NOT NULL DEFAULT now(),
    PRIMARY KEY (family, key, payload_sha256, entity, incoming, held)
);
ALTER TABLE slice_clashes ADD COLUMN IF NOT EXISTS incoming_name TEXT;
ALTER TABLE slice_clashes ADD COLUMN IF NOT EXISTS held_name TEXT;
-- A winning author's clash wins a day: past a few, its blob loses instead
-- (desktop/p2p/slice_checks.CLASH_WINS_A_DAY).
CREATE TABLE IF NOT EXISTS slice_clash_wins (
    day    DATE     NOT NULL,
    author CHAR(64) NOT NULL,
    wins   INTEGER  NOT NULL,
    PRIMARY KEY (day, author)
);
CREATE TABLE IF NOT EXISTS slice_burned_mailboxes (
    email_token TEXT        PRIMARY KEY,
    burned_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE mb_slice_blobs ADD COLUMN IF NOT EXISTS relay CHAR(64);
ALTER TABLE mb_slice_blobs ADD COLUMN IF NOT EXISTS checked_at TIMESTAMPTZ;
ALTER TABLE mb_slice_blobs ADD COLUMN IF NOT EXISTS suspect BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE mb_slice_blobs ADD COLUMN IF NOT EXISTS contested BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE mb_slice_blobs ADD COLUMN IF NOT EXISTS payload_sha256 CHAR(64);
ALTER TABLE mb_slice_blobs ADD COLUMN IF NOT EXISTS mbids UUID[] NOT NULL DEFAULT '{}';
UPDATE mb_slice_blobs SET payload_sha256 = encode(sha256(blob_gz), 'hex') WHERE payload_sha256 IS NULL;
CREATE INDEX IF NOT EXISTS idx_mb_slice_blobs_mbids ON mb_slice_blobs USING gin (mbids);
ALTER TABLE lb_slice_blobs ADD COLUMN IF NOT EXISTS relay CHAR(64);
ALTER TABLE lb_slice_blobs ADD COLUMN IF NOT EXISTS checked_at TIMESTAMPTZ;
ALTER TABLE lb_slice_blobs ADD COLUMN IF NOT EXISTS suspect BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE mb_slice_fetches ADD COLUMN IF NOT EXISTS relay CHAR(64);
ALTER TABLE mb_slice_fetches ADD COLUMN IF NOT EXISTS void BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE lb_slice_fetches ADD COLUMN IF NOT EXISTS relay CHAR(64);

-- An MB artist or release group is one row per MBID. A gid two rows share
-- came from a slice (a dump has none); the rows go, and the node's rebuild
-- from its kept slices puts back what MusicBrainz's answers settle.
DELETE FROM mb_artist WHERE gid IN (SELECT gid FROM mb_artist WHERE gid IS NOT NULL
                                     GROUP BY gid HAVING count(*) > 1);
DELETE FROM mb_release_group WHERE gid IN (SELECT gid FROM mb_release_group WHERE gid IS NOT NULL
                                            GROUP BY gid HAVING count(*) > 1);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mb_artist_gid ON mb_artist (gid);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mb_release_group_gid ON mb_release_group (gid);
DROP INDEX IF EXISTS idx_mb_artist_gid;
DROP INDEX IF EXISTS idx_mb_rg_gid;
