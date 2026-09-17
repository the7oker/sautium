-- 016 — Bandcamp URLs from MusicBrainz (2026-09-17). The Buy button on a
-- phantom album used to open a Bandcamp search built from the credits, which
-- often lands on nothing; Bandcamp offers no catalogue API and its site sits
-- behind a bot challenge, while MusicBrainz carries the exact page as a
-- release-URL relationship. Three dump tables, Bandcamp rows only (the loader
-- filters at COPY time — mb_dump_load._ROW_FILTERS). Mirrors the 001 blocks
-- verbatim, so it is a no-op on a database that just ran 001.

CREATE TABLE IF NOT EXISTS mb_url (
    id            INTEGER PRIMARY KEY,
    gid           UUID,
    url           TEXT,
    edits_pending INTEGER,
    last_updated  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS mb_l_artist_url (
    id             INTEGER PRIMARY KEY,
    link           INTEGER,
    entity0        INTEGER,        -- mb_artist.id
    entity1        INTEGER,        -- mb_url.id
    edits_pending  INTEGER,
    last_updated   TIMESTAMPTZ,
    link_order     INTEGER,
    entity0_credit TEXT,
    entity1_credit TEXT
);

CREATE TABLE IF NOT EXISTS mb_l_release_url (
    id             INTEGER PRIMARY KEY,
    link           INTEGER,
    entity0        INTEGER,        -- mb_release.id
    entity1        INTEGER,        -- mb_url.id
    edits_pending  INTEGER,
    last_updated   TIMESTAMPTZ,
    link_order     INTEGER,
    entity0_credit TEXT,
    entity1_credit TEXT
);

CREATE INDEX IF NOT EXISTS idx_mb_l_artist_url_artist   ON mb_l_artist_url(entity0);
CREATE INDEX IF NOT EXISTS idx_mb_l_release_url_release ON mb_l_release_url(entity0);

-- The slice wire format grew: a v3 blob carries the artist's url subtree, and
-- every blob signed before this delta lacks it (its signature context is v2,
-- so a v3 requester cannot even verify it). Reset both slice ledgers on EVERY
-- node: a dump node rebuilds its cache on demand, a replica's inventory
-- refills from its own re-fetch, and a requester's provenance log re-opens
-- every name so pending_slice_names asks again — the rows already imported
-- stay (ON CONFLICT DO NOTHING), only the url rows are new. Neither table is
-- a fact about music; both record who answered what.
CREATE TABLE IF NOT EXISTS mb_slice_blobs (
    name_key      TEXT PRIMARY KEY,
    dump_version  TEXT NOT NULL,
    author_pubkey CHAR(64) NOT NULL,
    sig           CHAR(128) NOT NULL,
    blob_gz       BYTEA NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
DELETE FROM mb_slice_blobs;
DELETE FROM mb_slice_fetches;
