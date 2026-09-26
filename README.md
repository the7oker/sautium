# Sautium

Sautium is a self-hosted music companion for the collection you own: a
library interface over your own files, search by sound, mood or lyrics, an
assistant that knows what you have, and a phone remote for HQPlayer and every
other output in the house — DLNA renderers, the browser, the host's own sound
card. It runs on your machine, shares nothing but signed analysis with a
collectors' network, and is source-available under the PolyForm
Noncommercial 1.0.0 licence — free for personal use.

Website and guides: https://sautium.net · Downloads: https://sautium.net/download

<!-- Screenshots: docs/screenshots/ (360x800 phone and 834x1194 tablet
     frames), captured on a demo library with
       node scripts/ui-shots.mjs --out docs/screenshots --widths 360x800,834x1194 \
         --routes home,home+np,discovery,more/output,more/sync
     Add the <img> tags here once the folder exists; no demo track on screen. -->

> Phases 1–3 (MVP + enrichment + audio analysis + HQPlayer + Web UI +
> launcher) and P2P phases P0–P4 (sync, NAT traversal, account system, E2E
> chat) are **done**, as is the 2026-08 network run (relay forwarding, carry,
> peer-relays, MusicBrainz and ListenBrainz slice replication).

## Features

- **Audio content analysis** — 512-d CLAP audio embeddings (`laion/clap-htsat-unfused`)
  on GPU, plus librosa DSP features (tempo, spectral, MFCC) and an AST + PaSST
  ensemble for multi-label instrument tagging.
- **Hybrid semantic search** — one engine composes heterogeneous signals per
  query: CLAP text→audio, 1024-d multilingual text embeddings (BGE-M3) over
  bios and lyrics, trigram identity matching across scripts, and binary gates
  (vocalist, gender, energy, instruments, year…). Each source is normalized
  against calibrated bounds rather than rank-fused, so magnitude survives, and
  owned files and phantom rows are corpus layers of the same query.
- **AI assistant** — natural-language music discovery through an agent CLI
  (Claude Code or OpenAI Codex, selectable) driving two MCP servers: Sautium's
  own (41 tools — search, playback, queue, HQPlayer DSP, gear, dump control)
  and a read-only PostgreSQL one, instead of a custom RAG pipeline. Pluggable
  LLM providers for the non-agent paths (Claude API, OpenAI, Groq,
  OpenAI-compatible endpoints).
- **Metadata enrichment** — Last.fm bios, tags, similar artists and album
  wikis in normalized tables (node-local: Last.fm's terms don't allow
  redistributing its answers); ListenBrainz listening statistics from the
  public CC0 dump, whole on a dump node or as signed per-artist slices over
  P2P; an optional local MusicBrainz dump as the canonicalization spine;
  bio-derived classifiers (gender, vocalist); artist photos and catalog
  lookups from the Deezer public API. Idempotent and incremental.
- **Audio outputs** — one canonical queue behind an output picker:
  **HQPlayer** (XML control over TCP 4321 — transport, DSP filter/shaper
  selection, matrix profiles, convolution, parametric-EQ preset generation),
  **DLNA** renderers, the **browser** itself, and the host's local sound
  device. Play tracking (`listening_history` + Last.fm scrobbling) lives in
  the backend and is keyed on the track UUID, so a streamed track counts like
  an owned file.
- **Music beyond the library** — artists, albums and tracklists the node does
  not own are minted as phantom rows and play through the demo channel: one
  full listen per track, then the catalog's 30 s excerpt. A bring-your-own
  lossless provider plugs into the same registry.
- **Web UI** — phone-first vanilla HTML/CSS/JS (no build step, no framework)
  served by FastAPI, with a tokens-based design system and SSE-driven player.
- **Serverless P2P network** — share sealed audio analysis (CLAP segments,
  audio features) and the canon layer over a libtorrent DHT — never what
  Last.fm answered; deterministic identity (Argon2id → Ed25519); E2E
  encrypted chat (NaCl Box); relays for nodes behind hostile NAT, carry
  (push-seeding analysis nobody asked for yet), and signed MusicBrainz /
  ListenBrainz slices instead of every node holding a 19 GB dump. See
  `P2P_NETWORK.md`.
