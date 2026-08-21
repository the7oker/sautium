# Sautium — Progress

Short design log. **Why** things are the way they are — state, numbers and
implementation details live in the code, DB and git history.

---

## Phases

| Phase | Scope | Status |
|-------|-------|--------|
| **P1** | Docker env, scanner, CLAP embeddings, audio similarity search, Claude integration | DONE |
| **P2** | Last.fm enrichment (bios, tags, similar, track stats, album wiki), text embeddings (BGE-M3), enhanced RAG | DONE |
| **P3.1** | Audio feature extraction (librosa + CLAP zero-shot, no essentia) | DONE |
| **P3.2** | HQPlayer control via XML protocol (port 4321) | DONE |
| **P3.3** | MCP server for HQPlayer + PostgreSQL + search (22 tools) | DONE |
| **P3.4** | Windows desktop launcher (CustomTkinter + PyInstaller) | DONE |
| **P2P-P0..P4** | Launcher↔backend bridge, UUID v5 refactor, P2P sync, NAT traversal, account system, E2E chat, email CA | DONE — see `P2P_NETWORK.md` |
| **P4 voice** | Whisper input + TTS output + voice conversation loop | TODO |
| **P5 files** | BitTorrent file sharing for legal content (CC, indie) | TODO |

---

## Design Decisions

### Architecture & data model

- **Normalized multi-source metadata**. Last.fm/Spotify/MusicBrainz data lives
  in separate normalized tables (`artist_bios`, `artist_tags`, `similar_artists`,
  `album_info`, `track_stats`), not JSONB blobs. Allows per-source re-fetch and
  provenance tracking. First iteration used JSONB in `external_metadata`, that
  was scrapped because PostgreSQL functions on JSONB get unreadable fast.
- **Canonical UUID v5 for shareable entities**. Artist/Album/Track/Genre/Tag/
  EmbeddingModel all use `uuid5(NAMESPACE, "entity:{normalize(...)}")` so the
  same data on different nodes collapses to the same ID. Namespace is
  `adc1ec0b-2c81-5e26-9938-a369c6f7a5e1` (in `backend/uuid_utils.py`).
- **Album has no artist_id**. Artists derived via `track_artists` — handles
  compilations, features, collaborations without awkward joins or nullable FKs.
- **Genre is a track property, not album**. Many-to-many via `track_genres`.
  One album can span multiple genres track-by-track.
- **`ON UPDATE CASCADE` on all track/album UUID FKs**. Artist normalization
  rewrites UUIDs when cleaning names — cascade makes this safe without custom
  migration scripts.
- **ENUM over VARCHAR+CHECK** for `artists.gender` and `artists.is_vocalist`.
  Type-level enforcement, smaller on-disk footprint, self-documenting schema
  (`\d+ artists` shows allowed values).

### Embeddings & search

- **CLAP (laion/clap-htsat-unfused) for audio**, 512-d, middle 30s of each
  track for consistency. Batched on GPU.
- **BGE-M3 for text embeddings** (1024-d, multilingual). Switched from
  all-MiniLM-L6-v2 after multilingual queries broke. Text is composed from
  ALL available metadata per track (tags, bios, genres) in one SQL query
  with JOINs and LATERALs.
- **Hybrid search default: 70% text + 30% audio**. Text captures semantic/
  conceptual similarity, audio captures sonic. Tunable per query.
- **Retrieval floor: 0.3 min_similarity, cap at 30 tracks for context**. Wide
  pool, let Claude decide. Higher thresholds caused false "nothing found".
- **Subtle popularity boost (15%, log-scale)**. Listeners range from 6 to 300k+
  → power-law distribution → log normalization. Without boost, obscure tracks
  dominate by chance; with >15% boost, popular tracks crowd everything else.

### Audio analysis

- **No Spotify**. Audio Features API deprecated Nov 27, 2024 for new apps.
  Replaced with own pipeline: librosa (tempo, spectral, MFCC) + CLAP zero-shot
  (instruments, moods, danceability).
- **CLAP zero-shot instead of essentia**. essentia brings TensorFlow dependency
  and trained models; CLAP already loaded, works out of the box with text
  prompts ("energetic rock song", "ambient pad"). Simpler, no TF.
- **Two sample rates per track**: 22kHz for librosa (sufficient for DSP,
  faster), 48kHz for CLAP (model requirement). Load once at 48k, downsample
  for librosa.
- **Vocal detection thresholds**: >0.65 vocal, <0.35 instrumental, else mixed.
  Known unreliable — for vocal/instrumental queries use `artists.is_vocalist`
  (classified from Last.fm bio keywords).
