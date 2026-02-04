# Music AI DJ Project

## Project Overview

An AI-powered music library management system for a personal FLAC collection (~30,000 tracks). The system will analyze audio content, provide intelligent search and recommendations, and eventually integrate with HQPlayer for automated DJ capabilities with voice control.

**Development Philosophy**: Build incrementally, understand each step, start with the simplest working pipeline, then gradually improve.

---

## Tech Stack

### Core
- **Language**: Python 3.11+ (best compatibility with ML libraries)
- **Framework**: FastAPI (async-ready for future, but start with synchronous code)
- **Database**: PostgreSQL 16 + pgvector extension
- **Containerization**: Docker + Docker Compose
- **GPU**: NVIDIA RTX 4090 (for audio embeddings)

### ML/AI Libraries
- **Audio Processing**: librosa, mutagen (FLAC metadata)
- **Audio Embeddings**: transformers (CLAP model from LAION)
- **Text Embeddings**: sentence-transformers (Phase 2)
- **LLM Integration**: anthropic SDK (Claude API)

### External APIs (Phase 2)
- Spotify Web API (free tier)
- Last.fm API (free tier)

### Development Tools
- **MCP Integration**: Optional PostgreSQL MCP server for Claude Code to interact with database during development
- **Code Style**: Start synchronous, refactor to async later if needed

---

## Library Structure

### Path Configuration
```
ROOT: E:\Music (may change to SSD in future - keep configurable!)

Structure:
E:\Music\{Genre}\{Artist}\{Album}\{Track}.flac

Example:
E:\Music\Blues\Sade\The Best Of Sade\Sade - 01. Your Love Is King.flac
```

### Artist Folder Subfolders
- `[Vinyl]` - vinyl rips
- `[TR24]` - official hi-res albums
- `[MP3]` - MP3 format
- Root of artist folder - CD 16bit

### Quality Source Detection
System should automatically detect quality source from folder structure:
- Path contains `[Vinyl]` → quality_source = 'Vinyl'
- Path contains `[TR24]` → quality_source = 'Hi-Res'
- Path contains `[MP3]` → quality_source = 'MP3'
- Otherwise → quality_source = 'CD'

---

## Development Phases

## PHASE 1: MVP Foundation (Core Indexing & Search)

**Goal**: Create a working system that can index the music library, generate audio embeddings, and provide AI-powered recommendations.

### Step 1.1: Project Setup & Docker Environment
**Deliverable**: Working Docker environment with PostgreSQL + pgvector and Python backend

**Project Structure**:
```
music-ai-dj/
├── docker-compose.yml
├── .env.example
├── README.md
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── config.py           # Configuration (paths, API keys)
│   └── main.py             # FastAPI entry point (minimal)
├── data/                   # External volume mounts
│   ├── postgres/           # PostgreSQL data (persistent)
│   └── cache/              # Model cache, temp files
└── scripts/
    └── init_db.sql         # Database schema
```

**Key Components**:
- Docker Compose with PostgreSQL (pgvector enabled) and Python backend
- Configuration management (paths, API keys, feature flags)
- External volumes for database persistence and model cache
- GPU support for backend container (NVIDIA runtime)

**Important Considerations**:
- Music library mounted as read-only: `E:\Music:/music:ro`
- Database data persists outside containers: `./data/postgres:/var/lib/postgresql/data`
- Model cache persists: `./data/cache:/root/.cache`
- Configurable music library path (will change from HDD to SSD)

---

### Step 1.2: Library Scanner (Metadata Extraction)
**Deliverable**: Can scan FLAC library, extract metadata, store in PostgreSQL

**Key Functionality**:
- Recursively scan music library for FLAC files
- Extract metadata using mutagen (artist, album, title, genre, year, track number, duration, etc.)
- Detect quality source from folder structure ([Vinyl], [TR24], [MP3], or CD)
- Handle edge cases (malformed tags, missing metadata, special characters)
- Store in normalized database schema

