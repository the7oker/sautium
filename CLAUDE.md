# Sautium

AI-powered management system for a personal FLAC library (~30k tracks): audio
content analysis, semantic search, recommendations, HQPlayer integration,
serverless P2P network for sharing analytics between collectors.

Phases 1–3 (MVP + enrichment + audio analysis + HQPlayer + Web UI + launcher)
and P2P phases P0–P4 (sync, NAT, account system, E2E chat) are **done**, as is
the 2026-08 network run: relay forwarding, carry (push-seeding of sealed audio
analysis), peer-relays, and MB-slice replication. Currently iterating on
quality-of-life fixes and artist metadata enrichment. Voice interface
(Whisper + TTS) and file sharing over libtorrent remain on the roadmap.

See:
- `PROGRESS.md` — design decisions and lessons learned (non-P2P).
- `P2P_NETWORK.md` — P2P architecture, technology choices, security model.
- `git log` — authoritative "what changed and when".

---

## Tech Stack

- **Python 3.11+** (best ML library support)
- **FastAPI** backend (async)
- **PostgreSQL 18 + pgvector 0.8** (vector similarity + relational data; image `pgvector/pgvector:pg18` — the data volume is a PG18 datadir, keep the major)
- **SQLAlchemy** ORM + `psycopg2` for raw SQL and batch operations
- **Docker + Docker Compose** (WSL2 on Windows, native on macOS)
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
- **anthropic SDK** for Claude API; Claude Code + MCP tools for the AI assistant (chat); **OpenAI Codex CLI** as the second selectable chat agent (`backend/codex_runner.py` — same MCP servers, `codex exec --json`, DJ prompt via AGENTS.md, auth via `codex login` / minted from `OPENAI_API_KEY`)
- **libtorrent** for DHT (NOT pure-python `kademlia` — incompatible with BT DHT)
- **CustomTkinter + PyInstaller** for the Windows desktop launcher
- **aiohttp + PyNaCl + miniupnpc** for the P2P layer
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
- **Log at the right level.** `logger.debug` for per-row noise, `logger.info`
  for lifecycle events, `logger.warning` for recoverable anomalies,
  `logger.error` for actual failures. Don't log stack traces at `info`.
- **Rate-limit external APIs.** Last.fm: 0.2s delay (~5 req/sec). Claude
  Haiku translation: non-Cyrillic queries bypass the call entirely.

---

## Data Model Conventions

- **Normalized schemas, not JSONB blobs.** External metadata (Last.fm,
  MusicBrainz) lives in normalized tables (`artist_bios`, `artist_tags`,
  `similar_artists`, `album_info`, `track_stats`) with a `source` column
  for provenance. We migrated away from JSONB — functions on JSONB get
  unreadable fast and can't be indexed cleanly.
- **UUID v5 for all shareable entities.** Same data on different nodes
  must collapse to the same ID. Namespace
  `adc1ec0b-2c81-5e26-9938-a369c6f7a5e1` (in `backend/uuid_utils.py`).
  Formula: `uuid5(NAMESPACE, "entity_type:{normalize(identifier)}")`.
  Covers: Artist, Album, Track, Genre, Tag, EmbeddingModel.
- **Album has no `artist_id`.** Artists derived via `track_artists` —
  compilations, features and collaborations work without nullable FKs.
- **Genre is a track property**, not album. Many-to-many via `track_genres`.
- **`ON UPDATE CASCADE` on all track/album UUID FKs.** Artist normalization
  rewrites UUIDs when cleaning names — cascade makes rewrites safe.
- **PostgreSQL ENUM over VARCHAR+CHECK** for constrained string columns
  (e.g. `artist_gender`, `artist_vocalist`). Type-level enforcement,
  smaller on-disk footprint, self-documenting schema.
- **Playback uses `media_files.id` (SERIAL)**, not `tracks.id` (UUID).
  The track UUID is for logical identity; the media_file id is for the
  physical audio file on disk.