- **Desktop launcher** — CustomTkinter app that manages the backend, P2P
  layer and account; ships as a Windows installer and a macOS bundle, both
  carriers for a git clone that updates itself.
- **Gear advisor** — the audio chain as data: researched specs and community
  sentiment per component, a deterministic pair engine (impedance, SPL
  headroom, gain staging, format chains) and an upgrade advisor that diagnoses
  where the chain has measurably plateaued. Verdicts carry provenance; nothing
  is averaged into a score. See `docs/design/GEAR-ADVISOR.md`.
- **Node backup and restore** — one encrypted `.sbk` file per node (the
  database without the MusicBrainz layer, plus the identity documents),
  keyed by the account password through Argon2id in its own salt domain and
  streamed through chunked XChaCha20-Poly1305. The launcher writes and
  restores it (Settings & Tools › Backup & Restore; the setup wizard restores too); a
  Docker node uses `python -m backup create|restore`. The same place exports
  the node's sealed records for another collector (`python -m backup export`)
  and merges theirs through the P2P sync gate (`import`, adding their albums
  or enriching only what you have). Two machines, one account: "Merge from
  backup…" (`python -m backup merge`) unions your listening history,
  friends, chats and gear out of the other machine's backup, keyed so a
  second merge changes nothing. See `docs/design/BACKUP.md`.

## Architecture

```
Sautium node
├── Docker backend         FastAPI + PostgreSQL 18/pgvector + GPU (CLAP, BGE-M3)
│   ├── routers/           home, discovery, artists, albums, genres, player,
│   │                      hqplayer, eq, covers, chat, p2p, sync, profile, settings
│   ├── playback/          output backends (HQPlayer, DLNA, browser, local),
│   │                      the canonical queue and play tracking
│   ├── streaming/         provider registry, demo policy, media proxy
│   ├── canon/             identity, normalization and MusicBrainz-anchored canon
│   ├── static/            Web UI (index.html + app-shell.js + player.js + tokens.css)
│   └── p2p_app.py         the peer surface (own TLS, pinned to the node key)
├── MCP servers            assistant (41 tools) + a read-only postgres one,
│                          spawned for the agent CLI on the host
├── Desktop launcher       CustomTkinter; owns UPnP + DHT for P2P
│   └── p2p/               sync server, DHT, chat, NAT traversal, account identity,
│                          the shared sync walk both surfaces import
└── Cloudflare Worker      email-verification CA, signed invites, notary timestamps
```

The backend serves plain HTTP on `0.0.0.0:8800` (LAN access from phones is
the primary workflow; `PROGRESS.md` § "HTTP on the LAN" is why there is no
TLS on it). PostgreSQL (`5432`) binds to loopback. The P2P DHT listens on
`19001/udp`, and the Docker peer surface on `8801` is the one port meant to
face the internet. Two LAN-only plain-HTTP helpers carry audio bytes to
devices that cannot sign a request: the media proxy (`8830`, launcher
`8832`) and the DLNA event listener (`8831`, launcher `8833`).

## Tech Stack

- **Python 3.11+**, **FastAPI** (async)
- **PostgreSQL 18 + pgvector** — vector similarity + relational data
- **SQLAlchemy** ORM + `psycopg2` for raw SQL / batch operations
- **Docker + Docker Compose** (WSL2 on Windows, native on macOS)
- **NVIDIA RTX 4090** for GPU work (CLAP embeddings, BGE-M3 text encoding)
- **CLAP** (audio, 512-d) + **BGE-M3** (text, 1024-d) + **librosa** + AST/PaSST
- **MADLAD-400** on CTranslate2 int8 (CPU) — any-language → English query
  translation for the English-only CLAP text encoder
- **anthropic SDK**; Claude Code or OpenAI Codex CLI + MCP for the assistant
- **libtorrent** for the DHT
- **aiohttp + PyNaCl + miniupnpc** for the P2P layer
- **CustomTkinter** for the launcher; **Inno Setup** wraps it on Windows, a DMG on macOS

## Requirements

The app detects the tier itself at every start (`docs/design/HARDWARE-TIERS.md`)
— there is nothing to choose. A tier decides what the node computes locally,
never what it stores or plays.

