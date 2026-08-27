-- 005 — the lite-profile phantom prune and its boot-streak counter are gone
-- (2026-08-27): a hardware profile governs compute, never retention. Nothing
-- reads the key any more; drop the row so settings dumps and support bundles
-- stop carrying it. Idempotent: a no-op wherever the row never existed.

DELETE FROM user_settings WHERE key = 'hardware.lite_streak';
