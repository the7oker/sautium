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
-- Function and dictionary are schema-qualified because PostgreSQL 17 builds
-- functional indexes under a restricted search_path (pg_catalog only); an
-- unqualified unaccent()/'unaccent' dictionary then fails to resolve at index
-- build time even though the extension lives in public. Qualifying keeps the
-- body inlinable (a SET search_path clause would disable inlining). PG15/16
-- tolerated the unqualified form; PG17 does not.
CREATE OR REPLACE FUNCTION f_unaccent(text) RETURNS text
  LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS
$$ SELECT lower(public.unaccent('public.unaccent'::regdictionary, $1)) $$;

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
    CREATE TYPE mb_match_confidence AS ENUM ('phantom', 'name_fuzzy', 'alias_exact', 'name_exact', 'overlap_verified', 'ai', 'user');
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
    CREATE TYPE gear_category AS ENUM ('headphones', 'iems', 'dac', 'amp', 'player',
        'streamer', 'power', 'cable', 'speakers', 'power_amp', 'preamp', 'integrated_amp',
        'turntable', 'cartridge', 'phono_stage');
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
    name_latin TEXT,                        -- Latin transliteration of name for cross-script fuzzy search (Phase 0a); TEXT not VARCHAR(500) — transliteration can exceed the source length (CJK→romaji expands ~1.4x)
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
    title_latin TEXT,                       -- Latin transliteration of title for cross-script fuzzy search (Phase 0a); TEXT not VARCHAR(500) — transliteration can exceed the source length
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
    title_latin TEXT,                       -- Latin transliteration of title for cross-script fuzzy search (Phase 0a); TEXT not VARCHAR(500) — transliteration can exceed the source length
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
    origin        analysis_origin,        -- NULL for rows imported over P2P:
                                          -- the wire deliberately omits how a
                                          -- peer obtained its audio (file vs
                                          -- stream), since that turns "I
                                          -- analysed this" into "I hold this
                                          -- file". Only OUR OWN rows carry it,
                                          -- and only they need it (overwrite
                                          -- ranking + the signing gate).
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
-- Signable material: every first-hand origin='local' analysis (the per-album
-- signing whitelist was dropped 2026-07-07 — an unsigned network breaks
-- integrity testing and the sync verify chain), or origin='deezer' AND
-- is_lossless (tier 3 — a clean stream signs against the STREAM's pcm_hash,
-- claiming no possession of any local rip).
-- ============================================================

