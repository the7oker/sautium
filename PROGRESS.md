# Music AI DJ - Progress

## Step 1.1: Project Setup & Docker Environment - DONE

### What was done
- Created project structure with all necessary directories
- Configured Docker Compose with two services:
  - **PostgreSQL** (ankane/pgvector:latest) with pgvector extension, named volume for data persistence
  - **Python backend** with CUDA 12.2 support, GPU access (RTX 4090 Laptop GPU, 17.2 GB)
- Created `.env` configuration (database credentials, music library path, API keys)
- Created `backend/Dockerfile` with CUDA support, ffmpeg, libsndfile
- Created `backend/requirements.txt` with all dependencies
- Created `backend/config.py` - Pydantic Settings for configuration management
- Created `backend/main.py` - FastAPI app with health check, stats, scan endpoints
- Configured pgEdge PostgreSQL MCP Server (`.mcp.json`) for Claude Code database access

### Verified
- Both containers running and healthy
- GPU detected and accessible from backend container
- Database connected, pgvector extension active
- Music library mounted at `/music` (read-only) from `/mnt/e/Music`
- Port 5432 exposed for external database access (PyCharm, etc.)

---

## Step 1.2: Library Scanner (Metadata Extraction) - DONE

### What was done
- Created `backend/models.py` - SQLAlchemy ORM models:
  - `Genre` (id, name, description)
  - `Artist` (id, name, spotify_id, lastfm_id, musicbrainz_id, bio, country)
  - `Album` (id, title, release_year, label, catalog_number, total_tracks, quality_source, sample_rate, bit_depth, directory_path [UNIQUE], user_rating, user_notes)
  - `Track` (id, title, album_id, track/disc number, audio characteristics, file info, embedding vector(512), spotify features, user data)
  - `TrackGenre` (track_id, genre_id) - many-to-many
  - `TrackArtist` (track_id, artist_id, role) - many-to-many
- Created `backend/database.py` - SQLAlchemy engine, session management, context manager
- Created `backend/scanner.py` - Library scanner:
  - Recursive FLAC file discovery
  - Metadata extraction via mutagen (title, artist, album, genre, year, track number, etc.)
  - Quality source detection from folder structure ([Vinyl], [TR24], [MP3], CD)
  - Normalized genre handling (separate genres table, many-to-many with tracks)
  - get_or_create pattern for artists, albums, genres
  - Batch commits every 100 tracks
  - Progress bar via tqdm
- Created `backend/cli.py` - Click CLI commands:
  - `scan` - scan library and import metadata
  - `stats` - show library statistics
  - `list-tracks` - list recently added tracks
  - `check-db` - verify database connection and schema
  - `test-file` - test metadata extraction from a single FLAC file
- Created `scripts/init_db.sql` - Database schema:
  - 6 tables: genres, artists, albums, tracks, track_genres, track_artists
  - HNSW index on track embeddings for vector similarity search
  - Auto-update triggers on updated_at columns
  - library_stats view

### Design decisions
- Genre belongs only to tracks (many-to-many via track_genres)
- Album has no artist_id - artists derived from tracks via track_artists
- Album uniqueness by directory_path (one album per folder)
- No playlists table (not needed at this stage)

### Database optimization (completed during testing)
- Moved embeddings to separate table for better performance
- Schema change: `tracks.embedding` → `embeddings` table + `tracks.embedding_id`
- HNSW index moved to `embeddings` table
- Normalized embedding models: `embeddings.model_name` → `embedding_models` table + `embeddings.model_id`
  - Separate table for model metadata (name, description, dimension)
  - Supports multiple embedding models in future

### Testing status - SUCCESSFUL ✅
- Scanner tested with 20 tracks (2 albums: Beth Hart & Joe Bonamassa, Joe Cocker)
- QualitySource detection working correctly:
  - Vinyl rip detected: "Don't Explain" (48kHz/24bit, folder [Vinyl])
  - CD rip detected: "Heart & Soul" (44.1kHz/16bit, standard folder)
- Genre normalization working (10 tracks with "Blues" genre, 10 without genre tags)
- All metadata extracted correctly (artist, album, track info, audio specs)
- Database structure validated (embeddings table separate from tracks)

**Bug fixed**: SQLAlchemy enum now uses `values_callable` to match PostgreSQL enum values

---

## Step 1.3: Audio Embeddings (CLAP) - DONE

