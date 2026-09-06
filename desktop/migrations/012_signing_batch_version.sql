-- The Worker's timestamp payload format a batch was countersigned under
-- (record_sig.timestamp_payload). Stored with the batch and carried on the
-- sync wire so a verifier checks each stamp against ITS format: a future
-- format bump then invalidates nothing already issued, instead of forcing a
-- network-wide re-stamp round (the v1→v2 lesson, 2026-07-10). Every batch
-- in existence is v2.
ALTER TABLE signing_batches
    ADD COLUMN IF NOT EXISTS timestamp_version SMALLINT NOT NULL DEFAULT 2;
