-- 075 — PI-NODE-2D item 5 (2026-11-24): the same DDL as 001_initial.sql's
-- blocks, for nodes that ran the earlier files. Idempotent: a no-op on the
-- schema 001 just created.
--
-- album_tracks.source: who wrote a tracklist slot — MusicBrainz (the phantom
-- mint; the seed's and a peer's rows are catalogue rows too and keep the
-- default) or this node's own files' tags (the materialiser). Until now
-- nothing said which, so a removed copy left its album and tracks behind as
-- a phantom built from the person's tags: the removal could not tell its own
-- slots from MusicBrainz's. Not a payload column: seal_guard_album_tracks
-- names the same six as before, and nothing of it travels.
--
-- The backfill runs with the column, once, and is conservative — in doubt a
-- slot is MusicBrainz's and stays: 'tag' where the slot is the node's own, a
-- file of a local copy of the album names it whole (the track, the disc, the
-- position and the length to the millisecond — the materialiser's own row)
-- and the slot is no older than the album's first copy (the mint skips an
-- album that has one).

DO $$ BEGIN
    CREATE TYPE tracklist_source AS ENUM ('mb', 'tag');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = current_schema() AND table_name = 'album_tracks'
                      AND column_name = 'source') THEN
        ALTER TABLE album_tracks ADD COLUMN source tracklist_source NOT NULL DEFAULT 'mb';
        UPDATE album_tracks at SET source = 'tag'
         WHERE NOT at.imported
           AND EXISTS (SELECT 1 FROM media_files mf
                         JOIN album_variants av ON av.id = mf.album_variant_id
                        WHERE av.album_id = at.album_id AND mf.track_id = at.track_id
                          AND COALESCE(mf.disc_number, 1) = at.disc
                          AND mf.track_number = at.position
                          AND (mf.duration_seconds * 1000)::int IS NOT DISTINCT FROM at.length_ms)
           AND at.created_at >= (SELECT MIN(av.created_at) FROM album_variants av
                                  WHERE av.album_id = at.album_id);
    END IF;
END $$;
