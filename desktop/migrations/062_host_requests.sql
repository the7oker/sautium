-- 062 — what the node asked of the host agent and what it answered
-- (2026-11-15, PI-NODE-2A): the same DDL as the host_requests block of
-- 001_initial.sql, for nodes that ran the earlier files. Idempotent: a no-op on
-- the schema 001 just created.

DO $$ BEGIN
    CREATE TYPE host_verb AS ENUM ('check', 'update', 'diagnostics');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE host_outcome AS ENUM ('current', 'offered', 'rewrite', 'updated', 'undone',
                                      'done', 'refused', 'floor', 'failed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS host_requests (
    verb host_verb PRIMARY KEY,
    asked_at TIMESTAMPTZ,
    outcome host_outcome,
    detail TEXT,
    release TEXT,
    report TEXT,
    answered_at TIMESTAMPTZ
);

