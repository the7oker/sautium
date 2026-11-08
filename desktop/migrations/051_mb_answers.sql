-- MusicBrainz's own answers, asked by MBID on a node that has catalogue
-- slices and no MusicBrainz database (2026-11-07). The same DDL as the
-- mbws_* block of 001_initial.sql, for nodes that ran the earlier files.
-- Idempotent: a no-op on the schema 001 just created.

DO $$ BEGIN
    CREATE TYPE mbws_kind AS ENUM ('artist', 'release_group', 'release', 'recording',
                                   'artist_release_groups');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE mbws_status AS ENUM ('found', 'forwarded', 'unknown');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE mbws_credit_owner AS ENUM ('release_group', 'release', 'track', 'recording');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE mbws_subject AS ENUM ('owned_artist', 'discography', 'phantom', 'credit_head',
                                      'scrobble_name', 'slice_check');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS mbws_answers (
    kind        mbws_kind   NOT NULL,
    mbid        UUID        NOT NULL,
    status      mbws_status NOT NULL,
    target      UUID,
    answered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, mbid)
);

CREATE TABLE IF NOT EXISTS mbws_artists (
    mbid           UUID PRIMARY KEY,
    name           TEXT NOT NULL,
    sort_name      TEXT,
    disambiguation TEXT,
    type           TEXT,
    area           TEXT
);
CREATE TABLE IF NOT EXISTS mbws_artist_aliases (
    artist    UUID     NOT NULL REFERENCES mbws_artists(mbid) ON DELETE CASCADE,
    ord       SMALLINT NOT NULL,
    name      TEXT     NOT NULL,
    sort_name TEXT,
    locale    TEXT,
    PRIMARY KEY (artist, ord)
);
CREATE TABLE IF NOT EXISTS mbws_artist_genres (
    artist UUID    NOT NULL REFERENCES mbws_artists(mbid) ON DELETE CASCADE,
    genre  TEXT    NOT NULL,
    count  INTEGER NOT NULL,
    PRIMARY KEY (artist, genre)
);
CREATE TABLE IF NOT EXISTS mbws_artist_members (
    artist      UUID NOT NULL REFERENCES mbws_artists(mbid) ON DELETE CASCADE,
    group_mbid  UUID NOT NULL,
    member_mbid UUID NOT NULL,
    PRIMARY KEY (artist, group_mbid, member_mbid)
);
CREATE TABLE IF NOT EXISTS mbws_artist_urls (
    artist UUID NOT NULL REFERENCES mbws_artists(mbid) ON DELETE CASCADE,
    url    TEXT NOT NULL,
    PRIMARY KEY (artist, url)
);

CREATE TABLE IF NOT EXISTS mbws_release_groups (
    mbid               UUID PRIMARY KEY,
    title              TEXT NOT NULL,
    primary_type       TEXT,
    first_release_date TEXT
);
CREATE TABLE IF NOT EXISTS mbws_release_group_types (
    release_group  UUID NOT NULL REFERENCES mbws_release_groups(mbid) ON DELETE CASCADE,
    secondary_type TEXT NOT NULL,
    PRIMARY KEY (release_group, secondary_type)
);
-- browsed_at: the start of the browse that listed the group; the browse's
-- last page removes what an earlier one listed and it did not.
CREATE TABLE IF NOT EXISTS mbws_artist_release_groups (
    artist        UUID        NOT NULL,
    release_group UUID        NOT NULL,
    browsed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (artist, release_group)
);

CREATE TABLE IF NOT EXISTS mbws_releases (
    mbid          UUID PRIMARY KEY,
    title         TEXT NOT NULL,
    status        TEXT,
    barcode       TEXT,
    release_group UUID,
    date          TEXT
);
CREATE INDEX IF NOT EXISTS idx_mbws_releases_group ON mbws_releases (release_group);
CREATE TABLE IF NOT EXISTS mbws_release_urls (
    release UUID NOT NULL REFERENCES mbws_releases(mbid) ON DELETE CASCADE,
    url     TEXT NOT NULL,
    PRIMARY KEY (release, url)
);
CREATE TABLE IF NOT EXISTS mbws_tracks (
    release      UUID     NOT NULL REFERENCES mbws_releases(mbid) ON DELETE CASCADE,
    disc         SMALLINT NOT NULL,
    position     SMALLINT NOT NULL,
    mbid         UUID,
    title        TEXT     NOT NULL,
    length_ms    INTEGER,
    recording    UUID,
    media_format TEXT,
    PRIMARY KEY (release, disc, position)
);
CREATE INDEX IF NOT EXISTS idx_mbws_tracks_recording ON mbws_tracks (recording);

CREATE TABLE IF NOT EXISTS mbws_recordings (
    mbid      UUID PRIMARY KEY,
    title     TEXT NOT NULL,
    length_ms INTEGER
);

CREATE TABLE IF NOT EXISTS mbws_credits (
    owner_kind  mbws_credit_owner NOT NULL,
    owner       UUID     NOT NULL,
    position    SMALLINT NOT NULL,
    artist      UUID     NOT NULL,
    artist_name TEXT     NOT NULL,
    credited_as TEXT     NOT NULL,
    join_phrase TEXT     NOT NULL DEFAULT '',
    PRIMARY KEY (owner_kind, owner, position)
);
CREATE INDEX IF NOT EXISTS idx_mbws_credits_artist ON mbws_credits (artist);

-- attempts, not_before: an item whose own request failed is asked again
-- only after not_before, while the rest of the queue goes on. browse_*: how
-- far a browse of an artist's groups got (the artist it is for, when it
-- began, the next page's offset), so a page is a step and a failure resumes.
CREATE TABLE IF NOT EXISTS mbws_queue (
    kind           mbws_kind   NOT NULL,
    mbid           UUID        NOT NULL,
    priority       SMALLINT    NOT NULL,
    author         TEXT,
    queued_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts       SMALLINT    NOT NULL DEFAULT 0,
    not_before     TIMESTAMPTZ,
    browse_target  UUID,
    browse_started TIMESTAMPTZ,
    browse_offset  INTEGER     NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, mbid)
);
CREATE INDEX IF NOT EXISTS idx_mbws_queue_order ON mbws_queue (priority, queued_at);
CREATE TABLE IF NOT EXISTS mbws_waiters (
    kind        mbws_kind    NOT NULL,
    mbid        UUID         NOT NULL,
    subject     mbws_subject NOT NULL,
    subject_key TEXT         NOT NULL,
    PRIMARY KEY (kind, mbid, subject, subject_key),
    FOREIGN KEY (kind, mbid) REFERENCES mbws_queue (kind, mbid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mbws_waiters_subject ON mbws_waiters (subject, subject_key);

CREATE TABLE IF NOT EXISTS mb_refuted_bindings (
    artist_id  UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE,
    mbid       UUID NOT NULL,
    refuted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (artist_id, mbid)
);

CREATE OR REPLACE FUNCTION artist_mbids_refuted() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM mb_refuted_bindings r
                WHERE r.artist_id = NEW.artist_id AND r.mbid = NEW.mbid) THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DO $$ BEGIN CREATE TRIGGER artist_mbids_refuted_trg
    BEFORE INSERT OR UPDATE OF mbid, artist_id ON artist_mbids
    FOR EACH ROW EXECUTE FUNCTION artist_mbids_refuted();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
