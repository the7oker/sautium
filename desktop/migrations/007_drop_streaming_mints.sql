-- 007 — streaming_mints is gone with the Deezer search tail (afe581d,
-- 2026-08-29): MusicBrainz is the catalog, phantoms are born canonical, and
-- the provenance that held provider-shaped phantoms against the discard pass
-- has nothing left to hold — the two albums it still guarded on the master
-- were merged into their MB phantoms first. Idempotent: a no-op wherever the
-- table never existed (a fresh node's 001 no longer creates it).

DROP TABLE IF EXISTS streaming_mints;
