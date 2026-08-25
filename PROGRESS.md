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
- **CUE images import as virtual tracks, not split files**. An EAC/XLD rip
  (one .ape/.flac/.mp3 image + .cue) becomes N `media_files` rows sharing one
  `file_path`, bounded by `cue_start_seconds`/`cue_end_seconds` (NULL for
  regular files; `UNIQUE NULLS NOT DISTINCT (file_path, cue_start_seconds)`).
  Because each slice is its own row, playback keying, analysis-source
  election, tracking and browser identity all worked unchanged. Two rules
  carry the design: *the cue governs the image* (scanner reconciliation
  supersedes a legacy whole-image row AND its whole-image analysis — no
  embeddings-spare there, unlike prune), and *a slice is always consumed as
  its own resource* — local engine decodes with `-ss/-t`, every HTTP consumer
  (HQPlayer, DLNA, browser) gets a cached tagged FLAC cut
  (`transcode.flac_slice_path_for_file`, Opus tiers chain off the cut), so
  positions are track-relative everywhere and the tracker never learned CUE
  exists. Sheet defects are tolerated: FILE extension/casing lies resolve
  against the real dir listing, encodings fall back utf-8-sig→cp1251→cp1252,
  multi-FILE cues (already-split rips) are skipped whole. pcm_hash/chromaprint
  are computed per-slice, so a slice's content address equals a properly split
  rip of the same disc — cross-rip P2P anchors converge; grid stays v1
  (windows are material-relative).

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

