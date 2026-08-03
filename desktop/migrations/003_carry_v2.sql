-- Carry v2: the canon mark travels with push-seeded audio analysis.
--
-- artist_mbids becomes a sealed category: the author signs
-- "this name-derived artist is this MB entity" so the property the carry
-- gate rests on ("canon only") survives transit — a carrier owns none of
-- the music and can never re-derive it locally. Same seal columns as every
-- other signed table; `imported` marks rows that arrived over carry rather
-- than from this node's own canonicalization.
--
-- Mirrored into 001_initial.sql for fresh installs — keep in step.

ALTER TABLE artist_mbids
    ADD COLUMN IF NOT EXISTS author_pubkey CHAR(64),
    ADD COLUMN IF NOT EXISTS signature     CHAR(128),
    ADD COLUMN IF NOT EXISTS batch_root    CHAR(64) REFERENCES signing_batches(batch_root),
    ADD COLUMN IF NOT EXISTS merkle_proof  JSONB,
    ADD COLUMN IF NOT EXISTS imported      BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_artist_mbids_unsigned
    ON artist_mbids (artist_id) WHERE signature IS NULL;

-- The signed payload binds artist_uuid:mbid:confidence. mbid is the PK
-- (immutable), but canonicalize_trackonly re-points artist_id
-- (ON CONFLICT (mbid) DO UPDATE SET artist_id) and canon may revise
-- confidence — either change invalidates the seal, so it must be NULLed,
-- not left asserting a binding that no longer exists.
CREATE OR REPLACE FUNCTION seal_guard_artist_mbids() RETURNS trigger AS $$
BEGIN
    IF (NEW.artist_id IS DISTINCT FROM OLD.artist_id
        OR NEW.confidence IS DISTINCT FROM OLD.confidence)
       AND NEW.signature IS NOT DISTINCT FROM OLD.signature THEN
        NEW.author_pubkey := NULL;
        NEW.signature     := NULL;
        NEW.batch_root    := NULL;
        NEW.merkle_proof  := NULL;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_artist_mbids_seal_guard ON artist_mbids;
CREATE TRIGGER trg_artist_mbids_seal_guard
BEFORE UPDATE ON artist_mbids
FOR EACH ROW EXECUTE FUNCTION seal_guard_artist_mbids();