-- One row per Worker-timestamped signing batch (the daily notary root that
-- anchors authorship priority for every record proving inclusion in it).
CREATE TABLE IF NOT EXISTS signing_batches (
    batch_root    CHAR(64) PRIMARY KEY,
    author_pubkey CHAR(64) NOT NULL,
    worker_date   TIMESTAMPTZ NOT NULL,
    -- uuid5(NAMESPACE, "ip:"+ip) of the batch submitter, computed by the Worker
    -- (the only party that sees the client IP) and bound into the timestamp
    -- signature — accountability for who notarized the batch, not a claim about
    -- who analyzed the audio. NULL only for pre-v2 batches awaiting re-timestamp
    -- (sign_audio.py --resign).
    ip_hash       UUID,
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
    alias_latin TEXT NOT NULL,
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
    CREATE TYPE gear_caveat_severity AS ENUM ('info', 'warn');
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
    -- Soft-delete: NULL = in the chain, set = hidden. We never hard-delete
    -- gear — the row carries the user's notes/status and re-adding restores
    -- it, and a tombstone flag merges cleanly under P2P sync where a vanished
    -- row would resurrect. The deterministic gear_model_id keeps the shared
    -- research reachable regardless.
    removed_at      TIMESTAMPTZ,
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

-- Canonical spec-attribute seed (regenerated by scripts/regen_attr_seed.py
-- from the live curated catalog; AI additions land seeded=FALSE pending review).
INSERT INTO gear_spec_attributes (id, key, label, description, unit, value_type, enum_values, applies_to, seeded) VALUES
    ('2b40923e-7db0-5918-b88f-7e5ad0b94181', 'amplifier_class', 'Amplifier Class', 'The output stage operating class of the amplifier (e.g. Class A, Class AB, Class D). Strongly influences heat output, efficiency, and is often associated (rightly or wrongly) with particular subjective sonic characters.', NULL, 'enum'::spec_value_type, ARRAY['Class A','Class AB','Class B','Class D','Class G','Class H'], ARRAY['amp'], TRUE),
    ('132dee98-6bf6-50f9-93e5-f7bc8d19644d', 'amp_topology', 'Amplifier Topology', 'The headline output-stage design at a glance: solid-state (optionally its operating class, e.g. Class A), Class D, tube/valve, a tube/solid-state hybrid, an OTL tube stage, or an electrostatic energizer. This single descriptor names the amp''s fundamental character; the finer operating class also lives in amplifier_class.', NULL, 'enum'::spec_value_type, ARRAY['solid_state','solid_state_class_a','class_d','tube','tube_hybrid','otl','energizer','hybrid'], ARRAY['amp'], TRUE),
    ('7060400b-c8cd-52df-9c8b-a20a1aa24f98', 'awg_gauge', 'AWG Gauge', 'American Wire Gauge rating expressing the conductor''s cross-sectional area on a standardized scale. Lower numbers indicate thicker conductors with less resistance, relevant for signal integrity and current-carrying capacity.', 'AWG', 'number'::spec_value_type, NULL, ARRAY['cable'], TRUE),
    ('d90ff658-0e4f-5f2e-9eec-8fa617b6a962', 'battery_capacity_mah', 'Battery Capacity', 'Capacity of the internal rechargeable battery in milliamp-hours. Combined with amplifier efficiency, determines runtime between charges.', 'mAh', 'number'::spec_value_type, NULL, ARRAY['player'], TRUE),
    ('b4c95ab1-3778-5994-8583-781fcf487d2c', 'battery_life_hours', 'Battery Life', 'Approximate continuous playback time on a full charge under typical conditions, a key practical consideration for a portable player.', 'hours', 'string'::spec_value_type, NULL, ARRAY['player'], TRUE),
    ('a3688bd7-4a66-58f4-900f-b0a05402aca4', 'bearing_type', 'Main Bearing Design', 'Construction of the platter''s main spindle bearing, i.e. the material pairing between shaft, sleeve and ball/thrust point. Bearing precision and material choice directly affect rumble, longevity and long-term speed stability.', NULL, 'string'::spec_value_type, NULL, ARRAY['turntable'], TRUE),
    ('ab068033-a60b-5536-b783-ca5b260fcf54', 'cable_type', 'Cable Type', 'The role the cable plays in the chain: analog interconnect (RCA/XLR line level), digital (S/PDIF coax, AES/EBU, I2S, optical), USB data, power/mains cord, headphone/IEM cable, or speaker cable. Determines where it belongs in the signal path and which connector and impedance conventions apply.', NULL, 'enum'::spec_value_type, ARRAY['interconnect','digital','usb','power','headphone','speaker'], ARRAY['cable'], TRUE),
    ('a362ab61-5ad7-5821-80b4-e2bae9572160', 'cantilever_material', 'Cantilever Material', 'Material of the rod connecting the stylus tip to the coil/magnet generator. Affects rigidity, resonance behavior, and effective moving mass, which in turn shape high-frequency extension and transient response.', NULL, 'enum'::spec_value_type, ARRAY['aluminum','boron','beryllium','sapphire','diamond','duralumin','other'], ARRAY['cartridge'], TRUE),
    ('ad314029-e678-5ea0-bd84-a1b29a5b3fba', 'cartridge_mass_g', 'Cartridge Mass', 'Cartridge body weight; part of the resonant mass on the tonearm.', 'g', 'number'::spec_value_type, NULL, ARRAY['cartridge'], TRUE),
    ('4994617d-16a8-5249-9f19-025689879da1', 'cartridge_type', 'Cartridge Type', 'Generator type: mm (moving magnet, ~3-6 mV), mc_low (low-output moving coil, ~0.2-0.6 mV), mc_high (high-output MC, ~2+ mV). Decides which phono input domain the cartridge needs.', NULL, 'enum'::spec_value_type, ARRAY['mm','mc_low','mc_high'], ARRAY['cartridge'], TRUE),
    ('8e2be0b8-22f1-575a-8912-d25211a976a0', 'channel_matching_db', 'Channel Gain Matching', 'Maximum deviation in gain between left and right channels across the volume range. Tighter matching preserves accurate stereo imaging at all listening levels, particularly at low volumes with sensitive IEMs.', 'dB', 'number'::spec_value_type, NULL, ARRAY['amp'], TRUE),
    ('df50a963-cefa-5a06-97ea-bc6cd338148e', 'channel_separation_db', 'Channel Separation', 'Isolation between left and right channels at a reference frequency (typically 1kHz), in dB. Higher separation improves stereo image precision and reduces crosstalk smearing.', 'dB', 'number'::spec_value_type, NULL, ARRAY['cartridge'], TRUE),
    ('0bc4f892-6c39-5c48-91cf-e83a72fd7458', 'cmrr_db', 'Common-Mode Rejection Ratio', 'A balanced amplifier''s ability to reject noise/interference common to both phases of a differential signal. Higher CMRR yields lower noise and crosstalk when operated in balanced mode.', 'dB', 'number'::spec_value_type, NULL, ARRAY['amp'], TRUE),
    ('7ef8972c-5f50-5503-a5ad-421f48b04ffe', 'compliance_cu', 'Dynamic Compliance', 'Dynamic compliance in cu (um/mN) at 10 Hz. CAUTION: Japanese makers spec at 100 Hz — multiply by ~1.7 for the 10 Hz figure before storing. Sets the tonearm resonance together with effective mass.', 'cu', 'number'::spec_value_type, NULL, ARRAY['cartridge'], TRUE),
    ('514b332e-a55e-57f6-8f98-ec6bf7310e45', 'conductor_cross_section_mm2', 'Conductor Cross-Section', 'The cross-sectional area of each individual conductor strand bundle, measured in square millimeters. Larger cross-sections lower DC resistance and can improve current delivery and bass control in interconnects and speaker cables.', 'mm2', 'number'::spec_value_type, NULL, ARRAY['cable'], TRUE),
    ('d288cab1-7704-5668-87de-5f92e42e9395', 'conductor_material', 'Conductor Material', 'The metal and purity grade used for the signal-carrying conductor. Higher-purity copper (e.g. 5N/99.999%) has fewer grain-boundary impurities, which proponents claim reduces micro-distortion and oxidation over time.', NULL, 'string'::spec_value_type, NULL, ARRAY['cable'], TRUE),
    ('bcfa9c0a-9423-5293-8536-484432380264', 'connector_plating', 'Connector Plating', 'The metal plating applied to connector contact pins/barrels. Gold plating resists oxidation and corrosion, maintaining low contact resistance over time compared to bare or nickel-plated contacts.', NULL, 'string'::spec_value_type, NULL, ARRAY['cable'], TRUE),
    ('bbdc9b18-b27e-56db-929e-4199567af3d5', 'connector_type', 'Connector Type', 'The physical interface standard used at each end of the cable, determining balanced or unbalanced operation and compatibility with source/amplifier equipment.', NULL, 'enum'::spec_value_type, ARRAY['XLR','RCA','TRS','Speakon','Banana','Spade','BNC'], ARRAY['cable'], TRUE),
    ('781aea87-c455-51d3-bda6-f6f8d9269438', 'crossover_frequencies_hz', 'Crossover Frequencies', 'The frequency point(s) at which signal handoff occurs between drivers in a multi-way speaker. Affects driver integration, off-axis coloration, and how cleanly voices/instruments transition across the crossover region.', 'Hz', 'string'::spec_value_type, NULL, ARRAY['speakers'], TRUE),
    ('356fbd8e-0a05-5d76-b72a-749961a9ad71', 'crossover_type', 'Crossover', 'Electrical crossover topology of a multi-driver transducer (e.g. 3-way passive). Multi-BA crossovers shape the impedance curve, which drives output-impedance sensitivity.', NULL, 'string'::spec_value_type, NULL, ARRAY['iems','headphones'], TRUE),
    ('11eba206-3c18-5acf-89f7-5023ab54a406', 'dac_architecture', 'DAC Architecture', 'The digital-to-analog conversion topology: delta-sigma, discrete R2R resistor ladder, FPGA-based, NOS (non-oversampling), or a hybrid. Distinct from the DAC chip — it describes how conversion is performed and strongly shapes tonal character and the measured-vs-musical tradeoff.', NULL, 'enum'::spec_value_type, ARRAY['delta_sigma','r2r','fpga','nos','hybrid'], ARRAY['dac'], TRUE),
    ('a191518c-9874-5d54-8e07-18687ebdfdd4', 'dac_chip', 'DAC Chip', 'The digital-to-analog converter IC used to decode the digital audio stream into analog signal. Determines native format support (PCM/DSD resolution ceilings) and contributes to the tonal character and measured noise floor of the player.', NULL, 'string'::spec_value_type, NULL, ARRAY['player','dac'], TRUE),
    ('bcd94c19-4913-50a8-a315-92af04346f23', 'damping_factor', 'Damping Factor', 'Ratio of load impedance to amplifier output impedance (ref 8 ohm). Above ~50 the audible effect on bass control is settled; single digits interact with the speaker impedance curve.', NULL, 'number'::spec_value_type, NULL, ARRAY['power_amp','integrated_amp'], TRUE),
    ('93373fe2-5189-5eb5-9082-249eda13bd6c', 'dc_offset_filtering_v', 'DC Offset Filtering Capacity', 'The maximum DC voltage offset the device can strip from the AC mains line before passing power downstream. Uncorrected DC offset causes toroidal/EI transformer cores in connected amplifiers and sources to saturate, producing audible mechanical hum and reduced dynamic headroom; this spec indicates how much offset the blocker can neutralize.', 'V', 'number'::spec_value_type, NULL, ARRAY['power'], TRUE),
    ('43c3b7c9-626b-588a-9055-b83ad41c8536', 'dielectric_material', 'Dielectric/Insulation Material', 'The insulating material surrounding the conductor. Its dielectric absorption and constant influence capacitance and how much energy is stored/released by the insulation, which some listeners associate with timing smear or coloration.', NULL, 'string'::spec_value_type, NULL, ARRAY['cable'], TRUE),
    ('a81ea8bb-98f7-5d19-aa38-55acf98476ef', 'digital_output_types', 'Digital Outputs', 'Digital audio outputs available (e.g. ''USB, optical''). Presence enables the transport role: feeding an external DAC instead of using the internal one.', NULL, 'string'::spec_value_type, NULL, ARRAY['player','streamer'], TRUE),
    ('87d5d2a6-c92b-5f91-aa77-b8c01a176512', 'dimensions_mm', 'Dimensions (LxWxH)', 'Physical outer dimensions of the unit in millimeters, length by width by height. Relevant for fitting within equipment racks or behind furniture.', 'mm', 'string'::spec_value_type, NULL, ARRAY['power'], TRUE),
    ('47748c1d-a66b-5478-a6cb-20d87605fca4', 'display_size_in', 'Display Size', 'Diagonal size of the built-in screen used for navigation and album art, affecting usability of the on-device UI.', 'in', 'number'::spec_value_type, NULL, ARRAY['player'], TRUE),
    ('4d8a8da3-6e34-591f-96b9-2400ed603c55', 'driver_configuration', 'Driver Configuration', 'Describes how the balanced-armature drivers in a multi-driver IEM are allocated across frequency bands (e.g. infra-low, low, medium, high). It indicates tonal balance strategy and complexity of the internal acoustic design.', NULL, 'string'::spec_value_type, NULL, ARRAY['iems'], TRUE),
    ('b8e18aa6-6564-5211-a73d-0a6741681512', 'driver_type', 'Driver Type', 'The fundamental transducer technology used to generate sound, such as dynamic, planar magnetic, electrostatic, or balanced armature. Fundamentally shapes distortion behavior, transient speed, and typical frequency response shape.', NULL, 'enum'::spec_value_type, ARRAY['dynamic','planar_magnetic','electrostatic','balanced_armature','hybrid','ribbon'], ARRAY['headphones','iems'], TRUE),
    ('0fdaf0af-ce4d-5d2b-93e8-f586a0ccbb5b', 'drive_type', 'Drive Type', 'Mechanism used to transfer motor torque to the platter. Belt and string drives mechanically decouple motor vibration from the platter for lower rumble; direct drive couples the motor directly for higher torque and speed accuracy; idler drive uses a rubber wheel for higher torque at the cost of more noise.', NULL, 'enum'::spec_value_type, ARRAY['belt','string','direct','idler','rim'], ARRAY['turntable'], TRUE),
    ('81b111b1-5987-5e9f-afb3-1d19b3a79973', 'dsd_max_rate', 'Max Native DSD Rate', 'The highest native (non-DoP) DSD sample rate the DAC can accept and convert, expressed as a DSD multiple (e.g. DSD256, DSD1024). Affects compatibility with high-rate DSD source files.', NULL, 'string'::spec_value_type, NULL, ARRAY['dac'], TRUE),
    ('7662776c-518b-5223-a976-1aa8d60f9f13', 'dynamic_range_db', 'Dynamic Range', 'The ratio between the loudest undistorted signal and the noise floor, expressed in dB. Higher values allow finer resolution of quiet passages and lower audible hiss.', 'dB', 'number'::spec_value_type, NULL, ARRAY['dac'], TRUE),
    ('6aaca1fd-bbfe-56bc-a301-907d25693c40', 'effective_mass_g', 'Tonearm Effective Mass', 'Effective mass of the (integrated) tonearm. With cartridge compliance and mass it sets the low-frequency resonance — target 8-12 Hz.', 'g', 'number'::spec_value_type, NULL, ARRAY['turntable'], TRUE),
    ('50c9ae9e-a33b-56ee-8193-d5531573ad1f', 'electrostatic_bias_v', 'Electrostatic Bias Voltage', 'DC bias an energizer supplies to an electrostatic diaphragm (e.g. 580V STAX PRO). Presence marks the amp as an energizer: it drives electrostats and nothing else.', 'V', 'number'::spec_value_type, NULL, ARRAY['amp'], TRUE),
    ('e67dc87c-500b-5636-80c9-4dcfe97bbfc8', 'enclosure_material', 'Enclosure Material', 'The material of the outer chassis housing the mains block internals. Metal enclosures like aluminum can provide shielding against ambient RFI/EMI compared to plastic housings.', NULL, 'enum'::spec_value_type, ARRAY['aluminum','steel','plastic','wood'], ARRAY['power'], TRUE),
    ('803e8d2a-58cd-5951-a4dc-360c7681cffd', 'external_storage_gb_max', 'Maximum External Storage', 'Total additional storage supported via removable memory cards (e.g. microSD slots), extending the library size beyond internal storage.', 'GB', 'number'::spec_value_type, NULL, ARRAY['player'], TRUE),
    ('050f7908-593e-5404-b3ec-b2409c541aab', 'feedback_topology', 'Feedback Topology', 'Whether the amplifier''s gain stages use global negative feedback, local/nested feedback only, or none at all. Zero-feedback designs are prized by some listeners for a more ''natural'' transient character at the cost of raw measured linearity.', NULL, 'enum'::spec_value_type, ARRAY['zero_feedback','local_feedback','global_feedback','nested_feedback'], ARRAY['amp'], TRUE),
    ('87d68b50-d9bb-586d-af32-bd23fb585457', 'filtered_socket_count', 'Filtered Socket Count', 'Number of outlets on the unit that pass through the internal RFI/EMI noise filtering stage, as opposed to raw unfiltered outlets typically reserved for current-hungry amplifiers. Affects how a system is partitioned between noise-sensitive source components and high-current power amps.', NULL, 'number'::spec_value_type, NULL, ARRAY['power'], TRUE),
    ('03f6bc31-23d2-53f0-b40b-6bfb8312ae47', 'form_factor', 'Form Factor', 'Physical form of the source device: a battery-powered portable player (DAP), a mains-powered desktop unit, or a pure digital transport feeding an external DAC. Sets portability and the typical use context.', NULL, 'enum'::spec_value_type, ARRAY['portable','desktop','transport'], ARRAY['player','streamer'], TRUE),
    ('55500c3b-144a-525e-9320-767815086860', 'freq_response_high_hz', 'Frequency Response Upper Bound (Hz)', 'The highest frequency the driver is specified to reproduce. Affects perceived air, sparkle, and treble extension, though audibility above ~20 kHz is debated.', 'Hz', 'number'::spec_value_type, NULL, ARRAY['headphones','iems'], TRUE),
    ('76bcb4c5-5aff-5eac-ad15-0acf4e0d1988', 'freq_response_low_hz', 'Frequency Response Lower Bound (Hz)', 'The lowest frequency the driver is specified to reproduce, as measured or claimed by the manufacturer. Influences perceived sub-bass extension and rumble.', 'Hz', 'number'::spec_value_type, NULL, ARRAY['headphones','iems'], TRUE),
    ('f8512c0e-3873-5796-b442-5ea4c3bb84c6', 'frequency_response', 'Frequency Response', 'The output level deviation across the audible band (typically 20Hz-20kHz), stated with a tolerance. Reflects how flat/neutral the electronics are before headphone-specific coloration.', NULL, 'string'::spec_value_type, NULL, ARRAY['amp','dac','player'], TRUE),
    ('e69bdb94-e2b0-5677-8886-5a81cb81970a', 'frequency_response_range', 'Frequency Response Range', 'Manufacturer-specified frequency range at the stated tolerance (e.g. ±3dB), indicating the low- and high-frequency extension of the speaker. Bounds achievable tonal balance and whether a subwoofer is needed for deep bass.', 'Hz', 'string'::spec_value_type, NULL, ARRAY['speakers'], TRUE),
    ('dd84c353-15c0-562e-bee0-3fb90cbacf4d', 'fuse_rating_a', 'Fuse Rating', 'Current rating of the internal protective fuse, in amps. Protects connected equipment and wiring from overcurrent faults; a blown fuse typically indicates a downstream fault or exceeded load.', 'A', 'number'::spec_value_type, NULL, ARRAY['power'], TRUE),
    ('0a568632-8d32-5cc5-8a6a-8c36438de80f', 'fuse_type', 'Fuse Type', 'The physical/electrical characteristic of the internal fuse (e.g. ceramic slow-blow vs fast-blow). Slow-blow fuses tolerate momentary inrush current from transformers without nuisance-tripping.', NULL, 'enum'::spec_value_type, ARRAY['ceramic_slow_blow','ceramic_fast_blow','glass_fast_blow','resettable_breaker'], ARRAY['power'], TRUE),
    ('50e079f6-a17d-5999-b95e-f8901538c394', 'headphone_output_connectors', 'Headphone Output Connectors', 'The set of physical headphone output jacks provided (single-ended and/or balanced). Determines cable/connector compatibility with a user''s existing headphones.', NULL, 'string'::spec_value_type, NULL, ARRAY['amp'], TRUE),
    ('d77a34a0-e3da-5f12-b7e7-e5542bb3b612', 'hp_out_vrms_max_bal', 'Max HP Output Voltage (Balanced)', 'Maximum voltage swing on the balanced headphone output at highest gain.', 'Vrms', 'number'::spec_value_type, NULL, ARRAY['player','amp'], TRUE),
    ('bbf3ef77-bb35-5a1a-98eb-dbe0da43c8f3', 'hp_out_vrms_max_se', 'Max HP Output Voltage (SE)', 'Maximum voltage swing on the single-ended headphone output at highest gain. With load impedance this bounds achievable SPL.', 'Vrms', 'number'::spec_value_type, NULL, ARRAY['player','amp'], TRUE),
    ('8aab202f-ade8-5fb2-8e10-072357600dec', 'imd_db', 'Intermodulation Distortion (SMPTE)', 'Level of intermodulation distortion products generated when two closely-spaced test tones are amplified together, measured per the SMPTE standard. Lower values indicate less added artificial harshness or grain on complex musical material.', 'dB', 'number'::spec_value_type, NULL, ARRAY['amp'], TRUE),
    ('1391e96b-5882-5022-8e89-8d47ecc420f4', 'impedance_min_ohm', 'Minimum Impedance', 'Lowest point of the impedance curve. Amplifier load compatibility is judged against THIS, not the nominal — a nominal 8-ohm speaker dipping to 2.9 ohm is a 4-ohm-class load.', 'ohm', 'number'::spec_value_type, NULL, ARRAY['speakers'], TRUE),
    ('4a0aba61-1587-5fdd-9962-81def281b9a3', 'impedance_ohm', 'Impedance (Ohms)', 'The electrical resistance the driver presents to an amplifier, measured in ohms. Lower impedance headphones are easier to drive to loud volumes from portable sources but can draw more current; higher impedance often pairs with more consistent damping across amplifiers.', 'Ω', 'number'::spec_value_type, NULL, ARRAY['headphones'], TRUE),
    ('7fc55caf-8dce-512f-81dd-b36d1105bfdc', 'input_impedance_ohm', 'Input Impedance', 'The impedance the amplifier presents to an upstream source (preamp, DAC). Affects gain-staging compatibility and how much the source''s own output impedance or cable capacitance can roll off treble or add noise.', 'ohm', 'number'::spec_value_type, NULL, ARRAY['integrated_amp','amp'], TRUE),
    ('5640622d-fe0d-5387-9000-8fd7b025613a', 'input_sensitivity_vrms', 'Input Sensitivity (full power)', 'Input voltage that drives the amplifier to full rated power. Gain staging: the preamp/DAC must reach this (with margin) for the amp to deliver its rating.', 'Vrms', 'number'::spec_value_type, NULL, ARRAY['power_amp','integrated_amp'], TRUE),
    ('05e2686f-a468-583e-a8fe-532ad608a95d', 'interface_load_ohm', 'Bundled Interface Load', 'Input impedance of the transducer''s BUNDLED domain interface (ribbon current-drive box, transformer). When present, an amplifier pairs against THIS load, never the raw motor — the sub-ohm ribbon figure is what the interface sees, not the amp.', 'ohm', 'number'::spec_value_type, NULL, ARRAY['headphones','iems'], TRUE),
    ('c99b6358-81c3-522b-8449-ba5585c09144', 'internal_impedance_ohm', 'Internal Impedance', 'Coil DC resistance/impedance. MC loading rule of thumb: resistive load 10-25x this value.', 'ohm', 'number'::spec_value_type, NULL, ARRAY['cartridge'], TRUE),
    ('daa52f53-bb6c-541f-bd60-ec3fe6b34716', 'internal_storage_gb', 'Internal Storage', 'Built-in non-expandable flash storage capacity available for music files.', 'GB', 'number'::spec_value_type, NULL, ARRAY['player'], TRUE),
    ('0ceccfa2-8c80-5d7d-b107-38aba15f0ec1', 'internal_wire_gauge_mm2', 'Internal Wire Gauge', 'Cross-sectional area of the internal conductors connecting inlet to outlets, in square millimeters. Larger gauge reduces resistive loss and voltage drop under high current draw, particularly relevant for power amplifiers.', 'mm2', 'number'::spec_value_type, NULL, ARRAY['power'], TRUE),
    ('19927306-9009-5b07-adfc-0a510513d28a', 'line_input_impedance_bal_ohm', 'Line Input Impedance (Balanced)', 'Input impedance of the balanced (XLR) line input. Bridging rule: source line out-Z x10 must not exceed it.', 'ohm', 'number'::spec_value_type, NULL, ARRAY['amp'], TRUE),
    ('391f6b46-f132-5647-a3e5-fbad55abb2a1', 'line_input_impedance_ohm', 'Line Input Impedance', 'Input impedance presented to an upstream source (e.g. DAC or preamp) on the analog line inputs. Affects gain-staging and interaction with the source''s output impedance.', 'ohm', 'number'::spec_value_type, NULL, ARRAY['amp'], TRUE),
    ('ad89300f-6520-5e47-ab77-6c2ef33d9cf4', 'line_output_impedance_ohm', 'Line Output Impedance', 'Output impedance of the unit''s preamp/line-level outputs feeding a downstream power amp or active speakers. Lower values minimize interaction with cable capacitance and downstream input impedance.', 'ohm', 'number'::spec_value_type, NULL, ARRAY['amp','dac'], TRUE),
    ('ebaf3248-4422-552d-b6d0-f6f38ec13413', 'line_out_vrms_bal', 'Line Out Voltage (Balanced)', 'Fixed line-level output voltage on the balanced (XLR/4.4mm) output. Activates the device''s source role for balanced chains.', 'Vrms', 'number'::spec_value_type, NULL, ARRAY['player','dac'], TRUE),
    ('a1920145-ba4d-5931-849f-4798f982bfce', 'line_out_vrms_se', 'Line Out Voltage (SE)', 'Fixed line-level output voltage on the single-ended (RCA/3.5mm) output. Activates the device''s source role and drives gain-staging checks into a downstream amplifier.', 'Vrms', 'number'::spec_value_type, NULL, ARRAY['player','dac'], TRUE),
    ('173e7a70-11bd-5bbb-a78b-a1f0a9dc7bee', 'max_current_a', 'Maximum Current', 'The maximum continuous current rating of the inlet/outlets and internal wiring, expressed in amps. Sets the ceiling for total connected load and interacts with the fuse rating for protection.', 'A', 'number'::spec_value_type, NULL, ARRAY['power'], TRUE),
    ('2e7a7f80-c2b1-5dec-b569-23b3a027a253', 'max_gain_db', 'Maximum Gain', 'The highest available gain setting on the volume control, expressed in dB. Defines the ceiling of amplification available for low-sensitivity, high-impedance headphones.', 'dB', 'number'::spec_value_type, NULL, ARRAY['amp'], TRUE),
    ('78b2c705-5667-5c21-bbd8-0e8f6afc9f01', 'max_input_vrms_bal', 'Max Input Level (Balanced)', 'Maximum balanced line input level before the input stage clips.', 'Vrms', 'number'::spec_value_type, NULL, ARRAY['amp'], TRUE),
    ('b541bcf4-adbf-56a6-a9ac-ec21895626ff', 'max_input_vrms_se', 'Max Input Level (SE)', 'Maximum single-ended line input level before the input stage clips. Gain-staging ceiling for the source.', 'Vrms', 'number'::spec_value_type, NULL, ARRAY['amp'], TRUE),
    ('26a65bb8-a482-5278-badc-9abbd7ad240b', 'max_power_w', 'Maximum Continuous Power', 'The rated continuous power the mains block can deliver across all outlets combined before the fuse trips, expressed in watts. Determines whether the unit can safely support power amplifiers or high-draw components.', 'W', 'number'::spec_value_type, NULL, ARRAY['power'], TRUE),
    ('d8b26191-d208-5482-991c-cc99b2c63595', 'max_spl_db', 'Maximum SPL (dB)', 'The maximum sound pressure level the driver can produce before significant distortion or damage, indicating dynamic headroom for loud transients.', 'dB SPL', 'number'::spec_value_type, NULL, ARRAY['headphones','iems'], TRUE),
    ('24e4ab03-fac0-5718-8dee-3ceaae127bf7', 'mc_gain_db', 'MC Gain', 'Gain of the MC input (typically 55-70 dB). Absent = no MC support: a low-output MC cartridge domain-fails here.', 'dB', 'number'::spec_value_type, NULL, ARRAY['phono_stage'], TRUE),
    ('078e7d93-9f2d-5c5e-b699-240eb9835d23', 'mc_input', 'MC Input Present', 'Whether the stage has a moving-coil input at all — distinct from the MC gain figure. Absence of a gain NUMBER must never be read as absence of the INPUT.', NULL, 'boolean'::spec_value_type, NULL, ARRAY['phono_stage'], TRUE),
    ('815ac3bd-0f64-56b2-b66f-b14a766d635e', 'mc_load_capacitance_max_pf', 'MC Load Capacitance Max', 'Highest selectable capacitive loading applied to the MC input, switchable via rear DIP switches on the Aria MK3. Provides fine-tuning of MC input stability/response alongside resistive loading.', 'pF', 'number'::spec_value_type, NULL, ARRAY['phono_stage'], TRUE),
    ('00bf18c7-f823-52a5-84ad-12072873fa0c', 'mc_load_capacitance_min_pf', 'MC Load Capacitance Min', 'Lowest selectable capacitive loading applied to the MC input. Unlike MM cartridges, most MC designs are largely insensitive to capacitance, but Rega''s Aria line exposes a switchable range that can subtly affect high-frequency stability with certain low-inductance MC coils.', 'pF', 'number'::spec_value_type, NULL, ARRAY['phono_stage'], TRUE),
    ('15f18854-4704-51cd-855f-f819d403d725', 'mc_load_max_ohm', 'MC Load Max', 'Highest selectable MC resistive load.', 'ohm', 'number'::spec_value_type, NULL, ARRAY['phono_stage'], TRUE),
    ('4bf2239d-35c5-5e75-80f4-cb8cef90cb88', 'mc_load_min_ohm', 'MC Load Min', 'Lowest selectable MC resistive load.', 'ohm', 'number'::spec_value_type, NULL, ARRAY['phono_stage'], TRUE),
    ('f7db4e85-6da3-5efa-9b92-ee294b9b5eec', 'mc_sensitivity_mv', 'MC Input Sensitivity', 'The input voltage at the MC terminal required to reach the phono stage''s rated output level; a lower number means higher effective gain. Manufacturers sometimes publish this instead of a dB gain figure, and it directly determines noise-floor/headroom compatibility with very low-output MC cartridges.', 'mV', 'number'::spec_value_type, NULL, ARRAY['phono_stage'], TRUE),
    ('cccd6729-f370-5dc1-b4b8-b3a9ffdeed8d', 'min_gain_db', 'Minimum Gain / Maximum Attenuation', 'The lowest (most attenuated) gain setting available on the volume control, expressed in dB relative to unity. Defines how low the amplifier can be turned down, important for very sensitive IEMs to avoid channel imbalance at usable listening levels.', 'dB', 'number'::spec_value_type, NULL, ARRAY['amp'], TRUE),
    ('82757cd1-2933-504d-8063-25ae2493d318', 'mm_gain_db', 'MM Gain', 'Gain of the MM input (typically ~40 dB).', 'dB', 'number'::spec_value_type, NULL, ARRAY['phono_stage'], TRUE),
    ('29ce0653-570b-5ec7-ba0b-b0e29d4f2140', 'mm_input_capacitance_pf', 'MM Input Capacitance', 'Input capacitance of the MM stage (add tonearm-cable capacitance). Sets the MM electrical HF resonance; mismatch tilts the top octave.', 'pF', 'number'::spec_value_type, NULL, ARRAY['phono_stage'], TRUE),
    ('827ad31d-b211-503b-bc27-b66da394efc8', 'mm_input_impedance_ohm', 'MM Input Impedance', 'The resistive loading presented to a moving-magnet cartridge, conventionally standardized around 47 kOhm. Combined with mm_input_capacitance_pf and tonearm-cable capacitance, it sets the cartridge''s electrical high-frequency resonance and top-octave tonal balance.', 'ohm', 'number'::spec_value_type, NULL, ARRAY['phono_stage'], TRUE),
    ('3a0975db-7158-555f-94a3-a5d84bdfacd7', 'mm_sensitivity_mv', 'MM Input Sensitivity', 'The input voltage at the MM terminal required to reach the phono stage''s rated output level. Lower values indicate higher gain; used alongside mm_gain_db when a manufacturer quotes sensitivity rather than a dB figure.', 'mV', 'number'::spec_value_type, NULL, ARRAY['phono_stage'], TRUE),
    ('3cf21bce-d304-5dbd-818e-e6cca035c804', 'motor_type', 'Motor Type', 'Electrical design of the turntable''s drive motor. Synchronous AC motors lock to a generated reference frequency for stable speed; DC servo motors use electronic feedback control; direct-drive motors integrate the platter into the motor rotor.', NULL, 'enum'::spec_value_type, ARRAY['synchronous_ac','dc_servo','direct_drive_motor','ac_induction'], ARRAY['turntable'], TRUE),
    ('f3bb8b05-4c3c-5c45-bde7-c292862743dd', 'negative_feedback_db', 'Negative Feedback', 'The amount of global negative feedback (in dB) applied around the output stage, sometimes user-switchable. More feedback tightens damping and lowers distortion/output impedance; less feedback is often described as more open or ''tube-like'' but with looser bass control.', 'dB', 'string'::spec_value_type, NULL, ARRAY['integrated_amp','amp'], TRUE),
    ('adc40f13-0a90-52fb-840c-e823a2ecd4b8', 'noise_floor_uvrms', 'Residual Noise', 'Wideband (20Hz-20kHz) RMS noise voltage measured at the output with no signal present. Lower values mean lower audible hiss through sensitive headphones or efficient speakers.', 'µVrms', 'number'::spec_value_type, NULL, ARRAY['amp','dac'], TRUE),
    ('bf956203-f2ee-5e13-a449-ef55927e755b', 'output_impedance_ohm', 'Output Impedance', 'Output impedance of the HEADPHONE output. Line-out impedance lives in line_output_impedance_ohm / output_impedance_rca_ohm / output_impedance_xlr_ohm.', 'ohm', 'number'::spec_value_type, NULL, ARRAY['amp','player'], TRUE),
    ('28e0dc55-06be-5872-b35b-6b606eb7d9df', 'output_impedance_rca_ohm', 'RCA Output Impedance', 'The source impedance seen at the single-ended output. Low values ensure the DAC drives long cables and low-impedance amplifier inputs without frequency response loss.', 'ohm', 'number'::spec_value_type, NULL, ARRAY['dac'], TRUE),
    ('e046f9e2-67f3-507c-b928-2d28d16aa62f', 'output_impedance_xlr_ohm', 'XLR Output Impedance', 'The source impedance seen at the balanced output. Low values ensure the DAC drives long cables and low-impedance amplifier inputs without frequency response loss.', 'ohm', 'number'::spec_value_type, NULL, ARRAY['dac'], TRUE),
    ('96703b31-1e1e-506a-8211-776e3d46a7f9', 'output_mv', 'Output Level', 'Nominal output at 5 cm/s. With the phono stage gain this sets the line level and the noise margin — 0.3 mV into an MM-gain input is a hiss bath, not a pairing.', 'mV', 'number'::spec_value_type, NULL, ARRAY['cartridge'], TRUE),
    ('20c4c775-9e1f-579b-9e8d-8911405cf415', 'output_power_150ohm_mw', 'Output Power @ 150 Ω', 'Continuous headphone-output power into a 150-ohm load.', 'mW', 'number'::spec_value_type, NULL, ARRAY['amp','player'], TRUE),
    ('21552789-af57-536f-ba91-44b4eb93885c', 'output_power_300ohm_mw', 'Output Power @ 300 Ω', 'Continuous headphone-output power into a 300-ohm load (HD650/ZMF-class dynamics).', 'mW', 'number'::spec_value_type, NULL, ARRAY['amp','player'], TRUE),
    ('a16f258c-f6d6-543d-a744-3b97499ec774', 'output_power_32ohm_mw', 'Output Power @ 32 Ω', 'Continuous headphone-output power into a 32-ohm load. Load-specific power points are the canonical form; unqualified power figures are meaningless for pairing.', 'mW', 'number'::spec_value_type, NULL, ARRAY['amp','player'], TRUE),
    ('6e8c1630-c8e5-54b1-8591-fc689263c0e0', 'output_power_4ohm_w', 'Output Power @ 4 Ohm', 'Continuous output power into a 4-ohm load. Should roughly double the 8-ohm figure in a current-capable design; a small increase signals a supply-limited amp on hard loads.', 'W', 'number'::spec_value_type, NULL, ARRAY['power_amp','integrated_amp'], TRUE),
    ('3c5e9db1-5f1f-5d07-b475-c8723394c184', 'output_power_60ohm_mw', 'Output Power @ 60 Ω', 'Continuous headphone-output power into a 60-ohm load (Susvara-class planars).', 'mW', 'number'::spec_value_type, NULL, ARRAY['amp','player'], TRUE),
    ('90773b44-ad82-54d3-8eac-c32bba5f8c74', 'output_power_8ohm_w', 'Output Power @ 8 Ohm', 'Continuous output power into an 8-ohm load, both channels driven.', 'W', 'number'::spec_value_type, NULL, ARRAY['power_amp','integrated_amp'], TRUE),
    ('7dbee578-3d8e-5153-84cd-84504193bb49', 'output_power_mw', 'Headphone Output Power', 'Maximum power delivered into a reference load (commonly 32Ω), determining how loud and well-controlled a given headphone can be driven, particularly relevant for harder-to-drive full-size headphones.', 'mW', 'number'::spec_value_type, NULL, ARRAY['amp','player'], TRUE),
    ('514480ba-4c2a-5f74-bf9f-3ebaca198359', 'output_transformer_impedance_ohm', 'Output Transformer Impedance', 'The nominal secondary tap impedance of a transformer-coupled tube amplifier''s output stage. It sets the speaker load for optimal power transfer, bass damping and distortion; a single low tap (e.g. 6 ohm) is a compromise chosen to reasonably match both 4 ohm and 8 ohm speakers.', 'ohm', 'number'::spec_value_type, NULL, ARRAY['integrated_amp','amp'], TRUE),
    ('bbb06d89-0ff7-5f16-8128-d3e0a699589b', 'output_voltage_rca_vrms', 'RCA Output Voltage', 'Maximum single-ended (RCA) analog output level in volts RMS at full scale. Determines gain-staging needs with downstream amplifiers.', 'Vrms', 'number'::spec_value_type, NULL, ARRAY['dac'], TRUE),
    ('607b9107-fee0-5b93-bf2c-23443c769b75', 'output_voltage_xlr_vrms', 'XLR Output Voltage', 'Maximum balanced (XLR) analog output level in volts RMS at full scale, typically double the RCA output. Determines gain-staging needs with balanced amplifier inputs.', 'Vrms', 'number'::spec_value_type, NULL, ARRAY['dac'], TRUE),
    ('38d6421e-f647-59e3-ab7b-f9c7868c04e0', 'pcm_max_bit_depth', 'Max PCM Bit Depth', 'The maximum PCM word length (bit depth) supported by the DAC chain, affecting theoretical dynamic range headroom.', 'bit', 'number'::spec_value_type, NULL, ARRAY['dac'], TRUE),
    ('412ec438-5816-58a9-afad-fd9eefb1b679', 'pcm_max_khz', 'Max PCM Sample Rate', 'The highest PCM sample rate the DAC''s digital inputs can decode, expressed in kHz. Higher values indicate support for higher-resolution PCM source material over USB/I2S.', 'kHz', 'number'::spec_value_type, NULL, ARRAY['dac'], TRUE),
    ('1d589880-c764-534c-a9a8-8ae16e95baa9', 'platter_mat_material', 'Platter Mat Material', 'Material(s) of the mat supplied between the platter surface and the record. Affects record coupling/damping, static buildup and how readily platter-borne resonance is transmitted into the vinyl.', NULL, 'string'::spec_value_type, NULL, ARRAY['turntable'], TRUE),
    ('50d73c35-ec52-514d-8579-7d7eb5ac7724', 'platter_thickness_mm', 'Platter Thickness', 'Thickness of the turntable''s platter through its main mass. Thicker platters generally add rotational inertia and mechanical damping, helping stabilize speed and reduce audible resonance.', 'mm', 'number'::spec_value_type, NULL, ARRAY['turntable'], TRUE),
    ('1aca21c6-9663-5709-9330-e06bc8d14c0a', 'platter_weight_kg', 'Platter Weight', 'Mass of the rotating platter alone, excluding plinth and motor. Higher platter mass increases flywheel effect, improving speed stability and reducing the audible impact of motor/bearing noise.', 'kg', 'number'::spec_value_type, NULL, ARRAY['turntable'], TRUE),
    ('645ee5db-9235-5331-be85-5345801b0555', 'plinth_thickness_mm', 'Plinth Thickness', 'Thickness of the main chassis/plinth body that houses the bearing and mounts the tonearm. A thicker, denser plinth resists flexing and resonance, improving isolation from external vibration.', 'mm', 'number'::spec_value_type, NULL, ARRAY['turntable'], TRUE),
    ('31ac572f-5166-52a8-b350-31f6a591d098', 'power_consumption_w', 'Power Consumption', 'Typical operating power draw in watts, relevant for running cost and heat output in an equipment rack.', 'W', 'number'::spec_value_type, NULL, ARRAY['amp','dac'], TRUE),
    ('de8d7078-bc0d-51f5-a7b0-9bc2bbc45fcd', 'powered_speaker', 'Powered (Active)', 'Whether the speaker has built-in amplification and takes line level directly — pairs with preamp/DAC outputs instead of a power amplifier.', NULL, 'boolean'::spec_value_type, NULL, ARRAY['speakers'], TRUE),
    ('dff115bd-f4cc-58c5-a931-53dd2b5be893', 'power_handling_continuous_w', 'Continuous Power Handling', 'Manufacturer-rated continuous (thermal) power handling of the speaker, distinct from recommended amplifier power. Exceeding it risks driver/voice-coil damage from sustained heat rather than transient clipping.', 'W', 'number'::spec_value_type, NULL, ARRAY['speakers'], TRUE),
    ('34766f5a-e5ab-5590-a4f0-bd43babc5d07', 'power_handling_peak_w', 'Peak Power Handling', 'Manufacturer-rated short-term peak (transient) power handling before mechanical or thermal damage risk, relevant to dynamic passages and headroom.', 'W', 'number'::spec_value_type, NULL, ARRAY['speakers'], TRUE),
    ('d0365d2e-5c81-5e20-8310-9f4424c2a174', 'power_output_w', 'Power Output', 'Maximum RMS power the amplifier can deliver into a specified load impedance. Determines whether the amp can adequately drive low-sensitivity planar magnetic or electrostatic-class headphones without clipping.', 'W', 'number'::spec_value_type, NULL, ARRAY['amp'], TRUE),
    ('42fde960-aa5c-591c-9899-f3361f2a0a7b', 'power_type', 'Power Device Type', 'The functional role of the mains device: a filtering conditioner, a passive distributor, a DC blocker, a voltage stabilizer/regulator, an AC regenerator, or an isolation transformer. Names which mains problem it solves (RFI/EMI, DC-offset transformer hum, voltage sag).', NULL, 'enum'::spec_value_type, ARRAY['conditioner','distributor','dc_blocker','voltage_stabilizer','regenerator','isolation_transformer'], ARRAY['power'], TRUE),
    ('e4878e9b-f005-5917-aa25-70a915466883', 'preamp_out_vrms_max_bal', 'Preamp Out Max (Balanced)', 'Maximum balanced (XLR) preamp output level.', 'Vrms', 'number'::spec_value_type, NULL, ARRAY['amp','preamp','dac'], TRUE),
    ('9df14cb8-6283-5c81-a46e-93623f726698', 'preamp_out_vrms_max_se', 'Preamp Out Max (SE)', 'Maximum single-ended (RCA) preamp output level. With the power amp input sensitivity this decides whether full power is reachable.', 'Vrms', 'number'::spec_value_type, NULL, ARRAY['amp','preamp','dac'], TRUE),
    ('b6866a97-cf3c-5ae1-89b7-0a42d7ed01c1', 'price_usd', 'Price (USD)', 'Manufacturer or authorized-dealer retail price in US dollars. Reflects positioning within the market and value relative to measured performance.', 'USD', 'number'::spec_value_type, NULL, ARRAY['amp','dac','headphones','iems'], TRUE),
    ('4a53668d-da35-5499-9de2-6c12b44d8af1', 'recommended_load_ohm', 'Recommended Load', 'Manufacturer-recommended resistive load or range (e.g. "100-500"). Checked against the phono stage load options.', 'ohm', 'string'::spec_value_type, NULL, ARRAY['cartridge'], TRUE),
    ('e66045dd-a1cc-55a5-8bcc-f3426a00eb02', 'recommended_power_w', 'Recommended Amplifier Power', 'Manufacturer-recommended amplifier power (upper figure when a range is given). Advisory tier — the SPL math is the real check.', 'W', 'number'::spec_value_type, NULL, ARRAY['speakers'], TRUE),
    ('5d660801-7c23-5995-946c-eef72b8eafc1', 'rfi_damping_db', 'RFI Damping', 'The maximum attenuation, in decibels, the internal filter applies to radio-frequency interference riding on the mains line. Higher values indicate stronger suppression of noise that can otherwise couple into sensitive analog and digital audio circuits.', 'dB', 'number'::spec_value_type, NULL, ARRAY['power'], TRUE),
    ('eacc113c-ee6f-54f5-a4fe-cbeae024d2e9', 'riaa_accuracy_db', 'RIAA Accuracy', 'Maximum deviation from the ideal RIAA equalization curve across the audio band (commonly 20Hz-20kHz). Tighter tolerance means more accurate tonal balance reproduction of vinyl recordings.', 'dB', 'number'::spec_value_type, NULL, ARRAY['phono_stage'], TRUE),
    ('ce589253-eb2a-50a1-9350-e58857ef9989', 'ribbon_surface_area_cm2', 'Ribbon Surface Area (cm²)', 'The total radiating surface area of all ribbon diaphragm segments combined, measured in square centimeters. Larger ribbon surface area increases achievable SPL and low-frequency output for ribbon transducers, playing a role analogous to diaphragm area in dynamic or planar drivers.', 'cm2', 'number'::spec_value_type, NULL, ARRAY['headphones'], TRUE),
    ('41b1778e-891d-5e1a-95e8-71a1a82447e1', 'sensitivity_db_100v', 'Sensitivity (dB/100Vrms)', 'Electrostatic sensitivity: SPL produced at 100 Vrms input. Electrostats live in a hundreds-of-volts domain — never comparable with dB/mW figures of dynamic/planar headphones.', 'dB SPL', 'number'::spec_value_type, NULL, ARRAY['headphones'], TRUE),
    ('7149a115-569b-52c4-a91d-9d217a2e6dc0', 'sensitivity_db_mw', 'Sensitivity (dB SPL/mW)', 'Sound pressure level produced per milliwatt of input power at 1 kHz. Higher values mean the headphone reaches a given loudness with less amplifier power, affecting how easy it is to drive from portable sources.', 'dB SPL/mW', 'number'::spec_value_type, NULL, ARRAY['headphones'], TRUE),
    ('08d47779-32b4-5889-974d-ddeae65faafa', 'sensitivity_db_v', 'Sensitivity (dB SPL/V)', 'Sound pressure level produced per 1 volt RMS of input at 1 kHz. High-impedance dynamic-driver headphones (notably Sennheiser''s HD6XX/HD8XX lines) are rated this way rather than in dB/mW because voltage sensitivity is independent of impedance; it should not be compared directly to dB/mW figures without converting through the headphone''s impedance.', 'dB SPL', 'number'::spec_value_type, NULL, ARRAY['headphones','iems'], TRUE),
    ('b1b74abb-3512-5f64-810a-9b3ad5dcd1a0', 'shielding_type', 'Shielding Type', 'The construction used to block electromagnetic and radio-frequency interference from reaching the signal conductors. Foil shielding gives full coverage against high-frequency noise, while braid adds mechanical durability and low-frequency rejection.', NULL, 'enum'::spec_value_type, ARRAY['foil','braid','foil_and_braid','none'], ARRAY['cable'], TRUE),
    ('9ec8a827-701c-5686-8b11-2d0dbb1f4958', 'signal_to_noise_db', 'Signal-to-Noise Ratio', 'Ratio between the maximum output signal level and the amplifier''s residual noise floor. Higher values indicate a blacker background, which is critical for resolving low-level detail with sensitive IEMs.', 'dB', 'number'::spec_value_type, NULL, ARRAY['amp','dac'], TRUE),
    ('24678976-28f3-5cdc-8540-d8f68813cae1', 'sinad_db', 'SINAD', 'Signal-to-noise-and-distortion ratio in dB, a composite figure widely used to rank DAC measured fidelity. Higher is generally better, though R2R designs are sometimes preferred despite lower SINAD than delta-sigma competitors.', 'dB', 'number'::spec_value_type, NULL, ARRAY['dac'], TRUE),
    ('6c6a51d8-155f-5a30-9a1c-0982bc297162', 'socket_count', 'Socket Count', 'Total number of AC output receptacles on the mains block. Determines how many downstream components (amp, DAC, source) can be connected to a single unit without daisy-chaining.', NULL, 'number'::spec_value_type, NULL, ARRAY['power'], TRUE),
    ('a4de84df-33a3-5e1d-8725-e403412f4589', 'speaker_sensitivity_db_2v83', 'Sensitivity (dB/2.83V/1m)', 'SPL at 1 m for 2.83 Vrms input (1 W into 8 ohm). The speaker-domain analog of headphone sensitivity; with amplifier power and listening distance it bounds achievable SPL.', 'dB SPL', 'number'::spec_value_type, NULL, ARRAY['speakers'], TRUE),
    ('68a426f6-8eee-57db-b1ad-213518818105', 'speed_options', 'Supported Speeds', 'Rotational speeds (in RPM) the turntable can play back, typically switched electronically or by manually moving a belt. Determines compatibility with standard LP (33 1/3), single (45) and vintage (78) records.', 'rpm', 'string'::spec_value_type, NULL, ARRAY['turntable'], TRUE),
    ('ea7d80d7-ca5c-5804-bda9-6cc0c78c1765', 'stable_to_ohm', 'Stable Into (min load)', 'Minimum load impedance the amplifier is specified stable into. Compared against the SPEAKER MINIMUM impedance, not nominal.', 'ohm', 'number'::spec_value_type, NULL, ARRAY['power_amp','integrated_amp'], TRUE),
    ('0e206ee8-dfdd-5bed-beee-ebc3a1711ba6', 'stylus_profile', 'Stylus Tip Profile', 'Geometric profile of the diamond stylus tip (e.g. conical, elliptical, line-contact, microridge, Shibata). Determines groove contact area and high-frequency tracing ability, and influences record/stylus wear rates.', NULL, 'enum'::spec_value_type, ARRAY['conical','elliptical','line_contact','microridge','shibata','fine_line','other'], ARRAY['cartridge'], TRUE),
    ('ccabca25-e3f6-58f4-81fe-458fd1b395cd', 'thd_n_db', 'THD+N Level', 'Total harmonic distortion plus noise expressed in dB relative to the fundamental at a stated output level and load. Lower (more negative) values indicate a cleaner, more linear amplification stage with fewer added harmonics.', 'dB', 'number'::spec_value_type, NULL, ARRAY['amp'], TRUE),
    ('4897b47a-535d-5014-90b6-137cc210899e', 'total_harmonic_distortion_pct', 'THD+N', 'Total harmonic distortion plus noise as a percentage, measured at 1kHz full-scale under stated conditions. Lower values indicate a cleaner, less colored analog output.', '%', 'number'::spec_value_type, NULL, ARRAY['amp','dac'], TRUE),
    ('9837b612-8b20-5ec3-9646-4fe94a6a0139', 'total_weight_kg', 'Total Weight', 'Overall mass of the assembled turntable (plinth, platter, motor pod and feet), excluding tonearm and cartridge. Heavier designs typically resist external vibration and footfall better.', 'kg', 'number'::spec_value_type, NULL, ARRAY['turntable'], TRUE),
    ('5ec7d6fe-6e26-53a4-a83e-b93e860df6a0', 'tracking_force_g', 'Vertical Tracking Force', 'Recommended downforce applied to the stylus by the tonearm, in grams. Affects tracking ability, groove/stylus wear, and how the New-Angle-type damper alignment (where applicable) settles into optimal coil geometry.', 'g', 'number'::spec_value_type, NULL, ARRAY['cartridge'], TRUE),
    ('aa8b9b0d-98f1-5144-ba4c-a666c2c7179d', 'tube_complement', 'Valve/Tube Complement', 'The specific vacuum tube types and quantities used in the preamp/driver and output stages. Determines available tube-rolling options and contributes strongly to the amplifier''s tonal character.', NULL, 'string'::spec_value_type, NULL, ARRAY['integrated_amp','amp'], TRUE),
    ('d573a812-5bed-51e3-8a91-e80a4818eb60', 'usb_dac_mode', 'USB DAC Mode', 'Whether the device can act as a USB DAC for an upstream computer or phone. Multifunction players expose this as a separate role in pair analysis.', NULL, 'boolean'::spec_value_type, NULL, ARRAY['player'], TRUE),
    ('b86b3f19-11ce-541f-9d17-9f5fb841f0fc', 'volume_steps', 'Volume Control Steps', 'Number of discrete gain steps provided by the volume control mechanism (e.g. relay-switched resistor ladder). More steps allow finer-grained level matching, especially valuable with highly sensitive IEMs.', 'steps', 'number'::spec_value_type, NULL, ARRAY['amp'], TRUE),
    ('e697b0cd-a94c-5a43-8eef-ef1d001e1747', 'weight_g', 'Weight', 'Device weight in grams. Stored in grams for all categories; render layer may convert to kg for electronics.', 'g', 'number'::spec_value_type, NULL, ARRAY['headphones','iems','dac','amp','player','power'], TRUE),
    ('2591a25b-9f50-540d-a89a-4809ef2356aa', 'wire_material', 'Internal Wire Material', 'The conductor metal/purity used for internal wiring, such as oxygen-free copper. Affects conductivity and long-term oxidation resistance, which some audiophiles associate with sonic transparency.', NULL, 'enum'::spec_value_type, ARRAY['oxygen_free_copper','silver_plated_copper','standard_copper','silver'], ARRAY['cable','power'], TRUE),
    ('1c0460cb-1565-51c9-b31a-5aa9fc776dfc', 'wow_and_flutter_pct', 'Wow and Flutter', 'Short-term speed variation of the platter expressed as a percentage, measured with a test record and analyzer. Lower values indicate steadier pitch with less audible pitch wavering (wow) or fine flutter on sustained notes.', '%', 'number'::spec_value_type, NULL, ARRAY['turntable'], TRUE)
ON CONFLICT (key) DO NOTHING;

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

-- Measurement-sourced behavioral caveats the spec sheet hides:
-- "clips into low-Z loads at high gain", "driver bottoms out at
-- 114 dB". Distinct from sentiment terms (community opinion) --
-- these come from measurement-tier sources only and gate pair
-- verdicts in the deterministic engine. role: hp_out | line_out |
-- transducer | NULL (model-wide).
CREATE TABLE IF NOT EXISTS gear_measured_caveats (
    id             UUID PRIMARY KEY,
    gear_model_id  UUID NOT NULL REFERENCES gear_models(id) ON DELETE CASCADE,
    role           VARCHAR(20),
    severity       gear_caveat_severity NOT NULL DEFAULT 'warn',
    -- optional machine-readable condition: caveat applies only when
    -- the partner load impedance is below this (ohms)
    load_z_below   REAL,
    -- optional condition: applies only when the pairing needs more
    -- than this many Vrms (i.e. high-gain territory)
    only_above_vrms REAL,
    text           TEXT NOT NULL,
    source_url     TEXT,
    created_at     TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (gear_model_id, text)
);
CREATE INDEX IF NOT EXISTS idx_gear_caveats_model ON gear_measured_caveats(gear_model_id);

-- Community pair-synergy notes: how a specific combination SOUNDS
-- per aggregated owner experience. Third knowledge type after model
-- facts and pair physics; researched strictly on demand (combinatorics
-- forbid "research everything"), cached forever, P2P-mergeable via
-- deterministic id. model_a/model_b are canonically ordered (a < b).
CREATE TABLE IF NOT EXISTS gear_pair_notes (
    id              UUID PRIMARY KEY,
    model_a         UUID NOT NULL REFERENCES gear_models(id) ON DELETE CASCADE,
    model_b         UUID NOT NULL REFERENCES gear_models(id) ON DELETE CASCADE,
    research_state  gear_research_state NOT NULL DEFAULT 'queued',
    summary         TEXT,
    terms           TEXT[],
    sample_size     INTEGER,
    source_urls     TEXT[],
    researched_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (model_a, model_b),
    CHECK (model_a < model_b)
);
CREATE INDEX IF NOT EXISTS idx_gear_pair_notes_state
    ON gear_pair_notes(research_state) WHERE research_state IN ('queued','researching');

-- Measurement-registry entries: models known from public FR databases
-- (AutoEq mirrors of oratory1990/...), imported WITHOUT minting
-- gear_models rows -- the research worker must never see thousands of
-- queued models. An entry links to the catalog when the same model
-- exists there (normalized-name match) or when the user promotes it
-- to 'want'. source encodes the measurer (= the rig): band deltas are
-- comparable ONLY within one source.
CREATE TABLE IF NOT EXISTS gear_registry_entries (
    id               UUID PRIMARY KEY,
    source           VARCHAR(60) NOT NULL,
    category         gear_category NOT NULL,
    model_name       TEXT NOT NULL,
    gear_model_id    UUID REFERENCES gear_models(id) ON DELETE SET NULL,
    dev_sub_bass_db  REAL,
    dev_bass_db      REAL,
    dev_mids_db      REAL,
    dev_presence_db  REAL,
    dev_treble_db    REAL,
    -- loudspeaker aggregates (spinorama sources): CEA-2034 Olive
    -- preference score, score with ideal sub, LFX bass extension
    pref_score       REAL,
    pref_score_wsub  REAL,
    lfx_hz           REAL,
    sens_db          REAL,
    price_usd        REAL,
    shape            VARCHAR(30),
    quality          VARCHAR(12),
    active_speaker   BOOLEAN,
    imported_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source, model_name)
);
CREATE INDEX IF NOT EXISTS idx_gear_registry_model ON gear_registry_entries(gear_model_id);
CREATE INDEX IF NOT EXISTS idx_gear_registry_cat ON gear_registry_entries(category);

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
    source_addr    UUID,                       -- uuid5(NS, 'node_addr:{host}') — pseudonymized, ban correlation
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Local ban list for P2P slice sources. Ban anchor is the PUBKEY (proven by
-- receipts); addr is the secondary net: it catches a banned key returning
-- under a fresh identity from the same address. The address id is
-- uuid5(NS, 'node_addr:{host}') — deterministic on every node (cross-node
-- evidence correlation), enumerable back to an IPv4 by design
-- (pseudonymization for uniform storage, not privacy). Bans are LOCAL
-- (no global blacklist — see P2P-SYNC-INTEGRITY); rows are inserted manually
-- or by the future spot-check/flag flow.
CREATE TABLE IF NOT EXISTS p2p_node_bans (
    id          SERIAL PRIMARY KEY,
    pubkey      TEXT,
    addr        UUID,
    reason      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (pubkey IS NOT NULL OR addr IS NOT NULL)
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

-- Persistent rate-limit cooldown for external APIs (Last.fm error 29,
-- Deezer 429/quota). One row per source; the backend arms a row with
-- exponential backoff (30m→24h) on a rate-limit and honors cooldown_until
-- before every call, so a restart mid-cooldown doesn't resume hammering a
-- still-banning source. See backend/api_cooldown.py.
CREATE TABLE IF NOT EXISTS external_api_cooldown (
    source          TEXT PRIMARY KEY,
    cooldown_until  TIMESTAMP WITH TIME ZONE NOT NULL,
    reason          TEXT,
    strikes         INTEGER NOT NULL DEFAULT 1,
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

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
                    JOIN media_files mf ON mf.album_variant_id = av.id
                    WHERE av.album_id = al.id)) as total_albums,
    (SELECT COUNT(*) FROM tracks t
      WHERE EXISTS (SELECT 1 FROM media_files mf
                    WHERE mf.track_id = t.id)) as total_tracks,
    (SELECT COUNT(*) FROM media_files) as total_media_files,
    -- The coverage counters carry the SAME physical-presence rule as the
    -- totals they are read against. Counting whole tables instead made the
    -- numerator describe a different library from the denominator: phantoms
    -- carry embeddings, and a synced node holds analysis for tracks it has no
    -- file for, so "977 / 442 (221%)" was an honest report of two unrelated
    -- numbers. Per-track counts, not row counts — a second embedding model
    -- would otherwise double the figure on its own.
    (SELECT COUNT(*) FROM tracks t
      WHERE EXISTS (SELECT 1 FROM media_files mf WHERE mf.track_id = t.id)
        AND EXISTS (SELECT 1 FROM embeddings e WHERE e.track_id = t.id)
    ) as tracks_with_embeddings,
    (SELECT COUNT(*) FROM tracks t
      WHERE EXISTS (SELECT 1 FROM media_files mf WHERE mf.track_id = t.id)
        AND EXISTS (SELECT 1 FROM track_lyrics tl WHERE tl.track_id = t.id)
    ) as tracks_with_lyrics,
    (SELECT SUM(duration_seconds) FROM media_files) as total_duration_seconds,
    (SELECT SUM(file_size_bytes) FROM media_files) as total_file_size_bytes,
    -- Genres present in the owned library, not every genre name the node has
    -- ever heard of — a synced node knows thousands it owns no track in.
    (SELECT COUNT(DISTINCT ag.genre_id) FROM album_genres ag
      WHERE EXISTS (SELECT 1 FROM album_variants av
                    JOIN media_files mf ON mf.album_variant_id = av.id
                    WHERE av.album_id = ag.album_id)) as unique_genres;