| | Lite | Standard | Curator |
|---|---|---|---|
| CPU | x86-64 with AVX2, 4 cores, or any Apple Silicon | 6+ cores | 8+ cores |
| RAM | 8 GB | 16 GB | 32 GB (Mac: 24 GB unified) |
| Disk | 25 GB free, SSD | 40 GB free, SSD | 100 GB+ free, NVMe |
| GPU | none | NVIDIA ≥ 6 GB (Turing or newer) or Apple Silicon with 16 GB | NVIDIA ≥ 8 GB (Ampere or newer) or an M-Pro-class Mac |
| What you get | Library, every output, enrichment, the peer network and chat, text and sound search (the first sound query warms the encoder for 1–3 min), similarity over analysis imported from peers. No bulk analysis of your own files — it arrives from the network. | Everything; analysing a large library is an overnight job; instrument tagging optional. | Everything, in hours to a day; serves analysis and catalogue slices to peers. |

Unsupported: less than 8 GB RAM, a hard disk (model cold-loads take minutes
even on a fast one), 32-bit systems. The first run downloads ~6–8 GB of
models and runtime; the database grows by about 3 GB per 10,000 analysed
tracks, plus the optional ~21 GB MusicBrainz catalogue.

## Prerequisites

- Docker and Docker Compose
- NVIDIA GPU with CUDA support + NVIDIA Container Toolkit (Linux/WSL2 path).
  Apple Silicon uses a CPU-only variant — see below.
- An LLM provider: an Anthropic API key, or a logged-in Claude Code CLI whose
  `~/.claude` is mounted into the backend (default provider is `claude_code`);
  an OpenAI Codex CLI login works as the second selectable agent.
- A Last.fm API key for metadata enrichment and scrobbling (optional).
- HQPlayer, a DLNA renderer or neither — the browser and the host's own sound
  device work out of the box.

## Quick Start

### 1. Clone

```bash
git clone https://github.com/the7oker/sautium.git sautium
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
- `SAUTIUM_HOST_IPS` — your host's LAN IP, so the backend accepts requests
  addressed to it (the Host guard) and phones can open
  `http://<lan-ip>:8800` (Docker can't auto-detect it from inside the
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

This starts PostgreSQL and the FastAPI backend; on its first start the
backend builds the schema (`desktop/migrations/001_initial.sql` plus the
numbered deltas, `backend/db_migrate.py`).

### 4. Verify

```bash
docker compose ps
docker compose logs -f backend     # wait for "Application startup complete"
```

- **Web UI:** `http://localhost:8800/`
- **API docs:** `http://localhost:8800/docs`

The backend is also reachable from phones/tablets on the same Wi-Fi at
`http://<host-LAN-IP>:8800/` — list that address in `SAUTIUM_HOST_IPS`.

### Desktop launcher (optional)