**Database Design Requirements**:
- Design normalized schema (avoid redundancy)
- Separate tables for artists, albums, and tracks with proper foreign keys
- Support for multiple artists per track (features, compilations)
- Indexes for fast querying (artist, album, genre)
- pgvector extension enabled for embedding storage
- Fields for future use (play counts, user notes, external service IDs)

**Testing Strategy**:
- Start with limited scan (e.g., 100 tracks) for testing
- Verify quality detection works correctly for all folder types
- Check metadata extraction accuracy across different files
- Test with files that have non-standard tags

---

### Step 1.3: Audio Embeddings (CLAP)
**Deliverable**: Generate audio embeddings for tracks using CLAP model on RTX 4090

**Key Functionality**:
- Load CLAP model (laion/clap-htsat-unfused) on GPU
- Process audio files to generate 512-dimensional embeddings
- Use middle 30 seconds of each track for consistent analysis
- Batch processing for efficiency (optimize batch size for RTX 4090)
- Handle different track lengths gracefully
- Store embeddings in database with pgvector
- Track which files have been processed (avoid reprocessing)

**Performance Optimization**:
- Benchmark actual speed on RTX 4090 (estimate: 0.3-0.5 sec/track with batching)
- Experiment with batch sizes (start with 16, try 32, 64)
- Cache models in external volume to avoid re-downloading
- Process incrementally (only new tracks without embeddings)
- Background processing capability for large library
- Progress tracking and logging

**Testing Strategy**:
- Select test albums from different genres (Rock, Jazz, Electronic, Classical, Ambient)
- Process 50-100 tracks initially to verify pipeline works
- Compare processing speed with different batch sizes
- Manually verify embedding quality by checking similarity of known similar tracks
- Test edge cases (very short tracks, very long tracks, different sample rates)

---

### Step 1.4: Semantic Search by Audio
**Deliverable**: Search tracks by audio similarity using embeddings

**Key Functionality**:
- Find tracks similar to a given track using cosine similarity
- Search by audio embedding vector directly
- Basic metadata filtering (artist, album, genre, quality source)
- Return results with similarity scores (0-1 range)
- Use pgvector's efficient similarity search operators
- Configurable number of results and minimum similarity threshold

**Search Types to Implement**:
1. **Similar to track**: Given track ID, find similar tracks
2. **Similar to embedding**: Given embedding vector, find similar tracks
3. **Metadata filter**: Search by artist/album/genre/quality
4. **Combined search**: Similarity + metadata filters

**Testing & Validation**:
- Manually verify "similar" tracks make musical sense
- Test across different genres - should find genre-appropriate matches
- Check if covers/live versions cluster together
- Verify remasters and different editions are found as similar
- Ensure search performance is acceptable (< 1 second for typical queries)
- Test edge cases (tracks with no similar matches, very common patterns)

---

### Step 1.5: Claude Integration (RAG for Music)
**Deliverable**: AI assistant that can recommend tracks using RAG over library

**Key Functionality**:
- Integrate Claude API (claude-sonnet-4-20250514)
- Retrieve relevant tracks for user queries
- Build structured context from track data for Claude
- Natural language recommendations with explanations
- Handle various query types:
  - "Find something energetic for workout"
  - "Recommend music similar to [artist/track]"
  - "What ambient tracks do I have?"
  - "Show me vinyl rips from the 1970s"

**RAG Strategy**:
- Initial implementation: keyword extraction from queries
- Retrieve relevant tracks based on keywords or similarity
- Format context clearly for Claude (artist, album, title, genre, quality)
- Let Claude reason about recommendations based on context
- Keep context size manageable (20-50 tracks typical)

**Limitations (Phase 1 MVP)**:
- Text embeddings come in Phase 2 (will improve semantic understanding)
- Basic keyword matching for now (good enough for MVP)
- Limited to tracks with audio embeddings processed
- No external data yet (comes in Phase 2)

