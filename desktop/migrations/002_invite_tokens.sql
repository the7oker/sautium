-- ============================================================
-- 002: P2P invite tokens, grants, friend provenance, wake trigger
-- ============================================================
-- Keep in step with the "P2P Invite Tokens" block in 001_initial.sql:
-- fresh installs get this schema from 001, already-migrated installs get
-- it from this file. The DDL is identical and idempotent, so applying
-- both is a no-op the second time.

DO $$ BEGIN
    CREATE TYPE p2p_right AS ENUM ('can_message', 'can_search');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE friend_source AS ENUM ('manual', 'token', 'master');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Issuer side: tokens this node minted. The UUID id doubles as the bearer
-- secret — crypto-random v4, NOT the project's v5 convention, because a
-- secret must not be derivable from public data.
CREATE TABLE IF NOT EXISTS invite_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label VARCHAR(128) NOT NULL DEFAULT '',
    max_uses INTEGER,
    use_count INTEGER NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    require_birth_cert BOOLEAN NOT NULL DEFAULT FALSE,
    welcome_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS invite_token_rights (
    token_id UUID NOT NULL REFERENCES invite_tokens(id) ON DELETE CASCADE,
    p2p_right p2p_right NOT NULL,
    PRIMARY KEY (token_id, p2p_right)
);

-- Rights snapshot at accept time: editing or revoking a token affects
-- FUTURE uses only, existing friendships keep what they were granted.
CREATE TABLE IF NOT EXISTS friend_rights (
    friend_id INTEGER NOT NULL REFERENCES friends(id) ON DELETE CASCADE,
    p2p_right p2p_right NOT NULL,
    PRIMARY KEY (friend_id, p2p_right)
);

-- Guest side: the issuer-signed grant — the contact-recovery document.
-- Its fields are a cryptographic unit re-canonicalized for signature
-- verification (birth-certificate pattern), hence a table of their own.
CREATE TABLE IF NOT EXISTS friend_grants (
    friend_id INTEGER PRIMARY KEY REFERENCES friends(id) ON DELETE CASCADE,
    token_id UUID NOT NULL,
    issuer_pubkey_hex VARCHAR(128) NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    signature BYTEA NOT NULL
);

CREATE TABLE IF NOT EXISTS friend_grant_rights (
    friend_id INTEGER NOT NULL REFERENCES friend_grants(friend_id) ON DELETE CASCADE,
    p2p_right p2p_right NOT NULL,
    PRIMARY KEY (friend_id, p2p_right)
);

ALTER TABLE friends ADD COLUMN IF NOT EXISTS source friend_source NOT NULL DEFAULT 'manual';
ALTER TABLE friends ADD COLUMN IF NOT EXISTS source_token_id UUID
    REFERENCES invite_tokens(id) ON DELETE SET NULL;
ALTER TABLE friends ADD COLUMN IF NOT EXISTS join_token_id UUID;
ALTER TABLE friends ADD COLUMN IF NOT EXISTS favorite BOOLEAN NOT NULL DEFAULT FALSE;

-- Keyset pagination over the non-pinned list; favorites are fetched whole
-- (user-curated, small by definition).
CREATE INDEX IF NOT EXISTS idx_friends_page
    ON friends ((LOWER(COALESCE(NULLIF(display_name, ''), username))), id)
    WHERE favorite = FALSE;

-- Live-DB drift repair: 001 declares DEFAULT gen_random_uuid() on
-- p2p_messages.message_uuid, but drifted databases lack it — and a raw
-- psql INSERT (the maintainer's reply path on the master) then produces
-- NULL-uuid rows that break history-import dedup on every guest.
ALTER TABLE p2p_messages ALTER COLUMN message_uuid SET DEFAULT gen_random_uuid();
UPDATE p2p_messages SET message_uuid = gen_random_uuid() WHERE message_uuid IS NULL;

-- NOTIFY on message insert lives in a trigger so EVERY writer wakes the
-- SSE listeners — chat_service, the backend routers, and raw psql INSERTs
-- (maintainer replies via Claude Code on the master node) alike.
CREATE OR REPLACE FUNCTION notify_p2p_message() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('sautium_chat', 'msg:' || NEW.friend_id || ':' || NEW.direction::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$ BEGIN
    CREATE TRIGGER trg_p2p_messages_notify AFTER INSERT ON p2p_messages
        FOR EACH ROW EXECUTE FUNCTION notify_p2p_message();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
