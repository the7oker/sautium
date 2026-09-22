# Sautium — Progress

Short design log. **Why** things are the way they are — state, numbers and
implementation details live in the code, DB and git history.

---

## Phases

| Phase | Scope | Status |
|-------|-------|--------|
| **P1** | Docker env, scanner, CLAP embeddings, audio similarity search, Claude integration | DONE |
| **P2** | Last.fm enrichment (bios, tags, similar, album wiki), text embeddings (BGE-M3), enhanced RAG | DONE |
| **P3.1** | Audio feature extraction (librosa + CLAP zero-shot, no essentia) | DONE |
| **P3.2** | HQPlayer control via XML protocol (port 4321) | DONE |
| **P3.3** | MCP server for HQPlayer + PostgreSQL + search + gear (41 tools) | DONE |
| **P3.4** | Desktop launcher (CustomTkinter; Windows installer + macOS bundle as carriers) | DONE |
| **P2P-P0..P4** | Launcher↔backend bridge, UUID v5 refactor, P2P sync, NAT traversal, account system, E2E chat, email CA | DONE — see `P2P_NETWORK.md` |

---

## Design Decisions

### Architecture & data model

- **Normalized multi-source metadata**. Last.fm/MusicBrainz data lives
  in separate normalized tables (`artist_bios`, `artist_tags`, `similar_artists`,
  `album_descriptions`, `lb_recording`), not JSONB blobs. Allows per-source re-fetch and
  provenance tracking. First iteration used JSONB in `external_metadata`, that
  was scrapped because PostgreSQL functions on JSONB get unreadable fast.
- **Canonical UUID v5 for shareable entities**. Artist/Album/Track/Genre/Tag/
  EmbeddingModel all use `uuid5(NAMESPACE, "entity:{normalize(...)}")` so the
  same data on different nodes collapses to the same ID. Namespace is
  `adc1ec0b-2c81-5e26-9938-a369c6f7a5e1` (in `backend/uuid_utils.py`).
- **`normalize` v2 folds typography (2026-08-25)**. The identity key drops
  apostrophe-like marks and turns every other non-word character into a
  space (`Hello Dolly!` = `Hello Dolly`, `See - Line` = `See-Line`,
  `Don’t` = `Don't` = `Dont`); a name that folds to nothing (`!!!`, `†††`)
  keeps its key form. Measured before the change: of 1701 pairs where an
  owned track and an MB-minted phantom shared a recording under two uuids,
  197 differed by punctuation alone. Identifier-like keys (embedding model
  names, gear spec attribute keys, registry sources) stay on `normalize_key`
  — their punctuation is structure. A formula change is an identity
  migration, never a code-only change: `canon.migrations --renormalize`
  rewrote 1.35M track uuids, merged the collisions (survivor = whoever holds
  files/analysis/listens), shed every seal (payloads bind the uuids) and
  `sign_audio` re-sealed in one batch; the scanner minting on one rule next
  to rows on the other would fork every entity.
- **Phantom tracklist slots carry the TRACK's artist credit**. The MB mint
  keys each slot the way the scanner keys a tagged rip: title + the track's
  own credit (`mb_artist_credit.name`, join phrases included), not the
  album's artist — a "Various Artists" phantom keyed on the compilation
  could never collapse with a rip (530 same-recording pairs on the master).
  Credits that are not the album artist get a name-only artist row, no MB
  anchor: an anchor would enrol them in the discography re-derive, and a
  compilation's fifteen credits times their discographies is the fan-out the
  engagement rule forbids. Per-artist fan-outs (Last.fm bios included, since
  the same day) gate on engagement, never on "has a track" — a dump node
  minted ~250k such stubs. A slot that moves to another uuid carries its
  analysis/listens/files along (`_update_track_uuid` rename-or-merge).
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
  multi-FILE cues (already-split rips) are skipped whole. The chromaprint is
  computed per-slice, so a slice's content address equals a properly split
  rip of the same disc — cross-rip P2P anchors converge; grid stays v1
  (windows are material-relative).
- **The chromaprint is the only content address (2026-09-18)**. Until then
  `analysis_sources` carried two anchors: a BLAKE2b hash of the decoded PCM
  — the key, and a possession proof of exact bytes that changed with every
  decoder build and every lossy decode, so two nodes analysing the same
  stream addressed two materials — and the AcoustID fingerprint beside it.
  One anchor, and one that converges: the fingerprint is a public recording
  identity any decode reproduces. Its ~3 KB of text is too long for a btree
  row, so the key is a stored generated digest (`chromaprint_key`). The
  audio record payload went to v3 (enrichment records stay v2 — no material
  hash in them, so those seals survive); every audio seal was re-made,
  foreign audio analysis deleted (its authors' v3 seals return over the
  network), the seed bundle re-exported as v2 — and moved out of git: a
  bundle is a 17 MB artifact regenerated on every re-sign, and two of them
  made up ~90 % of the repository, so it is a GitHub Release asset now,
  fetched on a node's first start and checked against the sha256 that
  `backend/seed/bundle.json` pins (the history was rewritten once to shed
  the two committed copies). Material under one grid window (10 s) is not
  analysed at all: fpcalc yields nothing for a few seconds of audio, and
  no address means no analysis worth saving.
- **A hardware profile governs compute, never retention (2026-08-27)**. The
  lite tier used to auto-delete the phantom album/track layer after three
  consecutive lite boots (`hardware.lite_streak`, shipped 2026-07-10 as a
  disk saving for small machines). The trigger measured the wrong thing:
  `lite` means "no CUDA", not "small disk" — a 32-core / 16 GB laptop
  switched to iGPU-only mode would have lost 3.09M phantom tracks and 288k
  albums on its third start, and on a genuinely lite node the deletion is
  one-way (re-minting needs the MB dump it cannot hold). Now the layer is
  the owner's setting (`discovery.phantom_layer`, default on everywhere —
  off stops minting and keeps what exists) and removal is the explicit,
  confirmed "Remove phantom layer" job in Settings › Library. Nothing
  tier-driven deletes rows.

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
- **Subtle popularity boost (15%, log-scale)** — historical: the boost was
  ripped out of search later, and since 2026-09-20 popularity is a
  ListenBrainz rank, not a Last.fm figure. Listeners range from 6 to 300k+
  → power-law distribution → log normalization. Without boost, obscure tracks
  dominate by chance; with >15% boost, popular tracks crowd everything else.

### Audio analysis

- **No catalog audio-features API**. The industry one closed to new apps on
  Nov 27, 2024. Replaced with own pipeline: librosa (tempo, spectral, MFCC) +
  CLAP zero-shot (instruments, moods, danceability).
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
- **Manual run = analysis only; network enrichment lives in the background
  loop (2026-09-05).** "Analyse Library" (launcher) / "Analyse library"
  (More → Library) run audio embeddings + features, seal them, then the text
  encoders. Last.fm bios/stats/similars and lyrics are fetched only by
  `background_enrichment.py`, on every profile — lite's "manual button"
  exception went with the split. Its network steps drain a backlog
  pass-to-pass (a full batch means more behind it; a short batch, or one
  whose failed rows stay queued, is the signal to sleep out the 30-min
  interval — the failure taxonomy of 2026-09-22 below), the DB-only
  steps stay on the timer. One job per kind of work: the GPU run
  is bounded by the library, the network trickle by per-call API delays,
  and neither waits on the other. A node with no ML runtime has no button
  at all — launcher disabled, web hidden, endpoint 400 — because the run
  would be a no-op there (`/stats` → `analysis_available`).
- **Enrichment followed the sync (2026-09-05 → 2026-09-22).** While bios,
  stats and similars travelled between nodes, the background loop's first
  pass waited (≤10 min) for the first P2P sync and every later
  `sautium_sync_done` woke a pass: peers filled a fresh library's gaps for
  free, the API pass fetched the rest. The Last.fm layer became node-local
  on 2026-09-19 (migration 019) and a sync now imports only audio analysis
  and track anchors — nothing the loop's network or model steps consume —
  so the wait and the wake went on 2026-09-22: the loop runs on its 30-min
  interval, its own drain, the playback falling edge and the canon wake.
  The sync itself runs on events — first source after start, a scan that
  added files, a LAN peer appearing, the interval — and the manual buttons
  (launcher "Sync Library", web "Force sync now") are gone; the web button
  was already dead on a Docker node, which never pulls. Peer-search
  details in P2P_NETWORK.md § Layered sync flow.
