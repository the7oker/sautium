-- 009 — the album each session slot was queued from (2026-09-05). A
-- canonical track sits on every album that lists it, so a snapshot that
-- keeps only (track_id, media_file_id) forgets which edition the listener
-- pressed: it cannot group a session's tracklist by album, and a replay
-- hands the streaming resolver a context-less track. session_tracks.album_id
-- is the canonical queue's QueueItem.album_id, captured at archive time.
-- Mirrors the 001 block, so the DDL is a no-op on a database that just ran
-- 001; the backfills touch only rows archived before the column existed.

ALTER TABLE session_tracks
    ADD COLUMN IF NOT EXISTS album_id UUID REFERENCES albums(id) ON DELETE SET NULL ON UPDATE CASCADE;
CREATE INDEX IF NOT EXISTS idx_session_tracks_album
    ON session_tracks(album_id);

-- Owned slots: the file's album.
UPDATE session_tracks st
   SET album_id = av.album_id
  FROM media_files mf
  JOIN album_variants av ON av.id = mf.album_variant_id
 WHERE mf.id = st.media_file_id
   AND st.album_id IS NULL;

-- Streamed slots of an album session: the session's album, when it lists
-- the track (an album session's queue may have grown past its album).
UPDATE session_tracks st
   SET album_id = ls.origin_album_id
  FROM listening_sessions ls
 WHERE ls.id = st.session_id
   AND st.album_id IS NULL
   AND ls.origin_album_id IS NOT NULL
   AND EXISTS (SELECT 1 FROM album_tracks atk
                WHERE atk.album_id = ls.origin_album_id
                  AND atk.track_id = st.track_id);

-- Every other streamed slot: the display edition — the same row a
-- context-less phantom track resolves to today (_phantom_track_queries).
UPDATE session_tracks st
   SET album_id = pick.album_id
  FROM (
        SELECT DISTINCT ON (atr.track_id) atr.track_id, atr.album_id
          FROM album_tracks atr
          JOIN albums al ON al.id = atr.album_id
         WHERE atr.track_id IN (SELECT track_id FROM session_tracks WHERE album_id IS NULL)
         ORDER BY atr.track_id, (al.cover_url IS NOT NULL) DESC,
                  (atr.length_ms IS NOT NULL) DESC, al.id
       ) pick
 WHERE pick.track_id = st.track_id
   AND st.album_id IS NULL;

-- Mix cards used to read "N tracks" under the AI title; the shelf line is
-- identity, so it now names the artists (top three by share, "+N" for the
-- rest — the rule _compute_session_card applies at archive time). Only the
-- old placeholder form is rewritten, so this is a no-op once applied.
UPDATE listening_sessions ls
   SET subtitle = lines.line
  FROM (
        SELECT session_id,
               string_agg(name, ', ' ORDER BY rn) FILTER (WHERE rn <= 3)
               || CASE WHEN max(total) > 3 THEN ' +' || (max(total) - 3) ELSE '' END AS line
          FROM (
                SELECT session_id, name,
                       row_number() OVER (PARTITION BY session_id ORDER BY n DESC, first_pos) AS rn,
                       count(*) OVER (PARTITION BY session_id) AS total
                  FROM (
                        SELECT st.session_id, a.id, a.name,
                               count(*) AS n, min(st.position) AS first_pos
                          FROM session_tracks st
                          JOIN listening_sessions s ON s.id = st.session_id
                          JOIN track_artists ta ON ta.track_id = st.track_id AND ta.role = 'primary'
                          JOIN artists a ON a.id = ta.artist_id
                         WHERE s.origin = 'mix' AND s.ended_at IS NOT NULL
                         GROUP BY st.session_id, a.id, a.name
                       ) per_artist
               ) ranked
         GROUP BY session_id
       ) lines
 WHERE ls.id = lines.session_id
   AND lines.line IS NOT NULL
   AND ls.origin = 'mix'
   AND ls.subtitle ~ '^\d+ tracks?$';