**Testing Queries**:
```
1. "Find tracks similar to Pink Floyd - Comfortably Numb"
2. "Recommend energetic rock for morning workout"
3. "What ambient music do I have for focus work?"
4. "Show me vinyl rips from progressive rock artists"
5. "Something jazzy and mellow for evening"
```

---

## PHASE 2: External Data & Text Embeddings

**Goal**: Enhance search quality with external data sources and text-based semantic search

### Step 2.1: Text Embeddings from Metadata
- Generate text embeddings using sentence-transformers
- Embed artist names, album names, genres, descriptions
- Enable semantic text search ("melancholic piano music")
- Combine with audio embeddings for hybrid search

### Step 2.2: Spotify Integration
- Fetch Spotify audio features (tempo, energy, danceability, valence, etc.)
- Match library tracks to Spotify catalog
- Enrich database with Spotify IDs and features
- Use features to improve recommendations
- Free tier, batch processing (100 tracks per request)

### Step 2.3: Last.fm Integration
- Fetch artist information, biographies, tags
- Get similar artists data
- Popular track information
- User-generated tags and descriptions
- Free tier, rate-limited API

### Step 2.4: Enhanced RAG
- Combine audio embeddings, text embeddings, and external features
- Hybrid search strategies (weighted combinations)
- Better semantic understanding of user queries
- Richer context for Claude with multiple data sources
- Improved recommendation explanations

---

## PHASE 3: HQPlayer Integration & Web UI

**Goal**: Control music playback and create user interface

### Step 3.1: HQPlayer Control
- Research HQPlayer Desktop v5.16.3 API/CLI capabilities
- Implement control functions (play, pause, stop, next, volume)
- Queue management
- Current track info retrieval
- Integration with search and recommendations

### Step 3.2: MCP Server for HQPlayer (Optional)
- Create MCP tools for HQPlayer control
- Allow Claude to control playback directly
- Natural language playback commands
- "Play something similar", "Skip to next album", etc.

### Step 3.3: Minimal Web UI
- Technology choice: Streamlit (quick) or FastAPI + Vue.js (flexible)
- Basic features:
  - Search interface
  - Track browser
  - Playback controls (if HQPlayer integration ready)
  - Recommendation display
  - Library statistics
- Simple, functional design (beautification in Phase 4)

---

## PHASE 4: Voice Interface & Advanced Features

**Goal**: Enable voice control and add quality-of-life features

### Step 4.1: Voice Input (Whisper)
- OpenAI Whisper running locally on RTX 4090
- Real-time transcription (Ukrainian/English support)
- Integration with Claude conversation
- Voice commands for playback control
- Natural voice queries for music search

### Step 4.2: Voice Output (TTS)
- Technology choice: Piper (local, free) or OpenAI TTS (better quality, paid)
- Convert Claude responses to speech
- Natural conversation flow
- Adjustable voice settings

### Step 4.3: Voice Conversation Loop
- Complete voice dialogue with AI DJ
- "Hey Claude, play something energetic"
- "What's playing now?"
- "Tell me about this artist"
- "Play something similar but calmer"

### Step 4.4: Advanced Features
**Listening Statistics**:
- Track play counts
- Most played artists/albums/genres
- Listening trends over time
- Favorite tracks identification

**User Notes & Annotations**:
- Personal notes on tracks/albums/artists
- Rating system
- Tags and custom categories
- Integration with recommendations

**UI Improvements**:
- Better visualization
- Waveform display
- Album art integration
- Playlist management
- Export capabilities

---

## Docker Configuration

### docker-compose.yml Structure
- PostgreSQL service with pgvector extension
- Python backend with GPU support
- Volume mounts for persistence
- Environment variable configuration
- Network configuration

### Dockerfile Requirements
- Base image with CUDA support
- Python 3.11 installation
- Audio processing dependencies (ffmpeg, libsndfile)
- Python package installation
- Working directory setup