> **Public beta (2026-09-21).** The Windows build is unsigned and the macOS
> build ad-hoc signed, so both systems warn on first launch — the steps are
> below. Downloads: https://sautium.net/download and
> [GitHub Releases](https://github.com/the7oker/sautium/releases). An
> installed app updates itself from `main`.

The launcher runs the whole stack without Docker — PostgreSQL, backend, P2P and
the account system. From a checkout it is `python -m desktop`; for other people
it ships as a native app:

- **Windows** — `python desktop/build_windows.py` builds
  `dist/Sautium-<version>-Setup.exe`. Needs Inno Setup 6 (a per-user install
  is enough) and Pillow; runs on Windows or under WSL, where it finds the
  Windows-side compiler itself.
- **macOS** — `python desktop/build_macos.py` builds `Sautium.app` and
  `dist/Sautium-<version>-<arch>.dmg`. Needs Xcode Command Line Tools (`clang`,
  `iconutil`) and Pillow.

Neither package is a frozen launcher. Each carries a private CPython 3.12
(python-build-standalone, Tk included) plus a snapshot of the git-tracked tree
— the Windows one a MinGit as well — and a bootstrap installs the tree into the
data root on first run (`~/.local/share/Sautium/app`,
`%LOCALAPPDATA%\Sautium\app`), after which the launcher runs exactly as it does
from a checkout, because it is one: the tree is cloned from `main`, so
**Check for Updates** in the launcher pulls, runs new migrations and restarts
the backend, the same path a checkout uses. The bundled snapshot is the
fallback for an install that cannot reach GitHub. Two consequences: what lands
on `main` reaches every installed app, and a schema change only travels as a
NEW numbered migration — editing `001_initial.sql` in place never re-runs.
Freezing was rejected: the launcher provisions and then RUNS a Python
(pip-installing torch, spawning uvicorn and the MCP server), and inside a
frozen bundle `sys.executable` is the bundle. A new package is therefore only
ever a new runtime; `desktop/build_common.py` holds what the two builds share.

The Windows build is unsigned and the macOS build ad-hoc signed by default.
With a Developer ID:

```bash
python desktop/build_macos.py --sign "Developer ID Application: ..." \
                              --notarize <keychain-profile>
```

Since 2026-09-21 a build is published by `scripts/release-publish.sh`: it
creates the GitHub release `v<version>` (marked latest), uploads the
installers with `SHA256SUMS.txt` and a `downloads.json` describing them
(version, commit, per-asset URL, sha256 and size — what the website's
download page reads from `releases/latest/download/downloads.json`), then
re-downloads every asset over the public URL and verifies it.

#### Installing the Windows build

1. Run `Sautium-<version>-Setup.exe`. SmartScreen warns that the publisher is
   unknown — there is no code-signing certificate — so choose **More info →
   Run anyway**. The install is per-user, without an administrator prompt,
   into `%LOCALAPPDATA%\Programs\Sautium`.
2. The first launch clones the current `main` into `%LOCALAPPDATA%\Sautium\app`
   (the bundled copy is the offline fallback), installs the launcher's packages
   into the bundled Python and starts the launcher, which then downloads
   PostgreSQL 18, a Python for the backend, ffmpeg, flac, fpcalc and deno
   beside the tree. Windows asks for administrator permission once per
   firewall rule the launcher opens (web player, media surfaces, P2P).
3. The setup wizard creates the account and the database, as on macOS below.
   Node.js is downloaded from nodejs.org when an AI agent is picked.
4. Uninstalling (Apps & Features) removes the program and asks whether to
   delete the node too: `%LOCALAPPDATA%\Sautium` (database, logs, the app and
   its downloaded components), `%APPDATA%\Sautium` (settings, account key),
   `%USERPROFILE%\.sautium` (the browser certificate of earlier versions).

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
   opens `http://localhost:18000`.

#### Testing installs

`scripts/test-node.sh run` (macOS) and `scripts/test-node.ps1 run` (Windows)
start the installed app against a throwaway data root — its own wizard,
database and ports, leaving the node this machine already runs alone — and
`reset` deletes it. The macOS script launches the way the Dock does, with LANG
stripped, because that is where a locale-less PostgreSQL start fails and a
terminal never will. `wipe --yes` (`wipe -Yes`) deletes the real node on this
machine: `~/.config/Sautium` (settings, account key), `~/.local/share/Sautium`
(database, logs, the app's Python) and `~/.sautium` (the browser certificate
of earlier versions)
— on Windows `%APPDATA%\Sautium`, `%LOCALAPPDATA%\Sautium` and
`%USERPROFILE%\.sautium`. Homebrew packages, the installed program, the pip
cache and `~/.cache/huggingface` are left alone.

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
│   ├── entrypoint.py               # startup (migrations, model cache, uvicorn)
│   ├── p2p_app.py                  # the peer surface (TLS pinned to the node key)
│   ├── models.py                   # SQLAlchemy ORM models
│   ├── uuid_utils.py               # UUID v5 generators + normalization
│   ├── discovery_engine.py         # the search engine (tools, sources, bridges)
│   ├── lastfm.py / covers.py       # enrichment + artist/album artwork
│   ├── lb_dump_load.py             # ListenBrainz statistics dump loader
│   ├── audio_analysis.py           # librosa features + CLAP embeddings
│   ├── ensemble_instruments.py     # AST + PaSST instrument tagger
│   ├── notary.py / sign_audio.py   # sealing: author signature + Merkle timestamp
│   ├── hqplayer_client.py          # HQPlayer XML control client
│   ├── auth_hmac.py / device_auth.py  # request signing, device tokens, boxed credential exchange
│   ├── assistant_prompt.py         # AI assistant system prompt + schema description
│   ├── backup.py / share.py        # node backup, share export/import, life merge
│   ├── gear_pairs.py / gear_advisor.py  # deterministic pair engine + upgrade advisor
│   ├── playback/                   # output backends + canonical queue + tracker
│   ├── streaming/                  # provider registry, demo policy, media proxy
│   ├── canon/                      # identity + MusicBrainz-anchored canonicalization
│   ├── providers/                  # pluggable LLM providers
│   ├── routers/                    # FastAPI route modules
│   └── static/                     # Web UI (vanilla HTML/CSS/JS, no build)
├── desktop/
│   ├── launcher.py                 # desktop launcher (CustomTkinter)
│   ├── node_identity.py            # Ed25519 identity + account (Argon2id)
│   ├── sync_client.py              # import from remote + post-import classifiers
│   ├── node_backup.py              # the `.sbk` format (Argon2id + XChaCha20-Poly1305)
│   ├── migrations/001_initial.sql  # canonical schema + numbered deltas
│   ├── installer/                  # Inno Setup script (build_windows.py compiles it)
│   ├── windows/, macos/            # first-run bootstraps of the two packages
│   └── p2p/                        # sync server, DHT, chat, NAT traversal
├── mcp/
│   ├── assistant_server.py         # MCP server (41 tools for the agent CLI)
│   └── support_server.py           # maintainer-side support desk tools
├── worker/
│   └── verify.js                   # Cloudflare Worker (email CA, invites, notary)
├── docs/
│   ├── design/                     # POSITIONING, INFORMATION-ARCHITECTURE, BACKUP,
│   │                               # P2P-SYNC-INTEGRITY, HARDWARE-TIERS, GEAR-ADVISOR…
│   └── HQPLAYER_*.md               # HQPlayer integration + knowledge base
└── data/                           # postgres data, model cache, node identity (persistent)
```

## Security Posture (read before touching network/auth)

The backend is a **single-user home appliance**, defended primarily by network
isolation:

- Backend `8800` listens on `0.0.0.0` **by design** (phone/tablet use over home
  Wi-Fi) but is **never exposed to the public internet** — no port is forwarded
  and the P2P UPnP layer never maps it.
- All API requests are signed with **HMAC-SHA256** (`backend/auth_hmac.py`)
  using a per-browser device token, earned once with the account password or a
  pairing PIN; `auth.js` signs every `fetch`. The page carries no key.
- The Web UI rides **plain HTTP**: no certificate a phone would trust can exist
  for a LAN address, so the exchange that earns the token (password or PIN in,
  token out) is boxed end to end to a per-exchange key the node's identity
  signs, and the browser pins that identity on its first sign-in.
- The **peer surface** is the only one intended to face the internet (the
  launcher on a random port 20000–29999, a Docker node on `8801`; TLS pinned
  to the node key + timestamp-bound Ed25519 signatures on everything but the
  deliberately open sync pulls).
- The **media proxy and DLNA eventing** (`8830`/`8831`, launcher
  `8832`/`8833`) are plain HTTP on the LAN, gated by unguessable per-queue
  tokens — they exist because HQPlayer and DLNA renderers can neither sign a
  request nor trust a self-signed certificate.

Audit it yourself: `docs/AUDIT.md` is a ready-to-paste prompt for an AI
coding agent that checks the tree at one commit — outbound destinations,
listening ports, credentials, the demo ledger, the sync gate, the
diagnostics switch and the provenance of a downloaded installer — against
this section and `SECURITY.md`.

This is LAN-only by design. Public release / multi-user / remote-access would
require per-user credentials, TLS from a front with a real name (Tailscale
Serve, a reverse proxy) and CSRF-aware sessions — see the full **Security
Posture** section in `CLAUDE.md` before changing any of it.

## Database

The canonical schema is **`desktop/migrations/001_initial.sql`** — the single
source of truth for a fresh install (all types, tables, indexes and triggers).
The backend's migration runner applies it on the node's first start — Docker
and launcher alike — and the deltas after it. Highlights:

- **Numbered deltas on top of the baseline**: `001_initial.sql` is the
  readable source of truth for a fresh install, and every change also ships as
  an idempotent `NNN_<change>.sql` that existing nodes apply on start.
- **Normalized multi-source metadata** (`artist_bios`, `artist_tags`,
  `similar_artists`, `album_descriptions`, `lb_recording`) with a `source` column or
  a dump version for provenance — not JSONB blobs.
- **`media_files.id` identifies a FILE, `tracks.id` (UUID) identifies the
  MUSIC** — everything above the file speaks the UUID, which is what makes
  not-owned music playable through the same API. A single-file rip with a
  `.cue` sheet is N rows on one file, each bounded by its start/end offsets.
- **Deterministic UUID v5** for all shareable entities (Artist, Album, Track,
  Genre, Tag, EmbeddingModel) so the same data on different nodes collapses to
  the same ID. Namespace `adc1ec0b-2c81-5e26-9938-a369c6f7a5e1`.
- **Albums have no `artist_id`** — artists derive via `track_artists`, so
  compilations/features/collaborations work without nullable FKs.
- **`ON UPDATE CASCADE`** on track/album UUID FKs so artist-name normalization
  can safely rewrite UUIDs.

Backups are a product feature, not a hand-run `pg_dump`: `docker exec
sautium-backend python -m backup create --password-env P2P_PASSWORD` writes
`./data/backup/sautium-backup-<node>-<date>.sbk` (the launcher has the same
under Settings & Tools › Backup & Restore), and

```bash
docker compose stop backend
docker compose run --rm --no-deps backend python -m backup restore /app/data/backup/<file> --replace --identity
docker compose start backend
```

rebuilds the node from it (`--db music_ai_test` restores beside the live
database; `python -m backup selftest` round-trips and compares row counts).
The `mb_*` tables come back empty — the MusicBrainz dump loader refills them.
Share exports (`python -m backup export`) go to `./data/export/`.

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
  LAN IP (the Host guard answers 421 to any address that is not the node's
  own), then `docker restart sautium-backend`.
- **`Claude Code error` on AI queries** — the mounted `~/.claude` credentials
  are stale or missing. Use the Compose variant that matches where you ran
  `claude /login` (Windows vs WSL).

## Documentation

| File | Purpose |
|------|---------|
| `CLAUDE.md` | Project spec, architecture rules, conventions, security posture |
| `PROGRESS.md` | Design decisions and lessons learned (non-P2P) |
| `P2P_NETWORK.md` | P2P architecture, technology choices, security model |
| `SECURITY.md` | The public security model: surfaces, threat model, accepted limits |
| `docs/AUDIT.md` | "Audit it yourself": the agent prompt that checks the tree against the security model |
| `THIRD_PARTY_NOTICES.md` | Components, models, services and their licences |
| `docs/README.md` | Index of everything under `docs/` |
| `docs/design/POSITIONING.md` | Product positioning + UI design principles |
| `docs/design/INFORMATION-ARCHITECTURE.md` | Navigation model + screen inventory |
| `docs/design/DISCOVERY-SEARCH-ENGINE.md` | The search engine: tools, sources, bridges, corpus |
| `docs/design/PHANTOM-DISCOVERY.md` | Music beyond the local catalog |
| `docs/design/P2P-SYNC-INTEGRITY.md` | Provenance, seals, recompute-as-detector |
| `docs/design/BACKUP.md` | Backup, share export/import, life-data merge |
| `docs/design/HARDWARE-TIERS.md` | Resource map + the `full/standard/lite` profiles |
| `docs/design/GEAR-ADVISOR.md` | Audio-chain analysis and upgrade strategy |
| `docs/HQPLAYER_INTEGRATION.md` | HQPlayer integration + knowledge base |

## License

[PolyForm Noncommercial License 1.0.0](LICENSE) — free for noncommercial use.

## Acknowledgments

- **LAION** for the CLAP model
- **BAAI** for BGE-M3
- **Google** for MADLAD-400, the translation model behind non-English queries
- **Anthropic** for Claude and Claude Code
- **MetaBrainz** for MusicBrainz, ListenBrainz and the Cover Art Archive
- **pgvector** for efficient vector search in PostgreSQL
- **libtorrent** for the DHT layer

Full attribution, including every service and its terms, is in
`THIRD_PARTY_NOTICES.md`.
