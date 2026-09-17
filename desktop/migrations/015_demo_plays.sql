-- 015 — demo plays (2026-09-17). The demo channel (YouTube) streams a track
-- in full at most ONCE: the row is written the moment a listen passes 90 %,
-- and from then on the track streams only as a 30 s excerpt. One row per
-- track; on a life-data merge the earliest listen wins. Mirrors the 001
-- block verbatim, so it is a no-op on a database that just ran 001.

CREATE TABLE IF NOT EXISTS demo_plays (
    track_id UUID PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE,
    provider VARCHAR(32) NOT NULL,
    played_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
