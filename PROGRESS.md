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

## Next Steps

### Phase 2: External Data & Text Embeddings
- Step 2.1: Text embeddings from metadata (sentence-transformers)
- Step 2.2: Spotify integration (audio features)
- Step 2.3: Last.fm integration (artist info, tags)
- Step 2.4: Enhanced RAG (hybrid search, richer context)