- **CUE images = N virtual `media_files` rows on one `file_path`**, bounded
  by `cue_start_seconds`/`cue_end_seconds` (NULL for regular files, end NULL
  = to EOF; `UNIQUE NULLS NOT DISTINCT (file_path, cue_start_seconds)`).
  The cue governs the image (scanner reconciliation supersedes whole-image
  rows + their analysis); a slice is always consumed as its own resource —
  ffmpeg `-ss/-t` locally, a cached FLAC cut (`flac_slice_path_for_file`)
  for HQPlayer/DLNA/browser. pcm_hash/chromaprint are per-slice. See
  `PROGRESS.md` § CUE images.
- **`audio_features.vocal_instrumental` is unreliable.** For vocal/
  instrumental queries, use `artists.is_vocalist` (classified from bio
  keywords). See `PROGRESS.md` design decisions.

---

## Migration & DB Workflow

- **Schema lives in `desktop/migrations/001_initial.sql`.** It's the single
  source of truth for a fresh install, and the ONLY migration file: while the
  product is pre-release, the Docker DB is the one live instance that gets
  ALTERed by hand, and every other install is dropped and recreated on schema
  change — so numbered follow-ups carry no value. Fold the change into 001 and
  apply the equivalent DDL to Docker.
- **Temporary migrations go to `/tmp/*.sql`**, never into the repo.
  Use `docker exec -i sautium-postgres psql -U musicai -d music_ai < /tmp/foo.sql`
  to apply, then delete the tmp file and stage the equivalent change in
  `001_initial.sql`.
- **PostgreSQL ENUM type changes** require this exact sequence (a straight
  `ALTER TYPE` fails):
  ```
  ALTER TABLE t ALTER COLUMN c DROP DEFAULT;
  ALTER TABLE t DROP CONSTRAINT IF EXISTS chk_c;
  ALTER TABLE t ALTER COLUMN c TYPE new_enum USING c::new_enum;
  ALTER TABLE t ALTER COLUMN c SET DEFAULT 'x'::new_enum;
  ```
- **SQLAlchemy ENUM** uses `postgresql.ENUM(..., create_type=False)` — the
  SQL migration owns type creation so `Base.metadata.create_all()` stays
  lightweight and tests don't try to recreate existing types.
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
- **Post-import hooks.** After importing data from a peer (sync_client),
  re-run derived classifiers like `_update_artist_gender` and
  `_update_artist_is_vocalist` on the freshly-imported rows only.
- **Anything that fans out per artist must be gated on OWNED music**
  (`track_artists JOIN media_files`), never on `track_artists` alone —
  phantom tracklists made that column meaningless as "in catalog". This is
  why Last.fm similars, the DHT announce tail and the sync's similars pull
  all carry the same join. Without it every hop multiplies by ~50 Last.fm
  entries and the pipeline walks all recorded music.
