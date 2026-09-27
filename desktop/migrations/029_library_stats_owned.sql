-- The library totals count owned files wherever they live (2026-09-27):
-- a file on this node's disk or one held in an HQPlayer's library
-- (owned_files, migration 027). Bytes and playing time stay the disk's.
-- CREATE OR REPLACE keeps the column list, so the view is replaced in place.

CREATE OR REPLACE VIEW library_stats AS
SELECT
    -- artists in the catalogue: primary on >=1 track with an owned file — a
    -- file here or one held in an HQPlayer's library (owned_files). The join
    -- drops phantom artists (similar-artist and missing-album discovery rows
    -- with no owned files) — same presence rule as total_albums/total_tracks
    -- below. role = 'primary' also drops split markers (verified_split/
    -- verified_collab compounds, 0 tracks) and featured-only members:
    -- bookkeeping rows whose count diverges across nodes with each node's
    -- split/merge history while track identity converges.
    (SELECT COUNT(*) FROM artists a
      WHERE EXISTS (SELECT 1 FROM track_artists ta
                    JOIN owned_files f ON f.track_id = ta.track_id
                    WHERE ta.artist_id = a.id AND ta.role = 'primary')) as total_artists,
    -- owned albums/tracks only: phantom rows (MB missing-album discovery,
    -- no variants/files) are discovery data, not library contents
    (SELECT COUNT(*) FROM albums al
      WHERE EXISTS (SELECT 1 FROM album_variants av
                    JOIN owned_files f ON f.album_variant_id = av.id
                    WHERE av.album_id = al.id)) as total_albums,
    (SELECT COUNT(*) FROM tracks t
      WHERE EXISTS (SELECT 1 FROM owned_files f
                    WHERE f.track_id = t.id)) as total_tracks,
    (SELECT COUNT(*) FROM media_files) as total_media_files,
    -- The coverage counters carry the SAME presence rule as the totals they
    -- are read against. Counting whole tables instead made the numerator
    -- describe a different library from the denominator: phantoms carry
    -- embeddings, and a synced node holds analysis for tracks it has no
    -- file for, so "977 / 442 (221%)" was an honest report of two unrelated
    -- numbers. Per-track counts, not row counts — a second embedding model
    -- would otherwise double the figure on its own. (The audio figures the
    -- Library screen shows come from /stats, read against the tracks with
    -- bytes here — a copy held only at the HQPlayer is never analysed here.)
    (SELECT COUNT(*) FROM tracks t
      WHERE EXISTS (SELECT 1 FROM owned_files f WHERE f.track_id = t.id)
        AND EXISTS (SELECT 1 FROM embeddings e WHERE e.track_id = t.id)
    ) as tracks_with_embeddings,
    (SELECT COUNT(*) FROM tracks t
      WHERE EXISTS (SELECT 1 FROM owned_files f WHERE f.track_id = t.id)
        AND EXISTS (SELECT 1 FROM track_lyrics tl WHERE tl.track_id = t.id)
    ) as tracks_with_lyrics,
    -- bytes and playing time of the files on THIS node's disk
    (SELECT SUM(duration_seconds) FROM media_files) as total_duration_seconds,
    (SELECT SUM(file_size_bytes) FROM media_files) as total_file_size_bytes,
    -- Genres present in the owned library, not every genre name the node has
    -- ever heard of — a synced node knows thousands it owns no track in.
    (SELECT COUNT(DISTINCT ag.genre_id) FROM album_genres ag
      WHERE EXISTS (SELECT 1 FROM album_variants av
                    JOIN owned_files f ON f.album_variant_id = av.id
                    WHERE av.album_id = ag.album_id)) as unique_genres;