- **Analysis is the last step of setup, and only the Web UI starts it
  (2026-09-09).** The launcher's "Analyse Library" button is gone. Running
  the GPU straight after a scan burns it on tracks peers have already
  analysed: the scan gives the network something to match on, the sync
  brings the analysis back, and only the remainder is worth computing. So
  the button lives on one screen (Library, Web UI) and the node points at
  it when the moment is right — `GET /api/settings/guidance` reports
  `analyse_library` once a sync has run over what the last scan added
  (`sync.last_at >= library.last_scan_at`, or immediately with P2P off),
  the node analyses audio locally, no scan/enrich is running, and an owned
  track still lacks embeddings or features. It retires itself when the gap
  closes. The gate is `profile.local_analysis`, not `ml_available`: a lite
  node keeps the button (the run does its text encoders) but skips the audio
  phase, so the gap the trail points at never closes there and the mark
  would nag forever.
  The pending probe is driven from `media_files`, never from `tracks` —
  the phantom layer makes the tracks-first shape 2.9 s against 46 ms.
- **The text half of the analysis drains itself (2026-09-09).** Steps 6-8
  of the background loop — text, lyrics and bio/genre-wiki embeddings —
  moved out of the manual run into `background_enrichment`. They are work
  that loop creates: the bio was fetched two steps up, the lyric one step
  up, no peer carries these vectors (not sync categories) and nothing else
  computes them. Under the old split a person had to authorise embedding a
  bio the machine had just fetched, with no way to know it was owed — the
  first node asked was sitting on 7.3k unembedded bios. The manual run
  keeps the AUDIO phase, which only a new file creates and which therefore
  converges to zero; that is what the guidance trail can honestly point at.
  Two supporting fixes: the loop now calls `notify_library_subscribers()`
  at pass end (a producer that mutates state and tells nobody is what
  forces a client onto a timer — the Sync screen's own progress block was
  stale for the same reason), and `_wants_more` reads `failed` as well as
  `errors`, or a model step whose every item fails would drain forever.
  Three planner bugs surfaced the moment a loop ran these instead of a
  person, all pre-existing and all cheap only at one-run-per-button:
  `text_embeddings` and `lyrics_embeddings` both walked the phantom layer
  from `tracks` to find the owned few (3.4 s and 3.9 s per batch, against
  16 ms and 100 ms driven from `media_files` / `track_lyrics`), and the
  lyrics planner returned a track once PER SOURCE — `track_lyrics` is
  unique on `(track_id, source)`, so a track fetched from both lrclib and
  genius violated `uq_lyrics_embeddings_track_model_chunk` on its second
  copy. 92 tracks on this node. One failure per batch reads as "stop
  draining", so the queue would have advanced one batch per 30 min;
  `DISTINCT ON (track_id)` with the artist-bio generator's richest-field
  preference fixes it.
- **The text half yields to playback (2026-09-09).** BGE-M3 is the one part
  of the background loop heavy enough to be heard, and HQPlayer wants the
  machine more than we do, so steps 6-8 skip a pass while
  `load_meter.playback_active` — the same rule the identity miner already
  follows, and the same reason: playback is a priority signal, not a load.
  A batch already running yields at its next internal boundary (the model
  steps get a `cancel_flag` that also trips on playback), and the meter's
  playback falling edge wakes a pass, so a node played in half-hour
  stretches does not wait out the interval each time. Deliberately NOT
  gated on headroom: headroom counts our OWN process tree, so a step that
  paused on it would pause on its own CPU and oscillate — `mining_hold`
  states this for the miner. Foreign load (a game, a compile, HQPlayer's
  own CPU) is simply not measurable here: on a Docker install `/proc/stat`
  inside the container is the WSL VM's, and HQPlayer runs on the Windows
  side of it. Playback is the proxy because it is the one signal every
  runtime can observe.
- **A failed Last.fm fetch means one of three things (2026-09-22).** The
  source's verdict about the entity (a `WSError` that reaches the caller:
  not found, or another per-entity refusal) is cached in
  `external_metadata` for its window. The source being unavailable to us —
  a transport failure, a 5xx, its own "try again" statuses, and the
  refusals: rate limit 29, a dead key, an HTML challenge page where XML was
  due — ends the batch without marking anything (`SourceUnavailable`), the
  refusals also arming the persistent cooldown (`SourceRefused`). Our own
  failure (a UniqueViolation, a TypeError) is never cached, but registered
  for the process (`lastfm.internal_failures`) and excluded from the
  candidate queries until a restart, because the same code fed the same
  row fails the same way and a fix IS a restart. Before the split every
  error was one thing: a ban (an HTML page, swallowed per section) marked
  thirty innocent artists per pass as `error` for a week and genres as
  `not_found` for 90 days, while one bug of ours — uncached, alphabetically
  first, re-selected every pass — ended every batch "with an error", which
  the drain read as "back off", and held the step to 30 artists per 30 min
  instead of a few thousand an hour.

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
  "plugins available but not installed" list alone was 3 KB of
  streaming-service bait per session), prompt-level prohibition. Known residual: the
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

### Desktop packaging (Windows, 2026-09-14)

- **Same carrier, Windows shape.** `desktop/build_windows.py` stages a
  python-build-standalone CPython (Tk 8.6; the `.pdb`s — half the archive —
  pruned), a MinGit and the git-tracked payload into `build/windows`, and Inno
  Setup wraps them (`desktop/installer/sautium.iss`) into
  `Sautium-<version>-Setup.exe`. `desktop/windows/bootstrap.py` mirrors the
  macOS one: clone `main` into `%LOCALAPPDATA%\Sautium\app`, fall back to the
  payload offline, pip-install `desktop/requirements.txt`, start
  `python -m desktop`. The PyInstaller build (`desktop/build.py`) and the
  `.iss` that wrapped a frozen exe are gone, and `get_project_root()` lost its
  `sys.frozen` branch with them. `desktop/build_common.py` holds what the two
  builds share (version, the CPython pin, payload staging, the secret sweep);
  `desktop/icon.py` the mark — one renderer for the .icns, the .ico, the tray
  and every launcher window.
- **Per-user, never elevated, runtime used in place.** The runtime under
  `%LOCALAPPDATA%\Programs\Sautium\runtime` takes pip's packages directly (the
  macOS copy-out exists only because a signed bundle must not be written
  into), so the install folder has to be the user's own:
  `PrivilegesRequired=lowest`, no per-machine override. An upgrade wipes and
  re-lays runtime, git and payload (`[InstallDelete]`); the deps marker lives
  inside the runtime so it goes too, and the bootstrap reinstalls. Unsigned +
  per-user also spares the user the UAC prompt for an unknown publisher —
  SmartScreen's "More info → Run anyway" on the download is the one warning
  left, and the README says so. Firewall rules stay the launcher's job (one
  elevation per rule, as before); the uninstaller offers to close them with
  one more, skipped when silent.
- **The clone is made in place.** `git init` + `fetch` + `checkout -B main
  origin/main` in the app dir, not `git clone` into it: on Windows the
  launcher downloads PostgreSQL, the backend's Python and the audio tools
  BESIDE the tree (`pgsql/`, `python312/`, `ffmpeg/`…), so the folder an
  offline first start left behind is never empty, and a clone that needs it
  empty would throw those gigabytes away. The bundled git runs with
  `GIT_CONFIG_NOSYSTEM=1`: MinGit's own system config switches on autocrlf and
  the credential manager and includes a Git for Windows installed beside it,
  and the checkout must look the same on every machine (measured: HTTPS to
  GitHub works without it — the CA bundle is found by the compiled path, not
  through the config).
