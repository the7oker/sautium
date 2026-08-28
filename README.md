# Sautium

AI-powered management system for a personal FLAC library (~30k tracks): audio
content analysis, hybrid semantic search, AI-driven recommendations, HQPlayer
integration, a phone-friendly Web UI, and a serverless P2P network for sharing
analytics between collectors.

> Phases 1–3 (MVP + enrichment + audio analysis + HQPlayer + Web UI +
> launcher) and P2P phases P0–P4 (sync, NAT traversal, account system, E2E
> chat) are **done**. Voice interface (Whisper + TTS) and file sharing over
> libtorrent remain on the roadmap.

## Features

- **Audio content analysis** — 512-d CLAP audio embeddings (`laion/clap-htsat-unfused`)
  on GPU, plus librosa DSP features (tempo, spectral, MFCC) and an AST + PaSST
  ensemble for multi-label instrument tagging.
- **Hybrid semantic search** — combines audio embeddings with 1024-d
  multilingual text embeddings (BGE-M3), default mix 70% text + 30% audio,
  tunable per query, with a subtle log-scale popularity boost.
- **AI assistant** — natural-language music discovery powered by Claude Code + MCP
  tools (22 tools across HQPlayer, PostgreSQL and search) instead of a custom
  RAG pipeline. Pluggable LLM providers (Claude API, Claude Code, OpenAI,
  Groq, OpenAI-compatible endpoints).
- **Metadata enrichment** — Last.fm bios, tags, similar artists, track stats
  and album wikis in normalized tables; bio-derived classifiers (gender,
  vocalist); artist photos from Deezer with a Last.fm fallback. Idempotent and
  incremental.
- **HQPlayer integration** — XML control protocol over TCP 4321: transport,
  DSP filter/shaper selection, matrix profiles, convolution, and parametric-EQ
  preset generation. A separate playback-tracker daemon records
  `listening_history` and scrobbles to Last.fm.
- **Web UI** — phone-first vanilla HTML/CSS/JS (no build step, no framework)
  served by FastAPI, with a tokens-based design system and SSE-driven player.
- **Serverless P2P network** — share metadata, embeddings and audio features
  over a libtorrent DHT; deterministic identity (Argon2id → Ed25519); E2E
  encrypted chat (NaCl Box); optional email verification via a Cloudflare
  Worker acting as a CA. See `P2P_NETWORK.md`.
- **Windows desktop launcher** — CustomTkinter app (PyInstaller `.exe`, Inno
  Setup installer) that manages the backend, P2P layer and account.

## Architecture

```
Sautium node
├── Docker backend         FastAPI + PostgreSQL 18/pgvector + GPU (CLAP, BGE-M3)
│   ├── routers/           home, discovery, artists, albums, genres, player,
│   │                      hqplayer, eq, covers, chat, p2p, sync, profile, settings
│   ├── static/            Web UI (index.html + app-shell.js + player.js + tokens.css)
│   └── playback-tracker   separate daemon → listening_history + Last.fm scrobble
├── MCP server             22 tools for Claude Code (runs on host, not in Docker)
├── Desktop launcher       CustomTkinter; owns UPnP + DHT for P2P
│   └── p2p/               sync server, DHT, chat, NAT traversal, account identity
└── Cloudflare Worker      email-verification CA + signed invite delivery
```

The backend serves **HTTPS only** on `0.0.0.0:8800` (LAN access from phones is
the primary workflow). PostgreSQL (`5432`) and the playback tracker (`8765`)
bind to loopback. The P2P DHT listens on `19001/udp`.

## Tech Stack

- **Python 3.11+**, **FastAPI** (async)
- **PostgreSQL 18 + pgvector** — vector similarity + relational data
- **SQLAlchemy** ORM + `psycopg2` for raw SQL / batch operations
- **Docker + Docker Compose** (WSL2 on Windows, native on macOS)
- **NVIDIA RTX 4090** for GPU work (CLAP embeddings, BGE-M3 text encoding)
- **CLAP** (audio, 512-d) + **BGE-M3** (text, 1024-d) + **librosa** + AST/PaSST
- **anthropic SDK** + Claude Code & MCP for the AI assistant
- **libtorrent** for the DHT (and future file sharing)
- **aiohttp + PyNaCl + miniupnpc** for the P2P layer
- **CustomTkinter + PyInstaller + Inno Setup** for the Windows launcher/installer

