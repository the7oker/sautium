-- 068 — PI-NODE-2B (2026-11-19): the same DDL as 001_initial.sql's block,
-- for nodes that ran the earlier files. Idempotent: a no-op on the schema
-- 001 just created.
--
-- The library's folder as the owner chose it in the Web UI, on a node whose
-- console setup gave it roots to choose under (backend/library_root.py;
-- PI-NODE-2B, 2026-11-19): the root it lies under and the path beneath it
-- ('' = the root itself). The newest row is the folder; an earlier choice
-- stays as a row — a root with an identity, for the day a node reads more
-- than one. Whether the folder is there now is the mount table's to say,
-- never stored. Empty on every node whose folder is named outside it (a
-- launcher's, a Docker node's MUSIC_LIBRARY_PATH).
CREATE TABLE IF NOT EXISTS library_roots (
    id SERIAL PRIMARY KEY,
    mount TEXT NOT NULL,
    subpath TEXT NOT NULL DEFAULT '',
    chosen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT library_roots_place UNIQUE (mount, subpath)
);
