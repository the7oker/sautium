-- Migration 002: P2P Chat tables
-- Friends list and encrypted chat message storage

-- Friends list
CREATE TABLE IF NOT EXISTS friends (
    id SERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL DEFAULT '',
    public_key_hex VARCHAR(128) NOT NULL UNIQUE,
    invite_code VARCHAR(96) NOT NULL,
    display_name VARCHAR(128) NOT NULL DEFAULT '',
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP,
    is_blocked BOOLEAN DEFAULT FALSE,
    previous_public_key_hex VARCHAR(128)
);

CREATE INDEX IF NOT EXISTS idx_friends_invite_code ON friends(invite_code);
CREATE INDEX IF NOT EXISTS idx_friends_username ON friends(username);

-- Chat messages (stored as plaintext locally after decryption)
CREATE TABLE IF NOT EXISTS chat_messages (
    id SERIAL PRIMARY KEY,
    friend_id INTEGER NOT NULL REFERENCES friends(id) ON DELETE CASCADE,
    direction VARCHAR(3) NOT NULL CHECK (direction IN ('in', 'out')),
    content TEXT NOT NULL,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered BOOLEAN DEFAULT FALSE,
    read BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_friend ON chat_messages(friend_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_chat_messages_unread
    ON chat_messages(friend_id) WHERE direction = 'in' AND read = FALSE;
CREATE INDEX IF NOT EXISTS idx_chat_messages_pending
    ON chat_messages(friend_id) WHERE direction = 'out' AND delivered = FALSE;

-- Pending key rotations from friends (applied when user confirms)
CREATE TABLE IF NOT EXISTS pending_key_rotations (
    id SERIAL PRIMARY KEY,
    friend_id INTEGER NOT NULL UNIQUE REFERENCES friends(id) ON DELETE CASCADE,
    old_public_key_hex VARCHAR(128) NOT NULL,
    new_public_key_hex VARCHAR(128) NOT NULL,
    new_invite_code VARCHAR(96) NOT NULL,
    rotation_message BYTEA NOT NULL,
    signature BYTEA NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
