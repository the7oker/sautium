-- Sautium - Full Schema (Canonical + Physical entities)
-- ============================================================

-- Extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS unaccent;

-- IMMUTABLE unaccent wrapper (pins the dictionary) so it can back an expression
-- index — bridges ASCII-tagged names to MB's accented canonical in canon matching
-- ('Tomas Dvorak' → 'Tomáš Dvořák'). See canon.match._unaccent_gids.
CREATE OR REPLACE FUNCTION f_unaccent(text) RETURNS text
  LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS
$$ SELECT lower(unaccent('unaccent', $1)) $$;

-- ============================================================
-- Enum types (created before referencing tables)
-- ============================================================

DO $$ BEGIN
    CREATE TYPE artist_gender AS ENUM ('unknown', 'female', 'male', 'mixed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE artist_vocalist AS ENUM ('unknown', 'vocal', 'instrumental');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE session_origin AS ENUM ('album', 'track', 'radio', 'mix');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE artist_type AS ENUM ('unknown', 'solo', 'band', 'collaboration', 'orchestra', 'other');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE verification_status AS ENUM ('unverified', 'suspicious', 'verified_band', 'verified_split', 'verified_collab');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- How an MBID was matched (artist_mbids / albums.musicbrainz_id), ascending
-- confidence. MB-verified only — Last.fm is out of the MBID contour.
-- NULL = no MBID assigned.
DO $$ BEGIN
    CREATE TYPE mb_match_confidence AS ENUM ('phantom', 'name_fuzzy', 'alias_exact', 'name_exact', 'overlap_verified', 'ai');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE credit_role AS ENUM ('primary', 'featured', 'member');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE musical_key AS ENUM ('C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE musical_mode AS ENUM ('major', 'minor');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE vocal_class AS ENUM ('vocal', 'instrumental', 'mixed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE analysis_origin AS ENUM ('local', 'deezer', 'youtube');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE audio_file_format AS ENUM ('FLAC', 'APE', 'WAV', 'AIFF', 'WV', 'TTA', 'DSF', 'DFF', 'MP3', 'OGG', 'M4A');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE cover_source_type AS ENUM ('external', 'embedded', 'sentinel');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE metadata_entity_type AS ENUM ('artist', 'album', 'track', 'genre');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE metadata_kind AS ENUM ('bio', 'info', 'stats', 'lyrics', 'description');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE fetch_status AS ENUM ('success', 'error', 'not_found');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE chat_role AS ENUM ('user', 'assistant');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE message_direction AS ENUM ('in', 'out');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE gear_category AS ENUM ('headphones', 'iems', 'dac', 'amp', 'player', 'streamer', 'power', 'cable');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE gear_polarity AS ENUM ('praise', 'criticism');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE spec_value_type AS ENUM ('number', 'string', 'enum', 'boolean');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ============================================================
-- Embedding models (shared metadata)
-- ============================================================

CREATE TABLE IF NOT EXISTS embedding_models (
    id UUID PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    dimension INTEGER NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- Canonical entities (UUID PKs, shareable across users)
-- ============================================================

CREATE TABLE IF NOT EXISTS artists (
    id UUID PRIMARY KEY,
    name VARCHAR(500) NOT NULL UNIQUE,
    artist_type artist_type DEFAULT 'unknown',
    gender artist_gender DEFAULT 'unknown',
    is_vocalist artist_vocalist DEFAULT 'unknown',
    verification_status verification_status DEFAULT 'unverified',
    raw_name VARCHAR(500),
    name_latin VARCHAR(500),                -- Latin transliteration of name for cross-script fuzzy search (Phase 0a)
    -- MB identity lives in artist_mbids (1:N — name-UUID may conflate namesakes)
    last_album_sync TIMESTAMPTZ,            -- freshness gate for new-album discovery
    last_mb_sync TIMESTAMPTZ,              -- freshness gate for MB canonicalization pass
    last_similar_sync TIMESTAMPTZ,         -- freshness gate for Last.fm similar-artists backfill (incl. out-of-catalog phantoms)
    lastfm_mbid UUID,                      -- MB artist Last.fm treats as canonical for this name; disambiguates namesakes (gates photo/similar to the matching artist_mbids row)
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS albums (
    id UUID PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    title_latin VARCHAR(500),               -- Latin transliteration of title for cross-script fuzzy search (Phase 0a)
    release_year INTEGER,
    label VARCHAR(200),
    catalog_number VARCHAR(100),
    total_tracks INTEGER,
    musicbrainz_id UUID,                    -- MusicBrainz release-GROUP MBID (canonical album)
    mb_match_confidence mb_match_confidence, -- how musicbrainz_id was matched (NULL = none)
    cover_url TEXT,                         -- external cover (Cover Art Archive) for phantom albums with no local files
    user_rating NUMERIC(3, 2) CHECK (user_rating >= 0 AND user_rating <= 5),
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tracks (
    id UUID PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    title_latin VARCHAR(500),               -- Latin transliteration of title for cross-script fuzzy search (Phase 0a)
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS genres (
    id UUID PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tags (
    id UUID PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_tag_name_not_empty CHECK (LENGTH(TRIM(name)) > 0)
);

-- ============================================================
-- Association tables (canonical)
-- ============================================================

CREATE TABLE IF NOT EXISTS track_artists (
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE,
    artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE,
    role credit_role DEFAULT 'primary',
    PRIMARY KEY (track_id, artist_id, role)
);

CREATE TABLE IF NOT EXISTS album_artists (
    album_id UUID NOT NULL REFERENCES albums(id) ON DELETE CASCADE ON UPDATE CASCADE,
    artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE,
    role credit_role DEFAULT 'primary',
    mbid UUID,  -- materialized MB artist MBID for THIS album (which namesake); NULL = MB unavailable
    PRIMARY KEY (album_id, artist_id, role)
);

CREATE TABLE IF NOT EXISTS artist_members (
    compound_artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE,
    member_artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE,
    role credit_role NOT NULL DEFAULT 'member',
    PRIMARY KEY (compound_artist_id, member_artist_id)
);

-- MB entities a name-derived Sautium artist maps to (1:N — the name-UUID
-- conflates namesakes/case-variants). mbid PK = one MB entity → one artist;
-- the inverse (one artist → many MBIDs) is the 1:N. Mostly MB-content-verified;
-- confidence='phantom' rows are name+genre-derived for out-of-catalog artists
-- (no owned tracks to verify) — re-verify before trusting over P2P.
CREATE TABLE IF NOT EXISTS artist_mbids (
    mbid UUID PRIMARY KEY,
    artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE,
    confidence mb_match_confidence NOT NULL,
    name TEXT,    -- the MB-canonical name of THIS entity (denormalized per mbid): when one
                  -- name-derived Sautium artist maps to several MB entities (namesakes), this
                  -- is how to write each one; also gives dump-less / P2P nodes the spelling
                  -- without mb_artist. Auto-filled by fill_artist_mbid_meta().
    about TEXT,   -- namesake disambiguation caption: mb_artist comment, else type · area · year
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Denormalize the MB entity's name + disambiguation onto every artist_mbids row,
-- whatever canon tier inserted it (content / name_exact / split / phantom / ai) —
-- a single trigger so no insert site can forget it. Reads the local dump; on a
-- dump-less node it leaves the value the insert/sync supplied (the P2P-carried one).
CREATE OR REPLACE FUNCTION fill_artist_mbid_meta() RETURNS TRIGGER AS $$
DECLARE
    _name text; _comment text; _type int; _area text; _year int;
BEGIN
    SELECT m.name, m.comment, m.type, ar.name, m.begin_date_year
      INTO _name, _comment, _type, _area, _year
      FROM mb_artist m LEFT JOIN mb_area ar ON ar.id = m.area
     WHERE m.gid = NEW.mbid;
    IF FOUND THEN
        NEW.name  := _name;
        NEW.about := COALESCE(
            NULLIF(btrim(_comment), ''),
            NULLIF(concat_ws(' · ',
                CASE _type WHEN 1 THEN 'Person' WHEN 2 THEN 'Group' WHEN 3 THEN 'Other'
                           WHEN 4 THEN 'Character' WHEN 5 THEN 'Orchestra' WHEN 6 THEN 'Choir' END,
                _area, _year::text), ''));
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$ BEGIN CREATE TRIGGER fill_artist_mbid_meta_trg
    BEFORE INSERT OR UPDATE OF mbid ON artist_mbids
    FOR EACH ROW EXECUTE FUNCTION fill_artist_mbid_meta();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- 1:N: our track (a song) → MB recordings it conflates (studio/live/remaster of
-- the same song). recording_mbid PK = one recording → one song; the inverse is
-- the 1:N. Per-file recording lives on media_files.recording_mbid (the concrete
-- performance for that file), aggregated here.
CREATE TABLE IF NOT EXISTS track_mbids (
    recording_mbid UUID PRIMARY KEY,
    track_id       UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE,
    confidence     mb_match_confidence,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Genres live at ALBUM grain, not track: most albums are genre-homogeneous, so
-- per-track tags were ~8x bloat (a dup of the album genre on every track) with no
-- gain — artist/era genre evolution is already captured per-release by the album.
-- Real per-track multi-genre signal comes from audio (CLAP), not file tags.
--   source: 'filetag' (count = media-file occurrences of the tag in the album)
--         | 'mb'      (count = MusicBrainz release-group tag votes)
-- LOCAL-ONLY: albums don't sync over P2P (only tracks + artists do), so neither
-- do their genres — only genre entities + genre_descriptions propagate. Readers
-- dedup by genre_id across sources; ON UPDATE CASCADE carries genre-UUID rewrites
-- (normalize_genres renames) and album merges.
CREATE TABLE IF NOT EXISTS album_genres (
    album_id UUID NOT NULL REFERENCES albums(id) ON DELETE CASCADE ON UPDATE CASCADE,
    genre_id UUID NOT NULL REFERENCES genres(id) ON DELETE CASCADE ON UPDATE CASCADE,
    source   VARCHAR(20) NOT NULL,
    count    INTEGER,
    PRIMARY KEY (album_id, genre_id, source)
);

-- ============================================================
-- Physical entities (SERIAL PKs, per-user)
-- ============================================================

CREATE TABLE IF NOT EXISTS album_variants (
    id SERIAL PRIMARY KEY,
    album_id UUID NOT NULL REFERENCES albums(id) ON DELETE CASCADE ON UPDATE CASCADE,
    directory_path TEXT NOT NULL,          -- NOT unique: a box set is many albums in one folder, so the key is (directory_path, album_id) below
    raw_title TEXT,                        -- original (pre-canon) album title for this variant — source of truth; makes album rename/merge reversible (albums are local, not synced)
    edition TEXT,                          -- named edition ("Super Deluxe Edition") extracted from a dirty title on canon; NULL = standard
    release_mbid UUID,                     -- MB release MBID (specific edition under albums.musicbrainz_id RG); best-effort, often NULL
    sample_rate INTEGER,
    bit_depth INTEGER,
    is_lossless BOOLEAN DEFAULT TRUE,
    -- Denormalised MAX(media_files.file_modified_at) across this variant's
    -- files. Maintained by FOR EACH STATEMENT triggers on media_files so the
    -- Home "New in library" feed sorts by an indexed column instead of a
    -- runtime GROUP BY across 30k media_files on every request.
    file_modified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT album_variants_dir_album_key UNIQUE (directory_path, album_id)
);

-- Phantom-album tracklists (Phantom Discovery, Stage B). Owned albums keep
-- the authoritative albums → album_variants → media_files → tracks chain;
-- this junction links a PHANTOM album (no variants/files) to its canonical
-- MB tracklist. A track is "owned" iff it has media_files — a later rip of
-- a phantom track collapses onto the same UUID v5 row and simply gains
-- files, no migration.
CREATE TABLE IF NOT EXISTS album_tracks (
    album_id UUID NOT NULL REFERENCES albums(id) ON DELETE CASCADE ON UPDATE CASCADE,
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE,
    disc INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL,
    recording_mbid UUID,                   -- MB recording gid (dedup / future preview)
    length_ms INTEGER,                     -- MB track length (ms), stored at mint time so the
                                           -- phantom-preview YouTube timing-match reads it
                                           -- directly instead of re-joining the 39M-row
                                           -- mb_recording per request — the MB dump is an
                                           -- optional, refreshable layer that may be reloaded
                                           -- or removed after a phantom was minted. From the
                                           -- canonical release's mb_track.length.
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (album_id, disc, position)
);
CREATE INDEX IF NOT EXISTS idx_album_tracks_track ON album_tracks(track_id);

CREATE TABLE IF NOT EXISTS covers (
    id UUID PRIMARY KEY,                             -- uuid5(NS, 'cover:' || hash_hex)
    content_hash BYTEA NOT NULL UNIQUE,              -- BLAKE2b-256 of original bytes
    perceptual_hash BIGINT,                          -- pHash (64-bit), nullable
    source_type cover_source_type NOT NULL,
    source_path TEXT,                                -- fs path or 'flac:{path}#{idx}'
    source_mtime TIMESTAMPTZ,
    orig_width INTEGER,
    orig_height INTEGER,
    orig_format VARCHAR(16),                         -- 'jpeg' | 'png' | 'tiff' | 'webp' | 'bmp'
    orig_bytes INTEGER,
    width INTEGER NOT NULL,                          -- encoded WebP width
    height INTEGER NOT NULL,                         -- encoded WebP height
    data BYTEA NOT NULL,                             -- WebP q=85 bytes
    bytes INTEGER NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- Sentinel cover row. Referenced by media_files.cover_id when lazy
-- resolution has been attempted but no cover was found anywhere
-- (embedded → external file → Last.fm). Required for FK integrity.
INSERT INTO covers (
    id, content_hash, source_type, source_path,
    width, height, data, bytes
) VALUES (
    '00000000-0000-0000-0000-000000000000'::uuid,
    decode(repeat('00', 32), 'hex'),
    'sentinel',
    '',
    0, 0,
    decode('', 'hex'),
    0
)
ON CONFLICT (id) DO NOTHING;

-- Artist photo cover. Lazy-resolved on first /api/covers/by-artist/{id}
-- request: scrape Last.fm artist images page → encode → cache. Failed
-- resolution lands on the sentinel cover so we don't re-scrape on every
-- request. ALTER instead of inline column because covers is defined
-- after artists in this file.
ALTER TABLE artists ADD COLUMN IF NOT EXISTS photo_cover_id UUID
    REFERENCES covers(id) ON DELETE SET NULL;

-- Idempotent column-adds for existing installs that pre-date phantom-album
-- discovery (Phantom Discovery, Phase 1: new albums for local artists).
ALTER TABLE artists ADD COLUMN IF NOT EXISTS last_album_sync TIMESTAMPTZ;
ALTER TABLE artists ADD COLUMN IF NOT EXISTS last_mb_sync TIMESTAMPTZ;
ALTER TABLE artists ADD COLUMN IF NOT EXISTS last_similar_sync TIMESTAMPTZ;
ALTER TABLE artists ADD COLUMN IF NOT EXISTS lastfm_mbid UUID;
ALTER TABLE albums  ADD COLUMN IF NOT EXISTS cover_url TEXT;
-- Discography moved from Deezer to the local MB dump (2026-06-12) — the
-- cached Deezer artist id is gone; converge installs that pre-date the move.
ALTER TABLE artists DROP COLUMN IF EXISTS deezer_id;

CREATE TABLE IF NOT EXISTS media_files (
    id SERIAL PRIMARY KEY,
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE,
    album_variant_id INTEGER NOT NULL REFERENCES album_variants(id) ON DELETE CASCADE ON UPDATE CASCADE,
    file_path TEXT NOT NULL UNIQUE,
    file_format audio_file_format DEFAULT 'FLAC',
    is_lossless BOOLEAN DEFAULT TRUE,
    file_size_bytes BIGINT,
    file_modified_at TIMESTAMPTZ,
    sample_rate INTEGER,
    bit_depth INTEGER,
    bitrate INTEGER,
    channels INTEGER,
    duration_seconds NUMERIC(10, 2),
    track_number INTEGER,
    disc_number INTEGER DEFAULT 1,
    is_analysis_source BOOLEAN DEFAULT FALSE,
    play_count INTEGER DEFAULT 0,
    last_played_at TIMESTAMPTZ,
    isrc VARCHAR(20),
    -- Original tags as read from the file — ground truth for re-normalization / correction
    raw_track_name TEXT,
    raw_artist TEXT,
    raw_album_artist TEXT,
    raw_album TEXT,
    raw_year TEXT,
    recording_mbid UUID,                            -- materialized MB recording for THIS file (its concrete performance)
    cover_id UUID REFERENCES covers(id) ON DELETE SET NULL,
    cover_processed_at TIMESTAMPTZ,                  -- NULL = pending cover resolution
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_mf_file_size CHECK (file_size_bytes IS NULL OR file_size_bytes >= 0),
    CONSTRAINT chk_mf_duration CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    CONSTRAINT chk_mf_play_count CHECK (play_count >= 0)
);

-- ============================================================
-- Embeddings & Analysis (linked to tracks)
-- ============================================================

-- Provenance of one audio-analysis pass: the physical source material (local
-- file or streamed provider audio), content-addressed by pcm_hash (BLAKE2b of
-- the NATIVELY-decoded PCM — source rate/channels, pre-resample; deterministic
-- for lossless across ffmpeg builds) + chromaprint (AcoustID fp — the robust
-- cross-rip recording anchor, bound INTO record signatures from the start).
-- Registered AT ANALYSIS TIME by the scanner / stream enricher; never
-- recomputed after the fact. Content-keyed on (track_id, pcm_hash): unchanged
-- material reuses its row, a re-rip mints a new one. media_file_id survives
-- file deletion as NULL — the row remains the durable statement of WHAT was
-- analyzed.
CREATE TABLE IF NOT EXISTS analysis_sources (
    id            SERIAL PRIMARY KEY,
    track_id      UUID NOT NULL REFERENCES tracks(id) ON UPDATE CASCADE ON DELETE CASCADE,
    origin        analysis_origin NOT NULL,
    media_file_id INTEGER REFERENCES media_files(id) ON DELETE SET NULL,
    pcm_hash      CHAR(64) NOT NULL,
    chromaprint   TEXT,                   -- NULL only if fpcalc failed
    duration_seconds INTEGER,             -- whole seconds; part of the signed
                                          -- material declaration (cheap
                                          -- no-decode import gate)
    grid_version  SMALLINT NOT NULL DEFAULT 1,
    sample_rate   INTEGER,
    bit_depth     INTEGER,                -- NULL for lossy sources
    is_lossless   BOOLEAN,
    imported      BOOLEAN NOT NULL DEFAULT false,  -- arrived over P2P sync:
                                          -- the sender's material, never
                                          -- signable here; a first-hand
                                          -- registration flips it back
    computed_at   TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT chk_asrc_stream_no_file CHECK (origin = 'local' OR media_file_id IS NULL),
    UNIQUE (track_id, pcm_hash)
);
CREATE INDEX IF NOT EXISTS idx_analysis_sources_media_file ON analysis_sources(media_file_id);

CREATE TABLE IF NOT EXISTS embeddings (
    id SERIAL PRIMARY KEY,
    vector vector(512) NOT NULL,
    model_id UUID NOT NULL REFERENCES embedding_models(id) ON DELETE CASCADE ON UPDATE CASCADE,
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE,
    -- What material this analysis came from. SET NULL degrade = "stale, will
    -- re-analyze"; the enrichment pending-predicates key on this link.
    analysis_source_id INTEGER REFERENCES analysis_sources(id) ON DELETE SET NULL,
    -- Methodology version of the ANALYSIS that produced this row (not the
    -- model): v1 = random 10s crop of the middle 30s, v2 = normalized mean
    -- of canonical-grid segments (2026-07-05). Carried through P2P sync so
    -- peers re-pull rows whose methodology is older than the source's —
    -- existence-diff alone never delivers enrichment upgrades.
    analysis_version SMALLINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (track_id, model_id)
);

-- Windowed CLAP segment embeddings (Stage 2 of the analysis refactor).
-- segment_index addresses the CANONICAL 10s grid: window i covers
-- [i*10s, i*10s+10s) from the track start, so sampling strategies of any
-- density (economy/balanced/thorough, "deepen this artist") write
-- compatible, top-uppable subsets of the same grid. Keyed to the track's
-- embeddings row — track/model/provenance resolve through it, so segments
-- and their mean can never disagree about what material they came from.
-- Re-analysis DELETEs and re-inserts segments (fresh rows are unsigned by
-- construction — the seal-invalidation model for this table).
CREATE TABLE IF NOT EXISTS embedding_segments (
    id SERIAL PRIMARY KEY,
    vector vector(512) NOT NULL,
    embedding_id INTEGER NOT NULL REFERENCES embeddings(id) ON DELETE CASCADE,
    segment_index SMALLINT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (embedding_id, segment_index)
);

-- Segment-level KNN is the text→audio retrieve channel: normalized track-MEAN
-- vectors collapse toward the corpus centroid (text-query cosines span ~0.05),
-- so text search retrieves AND scores against segments (MAX per track).
CREATE INDEX IF NOT EXISTS idx_embedding_segments_vector
    ON embedding_segments USING hnsw (vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE TABLE IF NOT EXISTS text_embeddings (
    id SERIAL PRIMARY KEY,
    vector vector(1024) NOT NULL,
    model_id UUID NOT NULL REFERENCES embedding_models(id) ON DELETE CASCADE ON UPDATE CASCADE,
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (track_id, model_id)
);

CREATE TABLE IF NOT EXISTS audio_features (
    id SERIAL PRIMARY KEY,
    track_id UUID NOT NULL UNIQUE REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE,
    bpm DOUBLE PRECISION,
    key musical_key,
    mode musical_mode,
    key_confidence DOUBLE PRECISION,
    energy DOUBLE PRECISION,
    energy_db DOUBLE PRECISION,
    brightness DOUBLE PRECISION,
    dynamic_range_db DOUBLE PRECISION,
    zero_crossing_rate DOUBLE PRECISION,
    instruments JSONB,
    moods JSONB,
    vocal_instrumental vocal_class,
    vocal_score DOUBLE PRECISION,
    danceability DOUBLE PRECISION,
    analysis_source_id INTEGER REFERENCES analysis_sources(id) ON DELETE SET NULL,
    -- Analysis methodology version (v1 = middle-30s features + single-window
    -- instruments; v2 = whole-track amplitude + windowed-max instruments,
    -- 2026-07-03). Synced so peers re-pull methodology upgrades.
    analysis_version SMALLINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_af_bpm CHECK (bpm IS NULL OR bpm > 0),
    CONSTRAINT chk_af_key_confidence CHECK (key_confidence IS NULL OR (key_confidence >= 0 AND key_confidence <= 1)),
    CONSTRAINT chk_af_danceability CHECK (danceability IS NULL OR (danceability >= 0 AND danceability <= 1)),
    CONSTRAINT chk_af_vocal_score CHECK (vocal_score IS NULL OR (vocal_score >= 0 AND vocal_score <= 1))
);

-- ============================================================
-- Enrichment signing (phase 1) — see docs/design/P2P-SYNC-INTEGRITY.md
-- Author signatures + Worker-timestamped batches over audio-derived records.
-- The signed/verifiable unit is the SEGMENT (deterministic per index), not the
-- mean vector (varies with the sampled K). The content-address a record signs
-- against is its LINKED analysis_sources row (registered at analysis time).
-- Signable material: origin='local' + album in signing_whitelist, or
-- origin='deezer' AND is_lossless (tier 3 — a clean stream signs against the
-- STREAM's pcm_hash, claiming no possession of any local rip).
-- ============================================================

-- Owned-official albums whose audio analysis may be signed (Bandcamp
-- purchases now; grey/vinyl rips stay unsigned — an author signature is a
-- permanent possession proof).
CREATE TABLE IF NOT EXISTS signing_whitelist (
    album_id UUID PRIMARY KEY REFERENCES albums(id) ON DELETE CASCADE,
    reason   TEXT NOT NULL DEFAULT 'bandcamp',
    added_at TIMESTAMPTZ DEFAULT now()
);

-- One row per Worker-timestamped signing batch (the daily notary root that
-- anchors authorship priority for every record proving inclusion in it).
CREATE TABLE IF NOT EXISTS signing_batches (
    batch_root    CHAR(64) PRIMARY KEY,
    author_pubkey CHAR(64) NOT NULL,
    worker_date   TIMESTAMPTZ NOT NULL,
    worker_sig    CHAR(128) NOT NULL,
    authority     CHAR(64) NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT now()
);

-- Per-record author signature + Merkle inclusion in a timestamped batch.
-- NULL = unsigned (every existing and non-signable record).
ALTER TABLE embedding_segments
    ADD COLUMN IF NOT EXISTS author_pubkey CHAR(64),
    ADD COLUMN IF NOT EXISTS signature     CHAR(128),
    ADD COLUMN IF NOT EXISTS batch_root    CHAR(64) REFERENCES signing_batches(batch_root),
    ADD COLUMN IF NOT EXISTS merkle_proof  JSONB;

ALTER TABLE audio_features
    ADD COLUMN IF NOT EXISTS author_pubkey CHAR(64),
    ADD COLUMN IF NOT EXISTS signature     CHAR(128),
    ADD COLUMN IF NOT EXISTS batch_root    CHAR(64) REFERENCES signing_batches(batch_root),
    ADD COLUMN IF NOT EXISTS merkle_proof  JSONB;

CREATE INDEX IF NOT EXISTS idx_emb_segments_unsigned
    ON embedding_segments (embedding_id) WHERE signature IS NULL;

-- Seal invalidation at the data layer: any UPDATE that changes a signed
-- payload column without presenting a new signature loses the seal. This is
-- the invariant, not a convenience — writers (ORM, raw scripts, sync imports)
-- need no seal awareness and cannot silently break a sealed record. Linking
-- columns (embedding_id, analysis_source_id) are deliberately NOT payload:
-- re-keying/linking migrations must not shed seals. sign_audio.py's UPDATE
-- sets a new signature explicitly, so the guard leaves it alone.
CREATE OR REPLACE FUNCTION seal_guard_embedding_segments() RETURNS trigger AS $$
BEGIN
    IF (NEW.vector IS DISTINCT FROM OLD.vector
        OR NEW.segment_index IS DISTINCT FROM OLD.segment_index)
       AND NEW.signature IS NOT DISTINCT FROM OLD.signature THEN
        NEW.author_pubkey := NULL;
        NEW.signature     := NULL;
        NEW.batch_root    := NULL;
        NEW.merkle_proof  := NULL;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_embedding_segments_seal_guard ON embedding_segments;
CREATE TRIGGER trg_embedding_segments_seal_guard
BEFORE UPDATE ON embedding_segments
FOR EACH ROW EXECUTE FUNCTION seal_guard_embedding_segments();

CREATE OR REPLACE FUNCTION seal_guard_audio_features() RETURNS trigger AS $$
BEGIN
    IF (NEW.bpm IS DISTINCT FROM OLD.bpm
        OR NEW.key IS DISTINCT FROM OLD.key
        OR NEW.mode IS DISTINCT FROM OLD.mode
        OR NEW.key_confidence IS DISTINCT FROM OLD.key_confidence
        OR NEW.energy IS DISTINCT FROM OLD.energy
        OR NEW.energy_db IS DISTINCT FROM OLD.energy_db
        OR NEW.brightness IS DISTINCT FROM OLD.brightness
        OR NEW.dynamic_range_db IS DISTINCT FROM OLD.dynamic_range_db
        OR NEW.zero_crossing_rate IS DISTINCT FROM OLD.zero_crossing_rate
        OR NEW.danceability IS DISTINCT FROM OLD.danceability
        OR NEW.vocal_instrumental IS DISTINCT FROM OLD.vocal_instrumental
        OR NEW.vocal_score IS DISTINCT FROM OLD.vocal_score
        OR NEW.instruments IS DISTINCT FROM OLD.instruments
        OR NEW.moods IS DISTINCT FROM OLD.moods
        OR NEW.analysis_version IS DISTINCT FROM OLD.analysis_version)
       AND NEW.signature IS NOT DISTINCT FROM OLD.signature THEN
        NEW.author_pubkey := NULL;
        NEW.signature     := NULL;
        NEW.batch_root    := NULL;
        NEW.merkle_proof  := NULL;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audio_features_seal_guard ON audio_features;
CREATE TRIGGER trg_audio_features_seal_guard
BEFORE UPDATE ON audio_features
FOR EACH ROW EXECUTE FUNCTION seal_guard_audio_features();

-- ============================================================
-- Metadata tables (UUID FKs)
-- ============================================================

CREATE TABLE IF NOT EXISTS artist_bios (
    id SERIAL PRIMARY KEY,
    artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE,
    source VARCHAR(50) NOT NULL,
    summary TEXT,
    content TEXT,
    url TEXT,
    listeners INTEGER,
    playcount BIGINT,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_artist_bios UNIQUE (artist_id, source),
    CONSTRAINT chk_has_bio CHECK (summary IS NOT NULL OR content IS NOT NULL OR listeners IS NOT NULL OR playcount IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS artist_tags (
    id SERIAL PRIMARY KEY,
    artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE,
    tag_id UUID NOT NULL REFERENCES tags(id) ON DELETE CASCADE ON UPDATE CASCADE,
    weight INTEGER NOT NULL CHECK (weight >= 0 AND weight <= 100),
    source VARCHAR(50) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_artist_tags UNIQUE (artist_id, tag_id, source)
);

CREATE TABLE IF NOT EXISTS similar_artists (
    id SERIAL PRIMARY KEY,
    artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE,
    similar_artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE,
    match_score NUMERIC(5, 4) NOT NULL CHECK (match_score >= 0 AND match_score <= 1),
    source VARCHAR(50) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_similar_artists UNIQUE (artist_id, similar_artist_id, source),
    CONSTRAINT chk_not_self_similar CHECK (artist_id != similar_artist_id)
);

CREATE TABLE IF NOT EXISTS genre_descriptions (
    id SERIAL PRIMARY KEY,
    genre_id UUID NOT NULL REFERENCES genres(id) ON DELETE CASCADE ON UPDATE CASCADE,
    source VARCHAR(50) NOT NULL,
    summary TEXT,
    content TEXT,
    url TEXT,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_genre_descriptions UNIQUE (genre_id, source),
    CONSTRAINT chk_has_description CHECK (summary IS NOT NULL OR content IS NOT NULL)
);

-- Generic key/value store for app-level user preferences. Keys are
-- dotted strings (e.g. 'hqplayer.favorite_filters') and the value
-- carries whatever JSON the feature needs. Single-row-per-key by
-- design; no per-account isolation because the launcher is
-- single-user. Add new namespaces by inventing a key prefix.
CREATE TABLE IF NOT EXISTS user_settings (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS track_stats (
    id SERIAL PRIMARY KEY,
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE,
    source VARCHAR(50) NOT NULL,
    listeners INTEGER,
    playcount BIGINT,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_track_stats UNIQUE (track_id, source),
    CONSTRAINT chk_has_track_stats CHECK (listeners IS NOT NULL OR playcount IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS external_metadata (
    id SERIAL PRIMARY KEY,
    entity_type metadata_entity_type NOT NULL,
    entity_id TEXT NOT NULL,                          -- polymorphic: UUID for artist/album/track, int for genre
    source VARCHAR(50) NOT NULL,
    metadata_type metadata_kind NOT NULL,
    data JSONB NOT NULL,
    fetched_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    fetch_status fetch_status DEFAULT 'success',
    error_message TEXT,
    CONSTRAINT uq_external_metadata UNIQUE (entity_type, entity_id, source, metadata_type)
);

-- ============================================================
-- Chat / session history
-- ============================================================

CREATE TABLE IF NOT EXISTS chat_sessions (
    id SERIAL PRIMARY KEY,
    title VARCHAR(500),
    claude_session_id VARCHAR(100),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE ON UPDATE CASCADE,
    role chat_role NOT NULL,
    content TEXT NOT NULL,
    tracks_data JSONB,
    blocks_data JSONB,
    model VARCHAR(100),
    filters_detected JSONB,
    retrieval_log JSONB,
    tracks_retrieved INTEGER,
    is_not_relevant BOOLEAN DEFAULT FALSE,
    feedback_comment TEXT,
    feedback_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Idempotent column-add for existing installs that pre-date blocks_data.
ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS blocks_data JSONB;

-- ============================================================
-- Listening history
-- ============================================================

CREATE TABLE IF NOT EXISTS listening_history (
    id SERIAL PRIMARY KEY,
    media_file_id INTEGER REFERENCES media_files(id) ON DELETE SET NULL,
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    duration_listened NUMERIC(10, 2) CHECK (duration_listened >= 0),
    percent_listened NUMERIC(5, 2) CHECK (percent_listened >= 0 AND percent_listened <= 100),
    completed BOOLEAN DEFAULT FALSE,
    skipped BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS local_play_stats (
    track_id UUID PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE,
    play_count INTEGER NOT NULL DEFAULT 0,
    skip_count INTEGER NOT NULL DEFAULT 0,
    total_listen_time NUMERIC(12, 2) NOT NULL DEFAULT 0,
    avg_percent_listened NUMERIC(5, 2) NOT NULL DEFAULT 0,
    last_played_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_lps_play_count CHECK (play_count >= 0),
    CONSTRAINT chk_lps_skip_count CHECK (skip_count >= 0),
    CONSTRAINT chk_lps_listen_time CHECK (total_listen_time >= 0),
    CONSTRAINT chk_lps_avg_pct CHECK (avg_percent_listened >= 0 AND avg_percent_listened <= 100)
);

-- ============================================================
-- Listening sessions (queue-lifetime snapshots for the Home shelf)
-- ============================================================
-- A session is one queue lifetime. Each destructive play endpoint
-- (play-track / play-album / play-similar / play-tracks / radio-start)
-- archives the previous queue as an immutable snapshot and opens a new
-- active session. ended_at IS NULL ⇔ active; the partial unique index
-- enforces at most one active session at a time. The card cover is
-- denormalised from the first snapshot track so the Home shelf renders
-- without a join: cover_id (owned art) OR cover_url (a phantom album's CAA
-- art, which is not a covers(id) row). origin records how the queue started
-- — the one fact HQPlayer (source of truth for the live queue) does not know.
-- seed_track_id is the logical seed (owned AND phantom); seed_media_file_id is
-- the owned-only physical file (mirrors listening_history's dual key).
CREATE TABLE IF NOT EXISTS listening_sessions (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    origin             session_origin NOT NULL,
    title              TEXT,
    subtitle           TEXT,
    cover_id           UUID REFERENCES covers(id) ON DELETE SET NULL,
    cover_url          TEXT,
    origin_album_id    UUID REFERENCES albums(id) ON DELETE SET NULL ON UPDATE CASCADE,
    seed_track_id      UUID REFERENCES tracks(id) ON DELETE SET NULL ON UPDATE CASCADE,
    seed_media_file_id INTEGER REFERENCES media_files(id) ON DELETE SET NULL,
    track_count        INTEGER NOT NULL DEFAULT 0,
    started_at         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at           TIMESTAMPTZ,
    created_at         TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_listening_sessions_track_count CHECK (track_count >= 0)
);

-- Immutable per-session track snapshot, written when the session is
-- archived. Source-agnostic dual key (mirrors listening_history): track_id
-- (UUID, the logical identity, owned AND phantom) is the real key;
-- media_file_id is the owned-only physical file, NULL for streamed phantoms.
-- media_file_id ON DELETE SET NULL — a removed owned file must not erase
-- session history now that track_id carries identity (track_count is the
-- stored snapshot, intentionally not re-derived).
CREATE TABLE IF NOT EXISTS session_tracks (
    session_id     UUID NOT NULL REFERENCES listening_sessions(id) ON DELETE CASCADE,
    position       INTEGER NOT NULL,
    track_id       UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE,
    media_file_id  INTEGER REFERENCES media_files(id) ON DELETE SET NULL,
    PRIMARY KEY (session_id, position)
);

CREATE TABLE IF NOT EXISTS track_lyrics (
    id SERIAL PRIMARY KEY,
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE,
    source VARCHAR(50) NOT NULL,
    plain_lyrics TEXT,
    synced_lyrics TEXT,
    instrumental BOOLEAN DEFAULT FALSE,
    external_id INTEGER,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(track_id, source)
);

CREATE TABLE IF NOT EXISTS lyrics_embeddings (
    id SERIAL PRIMARY KEY,
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE ON UPDATE CASCADE,
    model_id UUID NOT NULL REFERENCES embedding_models(id) ON DELETE CASCADE ON UPDATE CASCADE,
    vector vector(1024) NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    chunk_text TEXT,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(track_id, model_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS artist_bio_embeddings (
    id SERIAL PRIMARY KEY,
    artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE,
    model_id UUID NOT NULL REFERENCES embedding_models(id) ON DELETE CASCADE ON UPDATE CASCADE,
    vector vector(1024) NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    chunk_text TEXT,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (artist_id, model_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS genre_desc_embeddings (
    id SERIAL PRIMARY KEY,
    genre_id UUID NOT NULL REFERENCES genres(id) ON DELETE CASCADE ON UPDATE CASCADE,
    model_id UUID NOT NULL REFERENCES embedding_models(id) ON DELETE CASCADE ON UPDATE CASCADE,
    vector vector(1024) NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    chunk_text TEXT,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (genre_id, model_id, chunk_index)
);

-- ============================================================
-- Indexes
-- ============================================================

-- Embedding indexes (single-column indexes on leading PK/UNIQUE columns omitted — covered by constraint indexes)
CREATE INDEX IF NOT EXISTS idx_embeddings_model_id ON embeddings(model_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_analysis_source ON embeddings(analysis_source_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_vector ON embeddings
    USING hnsw (vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Text embedding indexes
CREATE INDEX IF NOT EXISTS idx_text_embeddings_model_id ON text_embeddings(model_id);
CREATE INDEX IF NOT EXISTS idx_text_embeddings_vector ON text_embeddings
    USING hnsw (vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Lyrics embedding indexes
CREATE INDEX IF NOT EXISTS idx_lyrics_embeddings_model_id ON lyrics_embeddings(model_id);
CREATE INDEX IF NOT EXISTS idx_lyrics_embeddings_vector ON lyrics_embeddings
    USING hnsw (vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Artist bio embedding indexes
CREATE INDEX IF NOT EXISTS idx_artist_bio_emb_model ON artist_bio_embeddings(model_id);
CREATE INDEX IF NOT EXISTS idx_artist_bio_emb_vector ON artist_bio_embeddings
    USING hnsw (vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Genre description embedding indexes
CREATE INDEX IF NOT EXISTS idx_genre_desc_emb_model ON genre_desc_embeddings(model_id);
CREATE INDEX IF NOT EXISTS idx_genre_desc_emb_vector ON genre_desc_embeddings
    USING hnsw (vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Core table indexes
CREATE INDEX IF NOT EXISTS idx_artists_name_trgm ON artists USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_artists_verification_status ON artists(verification_status);
CREATE INDEX IF NOT EXISTS idx_artists_artist_type ON artists(artist_type);
CREATE INDEX IF NOT EXISTS idx_artists_gender ON artists(gender);
CREATE INDEX IF NOT EXISTS idx_artists_is_vocalist ON artists(is_vocalist);
CREATE INDEX IF NOT EXISTS idx_artists_last_album_sync ON artists(last_album_sync);
CREATE INDEX IF NOT EXISTS idx_artists_last_mb_sync ON artists(last_mb_sync);
CREATE INDEX IF NOT EXISTS idx_artists_last_similar_sync ON artists(last_similar_sync);

-- Artist members indexes
CREATE INDEX IF NOT EXISTS idx_artist_members_member ON artist_members(member_artist_id);
CREATE INDEX IF NOT EXISTS idx_artist_mbids_artist ON artist_mbids(artist_id);
CREATE INDEX IF NOT EXISTS idx_track_mbids_track ON track_mbids(track_id);
CREATE INDEX IF NOT EXISTS idx_albums_title ON albums(title);
CREATE INDEX IF NOT EXISTS idx_albums_title_trgm ON albums USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_albums_release_year ON albums(release_year);
-- release-group MBID lookups: phantom upsert/reconcile and cross-artist
-- owned checks key on it (~70k phantom rows carry one)
CREATE INDEX IF NOT EXISTS idx_albums_mbid ON albums(musicbrainz_id) WHERE musicbrainz_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tracks_title ON tracks(title);
CREATE INDEX IF NOT EXISTS idx_tracks_title_trgm ON tracks USING gin (title gin_trgm_ops);

-- Latin transliteration indexes for cross-script fuzzy search (Phase 0a).
-- Population: write-time hooks + backfill; the same latinize() runs on the query.
CREATE INDEX IF NOT EXISTS idx_artists_name_latin_trgm ON artists USING gin (name_latin gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_albums_title_latin_trgm ON albums USING gin (title_latin gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_tracks_title_latin_trgm ON tracks USING gin (title_latin gin_trgm_ops);

-- Phase 0b: CJK multi-form aliases — a second Latin reading for kana-less Han
-- (中森明菜 is stored as its pinyin reading in name_latin; its Japanese reading
-- is kept here) so the name is findable either way. Populated by the name_latin
-- backfill; searched alongside artists.name_latin in /discovery/artists.
CREATE TABLE IF NOT EXISTS artist_name_aliases (
    artist_id UUID NOT NULL REFERENCES artists(id) ON DELETE CASCADE ON UPDATE CASCADE,
    alias_latin VARCHAR(500) NOT NULL,
    source VARCHAR(20) NOT NULL DEFAULT 'cutlet',
    PRIMARY KEY (artist_id, alias_latin)
);
CREATE INDEX IF NOT EXISTS idx_artist_name_aliases_trgm ON artist_name_aliases USING gin (alias_latin gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_tags_name_lower ON tags(name text_pattern_ops);

-- Association indexes
CREATE INDEX IF NOT EXISTS idx_track_artists_artist_id ON track_artists(artist_id);
CREATE INDEX IF NOT EXISTS idx_album_artists_artist_id ON album_artists(artist_id);
CREATE INDEX IF NOT EXISTS idx_album_artists_mbid ON album_artists(mbid) WHERE mbid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_media_files_recording_mbid ON media_files(recording_mbid) WHERE recording_mbid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_album_genres_genre_id ON album_genres(genre_id);

-- Physical entity indexes
CREATE INDEX IF NOT EXISTS idx_album_variants_album_id ON album_variants(album_id);
CREATE INDEX IF NOT EXISTS idx_album_variants_file_modified_at_desc
    ON album_variants(file_modified_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_media_files_track_id ON media_files(track_id);
CREATE INDEX IF NOT EXISTS idx_media_files_album_variant_id ON media_files(album_variant_id);
CREATE INDEX IF NOT EXISTS idx_media_files_play_count ON media_files(play_count);
-- One analysis source per track, enforced at the type level. Writers flip the
-- flag in two statements (clear losers, then set the winner) — a single
-- UPDATE can transiently hold two TRUE rows mid-statement and trip this.
CREATE UNIQUE INDEX IF NOT EXISTS uq_media_files_analysis_source
    ON media_files(track_id) WHERE is_analysis_source;
CREATE INDEX IF NOT EXISTS idx_media_files_cover_id ON media_files(cover_id);
CREATE INDEX IF NOT EXISTS idx_media_files_cover_pending ON media_files(id)
    WHERE cover_processed_at IS NULL;

-- Cover art indexes
CREATE INDEX IF NOT EXISTS idx_covers_phash ON covers(perceptual_hash)
    WHERE perceptual_hash IS NOT NULL;

-- External metadata indexes
CREATE INDEX IF NOT EXISTS idx_external_metadata_entity ON external_metadata(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_external_metadata_source ON external_metadata(source);
CREATE INDEX IF NOT EXISTS idx_external_metadata_type ON external_metadata(metadata_type);
CREATE INDEX IF NOT EXISTS idx_external_metadata_status ON external_metadata(fetch_status);
CREATE INDEX IF NOT EXISTS idx_external_metadata_data ON external_metadata USING gin (data);

-- Audio feature indexes
CREATE INDEX IF NOT EXISTS idx_audio_features_analysis_source ON audio_features(analysis_source_id);
CREATE INDEX IF NOT EXISTS idx_audio_features_bpm ON audio_features(bpm);
CREATE INDEX IF NOT EXISTS idx_audio_features_key ON audio_features(key, mode);
CREATE INDEX IF NOT EXISTS idx_audio_features_energy ON audio_features(energy_db);
CREATE INDEX IF NOT EXISTS idx_audio_features_danceability ON audio_features(danceability);
CREATE INDEX IF NOT EXISTS idx_audio_features_vocal ON audio_features(vocal_instrumental);
CREATE INDEX IF NOT EXISTS idx_audio_features_instruments ON audio_features USING gin (instruments);
CREATE INDEX IF NOT EXISTS idx_audio_features_moods ON audio_features USING gin (moods);

-- Metadata indexes
CREATE INDEX IF NOT EXISTS idx_artist_bios_source ON artist_bios(source);
CREATE INDEX IF NOT EXISTS idx_artist_bios_listeners ON artist_bios(listeners) WHERE listeners IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_artist_bios_playcount ON artist_bios(playcount) WHERE playcount IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_artist_tags_tag ON artist_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_artist_tags_source ON artist_tags(source);
CREATE INDEX IF NOT EXISTS idx_artist_tags_weight ON artist_tags(weight);
CREATE INDEX IF NOT EXISTS idx_similar_artists_similar ON similar_artists(similar_artist_id);
CREATE INDEX IF NOT EXISTS idx_similar_artists_source ON similar_artists(source);
CREATE INDEX IF NOT EXISTS idx_similar_artists_match ON similar_artists(match_score);
CREATE INDEX IF NOT EXISTS idx_genre_descriptions_source ON genre_descriptions(source);
CREATE INDEX IF NOT EXISTS idx_track_stats_source ON track_stats(source);
CREATE INDEX IF NOT EXISTS idx_track_stats_listeners ON track_stats(listeners);
CREATE INDEX IF NOT EXISTS idx_track_stats_playcount ON track_stats(playcount);

-- Chat indexes (AI DJ sessions)
CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id);

-- Listening history indexes
CREATE INDEX IF NOT EXISTS idx_listening_history_media_file ON listening_history(media_file_id);
CREATE INDEX IF NOT EXISTS idx_listening_history_track ON listening_history(track_id);
CREATE INDEX IF NOT EXISTS idx_listening_history_started ON listening_history(started_at);
CREATE INDEX IF NOT EXISTS idx_listening_history_track_started ON listening_history(track_id, started_at DESC);

-- Local play stats indexes
CREATE INDEX IF NOT EXISTS idx_local_play_stats_last_played ON local_play_stats(last_played_at);
CREATE INDEX IF NOT EXISTS idx_local_play_stats_play_count ON local_play_stats(play_count);

-- Listening sessions indexes
-- At most one active session (ended_at IS NULL). Index a constant
-- expression over the partial set — UNIQUE(ended_at) WHERE ended_at IS
-- NULL would NOT work, since NULLs are distinct in a unique index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_listening_sessions_one_active
    ON listening_sessions((ended_at IS NULL)) WHERE ended_at IS NULL;
-- Home shelf: archived sessions, newest first.
CREATE INDEX IF NOT EXISTS idx_listening_sessions_archived
    ON listening_sessions(ended_at DESC) WHERE ended_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_session_tracks_media_file
    ON session_tracks(media_file_id);
CREATE INDEX IF NOT EXISTS idx_session_tracks_track
    ON session_tracks(track_id);
CREATE INDEX IF NOT EXISTS idx_listening_sessions_seed_track
    ON listening_sessions(seed_track_id);

-- ============================================================
-- Trigger function for updated_at
-- ============================================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create triggers (using DO block to avoid errors on re-run)
DO $$ BEGIN CREATE TRIGGER update_embedding_models_updated_at BEFORE UPDATE ON embedding_models
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER update_artists_updated_at BEFORE UPDATE ON artists
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER update_albums_updated_at BEFORE UPDATE ON albums
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER update_tracks_updated_at BEFORE UPDATE ON tracks
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER update_album_variants_updated_at BEFORE UPDATE ON album_variants
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER update_media_files_updated_at BEFORE UPDATE ON media_files
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Denormalise media_files.file_modified_at onto album_variants so the Home
-- "New in library" sort uses an index on album_variants instead of MAX() +
-- GROUP BY across 30k media_files on every request. FOR EACH STATEMENT (not
-- FOR EACH ROW) so a bulk import inserting hundreds of files triggers one
-- recompute, not one per row. Postgres forbids combining REFERENCING ...
-- TABLE with AFTER UPDATE OF <columns>, so the UPDATE trigger fires on any
-- column change and filters affected variants inside the function — rows
-- where neither file_modified_at nor album_variant_id changed produce an
-- empty array and skip the UPDATE entirely.
CREATE OR REPLACE FUNCTION refresh_av_file_modified_at(p_av_ids INTEGER[])
RETURNS VOID AS $$
BEGIN
    UPDATE album_variants av
    SET file_modified_at = (
        SELECT MAX(mf.file_modified_at)
        FROM media_files mf
        WHERE mf.album_variant_id = av.id
    )
    WHERE av.id = ANY(p_av_ids);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION trg_mf_av_mtime_ins() RETURNS TRIGGER AS $$
BEGIN
    PERFORM refresh_av_file_modified_at(
        ARRAY(SELECT DISTINCT album_variant_id FROM new_table)
    );
    RETURN NULL;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION trg_mf_av_mtime_upd() RETURNS TRIGGER AS $$
BEGIN
    PERFORM refresh_av_file_modified_at(
        ARRAY(
            SELECT DISTINCT av_id FROM (
                SELECT n.album_variant_id AS av_id
                FROM new_table n
                JOIN old_table o ON o.id = n.id
                WHERE n.file_modified_at IS DISTINCT FROM o.file_modified_at
                   OR n.album_variant_id IS DISTINCT FROM o.album_variant_id
                UNION
                SELECT o.album_variant_id
                FROM new_table n
                JOIN old_table o ON o.id = n.id
                WHERE n.album_variant_id IS DISTINCT FROM o.album_variant_id
            ) affected
        )
    );
    RETURN NULL;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION trg_mf_av_mtime_del() RETURNS TRIGGER AS $$
BEGIN
    PERFORM refresh_av_file_modified_at(
        ARRAY(SELECT DISTINCT album_variant_id FROM old_table)
    );
    RETURN NULL;
END $$ LANGUAGE plpgsql;

DO $$ BEGIN
    CREATE TRIGGER trg_media_files_av_mtime_ins
    AFTER INSERT ON media_files
    REFERENCING NEW TABLE AS new_table
    FOR EACH STATEMENT EXECUTE FUNCTION trg_mf_av_mtime_ins();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER trg_media_files_av_mtime_upd
    AFTER UPDATE ON media_files
    REFERENCING NEW TABLE AS new_table OLD TABLE AS old_table
    FOR EACH STATEMENT EXECUTE FUNCTION trg_mf_av_mtime_upd();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER trg_media_files_av_mtime_del
    AFTER DELETE ON media_files
    REFERENCING OLD TABLE AS old_table
    FOR EACH STATEMENT EXECUTE FUNCTION trg_mf_av_mtime_del();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER trg_covers_updated_at BEFORE UPDATE ON covers
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Cover resolution is lazy (on /api/covers/by-media/<id> request) — no
-- background worker, no LISTEN/NOTIFY plumbing.

DO $$ BEGIN CREATE TRIGGER update_embeddings_updated_at BEFORE UPDATE ON embeddings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER update_genres_updated_at BEFORE UPDATE ON genres
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER trg_artist_bios_updated_at BEFORE UPDATE ON artist_bios
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER trg_artist_tags_updated_at BEFORE UPDATE ON artist_tags
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER trg_similar_artists_updated_at BEFORE UPDATE ON similar_artists
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER trg_genre_descriptions_updated_at BEFORE UPDATE ON genre_descriptions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER trg_user_settings_updated_at BEFORE UPDATE ON user_settings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER trg_tags_updated_at BEFORE UPDATE ON tags
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER trigger_audio_features_updated_at BEFORE UPDATE ON audio_features
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER trg_lyrics_embeddings_updated_at BEFORE UPDATE ON lyrics_embeddings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER trg_artist_bio_emb_updated_at BEFORE UPDATE ON artist_bio_embeddings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER trg_genre_desc_emb_updated_at BEFORE UPDATE ON genre_desc_embeddings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER trigger_external_metadata_updated_at BEFORE UPDATE ON external_metadata
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER trg_text_embeddings_updated_at BEFORE UPDATE ON text_embeddings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER trg_chat_sessions_updated_at BEFORE UPDATE ON chat_sessions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER trg_local_play_stats_updated_at BEFORE UPDATE ON local_play_stats
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER trg_track_lyrics_updated_at BEFORE UPDATE ON track_lyrics
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER trg_track_stats_updated_at BEFORE UPDATE ON track_stats
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ============================================================
-- P2P Chat (friends + encrypted messaging)
-- ============================================================

CREATE TABLE IF NOT EXISTS friends (
    id SERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL DEFAULT '',
    public_key_hex VARCHAR(128) NOT NULL UNIQUE,
    invite_code VARCHAR(96) NOT NULL,
    display_name VARCHAR(128) NOT NULL DEFAULT '',
    added_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMPTZ,
    is_blocked BOOLEAN DEFAULT FALSE,
    previous_public_key_hex VARCHAR(128)
);

CREATE TABLE IF NOT EXISTS p2p_messages (
    id SERIAL PRIMARY KEY,
    friend_id INTEGER NOT NULL REFERENCES friends(id) ON DELETE CASCADE,
    direction message_direction NOT NULL,
    content TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered BOOLEAN DEFAULT FALSE,
    read BOOLEAN DEFAULT FALSE,
    message_uuid UUID DEFAULT gen_random_uuid()
);

CREATE TABLE IF NOT EXISTS pending_key_rotations (
    id SERIAL PRIMARY KEY,
    friend_id INTEGER NOT NULL UNIQUE REFERENCES friends(id) ON DELETE CASCADE,
    old_public_key_hex VARCHAR(128) NOT NULL,
    new_public_key_hex VARCHAR(128) NOT NULL,
    new_invite_code VARCHAR(96) NOT NULL,
    rotation_message BYTEA NOT NULL,
    signature BYTEA NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sent_invites (
    id SERIAL PRIMARY KEY,
    to_email VARCHAR(255) NOT NULL,
    sent_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_friends_invite_code ON friends(invite_code);
CREATE INDEX IF NOT EXISTS idx_friends_username ON friends(username);
CREATE INDEX IF NOT EXISTS idx_p2p_messages_friend ON p2p_messages(friend_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_p2p_messages_unread
    ON p2p_messages(friend_id) WHERE direction = 'in' AND read = FALSE;
CREATE INDEX IF NOT EXISTS idx_p2p_messages_pending
    ON p2p_messages(friend_id) WHERE direction = 'out' AND delivered = FALSE;
CREATE UNIQUE INDEX IF NOT EXISTS idx_p2p_messages_uuid
    ON p2p_messages(message_uuid) WHERE message_uuid IS NOT NULL;

-- ============================================================
-- Profile + Audio chain (single-user appliance)
-- ============================================================

DO $$ BEGIN
    CREATE TYPE gear_research_state AS ENUM ('queued', 'researching', 'cached', 'failed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE user_gear_status AS ENUM ('own', 'want', 'sell', 'previously_owned');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Profile of the launcher's owner. Single-row table — Sautium is a
-- single-user appliance per launcher install. NULL on display_name/
-- city/country/bio/avatar means "not set" (Codd-style). The id=1
-- CHECK keeps the row count at exactly one.
CREATE TABLE IF NOT EXISTS user_profile (
    id              INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    display_name    VARCHAR(128),
    city            VARCHAR(128),
    country         CHAR(2),                       -- ISO 3166-1 alpha-2
    bio             TEXT,
    avatar_cover_id UUID REFERENCES covers(id) ON DELETE SET NULL,
    public_gear     BOOLEAN NOT NULL DEFAULT FALSE,
    open_to_meet    BOOLEAN NOT NULL DEFAULT FALSE,
    -- True once the user's *current* identity email has been verified
    -- on the Worker. Reset to FALSE whenever the email changes or the
    -- password changes (a new password derives a new Ed25519 key and
    -- therefore a new invite_code that the Worker has no record of).
    -- See feedback memory on the reset-trigger rules.
    email_verified  BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO user_profile (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
ALTER TABLE user_profile ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE;

-- Peer profile fields, populated from the sync handshake. Avatar is
-- referenced into the covers table — same blob-cache pattern as
-- artist photos, so it survives peer offline and stays under our TLS.
ALTER TABLE friends ADD COLUMN IF NOT EXISTS city            VARCHAR(128);
ALTER TABLE friends ADD COLUMN IF NOT EXISTS bio             TEXT;
ALTER TABLE friends ADD COLUMN IF NOT EXISTS avatar_cover_id UUID
    REFERENCES covers(id) ON DELETE SET NULL;

-- Canonical brand catalog. UUID v5 from normalised name so two nodes
-- adding "Sennheiser" collapse to the same row — same pattern as
-- artists/genres. Brand-level metadata (website, country of origin)
-- is optional, populated by the research worker when adding a model.
CREATE TABLE IF NOT EXISTS gear_brands (
    id           UUID PRIMARY KEY,
    name         VARCHAR(200) NOT NULL UNIQUE,
    website      TEXT,
    country      CHAR(2),
    founded_year INTEGER,
    created_at   TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_gear_brands_name_lower ON gear_brands(LOWER(name));

DO $$ BEGIN CREATE TRIGGER trg_gear_brands_updated_at BEFORE UPDATE ON gear_brands
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Canonical model catalog. UUID v5 from (brand.name, model, category)
-- so the same model on two nodes collapses to the same id. Sentiment
-- score / sample size are top-level aggregate columns (no JSON);
-- detailed praise / criticism terms live in gear_sentiment_terms.
CREATE TABLE IF NOT EXISTS gear_models (
    id                     UUID PRIMARY KEY,
    brand_id               UUID NOT NULL REFERENCES gear_brands(id) ON DELETE RESTRICT,
    model                  VARCHAR(300) NOT NULL,
    category               gear_category NOT NULL,
    research_state         gear_research_state NOT NULL DEFAULT 'queued',
    research_summary       TEXT,
    researched_at          TIMESTAMPTZ,
    sentiment_score        NUMERIC(3, 1),
    sentiment_sample_size  INTEGER,
    sentiment_updated_at   TIMESTAMPTZ,
    created_at             TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at             TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (brand_id, model, category)
);

CREATE INDEX IF NOT EXISTS idx_gear_models_brand        ON gear_models(brand_id);
CREATE INDEX IF NOT EXISTS idx_gear_models_model_lower  ON gear_models(LOWER(model));
CREATE INDEX IF NOT EXISTS idx_gear_models_category     ON gear_models(category);
CREATE INDEX IF NOT EXISTS idx_gear_models_research_state
    ON gear_models(research_state) WHERE research_state IN ('queued', 'researching');

DO $$ BEGIN CREATE TRIGGER trg_gear_models_updated_at BEFORE UPDATE ON gear_models
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- The launcher owner's audio chain. NULL notes = "not written"; we
-- don't collapse that to ''.
CREATE TABLE IF NOT EXISTS user_gear (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gear_model_id   UUID NOT NULL REFERENCES gear_models(id)
                          ON DELETE CASCADE ON UPDATE CASCADE,
    status          user_gear_status NOT NULL DEFAULT 'own',
    notes           TEXT,
    added_at        TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (gear_model_id)
);

CREATE INDEX IF NOT EXISTS idx_user_gear_status ON user_gear(status);

DO $$ BEGIN CREATE TRIGGER trg_user_profile_updated_at BEFORE UPDATE ON user_profile
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TRIGGER trg_user_gear_updated_at BEFORE UPDATE ON user_gear
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- EAV "schema" side — canonical attribute catalog. UUID v5 from key
-- so two nodes adding "impedance_ohm" converge. Hand-curated seed
-- (seeded=TRUE) is the reuse-first target the research worker must
-- map to; AI-proposed additions land with seeded=FALSE and may
-- be promoted after human review.
CREATE TABLE IF NOT EXISTS gear_spec_attributes (
    id           UUID PRIMARY KEY,
    key          VARCHAR(60) UNIQUE NOT NULL,
    label        VARCHAR(100) NOT NULL,
    description  TEXT NOT NULL,
    unit         VARCHAR(20),
    value_type   spec_value_type NOT NULL,
    enum_values  TEXT[],
    applies_to   TEXT[] NOT NULL,
    seeded       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_gear_spec_attributes_applies_to ON gear_spec_attributes USING GIN(applies_to);

DO $$ BEGIN CREATE TRIGGER trg_gear_spec_attributes_updated_at BEFORE UPDATE ON gear_spec_attributes
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- EAV "value" side. value_text holds the canonicalised value (numbers
-- as text so unit / value_type from the attribute remain authoritative);
-- raw_value preserves the AI's original string for audit.
CREATE TABLE IF NOT EXISTS gear_specs (
    gear_model_id  UUID NOT NULL REFERENCES gear_models(id) ON DELETE CASCADE,
    attribute_id   UUID NOT NULL REFERENCES gear_spec_attributes(id) ON DELETE CASCADE,
    value_text     TEXT NOT NULL,
    raw_value      TEXT,
    source_url     TEXT,
    PRIMARY KEY (gear_model_id, attribute_id)
);

CREATE INDEX IF NOT EXISTS idx_gear_specs_attribute ON gear_specs(attribute_id);

-- Brand-IP / proprietary technology catalog. Distinct from specs:
-- Ring Radiator / OSPF / WTA filter are nominal brand assets, often
-- patented. brand_id is nullable for industry-wide tech (I²S, S/PDIF).
CREATE TABLE IF NOT EXISTS gear_technologies (
    id                UUID PRIMARY KEY,
    key               VARCHAR(80) UNIQUE NOT NULL,
    label             VARCHAR(150) NOT NULL,
    description       TEXT NOT NULL,
    brand_id          UUID REFERENCES gear_brands(id) ON DELETE SET NULL,
    patent_or_source  TEXT,
    introduced_year   INTEGER,
    applies_to        TEXT[] NOT NULL,
    seeded            BOOLEAN NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_gear_technologies_brand      ON gear_technologies(brand_id);
CREATE INDEX IF NOT EXISTS idx_gear_technologies_applies_to ON gear_technologies USING GIN(applies_to);

DO $$ BEGIN CREATE TRIGGER trg_gear_technologies_updated_at BEFORE UPDATE ON gear_technologies
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS gear_model_technologies (
    gear_model_id  UUID NOT NULL REFERENCES gear_models(id) ON DELETE CASCADE,
    technology_id  UUID NOT NULL REFERENCES gear_technologies(id) ON DELETE CASCADE,
    PRIMARY KEY (gear_model_id, technology_id)
);

CREATE INDEX IF NOT EXISTS idx_gear_model_technologies_tech ON gear_model_technologies(technology_id);

-- Praise / criticism tags from research worker, deduplicated per
-- (model, polarity, term). Searchable across catalog ("which gear
-- gets 'organic timbre' praise?").
CREATE TABLE IF NOT EXISTS gear_sentiment_terms (
    gear_model_id  UUID NOT NULL REFERENCES gear_models(id) ON DELETE CASCADE,
    polarity       gear_polarity NOT NULL,
    term           VARCHAR(80) NOT NULL,
    weight         REAL,
    PRIMARY KEY (gear_model_id, polarity, term)
);

CREATE INDEX IF NOT EXISTS idx_gear_sentiment_term_lookup ON gear_sentiment_terms(polarity, term);

-- ============================================================
-- MusicBrainz data-dump subset (Etap 1: artist + album canon)
-- ============================================================
-- Loaded from the fullexport mbdump.tar.bz2 by backend/mb_dump_load.py. These
-- are read-only reference tables: the MB internal `id` is kept as the join key
-- (no sequences), and column order mirrors the dump's headerless TSV (= MB
-- CreateTables.sql order) so a default COPY round-trips it. Empty on a fresh
-- install until the loader runs; the canonicalization layer degrades to the MB
-- API when they are empty.

CREATE TABLE IF NOT EXISTS mb_artist (
    id              INTEGER PRIMARY KEY,
    gid             UUID,
    name            TEXT,
    sort_name       TEXT,
    begin_date_year SMALLINT, begin_date_month SMALLINT, begin_date_day SMALLINT,
    end_date_year   SMALLINT, end_date_month   SMALLINT, end_date_day   SMALLINT,
    type            INTEGER,
    area            INTEGER,
    gender          INTEGER,
    comment         TEXT,
    edits_pending   INTEGER,
    last_updated    TIMESTAMPTZ,
    ended           BOOLEAN,
    begin_area      INTEGER,
    end_area        INTEGER
);

CREATE TABLE IF NOT EXISTS mb_artist_credit (
    id            INTEGER PRIMARY KEY,
    name          TEXT,
    artist_count  SMALLINT,
    ref_count     INTEGER,
    created       TIMESTAMPTZ,
    edits_pending INTEGER,
    gid           UUID
);

CREATE TABLE IF NOT EXISTS mb_artist_credit_name (
    artist_credit INTEGER,
    position      SMALLINT,
    artist        INTEGER,
    name          TEXT,
    join_phrase   TEXT,
    PRIMARY KEY (artist_credit, position)
);

CREATE TABLE IF NOT EXISTS mb_release_group (
    id            INTEGER PRIMARY KEY,
    gid           UUID,
    name          TEXT,
    artist_credit INTEGER,
    type          INTEGER,
    comment       TEXT,
    edits_pending INTEGER,
    last_updated  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS mb_release_group_primary_type (
    id          INTEGER PRIMARY KEY,
    name        TEXT,
    parent      INTEGER,
    child_order INTEGER,
    description TEXT,
    gid         UUID
);

CREATE TABLE IF NOT EXISTS mb_release_group_secondary_type (
    id          INTEGER PRIMARY KEY,
    name        TEXT,
    parent      INTEGER,
    child_order INTEGER,
    description TEXT,
    gid         UUID
);

CREATE TABLE IF NOT EXISTS mb_release_group_secondary_type_join (
    release_group  INTEGER,
    secondary_type INTEGER,
    created        TIMESTAMPTZ,
    PRIMARY KEY (release_group, secondary_type)
);

CREATE TABLE IF NOT EXISTS mb_release (
    id            INTEGER PRIMARY KEY,
    gid           UUID,
    name          TEXT,
    artist_credit INTEGER,
    release_group INTEGER,
    status        INTEGER,
    packaging     INTEGER,
    language      INTEGER,
    script        INTEGER,
    barcode       TEXT,
    comment       TEXT,
    edits_pending INTEGER,
    quality       SMALLINT,
    last_updated  TIMESTAMPTZ
);

-- Release dates (the release-group's first_year for missing-album discovery
-- = MIN over both). In the dump since 2026-06-12 — empty until the next
-- "Update MB data" run; consumers must tolerate NULL first_year meanwhile.
CREATE TABLE IF NOT EXISTS mb_release_country (
    release    INTEGER,
    country    INTEGER,
    date_year  SMALLINT,
    date_month SMALLINT,
    date_day   SMALLINT,
    PRIMARY KEY (release, country)
);

CREATE TABLE IF NOT EXISTS mb_release_unknown_country (
    release    INTEGER PRIMARY KEY,
    date_year  SMALLINT,
    date_month SMALLINT,
    date_day   SMALLINT
);

CREATE TABLE IF NOT EXISTS mb_artist_alias (
    id                 INTEGER PRIMARY KEY,
    artist             INTEGER,
    name               TEXT,
    locale             TEXT,
    edits_pending      INTEGER,
    last_updated       TIMESTAMPTZ,
    type               INTEGER,
    sort_name          TEXT,
    begin_date_year    SMALLINT, begin_date_month SMALLINT, begin_date_day SMALLINT,
    end_date_year      SMALLINT, end_date_month   SMALLINT, end_date_day   SMALLINT,
    primary_for_locale BOOLEAN,
    ended              BOOLEAN
);

CREATE TABLE IF NOT EXISTS mb_area (
    id              INTEGER PRIMARY KEY,
    gid             UUID,
    name            TEXT,
    type            INTEGER,
    edits_pending   INTEGER,
    last_updated    TIMESTAMPTZ,
    begin_date_year SMALLINT, begin_date_month SMALLINT, begin_date_day SMALLINT,
    end_date_year   SMALLINT, end_date_month   SMALLINT, end_date_day   SMALLINT,
    ended           BOOLEAN,
    comment         TEXT
);

-- Etap 2: tracklist (release-ID by content + track normalization).
-- Column order MUST match MB CreateTables.sql exactly (positional COPY).
CREATE TABLE IF NOT EXISTS mb_recording (
    id            INTEGER PRIMARY KEY,
    gid           UUID,
    name          TEXT,
    artist_credit INTEGER,
    length        INTEGER,
    comment       TEXT,
    edits_pending INTEGER,
    last_updated  TIMESTAMPTZ,
    video         BOOLEAN
);

CREATE TABLE IF NOT EXISTS mb_track (
    id            INTEGER,
    gid           UUID,
    recording     INTEGER,
    medium        INTEGER,
    position      INTEGER,
    number        TEXT,
    name          TEXT,
    artist_credit INTEGER,
    length        INTEGER,
    edits_pending INTEGER,
    last_updated  TIMESTAMPTZ,
    is_data_track BOOLEAN
);

CREATE TABLE IF NOT EXISTS mb_medium (
    id            INTEGER PRIMARY KEY,
    release       INTEGER,
    position      INTEGER,
    format        INTEGER,
    name          TEXT,
    edits_pending INTEGER,
    last_updated  TIMESTAMPTZ,
    track_count   INTEGER,
    gid           UUID
);

CREATE TABLE IF NOT EXISTS mb_medium_format (
    id            INTEGER PRIMARY KEY,
    name          TEXT,
    parent        INTEGER,
    child_order   INTEGER,
    year          SMALLINT,
    has_discids   BOOLEAN,
    description   TEXT,
    gid           UUID
);

CREATE TABLE IF NOT EXISTS mb_release_label (
    id             INTEGER,
    release        INTEGER,
    label          INTEGER,
    catalog_number TEXT,
    last_updated   TIMESTAMPTZ
);

-- Folksonomy tags (mb_tag, mb_artist_tag) come from the mbdump-derived archive
-- (column order MIRRORS the MB dump exactly — default COPY round-trips). The
-- curated genre vocabulary (mb_genre) is NOT in the streamed archives; it's
-- populated name-only from the MB API (mb_dump_load.load_genre_list). Genres are
-- the curated subset of tags: an artist's genres = artist_tag ⋈ tag ⋈ genre on
-- lower(name), count > 0 — raw folksonomy tags (e.g. "uk") never participate.
CREATE TABLE IF NOT EXISTS mb_genre (
    id            INTEGER,
    gid           UUID,
    name          TEXT,
    comment       TEXT,
    edits_pending INTEGER,
    last_updated  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS mb_tag (
    id        INTEGER,
    name      TEXT,
    ref_count INTEGER
);

CREATE TABLE IF NOT EXISTS mb_artist_tag (
    artist       INTEGER,
    tag          INTEGER,
    count        INTEGER,
    last_updated TIMESTAMPTZ
);

-- Release-group tags = the album-genre source (release_group ⋈ tag ⋈ genre).
-- Same mbdump-derived archive and column order as the other *_tag tables.
CREATE TABLE IF NOT EXISTS mb_release_group_tag (
    release_group INTEGER,
    tag           INTEGER,
    count         INTEGER,
    last_updated  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_mb_artist_gid            ON mb_artist(gid);
CREATE INDEX IF NOT EXISTS idx_mb_artist_name_trgm      ON mb_artist     USING gin (lower(name) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_mb_artist_sortname_trgm  ON mb_artist     USING gin (lower(sort_name) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_mb_artist_alias_artist   ON mb_artist_alias(artist);
CREATE INDEX IF NOT EXISTS idx_mb_artist_alias_name_trgm ON mb_artist_alias USING gin (lower(name) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_mb_artist_name_lower      ON mb_artist(lower(name));
CREATE INDEX IF NOT EXISTS idx_mb_artist_alias_name_lower ON mb_artist_alias(lower(name));
CREATE INDEX IF NOT EXISTS idx_mb_artist_unaccent        ON mb_artist(f_unaccent(name));
CREATE INDEX IF NOT EXISTS idx_mb_artist_alias_unaccent  ON mb_artist_alias(f_unaccent(name));
CREATE INDEX IF NOT EXISTS idx_mb_artist_sortname_lower  ON mb_artist(lower(sort_name));
CREATE INDEX IF NOT EXISTS idx_mb_track_credit           ON mb_track(artist_credit);
CREATE INDEX IF NOT EXISTS idx_mb_acn_artist            ON mb_artist_credit_name(artist);
CREATE INDEX IF NOT EXISTS idx_mb_rg_credit             ON mb_release_group(artist_credit);
CREATE INDEX IF NOT EXISTS idx_mb_rg_type               ON mb_release_group(type);
CREATE INDEX IF NOT EXISTS idx_mb_rg_name_trgm          ON mb_release_group USING gin (lower(name) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_mb_release_rg            ON mb_release(release_group);
CREATE INDEX IF NOT EXISTS idx_mb_release_country_rel   ON mb_release_country(release);
CREATE INDEX IF NOT EXISTS idx_mb_rgstj_secondary       ON mb_release_group_secondary_type_join(secondary_type);
CREATE INDEX IF NOT EXISTS idx_mb_track_medium          ON mb_track(medium);
CREATE INDEX IF NOT EXISTS idx_mb_medium_release        ON mb_medium(release);
CREATE INDEX IF NOT EXISTS idx_mb_release_label_rel     ON mb_release_label(release);
CREATE INDEX IF NOT EXISTS idx_mb_recording_ac          ON mb_recording(artist_credit);
CREATE INDEX IF NOT EXISTS idx_mb_track_recording       ON mb_track(recording);
CREATE INDEX IF NOT EXISTS idx_mb_artist_tag_artist     ON mb_artist_tag(artist);
CREATE INDEX IF NOT EXISTS idx_mb_tag_id                ON mb_tag(id);
CREATE INDEX IF NOT EXISTS idx_mb_rg_tag_rg             ON mb_release_group_tag(release_group);
CREATE INDEX IF NOT EXISTS idx_mb_genre_name_lower      ON mb_genre(lower(name));

-- P2P MB slices land via INSERT ... ON CONFLICT DO NOTHING, which needs a
-- conflict target on every shipped table — these four have no PK in the dump
-- schema. (idx_mb_tag_id above is redundant with uq_mb_tag_id but kept: live
-- DBs already have it and 001 must stay idempotent against them.)
CREATE UNIQUE INDEX IF NOT EXISTS uq_mb_track_id           ON mb_track(id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mb_tag_id             ON mb_tag(id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mb_artist_tag         ON mb_artist_tag(artist, tag);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mb_release_group_tag  ON mb_release_group_tag(release_group, tag);

-- Provenance of P2P-fetched MB slices (dump-less nodes). One row per queried
-- artist name, including zero-match (matched_ids = 0): the slice is a closed
-- world per name, so "no such artist" is an answer, not a retry. A later full
-- dump load supersedes slice rows (TRUNCATE+COPY) but this log stays — it only
-- gates re-asking peers, which the VERSION marker disables anyway.
-- Authorship: slice content is public MB data and is NOT signed per row; the
-- serving node signs the whole response (Ed25519 over the canonical payload
-- hash, see mb_slice_queries.receipt_message). source_pubkey + receipt +
-- payload_sha256 are the verified, durable evidence of who produced the batch
-- this name arrived in.
CREATE TABLE IF NOT EXISTS mb_slice_fetches (
    name_key       TEXT PRIMARY KEY,           -- lower(trim(name))
    source_node    TEXT,
    dump_version   TEXT,
    matched_ids    INTEGER NOT NULL DEFAULT 0,
    source_pubkey  TEXT,                       -- hex Ed25519, verified before import
    receipt        TEXT,                       -- hex signature over payload_sha256
    payload_sha256 TEXT,
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Streaming-minted phantoms
-- ============================================================

-- Provenance for phantoms the user minted from a streaming-provider search tile
-- (Discovery supplement): explicit user intent + provider ids. The phantom-canon
-- discard pass must never sweep these rows, MB-resolvable or not.
CREATE TABLE IF NOT EXISTS streaming_mints (
    id SERIAL PRIMARY KEY,
    artist_id UUID REFERENCES artists(id) ON UPDATE CASCADE ON DELETE CASCADE,
    album_id UUID REFERENCES albums(id) ON UPDATE CASCADE ON DELETE CASCADE,
    provider VARCHAR(32) NOT NULL,
    provider_id VARCHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CHECK (artist_id IS NOT NULL OR album_id IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_streaming_mints_artist
    ON streaming_mints (provider, provider_id) WHERE artist_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_streaming_mints_album
    ON streaming_mints (provider, provider_id) WHERE album_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_streaming_mints_artist ON streaming_mints (artist_id);
CREATE INDEX IF NOT EXISTS idx_streaming_mints_album ON streaming_mints (album_id);

-- ============================================================
-- Views
-- ============================================================

CREATE OR REPLACE VIEW library_stats AS
SELECT
    -- artists physically in the catalogue: primary on >=1 track that has an
    -- audio file. The media_files join drops phantom artists (similar-artist
    -- and missing-album discovery rows with no owned files) — same physical-
    -- presence rule as total_albums/total_tracks below. role = 'primary' also
    -- drops split markers (verified_split/verified_collab compounds, 0 tracks)
    -- and featured-only members: bookkeeping rows whose count diverges across
    -- nodes with each node's split/merge history while track identity converges.
    (SELECT COUNT(*) FROM artists a
      WHERE EXISTS (SELECT 1 FROM track_artists ta
                    JOIN media_files mf ON mf.track_id = ta.track_id
                    WHERE ta.artist_id = a.id AND ta.role = 'primary')) as total_artists,
    -- owned albums/tracks only: phantom rows (MB missing-album discovery,
    -- no variants/files) are discovery data, not library contents
    (SELECT COUNT(*) FROM albums al
      WHERE EXISTS (SELECT 1 FROM album_variants av
                    WHERE av.album_id = al.id)) as total_albums,
    (SELECT COUNT(*) FROM tracks t
      WHERE EXISTS (SELECT 1 FROM media_files mf
                    WHERE mf.track_id = t.id)) as total_tracks,
    (SELECT COUNT(*) FROM media_files) as total_media_files,
    (SELECT COUNT(*) FROM embeddings) as tracks_with_embeddings,
    (SELECT COUNT(*) FROM track_lyrics) as tracks_with_lyrics,
    (SELECT SUM(duration_seconds) FROM media_files) as total_duration_seconds,
    (SELECT SUM(file_size_bytes) FROM media_files) as total_file_size_bytes,
    (SELECT COUNT(*) FROM genres) as unique_genres;
