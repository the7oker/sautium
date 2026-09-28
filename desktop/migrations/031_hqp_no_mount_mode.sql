-- How HQPlayer reaches the files is read off its address (2026-09-28):
-- an HQPlayer on this machine opens them by path, one anywhere else is
-- handed streams. The mount mode — HQPlayer reading a share of this node's
-- library at its own root — is gone with the setting, and with it the
-- mapping an endpoint row kept for it. Mirrors the 001 baseline.
ALTER TABLE hqp_endpoints DROP COLUMN IF EXISTS library_root;
ALTER TABLE hqp_endpoints DROP COLUMN IF EXISTS library_root_local;
DELETE FROM user_settings WHERE key IN ('hqplayer.file_access', 'hqplayer.library_root');