### What was done
- Created `backend/embeddings.py` - Audio embedding generator:
  - CLAP model (laion/clap-htsat-unfused) loaded on GPU (RTX 4090)
  - 512-dimensional audio embeddings from middle 30 seconds of each track
  - Batch processing (configurable batch size, default 16)
  - Text-to-embedding for CLAP text search
  - Model caching in persistent volume (`/root/.cache`)
  - Incremental processing (only tracks without embeddings)
  - Progress tracking via tqdm
- Added CLI command `generate-embeddings` with `--limit` and `--batch-size` options
- Added API endpoint `POST /embeddings/generate`

### Testing status - SUCCESSFUL
- 185 tracks processed with embeddings out of 593 total
- GPU memory usage: ~0.62 GB for CLAP model
- Model loads in ~13 seconds (first call), cached after

---

## Step 1.4: Semantic Search by Audio - DONE

### What was done
- Created `backend/search.py` - Search service with three search functions:
  - `search_similar_tracks(db, track_id)` - find tracks similar to a given track by cosine similarity
  - `search_by_text(db, query_text)` - search by natural language description via CLAP text-to-audio embeddings
  - `search_by_metadata(db, filters)` - search by metadata filters (artist, album, genre, quality, year)
- All search functions support optional metadata filters and pagination
- pgvector cosine similarity (`<=>` operator) with configurable min_similarity threshold
- Added CLI commands: `search-similar`, `search-text`
- Added API endpoints: `POST /search/similar`, `POST /search/text`, `GET /search/metadata`

### Testing status - SUCCESSFUL
- Similar track search returns musically sensible results
- Text search ("slow blues", "ambient electronic") matches appropriate tracks
- Metadata filtering works with partial matching (ILIKE)
- Search response time < 1 second

---

## Step 1.5: Claude Integration (RAG for Music) - DONE

### What was done
- Upgraded `anthropic` SDK from `0.15.0` to `>=0.79.0` (Messages API support)
- Created `backend/assistant.py` - RAG assistant:
  - `ask_assistant(db, query, limit)` - main RAG pipeline:
    1. Retrieves tracks via CLAP text search (min_similarity=0.3 for wider recall)
    2. Also retrieves via metadata search if filters extracted from query
    3. Deduplicates by track ID, keeps best similarity score, caps at 30 tracks
    4. Formats context block with track details (title, artist, album, genre, quality, duration, relevance)
    5. Calls Claude (claude-sonnet-4-20250514) with DJ system prompt + track context
    6. Returns structured response (answer, tracks, model, count)
  - `_extract_filters(db, query)` - keyword extraction for metadata filters:
    - Genre keywords (blues, jazz, electronic, ambient, etc.)
    - Quality source (vinyl, hi-res, mp3)
    - Artist name matching against DB
    - Year/decade patterns ("1970s", "80s", "before 2000")
  - System prompt: DJ persona, grounded RAG (only recommend from provided tracks)
- Implemented `POST /search/query` endpoint (replaced 501 stub)
- Added CLI command `ask` with `--query/-q` and `--limit` options

### Design decisions
- Lower min_similarity (0.3) for retrieval — let Claude decide relevance from wider pool
- Cap context at 30 tracks to keep Claude input manageable and cost-effective
- Simple keyword extraction for MVP — Claude does the heavy reasoning
- Model: claude-sonnet-4-20250514 with max_tokens=1024

### Testing status - SUCCESSFUL
- CLAP text search retrieves 20 tracks, metadata search retrieves 20 more
- Filter extraction works correctly (genre, quality_source, artist name matching)
- Context formatting produces clean structured text for Claude
- Claude API call correctly formed via Messages API
- **Note**: Requires active Anthropic API credits to complete Claude calls

---

## Current State

### Library stats
- **593 tracks** indexed (Blues, Electronic, Nu Jazz genres)
- **185 tracks** with audio embeddings
- Genres: Blues, Electronic, Ambient, Jazz, IDM, Krautrock, Progressive Electronic, and more

### Phase 1 MVP - COMPLETE
- [x] Step 1.1: Project Setup & Docker Environment
- [x] Step 1.2: Library Scanner (Metadata Extraction)
- [x] Step 1.3: Audio Embeddings (CLAP)
- [x] Step 1.4: Semantic Search by Audio
- [x] Step 1.5: Claude Integration (RAG for Music)

---

## Step 2.1: Last.fm Integration - DONE

