-- Sealing is two stages since 2026-09-06 (backend/sign_audio.py sign() /
-- stamp(), driven by backend/notary.py): author signatures land the moment
-- analysis commits, the Worker stamp follows on the notary's own cadence.
-- "Signed, awaiting stamp" is therefore an ordinary persistent state, and
-- the stamp pass discovers it by scan — without these the discovery walks
-- 3.7 GB of segment vectors and 3.5 M tracklist rows per stamp. Partial on
-- exactly that state, so they stay tiny (a row leaves the index when its
-- stamp lands) and cost nothing on the signed-and-stamped bulk; the
-- signature rides along so the scan never touches the heap.
CREATE INDEX IF NOT EXISTS idx_emb_segments_unstamped
    ON embedding_segments (id) INCLUDE (signature)
    WHERE signature IS NOT NULL AND batch_root IS NULL;
CREATE INDEX IF NOT EXISTS idx_audio_features_unstamped
    ON audio_features (id) INCLUDE (signature)
    WHERE signature IS NOT NULL AND batch_root IS NULL;
CREATE INDEX IF NOT EXISTS idx_albums_unstamped
    ON albums (id) INCLUDE (signature)
    WHERE signature IS NOT NULL AND batch_root IS NULL;
CREATE INDEX IF NOT EXISTS idx_album_tracks_unstamped
    ON album_tracks (id) INCLUDE (signature)
    WHERE signature IS NOT NULL AND batch_root IS NULL;
