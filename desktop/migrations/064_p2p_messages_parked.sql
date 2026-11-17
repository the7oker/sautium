-- 064 — an outgoing message's state beside `delivered` (2026-11-17, B-92):
-- parked_at, when a message to the master was handed to its Worker mailbox
-- (kept for it, not yet proven at the master), and the notice a state change
-- sends the chat on screen. The same DDL as 001_initial.sql's, for nodes that
-- ran the earlier files. Idempotent: a no-op on the schema 001 just created.

ALTER TABLE p2p_messages ADD COLUMN IF NOT EXISTS parked_at TIMESTAMPTZ;

CREATE OR REPLACE FUNCTION notify_p2p_message_state() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('sautium_chat', 'state:' || NEW.friend_id);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$ BEGIN
    CREATE TRIGGER trg_p2p_messages_state AFTER UPDATE OF delivered, parked_at ON p2p_messages
        FOR EACH ROW WHEN (OLD.delivered IS DISTINCT FROM NEW.delivered
                           OR OLD.parked_at IS DISTINCT FROM NEW.parked_at)
        EXECUTE FUNCTION notify_p2p_message_state();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