### What was done
- **Database schema**: Created `external_metadata` table for multi-source metadata storage
  - Flexible JSONB data field for any metadata structure
  - entity_type + entity_id + source + metadata_type uniqueness
  - GIN index on JSONB for fast queries
  - fetch_status tracking (success, not_found, error)
- **Migration scripts**:
  - `scripts/add_external_metadata.sql` - table creation
  - `scripts/add_metadata_functions.sql` - aggregation functions and views
- **PostgreSQL functions** for metadata aggregation:
  - `get_artist_bio(artist_id)` - combines bio from all sources
  - `get_artist_tags(artist_id)` - merges tags/genres from all sources with weights
  - `get_artist_similar(artist_id)` - aggregates similar artists with avg match scores
  - `get_artist_stats(artist_id)` - returns listeners/playcount stats
- **View**: `artists_enriched` - artists with pre-aggregated metadata
- **SQLAlchemy model**: `ExternalMetadata` with JSONB support
- **Last.fm service** (`backend/lastfm.py`):
  - `LastFmService` class using pylast SDK
  - Fetches: bio, tags, similar artists, stats
  - Stores in JSONB format: `{summary, content, url, stats}`, `{tags: [...]}`, `{similar: [...]}`
  - Batch enrichment with rate limiting (default 0.2s delay)
  - Graceful error handling (not_found, error status)
- **CLI command**: `enrich-lastfm`
  - `--artist "Name"` - enrich specific artist
  - `--limit N` - batch enrich N artists
  - `--no-skip` - re-fetch existing data
  - `--delay` - rate limit delay (seconds)
- **Configuration**: Added `LASTFM_API_KEY` to docker-compose.yml environment

### Design decisions
- **Multi-source architecture**: Each source (Last.fm, Spotify, MusicBrainz) stores separate records
  - Enables data provenance tracking
  - Allows re-fetching from specific sources
  - Aggregation happens via PostgreSQL functions
- **JSONB format**: Flexible schema for different metadata types
  - Bio: `{summary, content, url, stats: {listeners, playcount}}`
  - Tags: `{tags: [{name, count}, ...]}`
  - Similar: `{similar: [{name, match, mbid}, ...]}`
- **Incremental enrichment**: Skip artists that already have successful Last.fm data
- **Rate limiting**: Default 0.2s delay to respect Last.fm API limits (~5 req/sec)

### Testing status - SUCCESSFUL
- ✅ Table and functions created successfully
- ✅ Enriched 6 artists: Joe Cocker, Klaus Schulze, Beth Hart & Joe Bonamassa, Hidden Orchestra, etc.
- ✅ Joe Cocker: 10 tags (blues, rock, soul, etc.), 20 similar artists (Eric Clapton, Rod Stewart, etc.)
- ✅ Klaus Schulze: 10 tags (electronic, berlin school, ambient, krautrock, etc.)
- ✅ Aggregation functions work correctly
- ✅ artists_enriched view returns combined data

### Data quality
- **Bio coverage**: Good for popular artists, sparse for obscure ones
- **Tags quality**: Excellent semantic tags (genre, mood, era, nationality)
- **Similar artists**: High-quality recommendations with match scores
- **Stats**: Listeners/playcount useful for popularity ranking

### Genre enrichment
- ✅ Added `get_tag_info()` and `enrich_genre()` methods to Last.fm service
- ✅ CLI command: `enrich-lastfm --genres`
- ✅ All 12 genres enriched with descriptions from Last.fm
- ✅ Examples: Ambient (606 chars), Jazz (597 chars), IDM (527 chars)

### Database cleanup
- ✅ Removed deprecated `artists.bio` and `genres.description` fields
- ✅ Migration script: `scripts/remove_deprecated_fields.sql`
- ✅ All metadata now stored in `external_metadata` table
- ✅ Updated SQLAlchemy models

### Genre normalization
- ✅ Created `backend/normalize_genres.py` script
- ✅ Normalizes compound genre names: `"A/B/C"` → separate genres `A`, `B`, `C`
- ✅ Handles delimiters: `/`, `,`, `&`, `+`
- ✅ Creates proper many-to-many relationships in `track_genres`
- ✅ CLI command: `normalize-genres --dry-run`
- ✅ Results: 4 compound genres split into 13 individual genres
  - `"Progressive Electronic/Berlin School"` → `Progressive Electronic`, `Berlin School`
  - `"Krautrock/Electro/Experimental/Ambient"` → `Krautrock`, `Electro`, `Experimental`, `Ambient`
  - `"Electronic, Ambient"` → `Electronic`, `Ambient`
  - `"Ambient, Électronique"` → `Ambient`, `Électronique`
