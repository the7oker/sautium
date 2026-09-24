# Sautium

AI-powered management system for a personal FLAC library (~30k tracks): audio
content analysis, semantic search, recommendations, HQPlayer integration,
serverless P2P network for sharing analytics between collectors.

Phases 1–3 (MVP + enrichment + audio analysis + HQPlayer + Web UI + launcher)
and P2P phases P0–P4 (sync, NAT, account system, E2E chat) are **done**, as is
the 2026-08 network run: relay forwarding, carry (push-seeding of sealed audio
analysis), peer-relays, and MB-slice replication. Currently iterating on
quality-of-life fixes and artist metadata enrichment.

See:
- `PROGRESS.md` — design decisions and lessons learned (non-P2P).
- `P2P_NETWORK.md` — P2P architecture, technology choices, security model.
- `git log` — authoritative "what changed and when".

---

## Tech Stack

- **Python 3.11+** (best ML library support)
- **PostgreSQL 18 + pgvector 0.8** (vector similarity + relational data; image `pgvector/pgvector:pg18` — the data volume is a PG18 datadir, keep the major)
- **NVIDIA RTX 4090** for GPU work (CLAP embeddings, BGE-M3 text encoding)
- **librosa** + CLAP zero-shot for audio feature extraction (no essentia)
- **CLAP** (`laion/clap-htsat-unfused`) — 512-d audio embeddings
- **BGE-M3** — 1024-d multilingual text embeddings (not sentence-transformers)
- **MADLAD-400-3B-MT** (`google/madlad400-3b-mt`, **Apache 2.0**) — local
  any-language→English query translation for the English-only CLAP text
  encoder (`backend/translation.py`), running on **CTranslate2 int8, CPU**
  (~3 GB RAM, 0 VRAM; one-time conversion from the BYO ~12 GB HF source,
  written next to the HF cache). No source-language detection: the model
  infers the input language, only the `<2en>` target prefix is set — that
  property is load-bearing, LID alternatives are rejected. **ASCII queries
  bypass MT entirely** (measured: int8 beam is not identity-safe for
  English; the diacritic-less fr/de residual is the accepted sound-scope
  trade). Profile policy: full pre-warms, standard lazy-loads on the first
  non-ASCII Sound query, lite never loads (UI shows `limited`, not
  warming). Replaced NLLB-600M 2026-07-18 — its CC-BY-NC license blocked
  monetization and its Cyrillic-only trigger silently skipped
  French/German/CJK queries; torch bf16 GPU serving (5.5 GB VRAM) replaced
  by ct2 int8 2026-07-21
