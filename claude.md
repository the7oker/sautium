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
- Last.fm API (free tier)

### Audio Analysis (Phase 3)
- **librosa**: Audio feature extraction (tempo, beat detection, spectral features)
- **essentia**: Advanced music information retrieval (key detection, mood, danceability)

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

### Step 2.1: Last.fm Integration
- Fetch artist information, biographies, tags
- Get similar artists data
- Album information and tags
- Track popularity statistics
- Genre descriptions
- User-generated tags and descriptions
- Free tier, rate-limited API

### Step 2.2: Text Embeddings from Metadata
- Generate text embeddings using sentence-transformers
- Embed artist names, album names, genres, descriptions, tags, bios
- Enable semantic text search ("melancholic piano music")
- Combine with audio embeddings for hybrid search

### Step 2.3: Enhanced RAG
- Combine audio embeddings, text embeddings, and Last.fm metadata
- Hybrid search strategies (weighted combinations)
- Better semantic understanding of user queries
- Richer context for Claude with multiple data sources
- Improved recommendation explanations

---

## PHASE 3: Audio Analysis & Playback

**Goal**: Extract audio features from FLAC files and integrate playback controls

### Step 3.1: Audio Feature Extraction (librosa/essentia)

**Deliverable**: Comprehensive audio analysis pipeline extracting musical characteristics from FLAC files

**Why**: Spotify Audio Features API is deprecated for new apps (Nov 2024). We extract features directly from our FLAC files using open-source tools.

**Libraries**:
- **librosa** (already in requirements): Basic audio analysis, tempo, beat tracking, spectral features
- **essentia** (to be added): Advanced music information retrieval, trained ML models

**Features to Extract**:

**Tempo & Rhythm** (librosa):
- BPM (beats per minute) - `librosa.beat.beat_track()`
- Beat positions and strength
- Onset detection (attack times)