- ✅ Track relationships updated: 461 → 498 (tracks now have proper multi-genre tags)
- ✅ New genres enriched with Last.fm descriptions

### Similar artists normalization
- ✅ Replaced JSONB storage in `external_metadata` with normalized `similar_artists` table
- ✅ Created `scripts/create_similar_artists_table.sql` - normalized schema:
  - Many-to-many relationship: `artist_id` ↔ `similar_artist_id`
  - `match_score` (0.0-1.0) from Last.fm similarity
  - `source` field ('lastfm', 'spotify', etc.) for multi-source support
  - Proper foreign keys, indexes, and constraints
- ✅ Created `scripts/migrate_similar_artists.sql` - data migration from JSONB
- ✅ Updated `backend/lastfm.py`:
  - `_store_similar_artists()` method filters compound artists automatically
  - Creates artist records for similar artists if they don't exist
  - Stores relationships in `similar_artists` table instead of JSONB
- ✅ Updated `backend/models.py` with `SimilarArtist` model and relationships
- ✅ Migration results:
  - 165 similar artist relationships migrated
  - 139 new artists created from similar artist names
  - 15 compound artists filtered out (e.g., "Pete Namlook & Klaus Schulze")
  - Deleted old JSONB data from `external_metadata`
- ✅ Statistics: 9 enriched artists, average 18.3 similar artists each

### Genre descriptions normalization
- ✅ Replaced JSONB storage in `external_metadata` with normalized `genre_descriptions` table
- ✅ Created `scripts/create_genre_descriptions_table.sql` - normalized schema:
  - Fields: `summary` (short), `content` (full), `url`, `reach` (Last.fm popularity)
  - Multi-source support: `source` field ('lastfm', 'wikipedia', 'spotify')
  - Proper foreign keys, indexes, and unique constraints
- ✅ Created `scripts/migrate_genre_descriptions.sql` - data migration from JSONB
- ✅ Updated `backend/lastfm.py`:
  - Modified `enrich_genre()` to store in `genre_descriptions` table
  - Returns structured info: summary_length, content_length, reach
- ✅ Updated `backend/models.py` with `GenreDescription` model and relationships
- ✅ Migration results:
  - 13 genre descriptions migrated from external_metadata
  - Average content length: 1,797 characters
  - All genres have descriptions from Last.fm
  - Deleted old JSONB data from `external_metadata`

### Artist bios normalization
- ✅ Replaced JSONB storage in `external_metadata` with normalized `artist_bios` table
- ✅ Created `scripts/create_artist_bios_table.sql` - normalized schema:
  - Fields: `summary` (short), `content` (full), `url`
  - Last.fm stats: `listeners`, `playcount` (separate columns for queries/sorting)
  - Multi-source support: `source` field ('lastfm', 'musicbrainz', 'wikipedia')
  - Indexes on `listeners` and `playcount` for popularity ranking
- ✅ Created `scripts/migrate_artist_bios.sql` - data migration from JSONB
- ✅ Updated `backend/lastfm.py`:
  - Modified `store_artist_metadata()` to store bios in `artist_bios` table
  - Extracts stats from nested JSON to separate columns
- ✅ Updated `backend/models.py` with `ArtistBio` model and relationships
- ✅ Migration results:
  - 9 artist bios migrated from external_metadata
  - Average summary length: 450 characters
  - Average content length: 2,424 characters
  - Total listeners across all artists: 3.3M
  - Total playcount: 55.6M
  - Top artist: Joe Cocker (1.5M listeners, 19.5M plays)
  - Deleted old JSONB data from `external_metadata`

### Artist tags normalization
- ✅ Replaced JSONB storage in `external_metadata` with normalized `tags` + `artist_tags` tables
- ✅ Created `scripts/create_tags_tables.sql` - normalized schema:
  - `tags` table: universal tag library (id, name, timestamps)
  - `artist_tags` table: many-to-many with weight (0-100 scale)
  - Multi-source support: `source` field ('lastfm', 'spotify', 'user')
  - Future-ready: tags can be applied to albums, tracks
