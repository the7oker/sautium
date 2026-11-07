-- Adds of this node's code by plain code, waiting for the person's "Add back"
-- (2026-11-07). The same DDL as the friend_requests block of 001_initial.sql,
-- for nodes that ran the earlier files. Idempotent: a no-op on the schema 001
-- just created.
CREATE TABLE IF NOT EXISTS friend_requests (
    id SERIAL PRIMARY KEY,
    invite_code VARCHAR(96) NOT NULL UNIQUE,
    public_key_hex VARCHAR(128) NOT NULL,
    username VARCHAR(64) NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL,
    ignored_at TIMESTAMPTZ,
    answered_at TIMESTAMPTZ
);