## Prerequisites

- Docker and Docker Compose
- NVIDIA GPU with CUDA support + NVIDIA Container Toolkit (Linux/WSL2 path).
  Apple Silicon uses a CPU-only variant — see below.
- An LLM provider: an Anthropic API key, or a logged-in Claude Code CLI whose
  `~/.claude` is mounted into the backend (default provider is `claude_code`).
- A Last.fm API key for metadata enrichment and scrobbling (optional).

## Quick Start

### 1. Clone

```bash
git clone <repository-url> sautium
cd sautium
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:
- `ANTHROPIC_API_KEY` — Anthropic key (or set `CLAUDE_CODE_ENABLED=true` and
  rely on a mounted Claude Code login).
- `MUSIC_LIBRARY_PATH` — host path Docker mounts read-only (e.g. `E:\Music`).
- `MUSIC_HOST_PATH` — native OS path stored in the DB so HQPlayer can open
  files directly (e.g. `E:/Music`).
- `POSTGRES_PASSWORD` — database password.
- `LASTFM_API_KEY` / `LASTFM_API_SECRET` — for enrichment + scrobbling (optional).
- `SAUTIUM_HOST_IPS` — your host's LAN IP, added to the TLS cert SAN so phones
  can reach the Web UI over HTTPS (Docker can't auto-detect it from inside the
  container).

### 3. Start services

Pick the Compose file for your host:

```bash
# Windows / Linux with an NVIDIA GPU (Claude Code login on the Windows side)
docker compose up -d --build

# Windows where your active Claude Code login lives inside WSL
docker compose -f docker-compose.wsl.yml up -d --build

# macOS (Apple Silicon, CPU-only PyTorch)
docker compose -f docker-compose.mac.yml up -d --build
```

This starts PostgreSQL (schema auto-applied from
`desktop/migrations/001_initial.sql`), the FastAPI backend (which generates a
self-signed TLS cert on first run), and the playback tracker.

### 4. Verify

```bash
docker compose ps
docker compose logs -f backend     # wait for "Application startup complete"
```

- **Web UI:** `https://localhost:8800/` (accept the self-signed cert once).
- **API docs:** `https://localhost:8800/docs`

The backend is also reachable from phones/tablets on the same Wi-Fi at
`https://<host-LAN-IP>:8800/`.

### Desktop launcher (optional)

The launcher runs the whole stack without Docker — PostgreSQL, backend, P2P and
the account system. From a checkout it is `python -m desktop`; for other people
it ships as a native app:

- **Windows** — `python desktop/build.py` builds the PyInstaller `.exe`, wrapped
  by the Inno Setup script in `desktop/installer/`.
- **macOS** — `python desktop/build_macos.py` builds `Sautium.app` and
  `dist/Sautium-<version>-<arch>.dmg`. Needs Xcode Command Line Tools (`clang`,
  `iconutil`) and Pillow.

The macOS bundle is not a frozen launcher. It carries a private CPython 3.12
(python-build-standalone, Tk included) plus a snapshot of the git-tracked tree,
and `Contents/Resources/bootstrap.py` installs both into `~/.local/share/Sautium`
on first run — after which the launcher runs exactly as it does from a checkout,
because it is one: the tree is cloned from `main`, so **Check for Updates** in
the launcher pulls, reinstalls changed requirements, runs new migrations and
restarts the backend, the same path a checkout uses. The bundled snapshot is
the fallback for an install that cannot reach GitHub. Two consequences: what
lands on `main` reaches every installed app, and a schema change only travels
as a NEW numbered migration — editing `001_initial.sql` in place never re-runs. Freezing was rejected: the launcher provisions and then RUNS
a Python (pip-installing torch, spawning uvicorn and the MCP server), and inside
a frozen bundle `sys.executable` is the bundle.

Builds are ad-hoc signed by default. With a Developer ID:

```bash
python desktop/build_macos.py --sign "Developer ID Application: ..." \
                              --notarize <keychain-profile>
```

