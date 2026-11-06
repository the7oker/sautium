-- Invites mailed with a single-use token, contacts bound to 128 bits of their
-- key, and messages that carry their sender's signature (2026-11-07). The
-- same DDL as the friends, p2p_messages and sent_invites blocks of
-- 001_initial.sql, for nodes that ran the earlier files. Idempotent: a no-op
-- on the schema 001 just created.
--
-- sent_invites: the invite's id (the token's hash, the Worker's key for it)
-- and when a pickup took its acceptance. A row from before has no id and
-- never matches an acceptance.
ALTER TABLE sent_invites ADD COLUMN IF NOT EXISTS invite_id UUID;
ALTER TABLE sent_invites ADD COLUMN IF NOT EXISTS taken_at TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS sent_invites_invite_id ON sent_invites (invite_id);

-- friends: the first 16 bytes of the SHA-256 of the key a contact made from
-- a long code (or an accepted invite) waits for; and, on a contact made by
-- accepting a mailed invite, that invite's id until a nudge has reached its
-- inviter.
ALTER TABLE friends ADD COLUMN IF NOT EXISTS key_fingerprint BYTEA;
ALTER TABLE friends DROP CONSTRAINT IF EXISTS friends_key_fingerprint_len;
ALTER TABLE friends ADD CONSTRAINT friends_key_fingerprint_len CHECK (octet_length(key_fingerprint) = 16);
ALTER TABLE friends ADD COLUMN IF NOT EXISTS accepted_invite_id UUID;

-- p2p_messages: a received message's sender's signature and the time it
-- signed.
ALTER TABLE p2p_messages ADD COLUMN IF NOT EXISTS sender_sig BYTEA;
ALTER TABLE p2p_messages ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ;
ALTER TABLE p2p_messages DROP CONSTRAINT IF EXISTS p2p_messages_sender_sig_len;
ALTER TABLE p2p_messages ADD CONSTRAINT p2p_messages_sender_sig_len CHECK (octet_length(sender_sig) = 64);
