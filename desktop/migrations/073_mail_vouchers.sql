-- 073 — SUPPORT-FIRST (2026-11-24): the master's voucher for its Worker
-- mailbox, as a node keeps it (B-118; 001's comment says what it is). The
-- master signs one at every handshake and wake-stream connect; a drop carries
-- the one for the key it is signed with. Idempotent.

CREATE TABLE IF NOT EXISTS mail_vouchers (
    public_key_hex VARCHAR(64) PRIMARY KEY,
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    signature BYTEA NOT NULL
);