### requirements.txt Key Packages
- FastAPI, uvicorn for web framework
- psycopg2, pgvector for database
- librosa, mutagen for audio processing
- torch, transformers for ML models
- anthropic for Claude API
- click for CLI tools

---

## MCP Integration (Optional for Development)

### PostgreSQL MCP Setup
After Docker containers are running, optionally connect Claude Code to database for interactive development:

**Benefits**:
- Claude Code can explore database schema interactively
- Generate queries and Python functions based on actual data
- Debug data issues in real-time
- Faster development iteration

**Security Note**: 
Consider creating read-only database user for MCP connection.

---

## Development Workflow

### Initial Setup Steps
1. Create project structure
2. Configure environment variables (.env file)
3. Start Docker containers
4. Verify database connection and GPU access
5. Run initial library scan (small subset for testing)
6. Generate embeddings for test tracks
7. Test search functionality
8. Test AI assistant integration

### Iterative Development
- Code changes reflected immediately via volume mounts
- Restart containers when needed
- Database data persists between sessions
- Model cache prevents re-downloading

---

## Testing Strategy

### Phase 1 Testing

**Test Album Selection**:
Pick 3-5 albums from different genres for initial testing:
- Rock: Pink Floyd, Led Zeppelin
- Jazz: Miles Davis, John Coltrane
- Electronic: Boards of Canada, Aphex Twin
- Classical: Bach, Mozart
- Ambient: Brian Eno, Stars of the Lid

**Benchmark Metrics**:
- Audio embedding speed per track
- Batch processing efficiency
- Search query response time
- Database query performance
- GPU memory usage

**Validation Queries**:
Test AI assistant with diverse queries to verify quality:
- Genre-specific requests
- Mood-based searches
- Artist similarity
- Quality/format filters
- Time period queries

### Edge Cases to Test
- Very short tracks (< 30 seconds)
- Very long tracks (> 30 minutes)
- Different sample rates (44.1kHz to 192kHz)
- Malformed metadata
- Special characters in filenames
- Missing tags

---

## Success Criteria

### Phase 1 MVP Success
✅ Can scan library and extract metadata correctly  
✅ Quality source detected properly from folder structure  
✅ Can generate audio embeddings on RTX 4090  
✅ Search returns musically sensible results  
✅ Claude provides relevant recommendations  
✅ All data persists correctly  
✅ Docker setup is reproducible  

**Expected Timeline**: 1-2 weeks for Phase 1 MVP

---

## Key Principles

1. **Incremental Progress**: Each step independently testable
2. **Data Persistence**: All important data in external volumes
3. **Flexibility**: Configuration adaptable for future changes
4. **Proper Design**: Normalized database schema from the start
5. **Error Handling**: Graceful handling of edge cases
6. **Clear Logging**: Comprehensive logging for debugging

---

## Important Considerations

- **Path Flexibility**: Music library path will change (HDD → SSD)
- **Schema Normalization**: Design proper relationships from start
- **Batch Optimization**: Find optimal sizes for RTX 4090
- **Model Caching**: Prevent re-downloading models
- **Read-Only Mount**: Protect original music library
- **Container Safety**: Can delete containers without data loss

---

## Questions for Implementation

1. **Database Schema**: Should schema be created via SQL scripts or ORM migrations?
2. **Logging**: Centralized logging configuration approach?
3. **Progress Tracking**: Use CLI progress bars for long operations?
4. **Error Recovery**: How to handle partial failures during batch processing?
5. **Code Organization**: Module structure and dependency management?
6. **Testing**: Unit tests from start or after MVP?

---

## Current Focus

**START HERE**: Step 1.1 - Project Setup & Docker Environment

After completing each step, verify functionality before moving to next step.

**Implementation Order**:
1. Project structure & Docker setup
2. Library scanner
3. Audio embeddings
4. Search functionality
5. Claude integration

Then move to Phase 2 for external data enrichment.