**Harmonic Features** (librosa + essentia):
- Key detection (C, C#, D, etc.) - `essentia.KeyExtractor`
- Mode (Major/Minor) - `essentia.KeyExtractor`
- Chroma features (pitch class distribution)
- Harmonic/percussive separation

**Spectral Features** (librosa):
- Spectral centroid (brightness)
- Spectral rolloff (high-frequency content)
- Spectral contrast (peaks vs valleys)
- Zero-crossing rate (noisiness/percussiveness)
- MFCC (timbre characteristics)

**Energy & Dynamics** (librosa + essentia):
- RMS energy (overall loudness)
- Dynamic range (difference between loud and quiet parts)
- Energy distribution over time

**High-Level Descriptors** (essentia trained models):
- Danceability (0-1) - `essentia.Danceability`
- Aggressiveness (0-1) - similar to Spotify's "energy"
- Mood classification (happy/sad, relaxed/energetic)
- Voice/instrumental detection
- Acoustic vs electronic classification

**Database Schema**:
```sql
-- New table: audio_features
CREATE TABLE audio_features (
    id SERIAL PRIMARY KEY,
    track_id INTEGER REFERENCES tracks(id) ON DELETE CASCADE,

    -- Tempo & Rhythm
    tempo NUMERIC(6, 2),           -- BPM (e.g., 120.50)
    tempo_confidence NUMERIC(3, 2), -- 0-1, confidence score

    -- Harmonic
    key INTEGER,                    -- 0=C, 1=C#, ..., 11=B
    mode INTEGER,                   -- 0=Minor, 1=Major
    key_confidence NUMERIC(3, 2),   -- 0-1

    -- Energy & Dynamics
    energy NUMERIC(3, 2),           -- 0-1 (RMS normalized)
    loudness NUMERIC(6, 2),         -- dB
    dynamic_range NUMERIC(6, 2),    -- dB difference

    -- Spectral characteristics
    brightness NUMERIC(3, 2),       -- 0-1 (spectral centroid normalized)
    timbre_vector VECTOR(13),       -- MFCC coefficients for similarity

    -- High-level descriptors (essentia)
    danceability NUMERIC(3, 2),     -- 0-1
    aggressiveness NUMERIC(3, 2),   -- 0-1
    acousticness NUMERIC(3, 2),     -- 0-1 (acoustic vs electronic)
    voice_instrumental NUMERIC(3, 2), -- 0=instrumental, 1=vocal

    -- Mood (optional, if using mood models)
    mood_happy NUMERIC(3, 2),       -- 0-1
    mood_sad NUMERIC(3, 2),         -- 0-1
    mood_relaxed NUMERIC(3, 2),     -- 0-1
    mood_aggressive NUMERIC(3, 2),  -- 0-1

    -- Metadata
    analysis_model_version VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(track_id)
);

CREATE INDEX idx_audio_features_track ON audio_features(track_id);
CREATE INDEX idx_audio_features_tempo ON audio_features(tempo);
CREATE INDEX idx_audio_features_key ON audio_features(key, mode);
CREATE INDEX idx_audio_features_energy ON audio_features(energy);
CREATE INDEX idx_audio_features_danceability ON audio_features(danceability);
```

**Implementation Module**: `backend/audio_analysis.py`

```python
class AudioAnalyzer:
    """Extract audio features from FLAC files using librosa and essentia."""

    def __init__(self):
        self.sample_rate = 44100  # Resample to consistent rate
        # Load essentia models (pre-trained)
        self.key_extractor = essentia.KeyExtractor()
        self.danceability_extractor = essentia.Danceability()
        # ... other extractors

    def analyze_track(self, file_path: str) -> dict:
        """
        Analyze single track and return all features.

        Returns dict with:
            - tempo, tempo_confidence
            - key, mode, key_confidence
            - energy, loudness, dynamic_range
            - brightness, timbre_vector
            - danceability, aggressiveness, acousticness
            - mood scores
        """
        # Load audio
        y, sr = librosa.load(file_path, sr=self.sample_rate, mono=True)

        # Extract features (parallel processing for speed)
        features = {}

        # Tempo
        tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
        features['tempo'] = float(tempo)

        # Key detection (essentia)
        key, scale, strength = self.key_extractor(y)
        features['key'] = key  # 0-11
        features['mode'] = 1 if scale == 'major' else 0
        features['key_confidence'] = strength

        # Energy & dynamics
        rms = librosa.feature.rms(y=y)[0]
        features['energy'] = float(np.mean(rms))
        features['loudness'] = float(librosa.amplitude_to_db(rms).mean())
        features['dynamic_range'] = float(rms.max() - rms.min())

        # Spectral
        spectral_centroid = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
        features['brightness'] = float(np.mean(spectral_centroid) / (sr/2))

        # MFCCs for timbre
        mfccs = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
        features['timbre_vector'] = np.mean(mfccs, axis=1).tolist()

        # Danceability (essentia)
        features['danceability'] = float(self.danceability_extractor(y))

        # ... other features

        return features

    def analyze_batch(self, db: Session, limit: int = None, batch_size: int = 10):
        """Batch process tracks without audio features."""
        # Similar to embedding generation pipeline
        # Process in parallel where possible
        # Store results in audio_features table
```

**CLI Commands**:
```bash
# Analyze all tracks
docker exec music-ai-backend python cli.py analyze-audio

# Analyze specific track
docker exec music-ai-backend python cli.py analyze-audio --track-id 123

# Re-analyze with updated models
docker exec music-ai-backend python cli.py analyze-audio --force

# Search by features
docker exec music-ai-backend python cli.py search-features --tempo 120-140 --key C --mode major
docker exec music-ai-backend python cli.py search-features --danceability 0.7-1.0 --energy 0.8-1.0
```

**Performance Considerations**:
- **Speed**: ~1-3 seconds per track (RTX 4090 can help with some features)
- **Parallel processing**: Process multiple tracks simultaneously
- **Incremental**: Only analyze tracks without features
- **For 685 tracks**: ~20-40 minutes initial analysis
- **For 30k tracks**: ~10-25 hours (run overnight)

**Testing Strategy**:
1. Start with 10-20 test tracks from different genres
2. Validate tempo against known BPM (check with online tools)
3. Validate key detection against manual listening
4. Compare danceability/energy with intuitive expectations
5. Check if similar tracks have similar features

**Integration with Search**:
- Add feature-based search: `search_by_features(tempo_range, key, energy_min, danceability_min)`
- Enhance hybrid search with feature similarity
- Use in RAG context: "energetic tracks" → filter by energy > 0.7
- Playlist generation: "high-energy workout mix" → sort by energy + tempo

**Benefits over Spotify**:
✅ Works on our FLAC files directly (no API limitations)
✅ No rate limits
✅ Offline analysis
✅ Customizable (can add more features)
✅ Open source, no vendor lock-in
✅ Can re-run with improved models

**Challenges**:
⚠️ Slower than API calls (but one-time cost)
⚠️ Model accuracy varies (but good enough for recommendations)
⚠️ Need to tune thresholds per genre

---

### Step 3.2: HQPlayer Control
- Research HQPlayer Desktop v5.16.3 API/CLI capabilities
- Implement control functions (play, pause, stop, next, volume)
- Queue management
- Current track info retrieval
- Integration with search and recommendations

### Step 3.3: MCP Server for HQPlayer (Optional)
- Create MCP tools for HQPlayer control
- Allow Claude to control playback directly
- Natural language playback commands
- "Play something similar", "Skip to next album", etc.

### Step 3.4: Minimal Web UI
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