- **P2P-facing settings** (all in `user_settings`, defaults in
  `backend/routers/settings.py` `_DEFAULTS`): `sync.announce_limit` (rare
  artist DHT tail), `sync.carry_limit` (foreign TRACKS carried, ~46 KB
  each), `p2p.relay_enabled` (relay role), `enrichment.reanalyze_imported`
  (re-derive first-hand analysis over P2P-imported — default off: the
  sync's whole point is not doing the work twice), `p2p.gate_mode`
  (`off|shadow|enforce`, default `shadow` — the admission gate prices
  strangers' requests in 64 MiB tasks; arming is a release decision, see
  P2P-SYNC-INTEGRITY.md § "Pricing formula v1").

---

## Docker & Runtime Layout

- `sautium-postgres` — PostgreSQL 18 + pgvector. Credentials `musicai/supervisor`, DB `music_ai`. Persistent volume `./data/postgres/`.
- `sautium-backend` — FastAPI on `0.0.0.0:8000`, exposed to host at `localhost:8800`. GPU access via NVIDIA runtime. Also owns **play tracking** (`listening_history` + `local_play_stats` + Last.fm scrobbling) inside its status poller — keyed source-agnostically on `track_id`, so streamed phantom plays track like owned files. There is **no separate tracker daemon** (consolidated 2026-06-30; the old `sautium-playback-tracker` was retired).
- Music library mounted read-only: `E:\Music` → `/music:ro` inside backend.
- Launcher tree mounted read-only: `./desktop` → `/app/desktop:ro`, so the peer
  surface imports `desktop.sync_client.import_pushed` and
  `desktop.p2p.sync_queries` instead of mirroring them. Anything the two
  surfaces must agree on *exactly* — the seal-verification gate, the carry SQL,
  the DHT announce query — lives there and is imported, not copied.
- Model cache external: `./data/cache` → `/root/.cache`.
- Identity documents: `./data/node_identity` → `/app/data/node_identity`
  (`birth_certificate.json` + `identity_proof.json` — the Docker node derives
  its KEY in memory, but the Worker-issued certificate and the mined
  proof-of-work must survive container recreation; same file names as the
  launcher so the export/import bundle moves between the two).
- **Never bake models into the image.** Cache lives on the host, survives container deletes.
- **Restart backend after model/prompt changes**: `docker restart sautium-backend` then check logs for "Application startup complete".

---

## Security Posture (read before touching network/auth)

**Current state (as of 2026-08-21):** the backend on port 8800 has
**HMAC-SHA256 request signing** (`backend/auth_hmac.py`) and serves
**HTTPS only** with a self-signed cert (`backend/tls_gen.py`).
`backend/static/auth.js` monkey-patches `window.fetch` to sign every
request as `hex(HMAC-SHA256(key, METHOD\nPATH\nTS\nsha256(body)))`
with a 60s replay window.

The key is a **per-browser device token**, not a shared secret. The
page carries no key at all: a browser earns its token once, with the
account password (checked by re-deriving the identity — there is no
password hash anywhere) or a pairing PIN shown on the host, and keeps
it in `localStorage`, which is bound to an origin. The server never
stores tokens; it derives them as
`HMAC(server secret, "sautium-device:v1:{epoch}:{node pubkey}")`, so
bumping `auth.token_epoch` ("log out everywhere") invalidates every
copy at once, and a **different node cannot mint the same token** —
that is what the node pubkey is doing in there. Deleting a node and
creating another one therefore revokes access, which it did NOT
before 2026-08-21: both inputs used to be node-independent, so a
recreated node handed every previously paired browser full access on
a page refresh.

The **server secret** (`<p2p_identity_dir>/.api_secret`, 0600) sits
beside the identity because it is part of who the node is, not of the
code it runs — it used to live in `backend/data/` inside the checkout,
where it outlived every uninstall and was SHARED by the launcher and
Docker nodes on one machine (compose bind-mounts `./backend`). It is
still accepted as a signing key on its own, for callers that already
live on the host and read the file (launcher, MCP server): whoever can
read it has the host anyway. Four readers, all resolving the same
path — `main`, `media_urls`, `desktop/api_client`, `mcp/assistant_server`.

Unsigned requests are accepted only on the whitelist
(`WHITELIST_EXACT`/`WHITELIST_PREFIX` in `auth_hmac.py`): the page and
its static assets, `/health`, the credential checks themselves
(`/api/auth/status|login|pair|create-account` — a client with no token
cannot sign, so these defend themselves: Argon2id under a semaphore,
five PIN attempts under a lock, account creation only while the node
has no identity), and the routes that carry their own signatures
(`/api/player/media/` query params, the peer `/api/sync/` surface).

HTTPS is required because browsers gate `crypto.subtle` (the API
auth.js needs to compute HMAC) behind secure contexts — over plain
HTTP from a phone on LAN nothing can sign and the Web UI dies silently
with 401s. **HTTPS is the only listening protocol** — uvicorn binds
with `--ssl-keyfile`/`--ssl-certfile`, no HTTP fallback.

The defence layer is still the network: backend is reachable on the
LAN (so phones/tablets can use the Web UI), but **never exposed to
the public internet**. Device tokens raise the bar for hostile LAN
devices — they now have to pass the password or PIN check — but they
are not a substitute for network isolation: the self-signed cert
protects transport only, and any process running as this user can
read the secret file and sign as the host.

**The threat model we defend against right now:**

- ✅ Random internet scanners / "young hackers" probing the public
  IP — blocked because nothing forwards 8800 outside the router.
- ⚠️ Other devices on the same LAN — they can load the page, and
  that is all: it carries no key, so they must pass the account
  password or a PIN read off the host screen to earn a token. What
  is left is the strength of that password and the window in which
  a PIN is live. Accepted: this is a single-user home appliance,
  the bar is "no random scanner", not "no targeted LAN attacker".
- ✅ DNS rebinding — a page on a hostile domain whose name resolves
  to this node, talking to us from the victim's own browser. Signed
  routes never fell to it (the device token is in localStorage, bound
  to an origin the attacker lacks), but the whitelist did, and that is
  where create-account/login/pair live. `_host_allowed` in
  `auth_hmac.py` runs BEFORE the whitelist and accepts only addresses
  that are OURS — own interfaces, `SAUTIUM_HOST_IPS` (names resolved
  once at startup), `SAUTIUM_ALLOWED_HOSTS`, loopback. NOT "is it
  private": that says yes to any RFC1918 name an attacker points at
  us and no to 100.64/10, which is how a phone reaches this node over
  Tailscale. Nothing ever resolves a client-supplied name. The peer
  surface (8801) has no such guard and must not get one — it answers
  to whatever Host a UPnP-mapped port produces.
- ❌ Malicious browser extensions / processes on the host machine —
  also out of scope.

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
   On this host, Docker Desktop's published ports listen IPv6-only on
   the Windows side: LAN IPv4 reachability rides `netsh portproxy`
   rules (0.0.0.0:PORT → WSL-VM IP) plus inbound firewall allows —
   "Music AI DJ" covers 8800; "Sautium Media (DLNA)" covers 8830-8831
   (profile=any, added 2026-07-12 — without it DLNA renderers accepted
   commands but could never fetch the audio bytes). If the WSL VM IP
   changes (reboot), the portproxy `connectaddress` must be updated.
   The media proxy (8830) serves phantom-preview buffers AND — since the
   DLNA output — owned-file bytes at `/file/{token}` plus cover art at
   `/art/{token}`; all gated by unguessable per-queue tokens (never the
   library at large) and exist because HQPlayer and DLNA renderers can
   neither sign HMAC nor trust the self-signed TLS. 8831 is the DLNA
   GENA event listener (renderer → backend callbacks). The launcher
   node uses its own pair — 8832 (media) / 8833 (GENA), `ports.media`/
   `ports.gena` in config.json → `MEDIA_PROXY_PORT`/`DLNA_GENA_PORT` —
   because the Docker portproxy holds 0.0.0.0:8830-8831 even while the
   container is down; the launcher auto-creates its firewall rule
   "Sautium (TCP 8832,8833)" (profile=any). Same LAN-only rules apply.
   None of these ports may ever be UPnP-forwarded or otherwise exposed
   beyond the LAN.
   The browser output adds NO new port: `<audio>` media rides the
   existing HTTPS origin at `/api/player/media/{kind}/{id}` behind
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
8. **TLS cert SAN — only private IPs.** `backend/tls_gen.py`
   filters auto-detected and explicit (`SAUTIUM_HOST_IPS` env)
   addresses through `ipaddress.ip_address().is_private`. Never
   add a public IP or DNS name to the SAN — a valid cert for a
   public hostname would make accidental internet exposure feel
   "safe" when it isn't (HMAC + LAN bind are still the actual
   defence). Static SAN entries: `localhost`, `host.docker.internal`,
   `127.0.0.1`, `::1`. Cert lives in `data/tls/` (Docker bind-mount)
   or `<launcher data_dir>/tls/` (launcher mode); the two runtimes
   are isolated and have separate certs — phone accepts one warning
   per mode, then both stick.

