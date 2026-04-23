# Sautium

AI-powered management system for a personal FLAC library (~30k tracks): audio
content analysis, semantic search, recommendations, HQPlayer integration,
serverless P2P network for sharing analytics between collectors.

Phases 1–3 (MVP + enrichment + audio analysis + HQPlayer + Web UI + launcher)
and P2P phases P0–P4 (sync, NAT, account system, E2E chat) are **done**.
Currently iterating on chat maturity, quality-of-life fixes and artist metadata
enrichment. Voice interface (Whisper + TTS) and file sharing over libtorrent
remain on the roadmap.

See:
- `PROGRESS.md` — design decisions and lessons learned (non-P2P).
- `P2P_NETWORK.md` — P2P architecture, technology choices, security model.
- `git log` — authoritative "what changed and when".

---

## Tech Stack

- **Python 3.11+** (best ML library support)
- **FastAPI** backend (async)
- **PostgreSQL 16 + pgvector** (vector similarity + relational data)
- **SQLAlchemy** ORM + `psycopg2` for raw SQL and batch operations
- **Docker + Docker Compose** (WSL2 on Windows, native on macOS)
- **NVIDIA RTX 4090** for GPU work (CLAP embeddings, BGE-M3 text encoding)
- **librosa** + CLAP zero-shot for audio feature extraction (no essentia)
- **CLAP** (`laion/clap-htsat-unfused`) — 512-d audio embeddings
- **BGE-M3** — 1024-d multilingual text embeddings (not sentence-transformers)
- **anthropic SDK** for Claude API; Claude Code + MCP tools for the AI DJ
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
- **Idempotent enrichment.** Every enrichment/sync task must be safe to re-run.
  "Skip if already done" is a **correctness property, not an optimization** —
  partial failures and resumes are normal operating conditions.
- **Persistent DB connections in long-lived services.** Opening a new
  connection per call costs real seconds (measured). Reuse a session/pool.
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
- **`audio_features.vocal_instrumental` is unreliable.** For vocal/
  instrumental queries, use `artists.is_vocalist` (classified from bio
  keywords). See `PROGRESS.md` design decisions.

---

## Migration & DB Workflow

- **Schema lives in `desktop/migrations/001_initial.sql`.** It's the single
  source of truth for a fresh install. All shared columns/indexes/types must
  end up there.
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

---

## Docker & Runtime Layout

- `sautium-postgres` — PostgreSQL 16 + pgvector. Credentials `musicai/supervisor`, DB `music_ai`. Persistent volume `./data/postgres/`.
- `sautium-backend` — FastAPI on `0.0.0.0:8000`, exposed to host at `localhost:8800`. GPU access via NVIDIA runtime.
- `sautium-playback-tracker` — separate daemon that writes to `listening_history`.
- Music library mounted read-only: `E:\Music` → `/music:ro` inside backend.
- Model cache external: `./data/cache` → `/root/.cache`.
- **Never bake models into the image.** Cache lives on the host, survives container deletes.
- **Restart backend after model/prompt changes**: `docker restart sautium-backend` then check logs for "Application startup complete".

---

## UI / Frontend Conventions

The web UI lives in `backend/static/`, served directly by FastAPI.
**Vanilla HTML + CSS + JS, no build step, no npm, no framework.** Keep
it that way — we rejected a React migration in favour of this
simplicity, and Claude Design's handoff format is plain HTML anyway
(see `docs/design/reference/now-playing-bundle/README.md`).

### Design tokens

`backend/static/tokens.css` is the canonical design-system source. It
exposes colours, typography, spacing, radii, shadows, motion, and
safe-area tokens as CSS custom properties. Load it first on every new
page: `<link rel="stylesheet" href="/static/tokens.css">`.

The palette and type scale are **locked** per
`docs/design/POSITIONING.md §"Colour palette (v1)"`. Do not introduce
new colour tokens — use existing ones, or raise the question first.

### Scaling model — mobile-first fluid proportional

The root font-size is driven by viewport width, with a single `--base`
knob:

