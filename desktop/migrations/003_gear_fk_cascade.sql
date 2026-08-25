-- 003 — gear FKs follow uuid rewrites (ON UPDATE CASCADE), the rule every
-- other uuid5 entity already had. Needed by the 2026-08-25 identity
-- re-normalization (canon.migrations --renormalize) which rewrites
-- gear_brands / gear_models ids. Idempotent: a fresh node's 001 already
-- declares these constraints this way, and re-declaring is a no-op.

ALTER TABLE gear_models DROP CONSTRAINT IF EXISTS gear_models_brand_id_fkey;
ALTER TABLE gear_models ADD CONSTRAINT gear_models_brand_id_fkey
    FOREIGN KEY (brand_id) REFERENCES gear_brands(id) ON UPDATE CASCADE ON DELETE RESTRICT;

ALTER TABLE gear_technologies DROP CONSTRAINT IF EXISTS gear_technologies_brand_id_fkey;
ALTER TABLE gear_technologies ADD CONSTRAINT gear_technologies_brand_id_fkey
    FOREIGN KEY (brand_id) REFERENCES gear_brands(id) ON UPDATE CASCADE ON DELETE SET NULL;

ALTER TABLE user_gear DROP CONSTRAINT IF EXISTS user_gear_gear_model_id_fkey;
ALTER TABLE user_gear ADD CONSTRAINT user_gear_gear_model_id_fkey
    FOREIGN KEY (gear_model_id) REFERENCES gear_models(id) ON UPDATE CASCADE ON DELETE CASCADE;

ALTER TABLE gear_specs DROP CONSTRAINT IF EXISTS gear_specs_gear_model_id_fkey;
ALTER TABLE gear_specs ADD CONSTRAINT gear_specs_gear_model_id_fkey
    FOREIGN KEY (gear_model_id) REFERENCES gear_models(id) ON UPDATE CASCADE ON DELETE CASCADE;

ALTER TABLE gear_model_technologies DROP CONSTRAINT IF EXISTS gear_model_technologies_gear_model_id_fkey;
ALTER TABLE gear_model_technologies ADD CONSTRAINT gear_model_technologies_gear_model_id_fkey
    FOREIGN KEY (gear_model_id) REFERENCES gear_models(id) ON UPDATE CASCADE ON DELETE CASCADE;

ALTER TABLE gear_sentiment_terms DROP CONSTRAINT IF EXISTS gear_sentiment_terms_gear_model_id_fkey;
ALTER TABLE gear_sentiment_terms ADD CONSTRAINT gear_sentiment_terms_gear_model_id_fkey
    FOREIGN KEY (gear_model_id) REFERENCES gear_models(id) ON UPDATE CASCADE ON DELETE CASCADE;

ALTER TABLE gear_measured_caveats DROP CONSTRAINT IF EXISTS gear_measured_caveats_gear_model_id_fkey;
ALTER TABLE gear_measured_caveats ADD CONSTRAINT gear_measured_caveats_gear_model_id_fkey
    FOREIGN KEY (gear_model_id) REFERENCES gear_models(id) ON UPDATE CASCADE ON DELETE CASCADE;

ALTER TABLE gear_pair_notes DROP CONSTRAINT IF EXISTS gear_pair_notes_model_a_fkey;
ALTER TABLE gear_pair_notes ADD CONSTRAINT gear_pair_notes_model_a_fkey
    FOREIGN KEY (model_a) REFERENCES gear_models(id) ON UPDATE CASCADE ON DELETE CASCADE;

ALTER TABLE gear_pair_notes DROP CONSTRAINT IF EXISTS gear_pair_notes_model_b_fkey;
ALTER TABLE gear_pair_notes ADD CONSTRAINT gear_pair_notes_model_b_fkey
    FOREIGN KEY (model_b) REFERENCES gear_models(id) ON UPDATE CASCADE ON DELETE CASCADE;

ALTER TABLE gear_registry_entries DROP CONSTRAINT IF EXISTS gear_registry_entries_gear_model_id_fkey;
ALTER TABLE gear_registry_entries ADD CONSTRAINT gear_registry_entries_gear_model_id_fkey
    FOREIGN KEY (gear_model_id) REFERENCES gear_models(id) ON UPDATE CASCADE ON DELETE SET NULL;