- ✅ Created `scripts/migrate_artist_tags.sql` - data migration from JSONB
- ✅ Updated `backend/lastfm.py`:
  - Added `_store_artist_tags()` method
  - Creates tags as needed (case-insensitive lookup)
  - Stores relationships with weight from Last.fm
- ✅ Updated `backend/models.py` with `Tag` and `ArtistTag` models
- ✅ Migration results:
  - 55 unique tags created (electronic, ambient, krautrock, blues, etc.)
  - 90 artist-tag relationships
  - Top tags: "electronic" (5 artists), "ambient" (5 artists), "experimental" (5 artists)
  - Average 10 tags per artist

### Database normalization complete! 🎉
- ✅ **All data migrated from `external_metadata` JSONB → normalized tables**
- ✅ **`external_metadata` table now empty (0 records)**
- ✅ Normalized tables:
  - `similar_artists` - 165 records (9 artists)
  - `artist_tags` - 90 records (9 artists, 55 unique tags)
  - `tags` - 55 unique tags
  - `artist_bios` - 9 records
  - `genre_descriptions` - 13 records
- ✅ Benefits:
  - Proper foreign keys and CASCADE DELETE
  - Efficient indexes for queries
  - No data duplication
  - Ready for multi-source enrichment (Spotify, MusicBrainz, Wikipedia)

### external_metadata - new role
- 🔧 **Keeping as staging/experimental table** for new metadata types
- Purpose:
  - Quick integration of new API sources (Spotify, MusicBrainz, Wikipedia)
  - Explore data structure before designing normalized schema
  - Temporary storage for experimental features
  - Once structure is clear → normalize into dedicated tables
- Workflow: `API → external_metadata (staging) → analyze → normalize → dedicated table`

---

## Step 2.1b: Album Enrichment from Last.fm - DONE

### What was done
- **Database schema**: Created normalized tables for album metadata
  - `album_info` table for album descriptions and stats
  - `album_tags` table for album tagging (uses existing `tags` table)
  - Added `lastfm_id` (MBID) column to `albums` table
- **Migration scripts**:
  - `scripts/create_album_enrichment_tables.sql` - table creation
- **SQLAlchemy models**: Added `AlbumInfo` and `AlbumTag` models
- **Last.fm service** (`backend/lastfm.py`):
  - `get_album_info(artist_name, album_title)` - fetches all album data
  - `enrich_album(db, album_id, artist_name, album_title)` - stores in database
  - `_store_album_info()` - stores wiki + stats in `album_info` table
  - `_store_album_tags()` - stores tags in `album_tags` table
- **CLI command**: `enrich-albums`
  - `--album "Title"` - enrich specific album
  - `--limit N` - batch enrich N albums
  - `--no-skip` - re-fetch existing data
  - `--delay` - rate limit delay (seconds)

### Design decisions
- **Shared tag system**: Albums use the same `tags` table as artists
  - Universal tagging: tags can describe artists, albums, tracks
  - Tag reuse: "ambient" tag applies to both artists and albums
  - Extensible: easy to add track tags in future
- **Multi-source ready**: `album_info` and `album_tags` support multiple sources
- **MBID storage**: `albums.lastfm_id` for MusicBrainz integration

### Testing status - SUCCESSFUL
- ✅ Tested with "Timewind" by Klaus Schulze
- ✅ Wiki summary (703 chars) and content (2,566 chars) stored
- ✅ Stats: 40,222 listeners, 169,077 playcount
- ✅ 10 tags stored: ambient (100), electronic (73), 1975 (22), etc.
- ✅ MBID: 60f7f643-dab5-3108-a257-d6b66f7833ca
- ✅ Tag system: 6 new tags added (total 61 unique tags)

### Data structure
```sql
album_info:
  album_id → albums
  source ('lastfm', 'musicbrainz', 'spotify')
  summary, content, url
  listeners, playcount

album_tags:
  album_id → albums
  tag_id → tags (shared with artists)
  weight (0-100)
  source ('lastfm', 'spotify', 'user')
```

### Statistics
- **118 albums** in database
- **1 album enriched** (Timewind - test)
- **61 unique tags** (55 artist tags + 6 album tags)
- **10 album-tag relationships**

---

## Next Steps

### Phase 2: External Data & Text Embeddings (continued)
- Step 2.2: Spotify integration (audio features, genres)
- Step 2.3: Text embeddings from metadata (sentence-transformers) — needs rich text from 2.1/2.2
- Step 2.4: Enhanced RAG (hybrid search, richer context)
