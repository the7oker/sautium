-- 004 — support diagnostics (P2P_NETWORK.md § "Support diagnostics").
--
-- Node side: diag_events is the node's own durable incident log (every
-- node records; a launcher node ships the content-free part to the master
-- as reports and the whole thing inside an `events` bundle scope), and
-- diag_warrants is the per-warrant state machine that makes a warrant
-- single-use: the PRIMARY KEY decides first receipt, the timestamps
-- decide what a re-delivery resumes.
--
-- Master side: support_* hold what user nodes sent — reports (decrypted,
-- queryable), issued warrants, and the boxed bundles as received.
-- The schema ships to every node (same file, same runner); only the
-- master ever writes the support_* rows.
--
-- Idempotent — mirrored into 001_initial.sql; a fresh node runs both.

CREATE TABLE IF NOT EXISTS diag_events (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind TEXT NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    reported_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_diag_events_unreported
    ON diag_events (ts) WHERE reported_at IS NULL;

-- NOTIFY in a trigger so every writer wakes the shipper — the backend
-- (settings, chat), the launcher and raw psql alike.
CREATE OR REPLACE FUNCTION notify_diag_event() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('sautium_diag', NEW.id::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$ BEGIN
    CREATE TRIGGER trg_diag_events_notify AFTER INSERT ON diag_events
        FOR EACH ROW EXECUTE FUNCTION notify_diag_event();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS diag_warrants (
    id UUID PRIMARY KEY,
    issuer TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    scopes TEXT[] NOT NULL,
    since TIMESTAMPTZ,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    collected_at TIMESTAMPTZ,
    uploaded_at TIMESTAMPTZ,
    size_bytes BIGINT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS support_warrants (
    id UUID PRIMARY KEY,
    node_pubkey TEXT NOT NULL,
    scopes TEXT[] NOT NULL,
    since TIMESTAMPTZ,
    note TEXT,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    dispatched_at TIMESTAMPTZ,
    fulfilled_at TIMESTAMPTZ,
    bundle_id UUID          -- no FK: support_bundles references this table
);
CREATE INDEX IF NOT EXISTS idx_support_warrants_node
    ON support_warrants (node_pubkey, issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_support_warrants_open
    ON support_warrants (node_pubkey) WHERE fulfilled_at IS NULL;

CREATE TABLE IF NOT EXISTS support_bundles (
    id UUID PRIMARY KEY,
    warrant_id UUID NOT NULL REFERENCES support_warrants(id) ON DELETE CASCADE,
    node_pubkey TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    size_bytes BIGINT NOT NULL,
    sha256 TEXT NOT NULL,
    ciphertext BYTEA NOT NULL,
    opened_at TIMESTAMPTZ,
    extract_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_support_bundles_node
    ON support_bundles (node_pubkey, received_at DESC);

CREATE TABLE IF NOT EXISTS support_reports (
    id BIGSERIAL PRIMARY KEY,
    node_pubkey TEXT NOT NULL,
    kind TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    detail JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_support_reports_node
    ON support_reports (node_pubkey, ts DESC);
CREATE INDEX IF NOT EXISTS idx_support_reports_kind
    ON support_reports (kind, ts DESC);