- **Track-by-track incremental enrichment**. Each step checks if data exists,
  only runs if missing. Resumable without losing progress. Idempotence is a
  **correctness property, not an optimization** — enrichment re-runs must be
  safe.

### AI DJ assistant

- **Claude Code + MCP tools, not custom RAG**. Earlier version had a 557-line
  `assistant.py` with multi-source retrieval, hybrid search, enrichment pipeline,
  popularity re-ranking. All ripped out. Claude Code with PostgreSQL + HQPlayer
  MCP tools writes SQL directly — more flexible, less code, traceable through
  MCP logs. Main trade-off: each query costs tokens for SQL, but quality is
  higher and maintenance drops.
- **Cyrillic queries translated via Haiku**, not transliteration. "шульце"
  → "shultse" (lossy) vs "Schulze" (correct). Algorithmic transliteration
  destroys proper names and grammatical cases ("від шульца" = genitive). Haiku
  adds ~$0.001 and ~0.3s per Cyrillic query; non-Cyrillic queries bypass
  translation entirely.
- **The chat stream reconnects; it never reports a dead socket as a failure.**
  A phone that sleeps mid-generation loses the TLS connection, and the reader
  surfaces that as `TypeError: network error` — which used to be printed in
  the thread while the finished reply sat in the DB unseen. Server-side
  keepalives don't help: they keep proxies from timing the connection out,
  but a client whose socket died silently never learns anything. The fix
  leans on `_LiveRun`, which already outlives its HTTP connection and replays
  its full history to any later subscriber: a transport drop now reattaches
  to `/stream` and repaints the same bubble (404 → the run finished, load the
  row from the DB). Only the backend speaking — `provider_error` or an
  `error` event — is fatal. Reconnects escalate (1/3/9/20s) because
  `navigator.onLine` stays true through most real drops, so there is no
  event to wait on and same-tick retries all burn before the network returns.

### HQPlayer integration

- **XML protocol over TCP port 4321**. HQPlayer Desktop API is simple
  request/response. No auth for local control.
- **Path translation**: Container `/music/...` → Windows `E:/Music/...` before
  sending to HQPlayer. Both sides see the same files through different mount
  points.
- **2s delay after playlist load, 1s after track selection**. HQPlayer needs
  time to process. Skipping the delays caused random "track not found" errors.
- **Stop-clear-add-select-play sequence** for `play_track`/`play_album`. Without
  explicit stop, HQPlayer occasionally started the wrong track from the
  existing queue.

### MCP server

- **Outside Docker**. Claude Code spawns MCP servers as child processes — must
  be on the WSL2/native host, not inside a container. All heavy work is
  delegated to the Docker backend via HTTP.
- **Lazy connections** (HQPlayer / DB / backend). Connect on first use,
  auto-reconnect on failure. A cold MCP startup is fast; only the first tool
  call pays the connection cost.
- **Dual fuzzy matching** (pg_trgm threshold 0.15 + ILIKE). Trigrams handle
  misspellings, ILIKE handles exact substrings. Single strategy left too many
  holes.

### Account & chat (P4)

See `P2P_NETWORK.md` — "Account System" and "Security Considerations" sections
for the full rationale (Argon2id, NaCl Box, mutual invites, Worker as CA,
TLS).

The short version of the hard-learned lessons:
- **Event-driven everywhere**, polling is banned in new code. DB polling for
  chat cost ~8s latency — unacceptable. SSE + direct HTTP push is the default.
- **Persistent DB connections in long-lived services**. `ChatService` opened
  a new connection per call and cost 2 seconds per message. Connection reuse
  is the default now.
- **Mutual invite exchange, enforced in code not docs**. The initial "design
  decided, impl pending" state was a real vulnerability — a leaked invite code
  granted friendship. Now both sides must add each other before the handshake
  completes.

---

## Known Gotchas

- **A dead SSE socket is silent, and painting its death is a UI lie.** Two
  distinct failure modes, both hit by phones: (1) a socket that dies without
  a FIN leaves `reader.read()` pending forever — every backend generator
  keepalives at 15-20s, so `sseStream` (auth.js) cancels the reader after
  45s of byte-level silence and reconnects; a frozen tab freezes that timer
  too, so on wake it fires immediately, which is exactly right. (2) A
  transport error says nothing about playback — the music keeps playing on
  the renderer — so player.js holds the last known state for a 10s grace
  before dispatching `disconnected` (mp.update treats that state as
  "nothing playing" and hides the bar). A successful reconnect always
  delivers a status message (the stream pushes current status on connect),
  which cancels the pending paint — so the paint fires only when the link
  has genuinely been down the whole window.
