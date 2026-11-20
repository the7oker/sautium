-- 068 — CANON-CLASSICAL (2026-11-18): the same DDL as 001_initial.sql's
-- blocks, for nodes that ran the earlier files. Idempotent: a no-op on the
-- schema 001 just created.
--
-- media_files: the revision of what the reader took from the file's tags
-- (scanner.TAGS_READ_REV; a row under it is read again in place) and the
-- barcode the tags carry (BARCODE, else UPC, else EAN; digits).
-- mb_match_confidence: 'positional' — an album, its copy's release and each
-- position's recording bound by the copy's positions and lengths
-- (canon.positional).
-- track_mbids: the matcher's methodology that made the binding, signed with
-- it (record_sig.MATCHER_VERSION); its seal guard watches it.
-- title_numbers / title_numbers_disagree: the numeral rule of the matcher.
-- album_variants: the positional pass's last abstention on the copy, as the
-- candidates it was asked with — asked again only when they change; and
-- whether the pass bound the copy, which no other tier then re-decides.
-- mb_release: the positional pass's candidates by the release's credit (on a
-- dump node an index over the whole table, built once here; the loader keeps
-- it across reloads).
-- mbws_tracks: a data track of an enhanced CD, which is no position of the
-- audio a copy holds (a release answered before keeps false: its data
-- tracks count until MusicBrainz's answer is stored again).

ALTER TABLE media_files ADD COLUMN IF NOT EXISTS tags_read_rev SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE media_files ADD COLUMN IF NOT EXISTS barcode TEXT;

ALTER TYPE mb_match_confidence ADD VALUE IF NOT EXISTS 'positional' BEFORE 'ai';

ALTER TABLE track_mbids ADD COLUMN IF NOT EXISTS matcher_version SMALLINT NOT NULL DEFAULT 1;

ALTER TABLE mbws_tracks ADD COLUMN IF NOT EXISTS is_data BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_mb_release_credit ON mb_release(artist_credit);

CREATE OR REPLACE FUNCTION seal_guard_track_mbids() RETURNS trigger AS $$
BEGIN
    IF (NEW.track_id IS DISTINCT FROM OLD.track_id
        OR NEW.confidence IS DISTINCT FROM OLD.confidence
        OR NEW.matcher_version IS DISTINCT FROM OLD.matcher_version)
       AND NEW.signature IS NOT DISTINCT FROM OLD.signature THEN
        NEW.author_pubkey := NULL; NEW.signature := NULL;
        NEW.batch_root := NULL;    NEW.merkle_proof := NULL;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

ALTER TABLE album_variants ADD COLUMN IF NOT EXISTS positional_asked TEXT;
ALTER TABLE album_variants ADD COLUMN IF NOT EXISTS positional_bound BOOLEAN NOT NULL DEFAULT FALSE;

-- The numbers a title carries that tell one work or movement from another
-- (CANON-CLASSICAL-DESIGN §2.2), read from the raw title: 'c:<system>:<n>' a
-- catalogue number, 'n:<n>' a No., 'm:<n>' a movement numeral (a roman I.-XX.
-- after the start or a space, a colon, a comma or a dash; an arabic 1. only
-- after a colon, a comma or a dash — at the start it is a track number).
CREATE OR REPLACE FUNCTION title_numbers(t TEXT) RETURNS TEXT[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT coalesce(array_agg(DISTINCT x ORDER BY x), '{}') FROM (
        SELECT 'c:' || m[1] || ':' || m[2]
          FROM regexp_matches(lower(t), '\m(op|bwv|rv|kv|k|d|hob|hwv|wwv|woo)\.?\s*([0-9]+[a-z]?)', 'g') m
        UNION ALL
        SELECT 'n:' || m[1] FROM regexp_matches(lower(t), '\mno\.?\s*([0-9]+)', 'g') m
        UNION ALL
        SELECT 'm:' || array_position(ARRAY['i','ii','iii','iv','v','vi','vii','viii','ix','x','xi','xii',
                                            'xiii','xiv','xv','xvi','xvii','xviii','xix','xx'], m[1])
          FROM regexp_matches(lower(t), '(?:^|[\s:,\-–])([ivx]{1,5})\.(?:\s|$)', 'g') m
         WHERE m[1] = ANY(ARRAY['i','ii','iii','iv','v','vi','vii','viii','ix','x','xi','xii',
                                'xiii','xiv','xv','xvi','xvii','xviii','xix','xx'])
        UNION ALL
        SELECT 'm:' || m[1]::int FROM regexp_matches(lower(t), '(?:[:,]|\s[\-–])\s*([0-9]{1,2})\.(?:\s|$)', 'g') m
    ) q(x) $$;

-- Two titles disagree on their numbers (CANON-CLASSICAL-DESIGN §2.2): a
-- catalogue system both carry with no value in common (op 8 against rv 315 is
-- two systems, no disagreement); a No. on both where neither set holds the
-- other (No. 14 against No. 14 … No. 2 agrees); a movement numeral on both
-- with none in common.
CREATE OR REPLACE FUNCTION title_numbers_disagree(a TEXT[], b TEXT[]) RETURNS BOOLEAN
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT cardinality(a) > 0 AND cardinality(b) > 0 AND (
        EXISTS (SELECT 1 FROM (SELECT split_part(x, ':', 2) FROM unnest(a) x WHERE x LIKE 'c:%'
                               INTERSECT
                               SELECT split_part(y, ':', 2) FROM unnest(b) y WHERE y LIKE 'c:%') s(sys)
                 WHERE NOT EXISTS (SELECT 1 FROM unnest(a) x JOIN unnest(b) y ON y = x
                                    WHERE x LIKE 'c:' || s.sys || ':%'))
        OR (EXISTS (SELECT 1 FROM unnest(a) x WHERE x LIKE 'n:%')
            AND EXISTS (SELECT 1 FROM unnest(b) y WHERE y LIKE 'n:%')
            AND NOT (ARRAY(SELECT x FROM unnest(a) x WHERE x LIKE 'n:%') <@ b
                     OR ARRAY(SELECT y FROM unnest(b) y WHERE y LIKE 'n:%') <@ a))
        OR (EXISTS (SELECT 1 FROM unnest(a) x WHERE x LIKE 'm:%')
            AND EXISTS (SELECT 1 FROM unnest(b) y WHERE y LIKE 'm:%')
            AND NOT EXISTS (SELECT 1 FROM unnest(a) x JOIN unnest(b) y ON y = x WHERE x LIKE 'm:%'))) $$;
