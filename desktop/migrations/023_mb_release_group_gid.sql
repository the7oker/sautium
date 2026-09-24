-- A release group is found by its gid (2026-09-24). Mirrors the 001 baseline.
--
-- The album row carries the release group's MusicBrainz gid, and every album
-- a discography mint materializes reads its releases' tracklists through it
-- (mb_local.fetch_release_tracklists), as do the release-group pages. 001 never
-- declared the index; the master had one made by hand, so every node built from
-- 001 scanned all 4.5M release groups per album instead — 110 ms against 3 ms,
-- ~3 s per artist minted on a launcher with a dump. Instant on a dump-less
-- node, a few seconds on a dump node; a no-op where the index already exists.

CREATE INDEX IF NOT EXISTS idx_mb_rg_gid ON mb_release_group(gid);
