-- 073 — AB-2 (2026-11-24): Compare by ear's silence is per step (B-131),
-- so each duel keeps the P it was played at; a sitting's own silence_s is
-- now the longest it played — the rows before this fit that already (P
-- never shrank, so "as finally played" was the longest). Each slip says what
-- read it (hqp_ab_slips, §13 Q9). Idempotent.

ALTER TABLE hqp_ab_duels ADD COLUMN IF NOT EXISTS silence_s NUMERIC(5,2);

DO $$ BEGIN
    CREATE TYPE hqp_ab_slip_cause AS ENUM ('late', 'check', 'mark', 'music');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS hqp_ab_slips (
    sitting_id BIGINT NOT NULL REFERENCES hqp_ab_sittings(id) ON DELETE CASCADE,
    n SMALLINT NOT NULL,
    filter TEXT NOT NULL,
    cause hqp_ab_slip_cause NOT NULL,
    warm BOOLEAN NOT NULL,
    slipped_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (sitting_id, n)
);