- **anthropic SDK** for Claude API; Claude Code + MCP tools for the AI assistant (chat); **OpenAI Codex CLI** as the second selectable chat agent (`backend/codex_runner.py` — same MCP servers, `codex exec --json`, assistant prompt as `model_instructions_file` replacing codex's built-in prompt and re-read on every spawn, MCP tools forced DIRECT via `features.code_mode.direct_only_tool_namespaces` (never the code-mode `exec` deferral), auth via `codex login` / minted from `OPENAI_API_KEY`)
- **libtorrent** for DHT (NOT pure-python `kademlia` — incompatible with BT DHT)
- **Inno Setup** for the Windows installer (`desktop/installer/sautium.iss`)

---

## Architecture Rules

- **Senior-engineer level only.** Before writing code, consider scalability,
  existing patterns in the project, proper data types, indexes and edge cases.
  Match the style and abstractions of the surrounding code.
- **Event-driven over polling.** Prefer SSE, `LISTEN/NOTIFY`, WebSocket or
  direct HTTP push. Polling is banned in new code — the chat rewrite proved
  the ~8s latency cost is never acceptable.
- **No quick patches — fix the architecture, not the symptom.** Before
  writing code, run a critical professional review of the proposed
  approach: "is this fixing the cause, or hiding a symptom?". If hiding a
  symptom, escalate to the architectural fix. Forbidden anti-patterns:
  - `setTimeout`/`sleep` to "wait for state to settle" — that's polling
    with a hardcoded latency guess. The right fix is an event from the
    party that mutated the state.
  - Defensive guards (`if (a !== b) skip and retry`) used as the primary
    fix for a race — they let the race leak into structural noise. Make
    the race impossible at the source. Defensive guards are acceptable
    only as a secondary safety net, never as the only fix.
  - Two parallel patch sites for the same problem (e.g., a playlist
    refetch fired from both `player.js` and `app-shell.js`). One source of truth.
  - Catch-and-ignore (`except Exception: pass`, swallowing fetch errors)
    — that's not a fix, that's hiding the bug from logs.
  
  If the proper fix feels too large, **stop and ask Valerii** before
  falling back to a smaller patch. Do not silently downgrade. The cost
  of asking is one round-trip; the cost of a hidden race-condition bug
  found weeks later is hours of debugging without context.
- **Idempotent enrichment.** Every enrichment/sync task must be safe to re-run.
  "Skip if already done" is a **correctness property, not an optimization** —
  partial failures and resumes are normal operating conditions.
- **Persistent DB connections in long-lived services.** Opening a new
  connection per call costs real seconds (measured). Reuse a session/pool.
- **Push computation into SQL.** Filtering, ranking, scoring,
  multi-tier ordering and `LIMIT` belong in the query whenever the
  database can express them. Pulling thousands of rows into Python
  just to `sorted(rows, key=...)` and `rows[:5]` wastes bandwidth,
  burns memory and skips every index Postgres would have used.
  Concretely: weighted scores (`a.metric * b.weight`), two-tier
  ranks (`CASE WHEN signal>0 THEN 0 ELSE 1 END`), top-N cuts and
  text normalisation (`regexp_replace`, `lower`, article stripping)
  are all in-SQL operations. Python-side post-processing is only
  for formatting the response (`12h 4m`, `added 3d ago`) — never
  for ordering or trimming the result set.
- **Trust framework and internal guarantees.** Only validate at system
  boundaries (user input, external APIs, file system). Don't defensively
  re-check what the ORM, framework or internal caller already enforced.
- **No feature flags or back-compat shims** for code the single maintainer
  controls end-to-end. Change it, migrate the data, move on.
- **A hardware profile governs compute, never retention.** `full/standard/
  lite` decides what the node derives locally (analysis, pre-warm, thread
  caps); it never decides what the node stores. Rows the owner can see are
  deleted only by an explicit, confirmed user action (Settings › Library ›
  "Remove phantom layer") — no startup job, boot-streak counter or tier
  heuristic may drop them. The phantom layer's growth is the owner's switch
  (`discovery.phantom_layer`, default on). The lite phantom prune of
  2026-07-10 broke this rule and was removed 2026-08-27.
- **No mocking the database or external enrichment APIs in tests** that are
  supposed to verify DB behavior. Integration tests that run against a real
  pgvector DB catch migration-and-query mismatches that mocks hide.

---

## Code Quality Rules

- **Use dedicated tools over Bash shell-outs.** `Grep`/`Glob`/`Read`/`Edit`
  over `grep`/`find`/`cat`/`sed`. Bash only when there is no dedicated tool.
- **Don't write comments that explain WHAT.** Well-named identifiers already
  do that. Only comment when the WHY is non-obvious: a hidden invariant, a
  workaround for a specific bug, a surprising constraint.
- **Don't add error handling for scenarios that can't happen.** Trust internal
  code. `except Exception: pass` is almost always wrong — if something can
  fail, fail loudly.
- **No "just in case" abstractions.** Three similar lines are better than a
  premature helper. Only extract after the third real duplication.
- **No trailing summaries in responses.** The diff speaks for itself.
- **Prefer editing existing files over creating new ones.** New files
  fragment the codebase and are the path of least resistance when the right
  answer is "modify the existing module".
- **Delete code confidently when unused.** Backwards-compat shims,
  re-exported types, `# removed` marker comments and renamed `_unused` vars
  all belong in the commit history, not the working tree.
- **A doc sentence that states STATE gets corrected in the same commit.**
  Design rationale ages well — why libtorrent, why UUID v5, why HTTP on the
  LAN read as written yesterday. What rots is status in the future tense:
  "Phase 3 — next", "not yet implemented", "fix planned", "22 tools". A
  reader cannot tell a stale line from a current one, so when a change
  contradicts a sentence in `docs/` or a root doc, fix that sentence — don't
  open a documentation task. The trigger is a contradiction you can see, not
  "check the docs" on every task. Write status as a DATED FACT ("pair engine
  shipped 2026-07-17") rather than a tense ("next"): a dated line never lies,
  it only ages. The drift a commit-time fix cannot catch — a doc nobody had
  reason to open while the work happened — is what a periodic pass over the
  docs touched by `git log` since the last one is for.
- **Log at the right level.** `logger.debug` for per-row noise, `logger.info`
  for lifecycle events, `logger.warning` for recoverable anomalies,
  `logger.error` for actual failures. Don't log stack traces at `info`.
- **Rate-limit external APIs.** Last.fm: one pace per process for every
  request (`lastfm.lastfm_network`, 0.34 s ≈ 3 req/s, since 2026-09-24) —
  never a sleep in a caller's loop. Query translation: ASCII queries bypass
  MT entirely.

---

## Data Model Conventions

- **Normalized schemas, not JSONB blobs.** External metadata (Last.fm,
  MusicBrainz, ListenBrainz) lives in normalized tables (`artist_bios`,
  `artist_tags`, `similar_artists`, `album_descriptions`, `lb_recording`)
  with a `source` column or a `dump_version` for provenance. We migrated away from JSONB — functions on JSONB
  get unreadable fast and can't be indexed cleanly.
- **The Last.fm layer is LOCAL-ONLY.** `artist_bios`, `artist_tags`,
  `similar_artists`, `genre_descriptions` carry no seal columns and no
  `imported` flag, and stay out of the sync protocol, the holdings filter,
  carry, the share export and the seed bundle (since 2026-09-19, migration
  019): Last.fm's API terms do not allow redistributing its answers, so
  every node fetches its own by name — the layer is reproducible from the
  API for two calls per artist. The `listeners` count is still read
  LOCALLY as the rarity proxy (announce tail, carry order).
  `album_descriptions` and `album_genres` are local for a different
  reason: albums never sync by UUID.
- **Listening statistics come from ListenBrainz and DO travel.** Track
  popularity left Last.fm on 2026-09-20 (migration 020 dropped
  `track_stats`): `lb_recording` / `lb_artist` hold per-MBID listen and
  listener counts aggregated from the ListenBrainz statistics dump (CC0).
  They are LOWER BOUNDS — the dump carries each LB user's top-1000 list,
  not every listen — so consumers rank by them and never quote them as
  totals. A dump node (`backend/lb_dump_load.py`, opt-in like the MB
  dump, its own `listenbrainz.db_version` marker) serves per-artist-MBID
  signed slices (`desktop/p2p/lb_slice_queries.py`, context
  `sautium-lb-slice-v1:`); every other node runs the shared cycle
  (`desktop/p2p/lb_slice_cycle.py` — launcher AND Docker) for its owned
  and engaged artists, and for a phantom artist when its page is opened
  (`lb_slice_requests`). The ledger `lb_slice_fetches` is versioned:
  requests carry `min_version`, a source's `/health` says which dump it
  serves, and a row older than the newest reachable version is re-asked —
  a signed zero-match included. A SEPARATE family from the MB slices in
  every artefact (context, ledgers, `/api/lb/slice`, `lbdump`/`lbslices`
  capabilities): a node may hold either dump. Widening the blob means
  bumping the context AND wiping every node's ledgers.
- **UUID v5 for all shareable entities.** Same data on different nodes
  must collapse to the same ID. Namespace
  `adc1ec0b-2c81-5e26-9938-a369c6f7a5e1` (in `backend/uuid_utils.py`).
  Formula: `uuid5(NAMESPACE, "entity_type:{normalize(identifier)}")`.
  Covers: Artist, Album, Track, Genre, Tag, EmbeddingModel. `normalize`
  (v2, 2026-08-25) folds typography: apostrophes dropped, every other
  non-word character → space, collapsed — `Hello Dolly!`/`Hello Dolly`,
  `Don’t`/`Don't`/`Dont` are one id; a name that folds to nothing (`!!!`)
  keeps its key form. Identifier-like keys (model names, spec attribute
  keys, registry sources) use `normalize_key` (NFC/lower/trim only).
  **Changing either is an identity migration** — run
  `python -m canon.migrations --renormalize` with the backend stopped on
  every live node; the seals bind the uuids and get re-signed.
- **Album has no `artist_id`.** Artists derived via `track_artists` —
  compilations, features and collaborations work without nullable FKs.
- **Genre is a track property**, not album. Many-to-many via `track_genres`.
- **`ON UPDATE CASCADE` on all track/album UUID FKs.** Artist normalization
  rewrites UUIDs when cleaning names — cascade makes rewrites safe.
- **PostgreSQL ENUM over VARCHAR+CHECK** for constrained string columns
  (e.g. `artist_gender`, `artist_vocalist`). Type-level enforcement,
  smaller on-disk footprint, self-documenting schema.
- **`media_files.id` (SERIAL) identifies a FILE; `tracks.id` (UUID) identifies
  the MUSIC.** An owned file is reached by its media_file id, but everything
  above the file speaks the UUID — the canonical queue (`QueueItem.track_id`),
  play tracking, and the whole AI surface (search tools return it, play/queue
  tools take it, `[SAUTIUM_BLOCKS]` carries it). That is what makes not-owned
  ("phantom") music playable through the same tools: it has a track UUID and no
  file, so the player streams it. Never make an AI-facing or queue-facing API
  take a media_file id — that is the mistake that left the assistant unable to
  play anything the user did not own (fixed 2026-08-25).
- **CUE images = N virtual `media_files` rows on one `file_path`**, bounded
  by `cue_start_seconds`/`cue_end_seconds` (NULL for regular files, end NULL
  = to EOF; `UNIQUE NULLS NOT DISTINCT (file_path, cue_start_seconds)`).
  The cue governs the image (scanner reconciliation supersedes whole-image
  rows + their analysis); a slice is always consumed as its own resource —
  ffmpeg `-ss/-t` locally, a cached FLAC cut (`flac_slice_path_for_file`)
  for HQPlayer/DLNA/browser. The chromaprint is per-slice. See
  `PROGRESS.md` § CUE images.
- **`audio_features.vocal_instrumental` is unreliable.** For vocal/
  instrumental queries, use `artists.is_vocalist` (classified from bio
  keywords). See `PROGRESS.md` design decisions.

---

## Migration & DB Workflow

- **Schema = `desktop/migrations/001_initial.sql` + numbered deltas.** 001
  is the fresh-install baseline and the single readable source of truth —
  fold every schema change into it AND ship the same DDL as an IDEMPOTENT
  `NNN_<change>.sql` delta for the external nodes that already ran the
  earlier files. One runner applies pending files on every node; never
  ALTER a node by hand. The runner, data-migration steps, the rehearsal
  database and the ENUM change sequence load with `desktop/migrations/`
  (`.claude/rules/db-migrations.md`).
- **pgvector parameter binding**: `text("... <=> :qvec::vector ...")` plus
  `db.execute(sql, {"qvec": _to_vector_param(v), ...})`. The double-colon
  cast is mandatory.
- **Batch inserts** via `psycopg2.extras.execute_values` with 500 rows per
  batch. A single connection per sync run, not per category.
- **`regexp_count` escaping**: double-escape word boundaries in Python
  strings (`'\\yword\\y'`), single-escape in raw `.sql` files (`\yword\y`).

---

## Enrichment Pipeline Conventions

- **Incremental by default.** Query for "what's missing", process that,
  commit. Never re-process data that already exists unless `--force`.
- **Per-track processing**, not batch-by-batch — gives resumability and
  error isolation. A failure on one track must not stop the run.
- **Each step checks its own precondition.** `enrich_bios` runs only if
  the artist has no bio, `classify_vocalist` runs only after bios exist, etc.
- **Explicit per-step stats.** Return a dict of `{step: count}` and log
  totals at the end — the caller is usually the launcher UI or a CLI
  progress callback.
- **Anything that fans out per artist must be gated on HUMAN ENGAGEMENT** —
  an OWNED file (`track_artists JOIN media_files`) or a COMPLETED listen
  (`listening_history.completed AND NOT skipped`, the scrobble rule) — never
  on `track_artists` alone: phantom tracklists made that column meaningless
  as "in catalog". Both signals are linear in user behavior, so a minted
  similar-stub only becomes a seed via a new human listen — no transitive
  expansion. Last.fm similars (`lastfm.backfill_similar`, the background
  `similar` step) and the sync's similars pull (`_engaged_artist_uuids`)
  carry this gate; the DHT announce tail is gated differently — on held
  analysis ("serveability", `ANNOUNCE_TAIL_SQL`), which name-only stubs
  never enter, and on the SIZE of the network (`desktop/p2p/network_size.py`:
  per-artist keys are announced AND searched only past ~1000 reachable
  nodes — below that the node key lists everyone and a per-artist key can
  name nobody it doesn't; the rare search set is the ENGAGED artists with
  an audio gap, never the phantom layer). Tail announces and rare lookups
  share one traversal lane in `dht_service` — one initiation per ≥3 s,
  alternating. Without a gate every hop multiplies by ~50 Last.fm entries
  and the pipeline walks all recorded music.
- **Streaming is a demo channel, and an excerpt is nothing.** The core
  provider (YouTube, `manifest.demo_limited`) streams a track in full at
  most ONCE — the listen is spent past 90 % (`demo_plays`, life data;
  `backend/streaming/demo.py`) — after which the track plays as the
  catalog's 30 s clip (`deezer_preview`, `manifest.excerpt`), the same
  fallback as when the core channel is missing or has nothing. A
  bring-your-own full-length provider is never limited. An excerpt
  (`FetchedAudio.excerpt`) is refused by `PreviewEnricher` before any
  decode or provenance row and ignored by the play tracker: no analysis,
  no listen, no scrobble. The ledger has no settings toggle — it is the
  product's legal posture, not a preference.
- **P2P-facing settings** (all in `user_settings`, defaults in
  `backend/routers/settings.py` `_DEFAULTS`): `sync.announce_limit` (rare
  artist DHT tail; 150 = half the traversal lane, announced only under the
  computed `p2p.rare_mode`), `sync.carry_limit` (foreign TRACKS carried, ~46 KB
  each), `p2p.relay_enabled` (relay role), `enrichment.reanalyze_imported`
  (re-derive first-hand analysis over P2P-imported — default off: the
  sync's whole point is not doing the work twice), `p2p.gate_mode`
  (`off|shadow|enforce`, default `shadow` — the admission gate prices
  strangers' requests in 64 MiB tasks; arming is a release decision, see
  P2P-SYNC-INTEGRITY.md § "Pricing formula v1"), `support.diagnostics_enabled`
  (default on: answer the master's signed diagnostic warrants and send
  content-free event reports — silent by design, the switch and the
  "Support" row are the disclosure; P2P_NETWORK.md § "Support diagnostics").

---

## Docker & Runtime Layout

- `sautium-backend` owns **play tracking** (`listening_history` + `local_play_stats` + Last.fm scrobbling) inside its status poller — keyed source-agnostically on `track_id`, so streamed phantom plays track like owned files. There is **no separate tracker daemon** (consolidated 2026-06-30; the old `sautium-playback-tracker` was retired).
- Launcher tree mounted read-only: `./desktop` → `/app/desktop:ro`, so the peer
  surface imports `desktop.sync_client.import_pushed`,
  `desktop.p2p.sync_queries` and the whole pull side,
  `desktop.p2p.sync_walk` (the Docker node walks the network like a launcher
  since 2026-09-08 — before that it only served and accepted carry), instead
  of mirroring them. Anything the two surfaces must agree on *exactly* — the
  seal-verification gate, the carry SQL, the DHT announce query, the walk —
  lives there and is imported, not copied.
- Identity documents: `./data/node_identity` → `/app/data/node_identity`
  (`birth_certificate.json` + `identity_proof.json` — the Docker node derives
  its KEY in memory, but the Worker-issued certificate and the mined
  proof-of-work must survive container recreation; same file names as the
  launcher so the export/import bundle moves between the two).
- **Never bake models into the image.** Cache lives on the host, survives container deletes.
- **Restart backend after model/prompt changes**: `docker restart sautium-backend` then check logs for "Application startup complete".

---

## Security Posture (read before touching network/auth)

The public model — surfaces, threat model, accepted limits — is in
`SECURITY.md`. The full picture (what is deployed today, the master/relay/
carry topology, host specifics) lives in the maintainer-private
`security-posture` skill and `docs/private/` (not in git): invoke the skill
before touching ports, auth, TLS, UPnP, or the peer surface. The hard rules
below bind with or without it.

**Hard rules — do not break without asking Valerii first:**

1. **Backend (8800) on `0.0.0.0` is intentional** — phone use over
   the home Wi-Fi is the primary workflow. Don't "tighten" it to
   `127.0.0.1` thinking you're improving security; you're breaking
   the product. Postgres (5432) **stays on `127.0.0.1`** — it has
   no remote-access reason (the former playback-tracker on 8765 is
   gone — tracking moved into the backend).
2. **Never let UPnP forward backend ports to the internet.**
   `desktop/p2p/p2p_manager.py` currently maps `[http_port]` only
   (P2P sync, which has its own auth). `docker_ports` from
   `desktop/config_manager.py:58` is for *localhost-only LAN
   discovery probes*, never for UPnP. If you ever combine them,
   you must add app-level auth to the backend first.
3. **Plain-HTTP media surfaces (8830, 8831) are LAN-only by design.**
   On a Windows host with Docker Desktop the published ports need a
   host-side forward plus firewall allows to be reachable over LAN IPv4
   (the maintainer's setup: `docs/private/DEPLOYMENT.md`).
   The media proxy (8830) serves phantom-preview buffers AND — since the
   DLNA output — owned-file bytes at `/file/{token}` plus cover art at
   `/art/{token}`; all gated by unguessable per-queue tokens (never the
   library at large) and exist because HQPlayer and DLNA renderers can
   neither sign HMAC nor trust the self-signed TLS. 8831 is the DLNA
   GENA event listener (renderer → backend callbacks). The launcher
   node uses its own pair — 8832 (media) / 8833 (GENA), `ports.media`/
   `ports.gena` in config.json → `MEDIA_PROXY_PORT`/`DLNA_GENA_PORT` —
   because the Docker node's forward holds 8830-8831 even while the
   container is down; the launcher auto-creates its firewall rule
   "Sautium (TCP 8832,8833)" (profile=any). Same LAN-only rules apply.
   None of these ports may ever be UPnP-forwarded or otherwise exposed
   beyond the LAN.
   The browser output adds NO new port: `<audio>` media rides the
   Web UI origin at `/api/player/media/{kind}/{id}` behind
   short-lived signed query params (`backend/media_urls.py`, 4 h TTL,
   HMAC from the same shared secret) because audio elements can't set
   the signing headers — that prefix is whitelisted in `auth_hmac.py`
   and does its own verification. A leaked media URL exposes one track
   for hours, never the API.
4. **The peer surface is the only network surface that *is* safe to
   expose to the public internet** — the launcher's sync server
   (random port 20000–29999, UPnP-mapped) and the Docker peer app
   (`backend/p2p_app.py`, port 8801, `python -m desktop.portmap map 8801`
   or a manual forward; portmap refuses any port serving the secret).
   Self-signed TLS **pinned to the node key** (the cert carries the
   node's Ed25519 signature over its own TLS key — `peer_auth`
   binding extension; peer clients verify it on every handshake, so
   the channel authenticates the server and `/health` cannot lie
   about `node_id`), per-IP rate limits, and per-endpoint auth: sync
   pulls are open by design (gated only by `sync.p2p_enabled`), while
   `/api/chat/*` and `/api/relay/*` require an invite-code↔pubkey
   binding plus, on the token/grant/wake/probe paths, an Ed25519
   signature over a **timestamp-bound** message (±60 s, mirroring the
   HMAC window). The peer surface may write ONLY to P2P domain tables
   (friends, p2p_messages, invite-token tables) behind that gating —
   it must never gain a route that reveals a secret or configuration.
   `/api/diag/*` (support diagnostics, `backend/routers/peer_diag.py`)
   follows the same rule: per-request Ed25519 via `verify_request`,
   friend-only, its own body caps, NaCl-boxed payloads the master alone
   can read, writes to `support_*` tables only, and it answers a bundle
   only for a warrant this node issued to that signer. A node never
   serves a diag route — it receives warrants solely on its own wake
   stream to the master and honours only the fixed scope enum
   (P2P_NETWORK.md § "Support diagnostics").
   Do not move backend endpoints into the peer surface, or vice
   versa, without redoing the auth story.
5. **No `Bearer` / cookie / `request.client.host` auth in Docker
   without thinking about NAT.** Containerised backend sees every
   request as coming from the docker bridge gateway
   (`172.x.0.1`), not the real client — `request.client.host`
   loopback checks silently allow everything. If app-level auth
   ever lands, do it with a shared secret (HMAC or signed token),
   not source-IP filtering. The master's peer surface runs behind
   a **trusted front** (`P2P_TRUSTED_FRONT`, `scripts/master-front/`)
   that rewrites `scope["client"]` from its own X-Forwarded-For, so
   there the address IS the real peer — still signals only (contact
   log, identity registry, pricing, backstops, similarity), never
   auth; the rule stands.
6. **Web UI lives at the same origin as the API.** CSRF is blocked
   by where the key lives: the device token is in `localStorage`,
   which is per-origin, so a foreign page cannot read it and cannot
   forge `X-Sautium-Sig` — and a request the browser sends on its own
   (a form post, an `<img>`) carries no signature at all. Don't
   replace this with cookies without thinking through
   `SameSite=Strict`: cookies ride along automatically, which is
   exactly the property being avoided here.
7. **Don't add Windows Firewall rules for 8800.** Windows mis-
   classifies networks as Public surprisingly often (Wi-Fi hand-off
   bugs, user clicks "Public" by mistake) — a profile-locked rule
   would silently break phone access at the worst moment. The
   bind-address layer is enough.
8. **The Web UI is plain HTTP — no browser certificate, ever.**
   A certificate a stock phone trusts cannot exist for a LAN address
   (public CAs may not issue one since 2015; a public name resolving
   to a private address is blocked by Fritz!Box/OpenWrt/pfSense-class
   routers), and the self-signed one only produced the warning that
   this replaced (PROGRESS.md § "HTTP on the LAN", 2026-09-17). What
   protects the credentials instead is the boxed exchange in
   `device_auth.py` ("Credential channel") + `auth.js`: password or
   PIN in and the token out ride a NaCl box to a per-exchange key the
   node's identity signs, and the browser pins that identity on first
   sign-in (a different one raises the known-hosts dialog). Signing
   itself never needed the transport — the token never travels. Do
   not add a self-signed listener back "for security": it adds a
   warning and no protection the box does not give. TLS for the Web
   UI is a deployment front with a real name (Tailscale Serve, a
   reverse proxy) in front of the HTTP port, its name listed in
   `SAUTIUM_ALLOWED_HOSTS`. `backend/tls_gen.py` now mints only the
   Docker peer-surface cert (pinned to the node key; the SAN is static
   because peers never read it), and the launcher never generates a
   certificate for the backend. Secure-context browser APIs
   (`crypto.subtle`, `navigator.clipboard`, `getUserMedia`, service
   workers) are unavailable on the http origin: HMAC comes from
   `sha256.js`, copy has an `execCommand` fallback, and anything
   needing a microphone would need the TLS front.

**Before any public release, multi-user deployment, or remote-access
feature** (Tailscale exposure, "headless mode", reverse proxy), the
rules above are no longer sufficient. HMAC over plain HTTP as
currently deployed is LAN-only by design: a node has exactly ONE
account — device tokens tell browsers apart, never people, so there
is nothing to grant, revoke or audit per user. Public exposure needs:
real per-user credentials, TLS from a front with a real name
(Tailscale Serve, a reverse proxy, a CA-signed cert), and a token
lifetime shorter than "forever, until someone bumps the epoch".
Revisit this section then.

---

## Public repository rules (the tree is public — every commit ships)

1. **Provider vocabulary follows the architecture.** Streaming providers are
   plugins behind a registry, so core code, UI copy and docs name the ROLE —
   "a lossless provider", "a provider plugin", "lossless before lossy" —
   because core genuinely is bound to none of them; a brand name there would
   describe coupling that does not exist. Where an integration IS real, name
   it plainly, with what it gives and on what terms: the **Deezer public API**
   for artist images, catalog lookup and 30 s previews, no auth
   (`deezer_photos.py`, `covers.py`, `streaming/deezer_catalog.py`,
   `streaming/deezer_preview.py`); **YouTube** as the demo channel
   (`streaming/demo.py`). Credit each one in `THIRD_PARTY_NOTICES.md` — a
   lawful call to a public API is attribution, not exposure. Data literals
   stay as they are (`origin='deezer'`, provider ids, enum values,
   `PROVIDER_NAMES`) — never rename stored values.

   What must stay out of the tree is a **capability, not a name**: credentials
   for someone's paid account, stream decryption, or bulk retrieval of a
   licensed catalog. This project does none of that — the ledger in
   "Enrichment Pipeline Conventions" (one full demo listen per track, then a
   30 s excerpt) is the posture, stated openly rather than worked around. A
   bring-your-own module a user installs for themselves is reached through the
   plugin contract and never mirrored here.
2. **No host specifics in tracked files.** No LAN / WSL / Tailscale addresses,
   tailnet hosts, checkout paths, machine or third-party names. Use
   `<windows-host-ip>`, `<lan-ip>`, `<repo>`, fictional names in mocks.
3. **No secrets, ever.** Keys, tokens, provider credentials, passwords
   (other than the documented loopback default) never enter the tree;
   `gitleaks git` with
   `.gitleaks.toml` must be clean before every push.
4. **No vendored third-party material.** Manuals, artwork, photos and SDK
   sources are linked, not committed; design references use the placeholders
   in `placeholders/`. A new dependency gets a line in
   `THIRD_PARTY_NOTICES.md`; no GPL Python library without asking Valerii.
5. **Commit trailers.** `Co-Authored-By` is fine; `Claude-Session:` is
   stripped by the commit-msg hook (`scripts/git-hooks/commit-msg`, enabled
   with `git config core.hooksPath scripts/git-hooks`) — never bypass it.
6. **Private material** (deployment specifics, defense status, marketing,
   secrets) lives in `docs/private/` or `docs/marketing/` — both junctions
   into the private repo and gitignored — never in the public tree.

---

## UI / Frontend Conventions

The web UI lives in `backend/static/`, served directly by FastAPI.
**Vanilla HTML + CSS + JS, no build step, no npm, no framework.** Keep
it that way — we rejected a React migration in favour of this
simplicity, and Claude Design's handoff format is plain HTML anyway
(see `docs/design/reference/claude-design-bundle/README.md`).

The conventions — design tokens (`backend/static/tokens.css`), the
locked design-pixel scaling model, typography, the reference-first
workflow for implementing a screen, navigation, and the view-layer
split (`app-shell.js` / `player.js`) — live in
`backend/static/CLAUDE.md`, loaded whenever you touch that directory.

One rule is binding everywhere, so it stays here: **never call
`alert()`, `confirm()`, or `prompt()`** — they render in OS chrome and
break the design language. Use `window.confirmDestructive()` for a
decision, `window.notifyDialog()` for an instruction that must not be
missed, and `window.notices.toast()` for everything else — a passive
toast takes no pointer events and never blocks the user (see
`backend/static/CLAUDE.md` § Notices).

---

## Key Files

| File | Purpose |
|------|---------|
| `backend/device_auth.py` | Device tokens (derived from the epoch + node key, never stored) and the boxed credential channel every login/pair/create-account/logout-all/change-identity exchange rides — the Web UI has no TLS (rule 8) |
| `desktop/agent_login.py` | Headless CLI sign-in driver (`claude auth login`, `codex login [--device-auth]` over pipes) shared by the wizard and the backend's `/api/settings/ai/<agent>/signin` — no console, completion is an event |
| `docs/design/POSITIONING.md` | Product positioning + UI design principles (source of truth) |
| `docs/design/INFORMATION-ARCHITECTURE.md` | Navigation model, screen inventory, state flows (source of truth for UI layout) |
| `docs/design/reference/claude-design-bundle/` | Claude Design handoff bundle — visual-intent reference |
| `backend/master_node.py` | Shipped master identity pins (mirrored in desktop/p2p/) |
| `backend/invite_tokens.py` | Invite tokens + signed grants (mirrored in desktop/p2p/) |
| `desktop/mb_slice_client.py` | Slice requester — per-name verification against the ORIGINAL dump node's key |
| `backend/lb_dump_load.py` + `backend/dump_common.py` + `backend/dump_job.py` | The ListenBrainz statistics loader (stream the archive once, COPY two members into staging, `GROUP BY` into `lb_recording`/`lb_artist`, swap), the download/checksum/index mechanics both loaders share, and the one background-job runner both dump families use (queued one at a time) |
| `desktop/p2p/lb_slice_queries.py` + `desktop/p2p/lb_slice_cycle.py` | The LB slice protocol (per-artist-MBID signed blobs, versioned serve/verify/import, the pending tiers) and the requester cycle shared by the launcher's P2PManager and the Docker backend (sources, `min_version`, the on-demand lane) |
| `backend/assistant_queries.py` | Catalog queries + result formatting SHARED by both assistant tool surfaces (MCP + `backend/tools/definitions.py`) — one copy, so neither drifts owned-only |
| `backend/notary.py` + `backend/sign_audio.py` | The sealing owner (one thread, woken by every producer of signable state) and its two stages: `sign()` author-signs at once, `stamp()` Merkle-batches + Worker-timestamps on the notary's cadence |
| `desktop/node_backup.py` | Node backup format v1 (`.sbk`: Argon2id KEK in its own salt domain, per-file data key, chunked XChaCha20-Poly1305 with the header as AAD, framed members) + `pg_dump`/`pg_restore` drivers, staged restore, identity write, selftest — shared by launcher and backend (`docs/design/BACKUP.md`) |
| `backend/backup.py` | The node binding and the ONE "make a backup": `python -m backup create\|inspect\|restore\|selftest\|export\|import\|merge` on either interpreter (loads `<data_dir>/backend.env` under the launcher), password check via `device_auth.verify_password`, the playback signal as a PostgreSQL session advisory lock (`PlaybackSignal` held by the backend, `PlaybackHold` waited on by the job). The weekly task calls `create --password-env P2P_PASSWORD` — a contract. No Web UI |
| `backend/share.py` | Share export/import (BACKUP.md Product B): gzip'd JSON lines with a signed trailer, streamed both ways; every sealed record the node holds under its author's seal, structural rows from the seed builders; import through `SyncClient._import_items` (the gate) after a verify pass, default (add phantoms) or `--existing-only` (create no artist/album/track) |
| `backend/life_merge.py` | Own life-data merge (BACKUP.md Product C): `python -m backup merge <own .sbk>` — same-account rule, the dump streamed once through `pg_restore --data-only -t …` into a scratch schema in the live database, keyed union (listens, sessions, friends, messages, chats, gear, allowlisted settings, rotation records) in one transaction, unknown tracks wait; `--dry-run` = rollback |
| `backend/play_stats.py` | `local_play_stats` is DERIVED from `listening_history` — the one statement the play tracker and the merge share; never increment those counters in place |
| `backend/streaming/demo.py` | The demo policy: the `demo_plays` ledger (one full demo-channel listen per track), the resolve's per-track provider order, the proxy's fetch-time gate (`link_admissible`), the status observer that spends the listen past 90 % and drops the spent buffer |
| `backend/streaming/deezer_catalog.py` + `deezer_preview.py` | Deezer public API in core: the catalog resolve (barcode → album tracklist → track gate, one pacer + memo per process) shared as `DeezerCatalogProvider` by the BYO lossless module and the core 30 s excerpt provider (`deezer_preview`, `manifest.excerpt`, always last in `providers_preferred()`) |

---

## Testing Expectations

- **Real DB, not mocks**, for anything that touches SQL or pgvector.
  Integration tests run against the live docker container; unit tests are
  for pure logic (bio classifier regex, UUID formulas, path parsing).
- **Spot-check on known examples.** Adele/Sade/Led Zeppelin should classify
  as `vocal`; Jon Hopkins/Tangerine Dream as `instrumental`; ABBA as mixed
  gender; Pink Floyd as mixed with vocals. If a classifier change flips one
  of these, investigate before committing.
- **Re-run enrichment on a subset** to verify idempotence before touching
  the full library.

---

## Language

Interactions with the user (Valerii) are in **Ukrainian**. Code, identifiers,
commit messages, log messages, comments and doc files stay in English.
