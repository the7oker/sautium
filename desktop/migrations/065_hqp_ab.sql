-- Comparing filters by ear (AB-1, 2026-11-17): the sittings and their duels.
-- Mirrors the 001 baseline. 065, not 064: B-92 holds 064 on the branch that
-- carries it.

-- Comparing filters by ear (AB-1, 2026-11-17): the person's own record of
-- what they picked — on this node, in the node backup (life data), never in
-- the sync, carry, the share export, the seed or a support bundle. A sitting
-- is kept under the three facts of the track it played (the mode, the slot
-- the source runs, a conversion across rate families) and the engine its
-- names belong to; a duel is one answer, in either form.
DO $$ BEGIN
    CREATE TYPE hqp_ab_form AS ENUM ('chain', 'pair');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE hqp_ab_mode AS ENUM ('pcm', 'sdm');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE hqp_ab_slot AS ENUM ('1x', 'nx');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE hqp_ab_axis AS ENUM ('phase', 'cutoff', 'ringing');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE hqp_ab_answer AS ENUM ('first', 'second', 'same');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE hqp_ab_end AS ENUM ('done', 'stopped', 'break', 'left', 'failed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS hqp_ab_sittings (
    id BIGSERIAL PRIMARY KEY,
    -- "Forget this library" removes its sittings with it (AB-1 Q15)
    hqp_endpoint_id INTEGER NOT NULL REFERENCES hqp_endpoints(id) ON DELETE CASCADE,
    engine TEXT NOT NULL,                   -- the build its filter names belong to
    mode hqp_ab_mode NOT NULL,
    slot hqp_ab_slot NOT NULL,              -- the slot the track's source runs, the one a swap writes
    fractional BOOLEAN NOT NULL,            -- the source and the output in different rate families
    form hqp_ab_form NOT NULL,
    track_id UUID REFERENCES tracks(id) ON UPDATE CASCADE ON DELETE SET NULL,
    from_s NUMERIC(9,3) NOT NULL,           -- the passage, in the track
    length_s NUMERIC(5,2) NOT NULL,
    lead_s NUMERIC(4,2),                    -- the cut's digital silence before it; NULL: ended before its warm-up
    silence_s NUMERIC(5,2),                 -- P, as finally played; NULL: ended before its warm-up
    tolerance_s NUMERIC(3,2) NOT NULL,      -- how far a silence may miss P (0.15 s PCM, 0.3 s SDM)
    slipped SMALLINT NOT NULL DEFAULT 0,    -- steps played again for a silence that missed
    learn_plays SMALLINT NOT NULL DEFAULT 0,  -- "Play" of the passage before the test: no duels, no listens
    -- What is put back: the slots HQPlayer held before, by name, and the
    -- volume (the warm-up's level, `muted_to`; NULL: a fixed volume)
    before_filter_nx TEXT,
    before_filter_1x TEXT,
    before_volume NUMERIC(6,2),
    muted_to NUMERIC(6,2),
    champion TEXT,                          -- the chain's champion now; the pair's: their own filter
    picked TEXT,                            -- the chain's champion at its end, the pair's winner; NULL: none
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMPTZ,
    ended hqp_ab_end,                       -- NULL with no sitting going: the process died in it
    owed BOOLEAN NOT NULL DEFAULT FALSE     -- ended, its put-back not taken by HQPlayer yet
);
CREATE INDEX IF NOT EXISTS idx_hqp_ab_sittings_context
    ON hqp_ab_sittings(hqp_endpoint_id, engine, mode, slot, fractional);
-- A sitting's natural key: the node backup's merge unions by it (life_merge)
CREATE UNIQUE INDEX IF NOT EXISTS uq_hqp_ab_sittings_start
    ON hqp_ab_sittings(hqp_endpoint_id, started_at);

CREATE TABLE IF NOT EXISTS hqp_ab_duels (
    sitting_id BIGINT NOT NULL REFERENCES hqp_ab_sittings(id) ON DELETE CASCADE,
    n SMALLINT NOT NULL,
    first_name TEXT NOT NULL,               -- heard first
    second_name TEXT NOT NULL,
    answer hqp_ab_answer NOT NULL,          -- the chain: better = second, worse = first
    axis hqp_ab_axis,                       -- the chain: the axis probed; NULL in the pair and the replay
    check_step BOOLEAN NOT NULL DEFAULT FALSE,   -- the champion's replay after "worse"
    counted BOOLEAN NOT NULL DEFAULT TRUE,       -- FALSE: set aside by a failed focus check
    swapped BOOLEAN,                        -- the chain: the answer made it champion (FALSE: kept theirs)
    answered_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (sitting_id, n)
);
