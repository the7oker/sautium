# Music AI DJ

An AI-powered music library management system for personal FLAC collections. The system analyzes audio content, provides intelligent search and recommendations, and integrates with Claude AI for natural language music discovery.

## Features

- **Audio Analysis**: Generate audio embeddings using CLAP model on NVIDIA GPU
- **Semantic Search**: Find similar tracks based on audio characteristics
- **AI Recommendations**: Natural language music discovery powered by Claude
- **Metadata Management**: Extract and organize FLAC metadata with quality source detection
- **Scalable**: Designed to handle large libraries (~30,000 tracks)

## Tech Stack

- **Backend**: Python 3.11 + FastAPI
- **Database**: PostgreSQL 16 with pgvector extension
- **ML Models**: CLAP (audio embeddings), transformers
- **AI**: Anthropic Claude API
- **Infrastructure**: Docker + Docker Compose with GPU support

## Prerequisites

- Docker and Docker Compose
- NVIDIA GPU with CUDA support (tested on RTX 4090)
- NVIDIA Container Toolkit
- Python 3.11+ (for local development)
- Anthropic API key

## Quick Start

### 1. Clone and Setup

```bash
git clone <repository-url>
cd music-ai-dj
```

### 2. Configure Environment

Copy the example environment file and edit it with your settings:

```bash
cp .env.example .env
```

Edit `.env` and set:
- `ANTHROPIC_API_KEY`: Your Anthropic API key
- `MUSIC_LIBRARY_PATH`: Path to your music library (e.g., `E:\Music` on Windows)
- `POSTGRES_PASSWORD`: Secure password for PostgreSQL

### 3. Start Services

```bash
docker-compose up -d
```

This will:
- Start PostgreSQL with pgvector extension
- Initialize the database schema
- Start the FastAPI backend with GPU support
- Mount your music library as read-only

### 4. Verify Installation

Check that services are running:

```bash
docker-compose ps
```

Check backend logs:

```bash
docker-compose logs -f backend
```

Access the API documentation:
```
http://localhost:8000/docs
```

## Project Structure

```
music-ai-dj/
├── docker-compose.yml          # Docker services configuration
├── .env                        # Environment variables (not in git)
├── .env.example                # Environment template
├── README.md                   # This file
├── backend/
│   ├── Dockerfile              # Backend container definition
│   ├── requirements.txt        # Python dependencies
│   ├── config.py               # Configuration management
│   └── main.py                 # FastAPI application
├── data/
│   ├── postgres/               # PostgreSQL data (persistent)
│   └── cache/                  # Model cache (persistent)
└── scripts/
    └── init_db.sql             # Database schema initialization
```

## Development Phases

### Phase 1: MVP Foundation (Current)
- ✅ Project setup and Docker environment
- ⏳ Library scanner (metadata extraction)
- ⏳ Audio embeddings (CLAP model)
- ⏳ Semantic search
- ⏳ Claude integration (RAG)

### Phase 2: External Data
- Text embeddings
- Spotify integration
- Last.fm integration
- Enhanced RAG

### Phase 3: Playback & UI
- HQPlayer integration
- Web interface
- Playlist management

### Phase 4: Voice Control
- Whisper (voice input)
- TTS (voice output)
- Voice conversation loop

## Music Library Structure

Expected library structure:
```
E:\Music\{Genre}\{Artist}\{Album}\{Track}.flac
```

Quality detection:
- `[Vinyl]` folder → Vinyl rips
- `[TR24]` folder → Hi-Res (24-bit)
- `[MP3]` folder → MP3 format
- Root folder → CD quality (16-bit)

Example:
```
E:\Music\Blues\Sade\The Best Of Sade\Sade - 01. Your Love Is King.flac
E:\Music\Rock\Pink Floyd\[Vinyl]\The Dark Side of the Moon\...
E:\Music\Jazz\Miles Davis\[TR24]\Kind of Blue\...
```

## Database Schema

The system uses a normalized schema with separate tables for:
- **artists**: Artist information
- **albums**: Album information with artist relationships
- **tracks**: Track metadata with embeddings
- **track_artists**: Many-to-many relationship for features/compilations

See `scripts/init_db.sql` for complete schema.

## API Endpoints

### Core Endpoints (Phase 1)
- `GET /`: Health check
- `GET /stats`: Library statistics
- `POST /scan`: Scan music library
- `POST /embeddings/generate`: Generate audio embeddings
- `POST /search/similar`: Find similar tracks
- `POST /search/query`: Natural language search with AI

Full API documentation available at `http://localhost:8000/docs`

## Development

### Local Development Setup

```bash
# Install dependencies locally (optional, for IDE support)
cd backend
pip install -r requirements.txt
```

### Logs and Debugging

```bash
# View all logs
docker-compose logs -f

# View backend logs only
docker-compose logs -f backend

# View database logs
docker-compose logs -f postgres
```

### Restart Services

```bash
# Restart backend (after code changes)
docker-compose restart backend

# Rebuild backend (after requirements.txt changes)
docker-compose up -d --build backend
```

### Stop Services

```bash
# Stop all services
docker-compose down

# Stop and remove volumes (WARNING: deletes database data)
docker-compose down -v
```

## Troubleshooting

### GPU Not Detected

Ensure NVIDIA Container Toolkit is installed:
```bash
# Check NVIDIA Docker runtime
docker run --rm --gpus all nvidia/cuda:12.0.0-base-ubuntu22.04 nvidia-smi
```

### Database Connection Issues

Check PostgreSQL is healthy:
```bash
docker-compose ps postgres
docker-compose logs postgres
```

### Permission Issues (Windows)

Ensure Docker has access to the music library drive in Docker Desktop settings.

## Performance Notes

- **Embedding Generation**: ~0.3-0.5 seconds per track with batching (RTX 4090)
- **Expected Processing Time**: ~2.5-4 hours for 30,000 tracks
- **Database Size**: ~50-100MB per 10,000 tracks (with embeddings)
- **Model Cache**: ~2-3GB for CLAP model

## License

Private project - All rights reserved

## Contributing

This is a personal project. External contributions not currently accepted.

## Acknowledgments

- LAION for CLAP model
- Anthropic for Claude API
- pgvector for efficient vector search