#### Installing the macOS build

1. Open the DMG, drag **Sautium** onto **Applications**.
2. An ad-hoc signature is blocked on first launch: open it once, then go to
   System Settings → Privacy & Security → **Open Anyway**. (Or run
   `xattr -dr com.apple.quarantine /Applications/Sautium.app` first.) A
   notarized build skips this step.
3. The first launch unpacks the app and builds its Python environment, then
   asks for **Homebrew** if it is missing — PostgreSQL 18, pgvector, ffmpeg,
   flac, fpcalc and deno all arrive through it.
4. The setup wizard creates the account and the database. Its MusicBrainz
   catalogue step is pre-ticked when the disk has room (~21 GB in the
   background) — untick it for a quick trial. Finishing the wizard starts the
   backend, which installs the ML stack on first run (~1.3 GB, once).
5. In the launcher: **Scan Library** picks the music folder, **Open Web UI**
   opens `https://localhost:18000` (accept the self-signed cert once).

#### Testing installs

`scripts/test-node.sh run` starts the installed app against a throwaway data
root — its own wizard, database and ports, leaving the node this machine
already runs alone — and `reset` deletes it. It launches the way the Dock does,
with LANG stripped, because that is where a locale-less PostgreSQL start fails
and a terminal never will. `wipe --yes` deletes the real node on this machine:
`~/.config/Sautium` (settings, account key), `~/.local/share/Sautium` (database,
logs, the app's Python) and `~/.sautium` (the browser certificate). Homebrew
packages, the pip cache and `~/.cache/huggingface` are left alone.

## Project Structure

```
sautium/
├── docker-compose.yml              # default (WSL2/Windows + NVIDIA GPU)
├── docker-compose.wsl.yml          # Claude Code login inside WSL
├── docker-compose.mac.yml          # Apple Silicon (CPU-only)
├── .env.example                    # environment template
├── CLAUDE.md                       # project spec, conventions, security posture
├── PROGRESS.md                     # design decisions and lessons learned
├── P2P_NETWORK.md                  # P2P architecture and security model
├── backend/
│   ├── main.py                     # FastAPI entry point
│   ├── entrypoint.py               # startup (TLS gen, migrations, uvicorn)
│   ├── models.py                   # SQLAlchemy ORM models
│   ├── uuid_utils.py               # UUID v5 generators + normalization
│   ├── search.py                   # hybrid audio + text semantic search
│   ├── lastfm.py / covers.py       # enrichment + artist/album artwork
│   ├── audio_analysis.py           # librosa features + CLAP embeddings
│   ├── ensemble_instruments.py     # AST + PaSST instrument tagger
│   ├── hqplayer_client.py          # HQPlayer XML control client
│   ├── auth_hmac.py / tls_gen.py   # HMAC request signing + self-signed TLS
│   ├── assistant_prompt.py         # AI assistant system prompt + schema description
│   ├── providers/                  # pluggable LLM providers
│   ├── routers/                    # FastAPI route modules
│   └── static/                     # Web UI (vanilla HTML/CSS/JS, no build)
├── desktop/
│   ├── launcher.py                 # Windows launcher (CustomTkinter)
│   ├── node_identity.py            # Ed25519 identity + account (Argon2id)
│   ├── sync_client.py              # import from remote + post-import classifiers
│   ├── migrations/001_initial.sql  # canonical schema (single source of truth)
│   ├── installer/                  # Inno Setup installer
│   └── p2p/                        # sync server, DHT, chat, NAT traversal
├── mcp/
│   └── assistant_server.py         # MCP server (37 tools for Claude Code)
├── worker/
│   └── verify.js                   # Cloudflare Worker (email CA, signed invites)
├── docs/
│   ├── design/                     # POSITIONING, INFORMATION-ARCHITECTURE, reference bundle
│   └── HQPLAYER_*.md               # HQPlayer integration + knowledge base
└── data/                           # postgres data, model cache, TLS certs (persistent)
```

## Security Posture (read before touching network/auth)

The backend is a **single-user home appliance**, defended primarily by network
isolation:

- Backend `8800` listens on `0.0.0.0` **by design** (phone/tablet use over home
  Wi-Fi) but is **never exposed to the public internet** — no port is forwarded
  and the P2P UPnP layer never maps it.
- All API requests are signed with **HMAC-SHA256** (`backend/auth_hmac.py`); the
  shared secret is injected into the page at `/` and `auth.js` signs every
  `fetch`. HTTPS is mandatory because browsers gate `crypto.subtle` (needed to
  compute the HMAC) behind secure contexts.
- TLS is **self-signed**, with only private IPs in the cert SAN.
- The **P2P sync server** is the only surface intended to face the internet
  (random port, self-signed TLS + Ed25519 request signatures).

This is LAN-only by design. Public release / multi-user / remote-access would
require per-user credentials, a CA-signed cert and CSRF-aware sessions — see the
full **Security Posture** section in `CLAUDE.md` before changing any of it.

## Music Library Structure

```
E:\Music\{Genre}\{Artist}\{Album}\{Track}.flac
```

Quality is inferred from a folder marker:
- `[Vinyl]` → vinyl rip
- `[TR24]` → Hi-Res (24-bit)
- `[MP3]` → MP3
- (no marker) → CD quality (16-bit)

```
E:\Music\Blues\Sade\The Best Of Sade\Sade - 01. Your Love Is King.flac
E:\Music\Rock\Pink Floyd\[Vinyl]\The Dark Side of the Moon\...
E:\Music\Jazz\Miles Davis\[TR24]\Kind of Blue\...
```

## Database

The canonical schema is **`desktop/migrations/001_initial.sql`** — the single
source of truth for a fresh install (all types, tables, indexes and triggers).
It is auto-applied on first container start. Highlights:

- **Normalized multi-source metadata** (`artist_bios`, `artist_tags`,
  `similar_artists`, `album_descriptions`, `track_stats`) with a `source` column for
  provenance — not JSONB blobs.
- **Deterministic UUID v5** for all shareable entities (Artist, Album, Track,
  Genre, Tag, EmbeddingModel) so the same data on different nodes collapses to
  the same ID. Namespace `adc1ec0b-2c81-5e26-9938-a369c6f7a5e1`.
- **Albums have no `artist_id`** — artists derive via `track_artists`, so
  compilations/features/collaborations work without nullable FKs.
- **`ON UPDATE CASCADE`** on track/album UUID FKs so artist-name normalization
  can safely rewrite UUIDs.

## Development

```bash
# Logs
docker compose logs -f backend
docker compose logs -f postgres

# Restart backend after code / prompt / model changes (auto-reload is off in Docker)
docker restart sautium-backend

# Rebuild after dependency changes
docker compose up -d --build backend

# Stop everything (add -v to also drop the database volume — destructive)
docker compose down
```

### Troubleshooting

- **GPU not detected** — verify the runtime:
  `docker run --rm --gpus all nvidia/cuda:12.0.0-base-ubuntu22.04 nvidia-smi`.
- **Phone can't reach the Web UI** — confirm `SAUTIUM_HOST_IPS` lists the host's
  LAN IP, then `docker restart sautium-backend` to regenerate the cert SAN, and
  accept the certificate warning once per host.
- **`Claude Code error` on AI queries** — the mounted `~/.claude` credentials
  are stale or missing. Use the Compose variant that matches where you ran
  `claude /login` (Windows vs WSL).

## Documentation

| File | Purpose |
|------|---------|
| `CLAUDE.md` | Project spec, architecture rules, conventions, security posture |
| `PROGRESS.md` | Design decisions and lessons learned (non-P2P) |
| `P2P_NETWORK.md` | P2P architecture, technology choices, security model |
| `docs/design/POSITIONING.md` | Product positioning + UI design principles |
| `docs/design/INFORMATION-ARCHITECTURE.md` | Navigation model + screen inventory |
| `docs/HQPLAYER_INTEGRATION.md` | HQPlayer integration + knowledge base |

## License

[PolyForm Noncommercial License 1.0.0](LICENSE) — free for noncommercial use.

## Acknowledgments

- **LAION** for the CLAP model
- **BAAI** for BGE-M3
- **Anthropic** for Claude and Claude Code
- **pgvector** for efficient vector search in PostgreSQL
- **libtorrent** for the DHT layer