### AI assistant

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
- **OpenAI Codex CLI as the second chat agent** (2026-08-23,
  `backend/codex_runner.py` + `codex_cli.py`, provider id `codex`). Mirrors
  the Claude runner shape (150s watchdog, stderr drain, non-root demote,
  same StreamEvent contract) with every difference forced by the CLI:
  no `--system-prompt` → assistant prompt is a file in a Sautium-owned
  workdir passed as `model_instructions_file`, which REPLACES codex's
  built-in coding-agent prompt (the `--system-prompt` analog) and is
  re-read on every spawn, `exec resume` included (measured 2026-08-25);
  volatile player context prefixes the user message so the instructions
  prefix stays cacheable. Until 2026-08-25 the prompt rode as AGENTS.md
  and every turn after the first ran WITHOUT it: `exec resume` takes no
  `--cd`, the process cwd fell back to the backend's (the repo, on the
  launcher), and codex told the model the AGENTS.md instructions "no
  longer apply" — the launcher's "codex only searches MusicBrainz when
  asked" complaint was a prompt-less turn 2. Every spawn now pins Popen
  cwd to the workdir. Same day: MCP tools forced DIRECT via
  `features.code_mode.direct_only_tool_namespaces=["mcp__<server>"...]` —
  codex 0.149 otherwise defers every MCP tool behind its code-mode
  `exec` JS host, where the model greps `ALL_TOOLS` by regex before it
  can call anything (the ENABLE_TOOL_SEARCH=false analog); reasoning
  effort `medium` — at `low` terra ran the tag SQL and gave up without
  naming one candidate from its own knowledge, at `medium` it named
  three and verified them in a single parallel mb_resolve round (+4s);
  agent SQL is capped per statement (30s: `PGOPTIONS=-c statement_timeout`
  on Docker's `@modelcontextprotocol/server-postgres`, explicit
  `DB_QUERY_TIMEOUT` on the launcher's `postgres-mcp-server`) — an OR'd-EXISTS
  join across every tagged artist ran 3+ minutes, ate the whole wallclock and,
  after the SIGKILL, kept running under an orphaned MCP server; inside the
  turn the same failure is a tool error the model retries lighter;
  no `--mcp-config` → the
  SAME mcp-docker/windows.json is translated at spawn into dotted `-c
  mcp_servers.*` overrides + `--ignore-user-config` (analog of
  `--strict-mcp-config`); no `--disallowed-tools`, and no
  sandbox either — under ANY sandbox mode `codex exec` auto-denies every
  MCP tool call ("requires approval, but approval policy is never";
  measured on macOS Seatbelt, upstream openai/codex#24135), so the
  dangerous bypass is mandatory, not a fallback — and it flips codex's
  default `web_search` to LIVE, so the runner sets `web_search="disabled"`
  explicitly. Actual fences: `--disable shell_tool` (verified — the model
  has no shell), `--disable plugins/apps/multi_agent/tool_suggest/goals/
  image_generation` (each measured to remove roster or prompt noise — the
  "plugins available but not installed" list alone was 3 KB of Spotify /
  Apple Music bait per session), prompt-level prohibition. Known residual: the
  apply_patch file tool has no off switch (feature flag unknown,
  include_apply_patch_tool=false inert — both measured) and stays
  reachable behind the prompt fence. Auth is auth.json-ONLY — a bare
  OPENAI_API_KEY env is ignored by the CLI (measured, 0.149), so the
  runner mints auth.json via `codex login --with-api-key` when needed,
  and POPS the env keys when auth.json exists (mirror of the
  ANTHROPIC_API_KEY strip: billing must not silently leave the
  subscription). Resume handle: `chat_sessions.codex_thread_id` next to
  `claude_session_id` — per-agent columns, switching providers
  mid-session resumes each agent's own thread. No sessions sub-mount for
  `~/.codex` (unlike claude_sessions): codex indexes threads in sqlite
  files at the .codex root, splitting `sessions/` onto a project volume
  would desync index from rollouts. Transient `error` JSONL events
  (websocket reconnects) are NOT failures — only `turn.failed` is
  authoritative. Known gap vs Claude: codex has no `system/init` MCP
  health report, so a dead tool server can't be caught pre-answer yet
  (stderr is scanned and logged loudly instead).

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

### Desktop packaging (macOS)

- **The .app is a carrier, not a frozen launcher.** `desktop/build_macos.py`
  ships a private CPython (python-build-standalone, Tk included) plus a
  snapshot of the git-tracked tree; `bootstrap.py` installs both into
  `~/.local/share/Sautium` on first run and execs the launcher from there.
  PyInstaller was the obvious answer and the wrong one: the launcher's job is
  to PROVISION and RUN a Python — pip-installing torch, spawning uvicorn and
  the MCP server — and inside a frozen bundle `sys.executable` is the bundle,
  `get_project_root()` is `Contents/MacOS`, and there is no `backend/` to run.
  Installing into a writable copy also means the launcher keeps the dev-mode
  shape it was written for, so the bundle needed zero launcher changes.
- **Nothing may write inside the bundle.** A venv keeps its stdlib in the base
  prefix, so a venv built against the bundled runtime had the launcher, the
  backend and every child process importing — and bytecode-caching into — the
  signed app: 46 `.pyc` files after one run, and `spctl` then reports "a sealed
  resource is missing or invalid". The runtime is therefore copied out to the
  data root, and the stub execs `python3 -B`. Same reason an app in
  `/Applications` must be treated as read-only.
- **The payload is `git ls-files`, read from the working tree.** Tracking is
  the filter — everything a build must not ship (`backend/data/.api_secret`,
  the maintainer's `mcp-windows.json`, pgdata) is already gitignored — while
  the bytes come from disk so an uncommitted fix still reaches the DMG. A name
  sweep over the staged payload fails the build if that ever stops holding.
- **macOS has no tray to minimise into.** pystray's AppKit backend wants the
  main thread, which Tk owns, so on darwin the close button only hides the
  window and the Dock tile takes over the role: `::tk::mac::ReopenApplication`
  brings it back, and `::tk::mac::Quit` routes Cmd+Q into `_quit` so
  PostgreSQL and the backend are stopped rather than orphaned.
- **Homebrew stays the macOS dependency.** PostgreSQL 18 + pgvector, ffmpeg,
  flac, fpcalc and deno all arrive through it (`db_init`), so the bundle asks
  for brew once — command ready to paste — instead of carrying relocated
  dylibs it would then have to keep patched.
- **The dependency install got an hour, not ten minutes.** The first backend
  start pulls ~1.3 GB of wheels; a 600 s cap is a guess about the builder's
  link speed, and when it expires the node has no backend at all.
- **The packaged tree is a clone, so updates are the launcher's own.** The
  bootstrap clones `main` into the data root instead of copying the bundle's
  snapshot (which stays as the offline fallback), because the update path it
  needs already exists and is exercised daily: pull, reinstall changed
  requirements, run new migrations, restart the backend. A clone is then left
  alone by every later DMG — a disk image must not roll a node back to whatever
  snapshot it happens to carry. The cost is deliberate and accepted: an install
  tracks `main` with no release branch between it and a work in progress.
- **A packaged install cannot pull, and no longer pretends to.** The unpacked
  tree is not a checkout, so the update check returned "no updates" and the UI
  said "You're up to date!" — a guess worn as a fact. Worse, `is_git_repo()`
  asked `--is-inside-work-tree`, which also answers for the nearest repository
  ABOVE the directory, and the tree now lives under `$HOME` — which plenty of
  people keep in git for dotfiles. It compares `--show-toplevel` against our
  own root now, "Check for Updates" becomes "Refresh Registries" (the other,
  real half of that button), the window title carries the build id, and the
  startup check does not shell out to `git` at all — on a Mac without the
  command line tools, invoking it pops the Xcode installer. New builds arrive
  as a new DMG.

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
- **`SetThreadExecutionState` is per-thread and dies with its thread.** The
  Windows sleep inhibition (`utils.keep_awake`) therefore parks a dedicated
  thread for the length of the hold; firing it from whichever worker called
  `start_all` would have evaporated the moment that worker returned —
  measured: after a thread that set ES_SYSTEM_REQUIRED exits, the flag is gone
  from the caller's state. macOS has the opposite shape: `caffeinate -w <pid>`
  outlives us on purpose and lifts on a hard kill. Use `-i`, never `-s`: `-s`
  is scoped to AC power by macOS, and a charge limiter (AlDente and friends)
  discharges the battery while the charger is plugged in — the system then
  reports battery power and the assertion silently stops applying, which is
  the worst possible failure direction. Neither API blocks deliberate sleep,
  and neither takes a display assertion.
- **A browser plays what a gesture allowed, not what the server asked for.**
  Media elements are activated by a `play()` inside a user gesture; ours
  arrives seconds later over SSE, after the provider fetch. So the first
  track of a fresh browser profile is refused, the renderer reports paused,
  and the tracker files a 0% skip for a track nobody heard — then a manual
  tap fixes it permanently (the element stays activated, and the browser's
  per-origin engagement score keeps growing). It is a first-impression bug by
  construction: it cannot be reproduced once it has been hit, only on a fresh
  profile. `maybeClaimRenderer()` therefore spends the gesture on a 45-byte
  silent clip; transport handlers ignore events while that clip is the src.
- **An `Image()` preload cannot be cancelled; an `<img>` src assignment
  cancels itself.** The mini-player preloads covers through a detached
  `Image()` so a 404 leaves the gradient placeholder standing — but nothing
  aborts that probe when the track changes, so a slow one (phantom art comes
  from an outside host, and a CAA 404 sends the chain on to a second URL)
  lands later and paints the previous cover over the new track. It carries a
  generation now. The Now Playing sheet paints into a real `<img>`, where
  assigning `src` aborts the load in flight — same task, no race, and the
  difference is the reason one of them needed fixing.
- **A name is not an identity — Deezer ranks namesakes by nothing useful.**
  `search/artist?q=vangelis` returns three artists called exactly "Vangelis",
  and the one it puts FIRST has one album and 20 followers while the composer
  has 68 and 209k — so `limit=1` put a stranger's face on the artist page.
  Artist-photo lookup now sends the album titles we credit to the artist and
  keeps the candidate whose catalogue matches most of them; followers only
  break a tie nothing else could. The titles must be CANONICAL to be worth
  sending: our rows are editions, and seven of them are one record ("Blade
  Runner (Esper Edition MK2)", "(Trilogy, 25th Anniversary)", "(Deck Art-765
  Limited Edition)"…) under names no catalogue carries — so the context
  collapses to the release group (`albums.musicbrainz_id` IS the RG id) and
  sends the group's shortest title. Matching still strips edition baggage
  from BOTH sides: Deezer has its own ("Blade Runner (Music From The Original
  Soundtrack)"), so plain containment fails in both directions. Deezer's advanced query
  `artist:"X" album:"Y"` looks like the shortcut and is a trap: it answers
  with other artists' COVERS of that album (John Beal for "Vangelis / Blade
  Runner"), trading a wrong namesake for an outright wrong artist. Photos are
  pinned once and never re-resolved, so anything resolved before this stays
  wrong until its `artists.photo_cover_id` is cleared.
- **Content hashing dedups files, not refetches.** `covers` keys on a BLAKE2
  of the bytes, which is exact for a file on disk and useless for anything
  pulled over a CDN: Deezer re-encodes on the fly, so the same artist photo
  arrived at 194,650 bytes and then 196,274 — a new row per refetch, the
  previous one orphaned, and nothing collects orphans. One pass over the
  library left 1,560 of them (153 MB). The perceptual hash is identical
  across that re-encode (the column and its index already existed; nothing
  queried them), so ingest now dedups on `(perceptual_hash, source_path)` —
  scoped to one asset so a refetch collapses while two different images that
  happen to hash alike, as album editions differing by a sticker can, stay
  apart.
- **A credential has to name what it grants access to.** The device token was
  `HMAC(secret, "sautium-device:v1:{epoch}")` — and neither input belonged to
  the node. The secret lived in `backend/data/` INSIDE the checkout (on the
  mac test node it was dated May 3 while the node's data dir was minted that
  morning), and the epoch lives in the node's own database, so a fresh node
  reset it. Delete a node, create another — new account, new identity, new
  database — and every browser paired with the old one authenticated against
  the new one on a page refresh. The same file also served BOTH nodes on this
  machine, since compose bind-mounts `./backend` into the container; only
  mismatched epochs kept their tokens apart, by luck. Now the node's public
  key is part of the derivation and the secret sits beside the identity
  (`<p2p_identity_dir>/.api_secret`), which every runtime already treats as
  node state and every uninstall already deletes. Readers: `main`,
  `media_urls`, `desktop/api_client`, `mcp/assistant_server` — the MCP one is
  easy to miss and breaks the AI assistant silently.
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