```css
:root {
  --base: 13;
  --design-viewport: 360;
  font-size: calc(100vw / 360 * 13 * 1px);
  --px: calc(1rem / var(--base));
}
```

On a 360px-wide viewport, `1rem == 13px`. On 720px, `1rem == 26px`.
Every layout token is expressed in "design-pixels" via the `--px`
helper, so changing `--base` in one place rescales the entire UI.

**At ≥ 768px the root font-size locks at 15px** and the body is
centred at ~420px — the design does not inflate on desktop.

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

### Reference bundle

`docs/design/reference/now-playing-bundle/` preserves the Claude
Design handoff: the Design System v1 HTML, the final Now Playing v4
iteration, the full design-conversation transcript, and the cover
assets used in mockups. Treat it as **reference for visual intent**,
not source to paste — recreate visual output in our tokens-based
vanilla stack.

### Navigation and screen architecture

`docs/design/INFORMATION-ARCHITECTURE.md` is the source of truth for
how the UI is laid out: the 4-tab bottom nav (Home · Discovery ·
Friends · More), the AI FAB overlay, the mini-player bar, the Now
Playing sheet state machine, URL hash routing, per-screen contents,
Play-vs-Queue action semantics, and the queue-history concept. Read
it before touching UI routing or screen layout.

### View-layer migration strategy

The UI is being rebuilt top-down from the old prototype
`index.html`. Per the migration plan: **`app.js` business logic is
preserved** (API calls, HQPlayer commands, P2P, chat). Only the
view layer (`index.html` + `style.css`) is rewritten to match the
new design system and information architecture. Function contracts
in `app.js` (playerCmd, doSearch, sendChat, addFriend, etc.) stay
stable; their DOM targets change.

---

## Key Files

| File | Purpose |
|------|---------|
| `backend/main.py` | FastAPI entry point |
| `backend/models.py` | SQLAlchemy ORM models |
| `backend/uuid_utils.py` | UUID v5 generators + normalization |
| `backend/lastfm.py` | Last.fm enrichment + bio-derived classifiers |
| `backend/search.py` | Hybrid audio + text semantic search |
| `backend/claude_dj_prompt.py` | System prompt + schema description for Claude Code + API variants |
| `backend/ensemble_instruments.py` | AST + PaSST instrument multi-label tagger (replaces CLAP zero-shot) |
| `backend/static/tokens.css` | Canonical design-system tokens (colours, type, spacing, scaling) |
| `docs/design/POSITIONING.md` | Product positioning + UI design principles (source of truth) |
| `docs/design/INFORMATION-ARCHITECTURE.md` | Navigation model, screen inventory, state flows (source of truth for UI layout) |
| `docs/design/reference/now-playing-bundle/` | Claude Design handoff bundle — visual-intent reference |
| `backend/routers/sync.py` | Backend sync endpoints (P2P protocol) |
| `backend/routers/p2p.py` | Web UI Friends/Chat endpoints |
| `backend/dht_service.py` | Docker backend libtorrent DHT integration |
| `desktop/launcher.py` | Windows launcher (CustomTkinter) |
| `desktop/node_identity.py` | Ed25519 identity + account system (Argon2id) |
| `desktop/sync_client.py` | Sync client (import from remote + post-import classifiers) |
| `desktop/p2p/sync_queries.py` | Shared SQL logic (extracted from backend router) |
| `desktop/p2p/sync_server.py` | aiohttp HTTPS sync server + chat + watchdog |
| `desktop/p2p/dht_service.py` | libtorrent DHT (per-artist + per-user announces) |
| `desktop/p2p/p2p_manager.py` | Orchestration (asyncio in background thread) |
| `desktop/p2p/chat_service.py` | NaCl Box encryption, friend CRUD |
| `desktop/p2p/email_verify.py` | Signed email verification + invite delivery |
| `desktop/migrations/001_initial.sql` | Canonical schema (all types, tables, indexes, triggers) |
| `mcp/hqplayer_server.py` | MCP server exposing 22 tools to Claude Code |
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
