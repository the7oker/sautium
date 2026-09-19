-- Last.fm data is node-local (2026-09-19): artist_bios, artist_tags,
-- similar_artists, track_stats and genre_descriptions leave the sync
-- contour — no seal, no import flag, no fetched_at — exactly like
-- album_descriptions. Last.fm's API terms do not allow redistributing what
-- it answers, so every node fetches its own by name and nothing of it goes
-- on the wire, into a share file or into the seed bundle. Mirrors the 001
-- baseline, where these tables no longer carry the columns.
--
-- Rows that ARRIVED over the network (imported) are deleted first: they are
-- the redistributed copies this change ends, and the node's own background
-- enrichment fetches them again from Last.fm — its "no bio yet"
-- precondition is true again once they are gone. First-hand rows stay.

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['artist_bios', 'artist_tags', 'similar_artists',
                             'track_stats', 'genre_descriptions']
    LOOP
        IF EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = t
                      AND column_name = 'imported') THEN
            EXECUTE format('DELETE FROM %I WHERE imported', t);
        END IF;
        EXECUTE format('DROP TRIGGER IF EXISTS trg_seal_guard_%s ON %I', t, t);
        EXECUTE format('DROP FUNCTION IF EXISTS seal_guard_%s()', t);
        EXECUTE format('DROP INDEX IF EXISTS idx_%s_unsigned', t);
        EXECUTE format($f$
            ALTER TABLE %I
                DROP COLUMN IF EXISTS fetched_at,
                DROP COLUMN IF EXISTS author_pubkey,
                DROP COLUMN IF EXISTS signature,
                DROP COLUMN IF EXISTS batch_root,
                DROP COLUMN IF EXISTS merkle_proof,
                DROP COLUMN IF EXISTS imported
        $f$, t);
    END LOOP;
END $$;

-- The batches only those seals referenced are orphans now; a batch that
-- also covers audio or canon leaves stays for them.
DELETE FROM signing_batches WHERE batch_root NOT IN (
    SELECT batch_root FROM embedding_segments WHERE batch_root IS NOT NULL
    UNION SELECT batch_root FROM audio_features WHERE batch_root IS NOT NULL
    UNION SELECT batch_root FROM albums         WHERE batch_root IS NOT NULL
    UNION SELECT batch_root FROM album_tracks   WHERE batch_root IS NOT NULL
    UNION SELECT batch_root FROM track_mbids    WHERE batch_root IS NOT NULL);