- **Task Manager, the taskbar and Setup all need to know which process is
  Sautium.** `pythonw.exe` copied as `Sautium.exe` gives the process its name
  (CPython finds its DLL and stdlib beside the exe, whatever it is called —
  what every Electron app is to `electron.exe`), and rcedit then rewrites
  the copy's icon and version strings: a renamed stub still said
  `FileDescription: Python` where Windows shows that instead of the file
  name (Task Manager's Processes tab, the firewall dialog), and its
  `OriginalFilename: pythonw.exe` is the mismatch "renamed binary" EDR
  heuristics look for. Nothing here is signed either way — the
  python-build-standalone binaries carry no Authenticode signature, and a
  signature is over the image, not the name; a certificate would sign
  `Setup.exe` (the one file SmartScreen judges, as the only one downloaded)
  and this copy in the same step.
  `SetCurrentProcessExplicitAppUserModelID` plus the same `AppUserModelID` on
  the installer's shortcut make the pinned tile and the running window one
  taskbar button; a named mutex (`AppMutex`) is how Setup and Uninstall refuse
  to rewrite a runtime that is in use. The three constants live in
  `desktop/utils.py` and are repeated in the bootstrap and the `.iss`, which
  cannot import it.
- **CustomTkinter owns the window icon unless told otherwise.** Both CTk and
  CTkToplevel stamp their own icon 200 ms after creation (the Toplevel
  unconditionally), so an icon set once was overwritten on every dialog.
  `desktop/icon.brand_windows` points both classes' `iconbitmap` at the
  rendered .ico — one place, not an `after(250)` at every Toplevel site.
- **Node is the launcher's to install, on Windows too.** The old installer
  bundled a portable Node beside the frozen exe; there is no "beside the exe"
  any more, and Node is only needed when an agent is picked.
  `db_init.install_node` downloads the pinned LTS zip into `<root>/node` (the
  path `get_bundled_node_dir` already looked at), and the wizard's panel
  offers the same button on both platforms.
- **Dependency installs have exactly one owner per interpreter.** The updater
  used to `pip install -r backend/requirements.txt` into `sys.executable` with
  a 300 s cap after a pull — on Windows that is the launcher's interpreter,
  not the backend's, and anywhere a torch bump blew the cap. Removed: the
  backend start already installs its own file from a requirements hash
  (`_ensure_backend_deps`, 3600 s), and the bootstrap installs
  `desktop/requirements.txt` — which is why `_restart_self` now relaunches
  THROUGH the bootstrap (`SAUTIUM_BOOTSTRAP`) on both platforms instead of
  `python -m desktop` directly.
- **Testing beside the checkout.** The installed app and the launcher
  checkout share `%LOCALAPPDATA%\Sautium` (pgdata, config), like the two
  runtimes on macOS. `scripts/test-node.ps1 run` points LOCALAPPDATA and
  APPDATA at a sandbox with shifted ports and `reset` deletes it; the launcher
  cannot be told apart by path (every install runs the same `Sautium.exe`),
  so `reset` asks for it to be quit first.

### CLI agent sign-in without a console (2026-09-09)

- **The console was the only interactive thing about either login.** Both
  CLIs finish their OAuth without a terminal: `claude auth login` and
  `codex login` run on plain pipes (no pty, no ConPTY), print the link, open
  the browser themselves and exit 0 with the credentials stored where the
  chat turns already read them. `desktop/agent_login.py` drives that process
  for the wizard and the backend alike; `desktop/utils.launch_*_setup` and
  the `cmd /k` / `osascript` console launchers are gone.
- **Claude prints one URL and opens another.** The printed link carries
  `redirect_uri=platform.claude.com/oauth/code/callback` — a page that shows
  a code — while the browser gets the same request with a
  `localhost:<random>/callback` redirect. On the machine running the CLI a
  click on Authorize therefore completes the login; the printed link plus
  the code (written to the CLI's stdin) is the path for a browser elsewhere:
  a phone, the host of a Docker node. Measured by capturing `$BROWSER`.
- **Codex has a device-code flow.** `codex login --device-auth` prints
  `auth.openai.com/codex/device` and a one-time code the user types there;
  the CLI polls OpenAI itself. Inside a container that is the only flow
  that can finish (the browser cannot reach the container's localhost:1455),
  so the backend forces it there; on the launcher the browser flow is the
  default and the device flow is the "not at the computer" alternative.
- **`codex login` is a logout first.** It deletes the existing `auth.json`
  the moment it starts, before any authorization (measured on 0.149 and
  0.153, browser and device flows alike); `claude auth login` keeps the old
  credentials until the new ones land. A cancelled codex re-authorization
  therefore leaves the node signed out of ChatGPT (falling back to
  `OPENAI_API_KEY` when one is set), and the Reauthorize row says so.
- **Docker signs in from the Web UI now.** Both CLIs are baked into the
  image and `~/.claude` / `~/.codex` are host mounts, so `host_unsupported`
  shrank to "a container without the CLI"; the sign-in process runs demoted
  to the agent user like every chat turn, which is what puts the credentials
  in the mounted HOME.
- **Completion is an event, not a poll.** The driver's reader thread wakes
  the settings SSE stream on every parsed change and on exit — the wizard's
  2-second credential poll (5 minutes, then "click Refresh") and the Web
  UI's "detects the sign-in automatically" promise that depended on a state
  read are gone. A failed attempt keeps the CLI's last line
  (`agent.signin_failed`), a 15-minute deadline kills an abandoned one
  (`agent.signin_timeout`).
- **Rejected: writing the credentials ourselves.** `claude setup-token` +
  `CLAUDE_CODE_OAUTH_TOKEN` is documented for CI but yields a one-year token
  with no refresh that Sautium would have to keep; `.credentials.json` is
  undocumented (and a Keychain entry on macOS) and performing the OAuth flow
  with the CLI's client id ourselves is neither. The CLI-driven login is the
  supported surface and needs none of it.

### The updater mirrors origin/main (2026-09-12)

- **A node's checkout is a mirror, not a branch.** It never authors a
  commit, so "update" means "make my tree equal to origin/main": `git fetch`
  + `git reset --hard origin/main` (`desktop/updater.py`). The former
  `git pull` merged the remote into a local branch and died on the first
  rewritten history — `fatal: Need to specify how to reconcile divergent
  branches` (git ≥ 2.33 with no `pull.rebase`) — leaving the node with
  "Update failed" forever and a person running the reset by hand. Reproduced
  in a sandbox for both shapes of rewrite: `filter-repo` (early commits keep
  their SHA) and a single orphan "initial" commit. Purging files from history
  is now an ordinary update.
- **Only a tip origin handed us may be moved anywhere.** `refs/sautium/mirrored`
  records the tip the checkout last received (minted whenever HEAD is level
  with `origin/main` before a fetch — clones and hand pulls qualify — and
  after every reset). A fast-forward loses nothing and is always taken; any
  other move is taken only when HEAD equals that marker. The developer's tree
  runs the same launcher as a test stand, and its unpushed commits would
  otherwise be one click from a `reset --hard`. A tracked file edited by hand
  refuses the update before the fetch, as `git pull` did.
- **The purge reaches the node's disk last.** A move that was not a
  fast-forward ends with `reflog expire --expire=now --all` + `gc
  --prune=now`: without it the reflog keeps the old commits — and the blobs
  upstream just purged — for 90 days. It runs AFTER the tree diffs against
  the old commit (requirements, migrations, launcher code), which read that
  commit; the first draft compacted inside the reset and those diffs
  silently answered "nothing changed".
- **One update at a time — the button is the flow's state.** The first
  live rollout (2026-09-12, a macOS node 121 commits behind) produced
  "Update failed" for an update that had succeeded: stopping services took
  25 s (the P2P loop's 15 s join timeout, then the backend's 10 s graceful
  kill), the "Check for Updates" button had been re-enabled the moment the
  dialog opened, a second click opened a second dialog, and two `git pull`s
  ran at once — concurrent fetches each append to FETCH_HEAD, two merge
  heads are never a fast-forward, so one pull died with `Need to specify
  how to reconcile divergent branches` while the other fast-forwarded.
  `launcher.py` now has `_update_flow_busy/_update_flow_idle`: the button
  is taken from the start of a check until the flow ends (no update, Later,
  the dialog's close box, failure, services ready), the startup check's
  verdict is painted only while idle, the tray menu enters through
  `ui_call` (pystray fires on its own thread), and a check whose git call
  raises hands the button back instead of dying on "Checking...".
- **Order of a rewrite.** The mirror updater must be ON the nodes before the
  force-push — the old `git pull` is what runs otherwise. Ship it, confirm on
  the support desk that every live node's `node.started` report carries the
  commit, then rewrite. A node that skipped that window needs the manual
  reset. Moving the repository to another account is a second break on top:
  the remote URL sits in every clone's `.git/config`, in
  `desktop/macos/bootstrap.py`, `desktop/windows/bootstrap.py` and
  `desktop/updater.py` — that needs a bridge
  commit on the old remote that repoints `origin`, and the old repository
  left alive as a frozen redirect.

---

### Node backup and restore (2026-09-13)

Product A of `docs/design/BACKUP.md`: one encrypted `.sbk` per node — the
`pg_dump` of everything but the `mb_*` data plus the identity documents —
keyed by the account password through Argon2id in a salt domain of its own
(`sautium-backup:v1:<username>`, never the identity seed's). What building
it taught:

- **SecretBox has no associated data.** "Header authenticated as AAD" needs
  an AEAD; libsodium's XChaCha20-Poly1305 through `nacl.bindings` is in the
  stack already. Every chunk carries the header digest as AAD, so a header
  edit fails chunk 0 — and the KDF parameters are pinned, so a header cannot
  ask the reader for 16 GiB before the password is even tried.
- **A tar cannot stream an unknown-length member.** pg_dump's size is
  known when it exits; a tar header wants it first. Framed records inside
  the encrypted stream (`MEMBER_START / DATA / MEMBER_END / END`) write and
  read sequentially with no spool, and truncation is detected because `END`
  is authenticated plaintext.
- **Counts and dump from one snapshot.** `pg_export_snapshot()` in a
  `REPEATABLE READ` transaction + `pg_dump --snapshot=<id>`: the manifest's
  row counts match the restored tables exactly even while the node writes.
- **jammy's `postgresql-client` is PG 14** and refuses to dump an 18 server
  ("server version mismatch") — the image installs `postgresql-client-18`
  from PGDG and pins `PG_BIN`. The launcher passes its own `pgsql/bin`.
- **A non-superuser restore** (the launcher's `sautium`) needs the
  extensions pre-created by the admin role and `--no-comments`: `COMMENT ON
  EXTENSION` from the dump is owner-only and would abort
  `--exit-on-error`. `--no-owner --no-privileges` do the rest.
- **Stage, then swap.** Restore into `<db>__restore`, apply newer
  migrations there, terminate sessions, rename; a database with own data
  survives as `<db>__previous`. Nothing is dropped that was not confirmed.
- **The private seed never enters the file** — re-derived from username +
  password at restore and written only when it reproduces the recorded
  public key. The rotation archive's private keys are the accepted loss.
- **Launcher and CLI, not the Web UI (2026-09-14).** The first cut had a
  Backup card in Settings › Library with a job inside the backend. Removed
  the day after: a browser cannot receive the file and the password belongs
  where the file lands. The launcher button now runs the same
  `python -m backup create` the weekly task runs, so there is one
  implementation; the playback hold survived the move as a PostgreSQL
  session advisory lock the backend holds while playing and the job waits
  on — event-driven and self-releasing when the backend dies.
- **Share export = the sync payload in a file (2026-09-14).** Product B
  reuses the seed bundle's builders and the sync client's import gate; the
  only new thing is the container: gzip'd JSON lines with a signed trailer,
  because a single JSON document cannot carry a 1.7 GB "everything I own"
  export through memory. Two-pass import — verify the whole file, then
  apply — since a streamed import cannot take anything back. A record
  altered inside a re-signed file dies at the seal, exactly as designed.
  The first cut exported first-hand records only; Valerii: "what I know"
  is more than "what I analysed" — the file carries everything sealed the
  node holds, the seals keep authorship straight. Import offers "add new
  artists and albums" (phantoms, the default) or "enrich only what I have".
- **A launcher-spawned child must not spawn git (2026-09-14).** The
  first export from the Settings window hung forever: the CLI's manifest
  stamp called `updater.current_commit` → `subprocess.run(["git",
  "rev-parse", …], timeout=30)`, and on Windows git's own child kept the
  pipes open past the timeout, so `communicate()` waited on the reader
  threads for good (py-spy showed it). `current_commit` now reads
  `.git/HEAD` / refs / packed-refs itself — no process, no git needed.
  Same session, same lesson for the runner: the child's stderr rides the
  stdout pipe, because a second pipe nobody drains until the first closes
  is a 4 KB deadlock waiting for a chatty child.
- **Life-data merge = keyed union, and play stats are derived
  (2026-09-14).** Product C (`backend/life_merge.py`, `python -m backup
  merge`) unions one account's listens, sessions, friends, messages, chats,
  gear, allowlisted preferences and rotation records out of a backup made
  on another machine. The dump streams once through `pg_restore
  --data-only -t <life tables> -f -` (one pass over a non-seekable stream
  selects twenty tables in ~20 s for 3.3 GB) into a scratch schema inside
  the live database, so the union and its rollback are one transaction and
  nothing decrypted touches the disk. Two findings: sessions cannot be
  recomputed from history (no session column there) — they merge by uuid,
  closed ones with all tracks known; and the incrementally kept
  `local_play_stats` had drifted (567 of 2,205 tracks counted a skip's
  seconds as listening time), so the table is now derived from history by
  one shared statement (`backend/play_stats.py`) in the tracker and the
  merge alike, and a one-time `db_migrate` step (`play_stats_derived_v1`)
  re-derives what every node already holds — two histories merged in either
  order give one answer.

### Notices: a toast is a signal, the row is the fact (2026-09-13)

The silent-events audit found background conditions that change what the
user sees with no word from the UI: a fresh node's slice fetch hit the
peer's per-IP window (60 requests/min, shared with the sync walk that had
just spent it), the batch was parked until the SIX-HOUR timer, and every
phantom artist stayed a bare card — "looks broken" for hours. Same shape
for a Last.fm cooldown (30 min doubling to 24 h) that reads as "Idle".

- **Three layers, not one.** A toast on the transition, a row on Sync & P2P
  lit by the guidance trail until visited, and the consequence explained
  where it shows (the bare card says the discography is on its way and
  when the network is asked next). A toast that nobody saw costs nothing
  because the other two layers still hold the fact.
- **`pointer-events: none` for the passive toast.** A tap goes to what is
  beneath, so a toast never blocks a control and the second tap of a
  double-tap cannot be hijacked into "open Sync". The toast is narrower
  than the header so the corner buttons stay uncovered; only a toast with
  an action opts back in. Long-lived conditions never float: the
  reconnecting strip sits in flow and pushes the app down.
- **Snapshots, not events.** The server publishes the whole active set on
  `/api/events` and the client diffs it; the connect-time copy paints state
  only. Conditions are DERIVED (cooldown ledger, the launcher's
  `mb_slice.status` row), never stored as a list, so one ends the moment
  its source does.
- **The honest number came from fixing the cause.** `Retry-After` on the
  peer surfaces' 429 and a one-minute wait-and-retry in the slice cycle
  turned "next attempt in six hours" into "about a minute"; the published
  `next_attempt_at` is the timed loop's real deadline. Left for later: the
  same 429 awareness in the pull walk, a slice loop on dump-less Docker
  nodes, and the remaining silent states (music mount, media tools).

### A double-tap is one tap (2026-09-13)

The same audit listed twenty-odd handlers a double-tap could double:
queue `×` removed two tracks (index-based API plus an optimistic
re-render that put the next `×` under the finger), the chat trash
swapped in a Delete exactly where the trash was, album bars closed
before their request and brought Play back under the second tap, "+ New"
minted two chats, invite/send-code mailed twice, Enter bypassed disabled
buttons, dialogs stacked, back went two screens up, and settings toggles
either dropped or crossed their writes.

- **Make the second activation impossible, per flow.** One latch
  (`onceInFlight`) around the whole flow including its dialog; the unit
  is whatever the second tap would land on. Time-based "ignore taps for
  300 ms" was rejected: it hides the slow cases and breaks intentional
  repeats (volume nudges).
- **Order writes, drop stale reads.** `serialized(key)` chains writes so
  the last tap is the last write; `claimFresh(key)` lets only the newest
  refresh paint. Toggles read their own DOM, never the state captured at
  render (that is what silently dropped the second tap).
- **Geometry is part of the fix.** What appears under the finger after
  the first tap must be harmless: bars stay up (disabled) until settled,
  the inline confirm puts Cancel where the trigger was.
- **Idempotent creates on the server** where a duplicate is a real row:
  an empty chat is reused, radio start is single-flight, a fresh Last.fm
  flow is handed back, queue removal carries the slot's track identity
  and refuses (409) when the queue moved.
- The browser knows a double-click: `e.detail > 1` on open/close toggles.

### HTTP on the LAN (2026-09-17)

The Web UI served HTTPS with a self-signed certificate, and every phone met
the browser's interstitial first. Looking for a way around it ended in a
structural answer: a certificate a stock phone trusts **cannot** exist for
a LAN address. Public CAs may not issue for private addresses or `.local`
(CA/B Forum Baseline Requirements, since 2015 — such a cert would be a
skeleton key for every network on earth), a public name that resolves to
a private address is what DNS-rebinding protection blocks by default on
Fritz!Box, OpenWrt, pfSense/OPNsense and Unbound-with-Pi-hole setups
(Plex lives with exactly this and falls back to plain HTTP), a private CA
on the phone is a worse first run than the warning, and the phone has no
hosts file. The industry's answer for LAN appliances (Jellyfin, Navidrome,
Home Assistant, Volumio) is plain HTTP, with TLS only through a name the
user brings.

- **HTTPS was never a security requirement here.** It existed because
  `crypto.subtle`, which the request signer used for HMAC, is withheld
  from an http origin. The signer never needed the transport — the token
  never travels, only signatures with a 60 s life. `sha256.js` (plain-JS
  SHA-256 + HMAC, checked against hashlib) replaced the Web Crypto call
  and HTTPS became optional; then it went, because a self-signed listener
  adds a warning and no protection.
- **Credentials get their own envelope.** The exchanges that DO carry a
  secret — password or PIN in, the device token out, on login, pair,
  create-account, logout-all and change-identity — ride a NaCl box
  (tweetnacl in the browser, PyNaCl on the node) to a per-exchange X25519
  key that `GET /api/auth/handshake` mints and the credential request
  consumes: no replay, no key that outlives its exchange, and nothing a
  listener on the Wi-Fi can read. The handshake is signed by the node's
  identity key; the browser pins that identity on its first sign-in
  (`sautium.node_pubkey` in localStorage) and, when a different one
  answers later, the gate asks before going on — SSH's known_hosts as a
  dialog. What stays out of scope is an active attacker present at a
  browser's very first sign-in, exactly as with SSH.
- **What is accepted.** API and media traffic on the LAN is readable by a
  device on the same network; the threat model already drew the bar at
  "no random scanner", not "no targeted LAN attacker" (SECURITY.md).
  Secure-context APIs are gone on the http origin: `navigator.clipboard`
  got an `execCommand` fallback (`copyText`), `crypto.randomUUID` already
  had one, service workers were never available on the bypassed cert
  either. A microphone (`getUserMedia`), if one is ever needed, will need
  the TLS front below.
- **TLS is a deployment front with a real name**, never a `tls_gen` job:
  `tailscale serve` terminates with a real Let's Encrypt cert for the
  `.ts.net` name and proxies to the HTTP port (MagicDNS resolves it on
  the phone, no router in the chain — the one reliable path, and free);
  a reverse proxy does the same on a LAN. The name goes in
  `SAUTIUM_ALLOWED_HOSTS` for the Host guard. `tls_gen.py` shrank to the
  Docker peer-surface cert (pinned to the node key, static SAN), the
  launcher no longer mints a certificate, `~/.sautium/tls` of earlier
  installs is just a leftover.
- **The Host guard learns its addresses from the interfaces (2026-09-21).**
  Its own-address set is every IPv4 bound to the node's interfaces
  (`tls_gen.detect_own_ipv4s`, psutil), Tailscale's 100.64/10 included,
  and an IP literal it does not hold is re-checked against the interfaces
  once (a tunnel started after the backend, a new lease); names are never
  resolved. Before this the launcher node answered `Host: 100.x` with 421:
  own-interface detection went through the LAN-only predicate, and only
  `SAUTIUM_HOST_IPS` entries — which the launcher never sets — took the
  wider one, so "Tailscale works because its address is one of the node's
  own interfaces" held for a Docker node with the variable set and not for
  the launcher.
- **Found on the way:** `portmap._serves_web_ui` still looked for the
  inlined-secret marker of the 2026-08 page, so it recognised no port as
  the Web UI; it now reads the `sautium-webui` health type.

### Streaming is a demo channel (2026-09-17)

The core streaming provider (YouTube) streamed phantom albums without
limit, which makes the product a free substitute for a streaming service
— a legal exposure for its author. Decided: streaming from the core
channel is an acquaintance tool. A track streams from it in full at most
**once**; past that it plays as the catalog's own 30 s excerpt.

- **The ledger is a table, not a stats flag.** `demo_plays(track_id PK,
  provider, played_at)` — `local_play_stats` is re-derived wholesale from
  `listening_history` (`play_stats.py`), so nothing sticky can live
  there, and `listening_history` records no provider. Life data: in the
  backup, merged by earliest `played_at`, never synced (a node-local fact
  about this listener). No settings toggle: legal posture, not preference.
- **A listen is spent by position, not by the tracker's rule.** The
  status observer (`streaming/demo.py`, `manager.subscribe_status`)
  writes the row the tick playback passes 90 % — the tracker's
  `completed` (50 % or 4 min) is the scrobble rule and says nothing about
  having heard the track. A seek into the last tenth counts: the listener
  reached what they came for.
- **The ledger governs what is fetched, at both ends.** The resolve
  waterfall builds a spent track's chain from a provider order WITHOUT
  the demo channel — the cache key is that order, so spending the listen
  is a cache miss and the excerpt provider gives a real answer (a
  post-filter of a cached chain would have left a lazy link availability
  reports as streamable on a guess). The proxy asks `link_admissible`
  before every link it fetches, because a chain is a plan and the bytes
  are fetched later. When the spent listen ends, the demo channel's
  buffer is dropped (`MediaProxy.drop_audio`) and `_mint` adopts RAM
  audio only from a provider the new chain names — a replay refetches
  and cascades to the excerpt. What an output already buffered for
  itself (a browser blob, a renderer's cache) is beyond reach; documented,
  not chased.
- **An excerpt is not the recording, so it is nothing downstream.**
  `FetchedAudio.excerpt` + `seconds` ride through `preview_meta` into the
  queue item (`QueueItem.excerpt`, `duration_seconds` = the clip's length —
  DLNA's `res@duration` and track-end detection read it) and the status
  (`excerpt` → the `[30s]` badge). `PreviewEnricher.submit` refuses it
  before any decode or provenance row (every first-hand stream analysis
  signs and syncs now). The tracker opens no session for it: 25 s of a
  30 s clip would have read as `completed`, and a clip over 30 s would
  have opened the `ARTIST_ENGAGED` fan-out.
- **The queue item follows the buffer.** A stream's item is built once,
  when its buffer is first ready; a refetch (budget eviction, a spent
  buffer) can change the provider and the length. The proxy's
  `track_ready_hooks` now run BEFORE the entry is marked ready, and the
  first hook (`CanonicalQueue.refresh_proxy_items`) patches every slot on
  the token — so whoever wakes on `wait_ready` (DLNA, before it builds
  the DIDL) reads an item that is already true.
- **One catalog resolve for two providers.** The BYO lossless module and
  the core excerpt provider are the same recording on the same catalog, so
  its public-API resolve moved into core (`streaming/deezer_catalog.py`,
  `DeezerCatalogProvider`): one pacer, one album memo, one preview-URL
  memo per process; the closed module keeps only its config and the
  streamrip download. Providers naming one `cooldown_source` are demoted
  together under a cooldown and are one voice for the `streaming.silent`
  notice. `providers_preferred()` ranks `(excerpt, not lossless, id)` —
  the excerpt is always the last resort — and the excerpt provider is
  registered unconditionally, which is what makes a node without yt-dlp
  still preview.

### Stream providers are a registry, not an enum (2026-09-18)

`analysis_sources.origin` was `ENUM ('local', 'deezer', 'youtube')`: the
id of a closed bring-your-own module hardcoded in the public schema, and
the reason `create_stream_source` refused provenance for any provider it
did not know — a new plugin's analysis stayed unlinked and never signed.
The enum also conflated two things: WHAT was analysed (the node's own
file vs a stream — the only distinction any rule reads: the file CHECK,
the upsert's anti-downgrade, the sync's protected set, the signing gate)
and WHICH plugin fetched it, which only the overwrite rank read, in three
copies (`provenance.ORIGIN_RANK` and two SQL CASEs in `canon/`).

- **`provider_id` references `stream_providers`**, the persisted snapshot
  of every registered manifest (id, name, lossless, excerpt,
  demo_limited, version) that `streaming.service` upserts at every start
  — core writes it on the plugin's behalf, so the plugin contract stays
  v1 and a row outlives its plugin with the name and tier intact. NULL
  means the node's own file, or a row imported over P2P (the wire
  withholds file-vs-stream; `chk_asrc_imported_anonymous` keeps it so).
  Delta `017`: seeds the registry from the origins present, moves the
  column, drops the enum — one transaction, no seal or wire change
  (`origin` was in neither).
- **Rank the material, not the brand.** Overwrite precedence is
  `own file > lossless stream > lossy stream`, read from the row's
  `is_lossless` (the ACTUAL fetch tier — a lossless provider degrades
  to 320/128 where it has no lossless tier) — the same rule the sync
  applies to peers' sources, in ONE place (`MATERIAL_RANK_SQL` /
  `material_rank`). The one behavioural change: a lossy fetch from the
  lossless provider no longer outranks YouTube by name; both are lossy,
  the analysis loses nothing measurable at those rates, and the
  enricher never re-analyses an embedded track anyway. Provider trust
  ("catalog-resolved beats search-resolved") was considered and
  rejected: `same_recording` already gates both providers' audio
  before analysis, and a brand rank on top would be a second guard for
  the same risk.
- **Natural TEXT key, refused duplicates.** The manifest id is the
  provider's identity everywhere (provenance, `demo_plays`, cooldowns,
  notices) and never crosses the wire, so UUID v5 buys nothing and a
  serial is opaque. A key type cannot prevent two plugins declaring one
  id — `ProviderRegistry.register` now refuses the second (it was a
  silent last-wins by directory order). The UI reads provider names
  from the notice payload (`streaming.silent` carries `name`), not a
  literal map.

### Buy resolves through MusicBrainz (2026-09-17)

The phantom album's Buy button opened a Bandcamp search built from the
credits, which often landed on nothing. Measured before changing it:
Bandcamp's official API (bandcamp.com/developer) is Account / Sales /
Merch Orders for labels and fulfilment partners, OAuth by request, no
catalogue search; the site's own JSON search, the HTML search page and
the artist-subdomain album pages all answer a non-browser client with a
JS "Client Challenge" (HTTP 200 and 3 KB of HTML — for a real page and
a made-up one alike), so neither a lookup nor a URL existence check can
run from a backend, and working around a bot challenge is not something
a public product does. MusicBrainz has the exact pages as release-URL
relationships: ~800k Bandcamp urls of ~22M (Beatport ~270k, 7digital
~26k, HDtracks ~4.5k across three URL generations — MB's cleanup rules
and importer userscripts exist for Bandcamp, not for the hi-res shops).

- **Bandcamp only, by Valerii's call.** One store means one button
  state, no store ENUM, no allowlist order; the space an allowlist would
  have saved is under a percent of the dump either way — the saving
  that matters is filtering `url` at all (22M rows → 0.8M).
- **Filtered at COPY time, not after.** `mb_dump_load._ROW_FILTERS`
  admits a `url` row only for `*.bandcamp.com` (editorial
  `daily.bandcamp.com` excluded) and a link row only when its url
  survived. The core archive delivers `l_artist_url` and `l_release_url`
  BEFORE `url` (MB's `@CORE_TABLE_LIST` order), so those two members are
  spooled to disk and loaded once the surviving ids are known.
- **Tables added after a dump landed load alone.** `stats()` reports
  `missing_tables` (empty TABLES behind the in-DB completion marker —
  the marker, not the VERSION file, which a dev host shares with a
  dump-less launcher); `download_and_load` fetches the same version's
  archive and streams just those (`stream_load(tables=…)`), and the
  Settings block turns Update amber. A dump older than the mirror's
  newest takes the full path, which includes them.
- **Slice format v3** — the artist's url subtree in the blob, receipt
  context bumped, migration 016 empties both slice ledgers everywhere,
  serving gated on the full wire table set (P2P_NETWORK.md § E). A
  fetch row is what `_mb_source_covers` and the Buy resolver read as
  "facts complete and current", so `pending_slice_names` gained a last
  tier — every canonized artist without one — or an already-shelved
  artist would have stayed on its v2 facts for good (the macOS node
  after 016: 6 695 of 7 280 phantoms stuck on `unknown`).
- **Four button states** from one query in `routers/albums.py`
  (`_buy_link`): album page → artist page → disabled (`absent`: the
  local facts cover the record — a dump with the url tables, or a slice
  fetched after 016 — and name no page) → the old search fallback for
  `unknown` (a carried phantom whose artist's slice has not landed, a
  record newer than the dump). Deterministic pick — earliest-dated
  release, `/album/` over `/track/`, then url — so every node lands on
  the same page. The artist state links the shop's `/music` grid, not
  the root MB stores: a Bandcamp root redirects to the artist's
  featured release, which read as "Buy opened a different album" on the
  first live tap.

### Last.fm data is node-local (2026-09-19)

Last.fm's API terms do not allow redistributing what the API answers, and
until now Sautium did exactly that in four places: the five Last.fm-fetched
tables (`artist_bios`, `artist_tags`, `similar_artists`, `track_stats` —
gone since 2026-09-20, see the ListenBrainz section — and
`genre_descriptions`) were sealed and served over the P2P pull protocol,
counted into the holdings filter, written into every share export, and the
seed bundle shipped bios/tags/similars for the picks' artists as a public
GitHub release asset. All four stopped in one change; P2P_NETWORK.md §
"Last.fm data is node-local" has the protocol side.

- **Local-only, not source-filtered.** Every row in those tables IS
  Last.fm (36.8k bios, 285k tags, 73k similars, 36k stats, 2.3k genre
  descriptions on the master — all `source='lastfm'`), so a `source`
  filter in every pull SQL would have kept five categories alive for no
  data. The tables lose their seal columns, `fetched_at` and `imported`
  (migration 019, folded into 001) and join `album_descriptions` as the
  local-only layer; the seal grammar keeps the carry canon kinds only.
- **Imported copies go, first-hand rows stay.** The migration deletes
  rows that arrived over the network — those are the redistributed copies
  — and the node's own background enrichment re-fetches them by name,
  because its "no bio yet" precondition is true again. A cache of a
  public API is re-derivable in a day; the retention rule (a profile never
  deletes what the owner can see) guards the phantom layer, which no node
  can rebuild without the MB dump.
- **The share file and the seed bundle moved versions** (v2 and v3): a
  file or bundle that carried the layer is refused rather than half-read,
  and the seed's fresh-node path no longer needs a sealed bio per pick —
  the coverage gate lost those two fatal checks.
- **What did not change**: the `listeners` count as the local rarity proxy
  (announce tail, carry order, rare-key search), gender/vocalist
  classification from the node's own bios, scrobbling.

### Last.fm attribution in the UI (2026-09-20)

The companion to the node-local change above: that fixed what the node
**sends**, this fixes what it **shows**. Clause 2.7 of the Last.fm API terms
wants a credit AND a link from displayed catalog data back to the matching
Last.fm page; nothing in the UI credited Last.fm at all, and `trimLastFmTail`
was cutting Last.fm's own `Read more on Last.fm` tail out of every bio. The
Artist and Genre screens now end with `data from Last.fm`, linking to the
artist's `/music/…` page and the genre's `/tag/…` page.

- **Why not the literal phrase.** Clause 2.7 says to use one of the
  `powered by AudioScrobbler` buttons from `last.fm/resources`. That page is
  **404** — the asset the clause points at no longer exists, so the clause
  cannot be performed literally and what survives is its substance, which
  clause 4.2.2 states plainly: credit **Last.fm**. The archaic phrase never
  contains those words, so a reader learns nothing from it; naming Last.fm
  and linking to it performs the real obligation better, and adds credit
  rather than removing it. What would cross the line is the opposite
  direction — implying endorsement ("in partnership with"), dropping the
  link, or hiding the credit behind a tap.
- **The URLs were already in the database.** `artist_bios.url` (36 793 rows)
  and `genre_descriptions.url` (2 322 — every genre entity) are pylast's
  `get_url()` output, populated at enrichment time and selected by nothing
  until now. Lower-cased and double-encoded (`ac%252fdc`), which looks broken
  and resolves fine. No new API call, no schema change, no URL builder of our
  own — the credit links to what the node actually stored.
- **`lastfm_url` survives the namesake nulling.** The bio is nulled off a
  non-dominant namesake because it is the merged "more than one artist named
  X" blob; the URL is name-keyed, so it is the right page for every namesake.
  On the lean namesake page the credit is the second provenance line under
  the existing `metadata from MusicBrainz` end-cap, sharing its divider.
- **Credit what was stored, not what might exist.** No artist has Last.fm
  tags or similar-artist edges without a bio row, so every page showing
  Last.fm prose, chips or similars carries the credit. 46 of 3 899 artists
  (1.2 %) have Last.fm `track_stats` but no bio yet — featured artists the
  engagement-gated `enrich_bios` has not reached. They show no credit until
  it does, which is self-healing and beats a second, hand-rolled URL source.
  (Since 2026-09-20 the numbers on those pages are ListenBrainz's, and the
  credit line names both sources — see the next section.)
- **The connected account links to its Last.fm page.** Clause 2.7 names the
  `/user/<name>` link specifically, so Profile › Account › Last.fm renders
  the username as a link to it.
- **Scope is otherwise Artist and Genre.** Deliberately uncredited: album
  genre chips that fall back to the artist's Last.fm tags, Discovery's
  bio-search scope, the 226 covers from `album.getInfo`, and assistant
  answers. No global footer credit.

### ListenBrainz replaces Last.fm track stats (2026-09-20)

Per-track listening statistics used to be one Last.fm `track.getInfo` call
per owned track (36k rows on the master), node-local by licence since 019.
They now come from ListenBrainz — CC0, so the layer travels — as a second
dump family built like the MusicBrainz one: an opt-in local dump on "dump
nodes", per-artist signed slices for everyone else. Migration 020 drops
`track_stats` and the Last.fm negative-cache rows behind it; the album
"Popularity" sort and the Popular tracks blocks read `track_mbids ⋈
lb_recording` (a track sums its recordings — 4 583 tracks bind to more
than one). The artist-level rarity proxy (`artist_bios.listeners`) is
untouched: Valerii's call, track level only.

- **What ListenBrainz actually publishes.** No public dump carries its
  `popularity` tables; the popularity API sums LB listens with MLHD+
  (non-commercial-only) and cannot seed a redistributed layer. The
  statistics dump (`listenbrainz-statistics-dump-<ts>.tar.zst`, ~22 GB,
  1st and 15th) holds one JSONL document per LB user per stat — the user's
  TOP 1000 recordings / artists. `lb_dump_load` streams the archive once
  (`zstandard` + `tarfile`), COPYs the items of `artists_all_time.jsonl`
  and `recordings_all_time.jsonl` into UNLOGGED staging, `GROUP BY`s them
  (SUM of listens, COUNT of users, `min(artist_mbids)` — a hash aggregate,
  never a Python dict) into `lb_recording_new` / `lb_artist_new`, indexes
  them and SWAPS them in: readers never block for the minutes the
  aggregation takes. Reading stops at the end of the recordings member —
  the tool writes artists first, recordings second, then releases and
  activity, which are never decompressed. The counts are LOWER BOUNDS by
  construction and every consumer treats them as a rank.
- **One completion marker**, `user_settings['listenbrainz.db_version']` —
  the only value signed into a slice and the only "loaded here" truth. The
  MB loader's VERSION-file + DB-key pair is what let its signed version
  and its serve gate disagree; the LB archive name carries the version for
  download resume instead. The `.sha256` is verified before anything reads
  the archive and fails closed (the MB `verify_md5` is dead code that
  fails open — left as is).
- **Versioned from the start.** The MB slice family has no staleness: a
  ledger row is never re-asked, a dump node keeps re-serving blobs cached
  from an older dump. Here `dump_version` rides in every ledger and data
  row, a request carries `min_version` (the newest version any reachable
  source advertised in `/health`: `lb_dump`, `lb_slices_version`), a cached
  blob older than that is `missing`, a dump node never serves a cache older
  than its own dump, imports move a row forward only (one recording is
  credited to two artists and arrives from two slices in any order), and
  `pending_slice_mbids` re-asks every row older than the newest — a signed
  zero-match included.
- **Owned + engaged in bulk, phantoms on demand.** The cycle asks for
  owned artists and completed-listen artists that have an MBID; a phantom
  artist is asked when its page is opened (`routers/artists.py` writes
  `lb_slice_requests` and NOTIFYs; the cycle serves that lane first; a
  `sautium_lb_done` NOTIFY reaches the tab as `{"t":"lb"}` on `/api/events`
  and the page patches its Popular tracks block in place). Popular tracks
  gained a phantom arm off `album_tracks.recording_mbid`, so a not-owned
  artist's hits render as phantom rows and stream like any other.
- **One cycle for both runtimes.** `desktop/p2p/lb_slice_cycle.py` runs
  in the launcher's P2PManager and in the Docker backend (the walk's
  `connect_peer` injected), which the MB family never got — a dump-less
  Docker node has no MB slice loop to this day. `verified` and `missing`
  are two explicit sets: `mb_slice_client.py`'s `missing` expression has an
  `and`/`or` precedence hole that drops a name whose blob failed
  verification. Discovery mirrors `_find_dump_peers` (replicas first, DHT
  `lbdump`, the Worker directory's `lbslices`/`lbdump`, the master hint
  last); moving the MB family onto `LbSliceCycle.find_sources` is the
  follow-up.
- **One dump-job runner** (`backend/dump_job.py`): the MB job's
  state/progress/budget/auto-update machinery generalised over a family,
  two instances, ONE worker thread — the wizard can tick both downloads
  and two bulk loads on one volume must never run at once (the second
  says "Queued…"). The Offline databases screen (`#more/databases`, its own
  section since 2026-09-21) renders both blocks from one
  `_dumpBlockHTML(family)`.
- **The Worker deploys first.** `DIRECTORY_CAPS` gains `lbdump` /
  `lbslices`, and the registration cap `capabilities.length > 4` becomes
  `> DIRECTORY_CAPS.size` — a node advertising six caps against the old
  Worker would lose its WHOLE registration, sync included.
- **Coverage trade, measured.** 84 % of owned tracks carry a recording
  MBID (31 365 of 37 115), and after the first master load **41 % (15 223)
  have a ListenBrainz count** — the rest of the MBID-bound tracks never
  reached any LB user's top 1000 and fall to the local-plays tier (Last.fm
  answered for 98 %; it counted every scrobble). 2 812 of 4 120 owned
  artists have an `lb_artist` row.
- **First master run (2026-09-20, dump 20260915-000002).** Download 21.8 GB
  in 17 min (~24 MB/s), checksum 3 min, stage + aggregate + swap 437 s:
  84 850 users, 29.6 M artist items (of 37.3 M — the rest unmapped) and
  39.1 M recording items (of 46.2 M) into 5.0 GB of staging, aggregated to
  4.9 M recordings + 0.67 M artists = 0.85 GB. Reading stopped at 22 % of
  the archive. Constants calibrated from it: ARCHIVE 22 + STAGING 6 +
  TABLES 2 + MARGIN 2 GB. A Vangelis slice builds in 0.3 s (635 recordings,
  16.8 KB gzipped).
- **Measured**: 276 tests green in the container, the DB-gated ones
  building the schema from 001 → 020 on a scratch database and running the
  aggregation through the real `GROUP BY`; 020 rehearsed against a real
  `track_stats` (dropped, negative-cache rows deleted, idempotent, the
  `artist_mbids` statement trigger fires one wake).

### Missing albums follow the albums sort (2026-09-21)

The artist page's Missing-albums shelf (phantom albums off the discography)
was always newest-first while the owned shelf took the user's pick. One
sort map now orders both (`discography.ALBUM_SORT_EXPR`, imported by the
router — one source of truth), and the phantom query computes the same
metrics over the tracklist: listening time from streamed plays
(`album_tracks ⋈ listening_history` — play tracking is keyed on the track,
so a phantom listen counts like an owned one) and popularity from the
ListenBrainz counts of the tracklist's recordings
(`album_tracks.recording_mbid ⋈ lb_recording`). "Recently added" has no
meaning for an album that was never added — its row appears when the
discography is minted, which says nothing the user did — so the shelf falls
back to release year (`PHANTOM_SORT_FALLBACK`), and the payload says which
sort the shelf actually took (`new_albums_sort`) so the tiles carry the
right glyph and metric. The fetch-on-view refresh passes the current sort,
so a reshuffled shelf keeps the order. Measured on Vangelis's 29 phantom
albums: every sort answers in a few ms.

### Last.fm authorization is callback-driven (2026-09-22)

The launcher's first-run dialog and the Profile sheet used to open the
Last.fm page and then ask the user to press "Complete" / "Finish" once they
had allowed access — the desktop flow, in which the app learns that the
browser step is over by asking a human. On the first launcher run of a
fresh install the click came ten seconds after the tab opened and Last.fm
answered `Unauthorized Token`; the second try, a minute later, answered the
same, and the dialog's only advice was "try again in Settings". Two things
were wrong, one hidden behind the other.

- **The completion event exists — Last.fm sends it.** The auth page takes a
  `cb` parameter and, once access is granted, redirects the browser to it
  with the authorised token (the web flow in Last.fm's own docs). The node
  now names itself as the callback, `<origin>/lastfm/auth/callback/<nonce>`,
  the origin being the one the CLIENT reached the node at — which only the
  client knows (127.0.0.1 for the launcher, the LAN or tunnel address for a
  phone, the front's name behind TLS) and which the Host guard vets. The
  callback exchanges the token, persists the session and wakes
  `/lastfm/auth/stream`; the dialog and the sheet read `/status` and close
  themselves. Nobody guesses. The route is unsigned by necessity (a redirect
  cannot carry HMAC headers) and admitted on the nonce: 128 bits, minted
  only for a signed caller, single use, gone with the flow; the page it
  renders names the user and nothing else (`backend/lastfm_auth.py`).
- **The desktop token stays as the fallback.** The page also carries a token
  this node minted (`auth.getToken`), so a Last.fm page that ignores `cb`
  can still be finished by hand ("Finish manually" in the launcher, "Finish
  without the redirect" in the sheet — both exchange that token). Kept until
  the callback is seen landing on a real account; then it goes.
- **Scrobbling never saw the in-app session.** `playback/tracker.py` built
  its network from `LASTFM_SESSION_KEY` in the environment, once, at the
  first scrobble — the session the flow had been persisting to
  `user_settings` since 2026-05-20 reached `/config` and nothing else. The
  Docker node never noticed because its key came from `.env` via the old
  CLI script. The scrobbler now follows `settings`, which the DB overlay
  fills at startup and the flow updates at runtime, and rebuilds its network
  when the session changes. The CLI script is gone (the file name now holds
  the flow), and the launcher no longer copies the session key into
  `config.json` and `backend.env`: it records the username, the credential
  stays in the database.

## Known Gotchas

- **Docker restores containers in no order, and a failed restore is final.**
  After a WSL restart Docker Desktop brings `restart: unless-stopped`
  containers back within seconds, before the distro's bind mounts exist and
  without the `depends_on` ordering `compose up` honours. On 2026-09-22
  postgres failed to start on its `001_initial.sql` mount and stayed down —
  the restart manager retries only a container that died after running —
  while the backend came up on its own, swallowed the failed DB check, timed
  out four more DB steps and died 25 s later on the first unguarded query,
  five times, until a human pressed start; `mb_backend` meanwhile had bound
  the HTTP API at import because its connection failed, and stayed there
  until a slice import called `refresh()`. Two rules came out of it: the
  process that launches uvicorn waits for postgres first (`entrypoint.py` in
  Docker, `service_manager._wait_for_postgres` in the launcher) and the
  lifespan treats the database as a precondition, never a step to log
  around — a connection failure propagates, only a missing table means
  "fresh install"; and the postgres container owns no bind mount from the
  checkout — the runner builds the schema on an empty database (rehearsed:
  20 files, 110 tables, 0.5 s), so `docker-entrypoint-initdb.d` did the same
  work a second time and was the only thing tying postgres to the distro's
  filesystem.
- **Loopback targets are addresses, never `localhost`.** Windows resolves the
  name to `::1` first, and every listener the launcher runs is IPv4-only (the
  bundled PostgreSQL on 127.0.0.1, uvicorn on 0.0.0.0, the media proxy), so a
  fresh connection first waits out a REFUSED IPv6 attempt — measured 2.05 s
  per connect on the launcher stand, 15 ms by address (2026-09-17). Three
  things turned that into the Web UI stalling in 2-second steps on every
  screen load: the psycopg2 pool keeps only `minconn` idle connections and
  closes the rest on `putconn`, so a burst opened a fresh server backend per
  request; the HMAC middleware read the token epoch from the DB on the event
  loop for EVERY request, so one slow connect froze the whole server, in-memory
  endpoints included; and `/health` opened its own connection inline. Docker
  never saw it — `sautium-postgres` resolves to one IPv4 address. The rules
  that came out of it: the launcher names loopback by
  `config_manager.LOOPBACK` (env, DSN, MCP config, db_init); nothing on the
  event loop touches psycopg2 (the epoch is a process cache primed at startup,
  health goes through the pool in a thread); the pool keeps eight idle
  connections. Chrome's own `localhost` handling was not measured.
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
  one that breaks. A node with a working lossless plugin masks a dead YouTube
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
- **A BYO plugin's fetch tool writes its tracebacks to STDOUT.** Every failure
  on the Windows launcher surfaced as a bare `rc=1`; the lossless plugin now
  runs its tool with a per-process config and reads stdout for the real error.

---

## References

- `CLAUDE.md` — project spec, tech stack, phase definitions
- `P2P_NETWORK.md` — P2P design decisions and architecture
- `git log` — what was done, when, by whom