- **libtorrent 2.1+ `peers()`** returns `(ip, port)` tuples, not objects with
  `.address()/.port()`. Compat handling in `dht_service.py`.
- **libtorrent DHT alerts** require `alert_mask += dht_operation_notification`
  — without this flag `dht_get_peers_alert` is silently not generated.
- **libtorrent `dht_announce()`** in 2.0.11 Python bindings takes 3 args
  (sha1, port, flags=0) — flags parameter is required.
- **libtorrent on Windows** needs OpenSSL 1.1 DLLs — `libtorrent-windows-dll`
  PyPI package auto-installed at launcher startup.
- **`regexp_count`** needs double-escaped word boundaries in Python strings
  (`\\yword\\y`) but single-escape in raw SQL files (`\yword\y`).
- **Bulk COPY into indexed tables is the slow path.** The MB dump loader
  drops every index + PK/UNIQUE constraint before each table's COPY and
  rebuilds after (sorted build ≫ per-row maintenance; the trigram GINs are
  the worst offenders). TRUNCATE+COPY share one transaction so
  `wal_level=minimal` (set in docker-compose + launcher db_init) skips WAL
  for the bulk write; a DDL snapshot next to the archive makes a crash
  recoverable. Rebuild sets `maintenance_work_mem=1GB` +
  `max_parallel_maintenance_workers=4` per statement via SET LOCAL.
- **PostgreSQL ENUM type changes** require drop default → drop constraint →
  `ALTER TYPE USING col::new_type` → set default. Straight `ALTER TYPE` fails.
- **yt-dlp is a perishable dependency.** YouTube changes its side every few
  weeks; upstream's *stable* channel lags and breaks (2026-08-18: every
  download 403'd on stable 2026.07.04 — the android_vr client was killed —
  while nightly already carried the fix). Both runtimes track **nightly** and
  refresh from ONE place — `streaming/service.py:ytdlp_refresh_loop`, which
  every runtime gets because every runtime runs this backend: `pip install -U
  --pre "yt-dlp[default]"` against its own interpreter at start, once a day
  after that, and on demand when a download fails the way a stale build fails
  (403 / signature — narrow on purpose, rate-limited to one pip run per 6 h).
  Start-only refreshes were not enough: this node runs `restart:
  unless-stopped` for weeks, the launcher's own update check is start-only
  too, and `docker-compose.mac.yml` bypasses `entrypoint.py` entirely. The
  provider invokes `<that interpreter> -m yt_dlp`. Standalone binaries were tried and dropped:
  the Windows onefile burns 0.95 s per run unpacking itself into `%TEMP%`, the
  ONEDIR zip reports variant `win_exe` so its own updater replaces it with the
  onefile, and brew's macOS formula is stable-channel and cannot self-update at
  all. **deno** ships as the sandboxed JS runtime for the player-challenge
  solver — the runtime-less extraction path is deprecated upstream and is the
  one that breaks. A node with a working Deezer plugin masks a dead YouTube
  provider: grep the log for `preview fetch failed … via youtube`.
- **Time to first sound is not dominated by the network.** Measured on the
  Windows launcher, 4.5-min YouTube track: 6.1 s before, 2.6 s after, and only
  ~1.5 s of that was ever transfer. The rest was a resolve pass repeated
  because the album page threw its result away (see `_chain_cache`), a
  PyInstaller onefile unpacking itself on every yt-dlp invocation, and a
  single YouTube connection shaped well below the link (`formats=dashy` +
  concurrent fragments, byte-identical output). What remains is structural:
  the provider downloads and transcodes a whole track before the proxy serves
  a byte. Below ~2.5 s means a progressive pipeline (yt-dlp → ffmpeg → proxy
  with a growing buffer), which the HTTP layer, the pre-buffer policy and the
  CLAP hook are all written against — and whose real unknown is HQPlayer's
  HEAD/Range behaviour without a Content-Length.
- **streamrip `--no-db` disables only the downloads db.** The Deezer plugin's
  config template carries the dump node's `/root/.config/streamrip/*.db`
  paths; the failed-downloads db still opened at that path → "unable to open
  database file" on every rip from the Windows launcher, reported as a bare
  `rc=1` because streamrip writes its tracebacks to STDOUT (rich). The plugin
  now switches both dbs off in its per-process config and reads stdout.

---

## References

- `CLAUDE.md` — project spec, tech stack, phase definitions
- `P2P_NETWORK.md` — P2P design decisions and architecture
- `git log` — what was done, when, by whom