**A Docker node is a full peer on its own** (since the peer-port
split, 2026-07-27). libtorrent IS installed in the image
(`backend/Dockerfile`), DHT announce/lookup runs from
`backend/dht_service.py`, and `backend/p2p_app.py` serves sync, MB
slices and — since the invite-token work — the chat/relay protocol
(`backend/routers/peer_chat.py`) on 8801. What Docker still lacks is
UPnP (SSDP multicast doesn't survive the bridge), so reaching it from
the internet needs `python -m desktop.portmap map 8801` from the host
or a manual router forward. On Windows a plain `netsh portproxy` +
firewall pair (rule #3's note) gets the port through, but Docker
Desktop's user-space hops then show every peer as the bridge gateway
— no addr/subnet axes, a per-address backstop that is really global,
a probe-connect calling back the wrong host. The master therefore
runs the **trusted front** instead (`scripts/master-front/`: Caddy as
a Windows service owns :8801, terminates the peer TLS and forwards
plain HTTP + X-Forwarded-For to the loopback-only upstream
`127.0.0.1:18801`; `.env` carries `P2P_SYNC_PUBLISH=127.0.0.1:18801`
+ `P2P_TRUSTED_FRONT=1`). Native-Linux Docker (iptables DNAT) sees
real addresses and needs none of this. See P2P_NETWORK.md § "Master
behind a trusted front".

**Master node + reachability.** The maintainer's Docker node ships as
a contact in every install: `master_node.py` (mirrored
`desktop/p2p/` ↔ `backend/`) pins its invite code, full pubkey and
public support-token UUID; `P2PManager._ensure_master_contact` seeds
it as a pending friend at start (silently — deleting it sets
`p2p.master_removed` and it never comes back on its own). Nodes that
cannot accept inbound connections (CGNAT) hold an outbound SSE
subscription to `/api/relay/wake-stream` and pull chat history when
pinged, and they **suppress their own DHT announces**
(`DHTService.set_announces_enabled`) — a dead address in the DHT
helps nobody. The reachability verdict lives in `user_settings`
(`p2p.reachability`) and comes from the router WAN address, the
DHT-observed external IP, `/api/relay/probe-connect` (the relay
connects back to the request's source address, BT-tracker style) and
passive inbound traffic.

**Any reachable node is a relay** (phase D, 2026-08-06) — the master
is only relay #0, and it carries clients **under the same cap as
everyone else**. A CGNAT client registers with K=2 peer relays on top
of the master by presenting a **voucher** (its own signature over
`sautium-relay-voucher:v1:{client}:{relay}:{until}`); the relay then
announces `Sautium-user:{invite}` on its behalf (BT DHT does not
verify infohash ownership — that is the feature) and serves the same
voucher at `GET /api/relay/voucher` as proof, so a sender verifies
authority before forwarding a byte. Announce lifetime = subscription
lifetime. A friend's bare subscription carries no voucher, costs no
cap slot and produces no announce — that is the phase-A wake channel,
a different thing living on the same endpoint. Details and the
adaptive-cap rule: `P2P_NETWORK.md` § "D: Peer-relays".

**Carry and MB slices are the two push/replicate paths** (see
`P2P_NETWORK.md`). Carry pushes SEALED audio analysis to a reachable
carrier; the offer round speaks recording MBIDs and the carrier
answers with its OWN existing track uuids, so its phantom catalogue
acts as the taste filter and nothing is minted remotely. MB slices are
signed **per artist** (`mb_slice_blobs` keeps verified blobs verbatim
with the ORIGINAL dump node's signature), so any node can re-serve
names it holds — replicas are asked before dump nodes. Two rules that
look like details and are not: **a name closes only on a signed
zero-match**, and **similars are pulled for OWNED artists only** —
without that gate each hop multiplies by a Last.fm list and the sync
becomes a breadth-first walk of all recorded music.

**Before any public release, multi-user deployment, or remote-access
feature** (Tailscale exposure, "headless mode", reverse proxy), the
rules above are no longer sufficient. HMAC + HTTPS as currently
deployed are LAN-only by design: the cert is self-signed, and a node
has exactly ONE account — device tokens tell browsers apart, never
people, so there is nothing to grant, revoke or audit per user.
Public exposure needs: real per-user credentials, a CA-signed cert
(Let's Encrypt or similar), and a token lifetime shorter than
"forever, until someone bumps the epoch". Revisit this section then.

---

## UI / Frontend Conventions

The web UI lives in `backend/static/`, served directly by FastAPI.
**Vanilla HTML + CSS + JS, no build step, no npm, no framework.** Keep
it that way — we rejected a React migration in favour of this
simplicity, and Claude Design's handoff format is plain HTML anyway
(see `docs/design/reference/claude-design-bundle/README.md`).

### Design tokens

`backend/static/tokens.css` is the canonical design-system source. It
exposes colours, typography, spacing, radii, shadows, motion, and
safe-area tokens as CSS custom properties. Load it first on every new
page: `<link rel="stylesheet" href="/static/tokens.css">`.

The palette and type scale are **locked** per
`docs/design/POSITIONING.md §"Colour palette (v1)"`. Do not introduce
new colour tokens — use existing ones, or raise the question first.

### Scaling model — locked design-pixels at and above baseline

The root font-size is driven by viewport width, with a single `--base`
knob:

```css
:root {
  --base: 13;
  --design-viewport: 360;
  --px: calc(1rem / var(--base));
  font-size: calc(100vw / 360 * 13 * 1px);   /* fluid below 360 */
}
@media (min-width: 360px) {
  :root { font-size: calc(var(--base) * 1px); }   /* locked at 13px */
}
```

Below 360px the root size scales fluidly so a tiny screen gets a
proportionally smaller copy of the design. **At and above 360px the
root size locks at 13px**, so design-pixel tokens (`19 * --px`,
`16 * --px`, etc.) render at exactly the same actual-pixel values on
any phone width — a 19px design title is 19px on a 360 device, on a
390 device, on a 430 device, and on a 768+ desktop. Containers
(`width: 100%`, `aspect-ratio: 1/1`, etc.) still expand naturally
with the viewport, so wider phones get more breathing room around the
same-sized typography and controls.

At ≥ 768px the body is centred at ~468px (`360 × 1.3`) so the design
does not float in a sea of empty desktop background.

**Why not pure fluid scaling?** Fluid scaling at all widths inflates
the design on phones wider than the 360 baseline (a 19px reference
title becomes ~23px on a 443px viewport), which contradicts pixel-
perfect parity with the reference HTML and pushes meta-row content
past the screen edge. Locking the size keeps both the typography
spec and horizontal layout predictable.

### How to write sizes

- **Never write raw `px` for layout.** Use `calc(N * var(--px))` for
  ad-hoc values or a pre-built `--space-*` / `--text-*` / `--radius-*`
  token.
- Raw `px` is acceptable only for true 1px hairlines where crispness
  matters, or for absolute anchor points (e.g., `max-width: 768px` in
  a media query — media queries must use `px`).
- Durations (`--dur-*`) and easing curves (`--ease-*`) are time-based
  and do not scale.
- Line-heights are unitless so they multiply with font-size; tracking
  uses `em` so it follows local font-size.

### Typography

Two font families, loaded via `<link>` in `<head>`:

- **Inter Tight** (sans) — all UI prose, titles, labels.
- **JetBrains Mono** — numeric readouts only: BPM, sample rate, bit
  depth, durations, cosine-similarity scores. The mono+blue combo is
  the signature "technical precision" token — do not use mono for
  prose.

The cool blue `#4A7FA7` accent is **reserved for technical numeric
data and focus states**. Never for brand, CTA, or decoration — that's
amber's job.

### Implementing a screen — reference-first workflow

**Mandatory** before writing any screen markup or styles:

1. **Open the reference HTML** for that screen under
   `docs/design/reference/claude-design-bundle/project/`. For Now
   Playing it's `Now Playing v4.html`; sessions cover the rest.
   Read the file **top to bottom** — DOM structure, every CSS class,
   every dimension, every colour. Do not implement from memory of
   what the design "felt like". The reference is pixel-perfect
   ground truth; producing anything looser is a process failure.
2. **Translate, do not paraphrase.** Each reference rule maps to our
   DS as: raw `px` → `calc(N * var(--px))`; `--color-*` and `--text-*`
   tokens stay; sizes that match an existing token use that token,
   sizes that don't get the explicit `calc()` form. Class names should
   stay close to the reference for grep-back-to-source.
3. **Inventory expected elements** before claiming "done". A list
   like "drag handle · chevron · menu · scrim · cover · lyrics btn ·
   title · artist · album+year (dim) · meta-row with divider above ·
   q-badge svg+H · key pill F + min · BPM num+label · energy +
   dots · progress 3px + circle head + halo · times mono blue ·
   total muted · repeat icon · transport prev 56 · play 68 · next 56
   · similar block with header divider · sim-rows with cover 44 +
   meta + score 0.94 + add btn" — every item must be present and
   styled, not paraphrased away.
4. **Cross-check tokens vs reference values.** When the reference
   uses `15px` and our DS has `--text-body: calc(15 * var(--px))`,
   prefer the token. When the reference uses `19px` (no token),
   write `calc(19 * var(--px))` explicitly — do not silently snap
   to the nearest token.
5. **Visual verify before declaring done.** Open both screens in
   browser at 360px viewport, compare side by side. The user
   expects pixel-perfect parity; "approximately right" is a bug.

This rule exists because building from memory produced a Now
Playing screen that missed eight visible elements (lyrics btn,
divider line, scrim, repeat icon, similar block, etc.) and got
proportions wrong on most of the others. Reference HTML is
non-negotiable input.

### Reference bundle

`docs/design/reference/claude-design-bundle/` preserves the latest
Claude Design handoff covering the full MVP screen set: Design
System v1 HTML, all four Session HTML files (Session 1: shell +
Home + Now Playing mini/expanded · Session 2 v3: Discovery + Artist
+ Album + Queue + Genre · Session 3 v2: AI sheet + More + Profile +
Settings + HQPlayer · Session 4: Friends + chat thread), plus the
canonical Now Playing v4 iteration and the cover/artist assets used
across mockups. Treat it as **reference for visual intent**, not
source to paste — recreate visual output in our tokens-based vanilla
stack.

### Navigation and screen architecture

`docs/design/INFORMATION-ARCHITECTURE.md` is the source of truth for
how the UI is laid out: the 4-tab bottom nav (Home · Discovery ·
Friends · More), the AI FAB overlay, the mini-player bar, the Now
Playing sheet state machine, URL hash routing, per-screen contents,
Play-vs-Queue action semantics, and the queue-history concept. Read
it before touching UI routing or screen layout.

### View-layer architecture

The top-down rebuild against the new design system and information
architecture is **done**, and the legacy single-file prototype
`app.js` is **gone**. The frontend is now three files in
`backend/static/`, loaded by `index.html` in this order:

- **`auth.js`** — HMAC `fetch` monkey-patch (see Security Posture).
- **`app-shell.js`** — the bulk of the app: hash routing (`render`,
  `navigateToEntity`, `registerScreen`), every screen renderer (Home,
  Discovery, Artist/Album/Genre detail, Friends, chat, Now Playing,
  Queue sheet), the AI overlay, and most screen-scoped `fetch` calls.
- **`player.js`** — transport/SSE primitives shared across screens:
  the `/api/player/status/stream` subscription plus `window.playerCmd`,
  `window.playTrack`, `window.togglePlayPause`, `window.fetchPlaylist`,
  `window.currentPlaylist`.

Keep transport primitives in `player.js` and screen logic in
`app-shell.js` — don't reintroduce a third catch-all module. The view
layer proper is `index.html` + `style.css` (+ `tokens.css`). Older
notes or commits that say `app.js` mean "now `app-shell.js` +
`player.js`".

### Native dialogs are an anti-pattern

**Never call `alert()`, `confirm()`, or `prompt()`.** Browsers
render them with the OS chrome (white panel, default sans-serif,
"localhost says" prefix on Chrome), which breaks the design
language the rest of the UI is built in — colour palette, type
scale, dark theme, terracotta accents all disappear the moment one
of these fires. They also block the JS event loop, can't be styled
or animated, can't carry rich content (icons, formatting, links),
and on mobile they're an interaction trap.

Use the HTML equivalents wired into the design system instead:

- **`window.notifyDialog({ title, message, kind })`** — replaces
  `alert()`. Single primary button, `kind` is `'error' | 'success'
  | 'info'` and tints the title via `.confirm-title.<kind>`.
  Returns `Promise<void>`.
- **`window.confirmDestructive({ title, message, confirmText,
  cancelText })`** — replaces `confirm()` for irreversible actions
  (delete friend, drop scan, reset key). Returns `Promise<boolean>`.
- **For text input** (`prompt()` replacement) build a small overlay
  in the `add-gear-sheet` style — see `openEmailVerifyFlow` and
  `openHqpConnectionEditor` in `app-shell.js` for the pattern.

Both dialogs live in `app-shell.js` and share the `.confirm-overlay
/ .confirm-sheet` shell in `style.css`. The `kind` accent and the
`.confirm-actions.single` modifier are the extension points — add
to those rather than minting parallel dialog systems.

Native dialogs are acceptable **only** when no HTML equivalent is
reachable — e.g. inside a Web Worker, or before `app-shell.js` has
loaded. In those rare cases, leave a `// native dialog: <reason>`
comment next to the call so future readers see it was deliberate.

Always escape user-controlled data with `window.escapeProfileHtml()`
before passing into `message` (both dialogs render `message` as
HTML so a `<b>highlight</b>` works — XSS is the caller's
responsibility).

---

## Key Files

| File | Purpose |
|------|---------|
| `backend/main.py` | FastAPI entry point |
| `backend/playback/` | Output-backend abstraction: PlaybackManager + canonical queue, HQPlayer backend, play tracker, listening sessions (HARDWARE-TIERS §2.6) |
| `backend/models.py` | SQLAlchemy ORM models |
| `backend/uuid_utils.py` | UUID v5 generators + normalization |
| `backend/lastfm.py` | Last.fm enrichment + bio-derived classifiers |
| `backend/search.py` | Hybrid audio + text semantic search |
| `backend/claude_dj_prompt.py` | System prompt + schema description for Claude Code + API variants |
| `backend/ensemble_instruments.py` | AST + PaSST instrument multi-label tagger (replaces CLAP zero-shot) |
| `backend/static/tokens.css` | Canonical design-system tokens (colours, type, spacing, scaling) |
| `docs/design/POSITIONING.md` | Product positioning + UI design principles (source of truth) |
| `docs/design/INFORMATION-ARCHITECTURE.md` | Navigation model, screen inventory, state flows (source of truth for UI layout) |
| `docs/design/reference/claude-design-bundle/` | Claude Design handoff bundle — visual-intent reference |
| `desktop/p2p/identity_pow.py` | Identity proof-of-work primitive (2 GiB Argon2id hashcash, difficulty = expected attempts) |
| `desktop/p2p/identity_proof.py` | Proof file + the background miner policy shared by launcher and Docker |
| `desktop/p2p/identity_registry.py` | `p2p_identities` registry + lazy `IdentityGate` (one-time proof verification, bans) |
| `desktop/p2p/peer_auth.py` | Wire format v1: peer request signatures, cert introduction, lanes |
| `backend/routers/sync.py` | Backend sync endpoints (P2P protocol) |
| `backend/routers/p2p.py` | Web UI Friends/Chat/invite-token endpoints |
| `backend/p2p_app.py` | Docker peer surface (8801): sync + chat/relay |
| `backend/routers/peer_chat.py` | Peer chat + `/api/relay/*` (mirrors sync_server) |
| `backend/master_node.py` | Shipped master identity pins (mirrored in desktop/p2p/) |
| `backend/invite_tokens.py` | Invite tokens + signed grants (mirrored in desktop/p2p/) |
| `backend/dht_service.py` | Docker backend libtorrent DHT integration |
| `desktop/launcher.py` | Windows launcher (CustomTkinter) |
| `desktop/node_identity.py` | Ed25519 identity + account system (Argon2id) |
| `desktop/sync_client.py` | Sync client + the seal-verifying import gate (`import_pushed` for carry); post-import classifiers |
| `desktop/p2p/sync_queries.py` | Shared SQL logic (pull handlers, carry offer/wanted, DHT announce tail) |
| `desktop/p2p/sync_server.py` | aiohttp HTTPS sync server + chat + relay (voucher/wake/forward) + watchdog |
| `desktop/p2p/mb_slice_queries.py` | MB slice protocol: per-artist signed blobs, `mb_slice_blobs` cache/replica inventory, pending-name priority |
| `desktop/mb_slice_client.py` | Slice requester — per-name verification against the ORIGINAL dump node's key |
| `desktop/p2p/dht_service.py` | libtorrent DHT (per-artist + per-user announces, announce-on-behalf for relay clients) |
| `desktop/p2p/p2p_manager.py` | Orchestration (asyncio in background thread) |
| `desktop/p2p/chat_service.py` | NaCl Box encryption, friend CRUD |
| `desktop/p2p/email_verify.py` | Signed email verification + invite delivery |
| `desktop/migrations/001_initial.sql` | Canonical schema (all types, tables, indexes, triggers) |
| `mcp/assistant_server.py` | MCP server exposing 37 tools to Claude Code (search, playback, MB catalog, HQP device) |
| `worker/verify.js` | Cloudflare Worker (email CA, signed invites) |

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
