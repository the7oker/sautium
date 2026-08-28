# Sautium — Information Architecture

_v1 · 2026-04-23 · source of truth for UI navigation, screens, and state flows_

This document defines the app shell, screen inventory, navigation model,
and interaction patterns for the mobile-first Sautium web UI. It is
**complementary to `POSITIONING.md`**:

- `POSITIONING.md` answers "what kind of product is this, what does it
  feel like, what are the design principles."
- `INFORMATION-ARCHITECTURE.md` (this file) answers "which screens
  exist, how do they connect, how does state transition."

---

## Scope

- Mobile web UI (target baseline 360 × 760, fluid scaling per
  `backend/static/tokens.css`).
- Desktop is **Phase 2**: for now desktop browsers see the
  mobile layout centred at ~420px. Dedicated tablet / desktop
  form factors may ship as separate HTML files later, selected
  by user-agent / viewport size.
- Business logic in `backend/static/app.js` stays intact
  (HQPlayer commands, search, P2P, chat) — this document only
  defines the new view layer.

---

## Navigation model

```
┌──────────────────────────────────┐
│  status bar                      │
│  ┌─── header (ctx-dependent) ──┐ │
│  │  ← back  |  title  |  ⋮     │ │
│  └──────────────────────────────┘ │
│                                  │
│         CONTENT AREA             │
│         (current tab, may        │
│          be a pushed detail)     │
│                                  │
├──────────────────────────────────┤
│  [mini-player]                   │   ← visible only when queue
├──────────────────────────────────┤       has content or HQP is
│  🏠   🔍   👥   ☰                │       playing/paused
│ Home Disc Fr  More               │   ← 4-tab bottom nav
└──────────────────────────────────┘
    🤖  ← AI FAB, bottom-left, persistent across all screens
```

### Bottom tab bar (4 slots)

| Icon | Tab | Role |
|------|-----|------|
| 🏠 | Home | Personalised entry: favourite artists, new in library, recommendations, recent queues |
| 🔍 | Discovery | Search + advanced filters + shuffle-mosaic |
| 👥 | Friends | Chat + invite flows (MVP == current interface) |
| ☰ | More | HQPlayer / DSP / Settings / Last.fm / Account — bottom-sheet drawer |

### AI — FAB only

AI assistant is **not** a bottom tab. It is a **floating action button** in
the bottom-left corner, visible on every root surface. Tap opens a
chat sheet with the current session list (ported from the existing
`app.js` chat implementation). The sheet passes an **invisible
context** to the AI: which screen the user opened it from (Artist X,
Album Y, HQPlayer settings, etc.), so the AI can reason about what
the user is looking at.

FAB is hidden inside pushed detail screens if it would overlap
important content, and hidden when Now Playing is expanded full-
screen (dismiss-to-return pattern).

### Mini-player (persistent bar)

A single row above the tab bar. Visible when:

- `queue.length > 0`, OR
- `hqp.state ∈ {playing, paused}`

States:

| Playback state | Bar content |
|----------------|-------------|
| Playing / paused | album thumb · title · artist · play/pause · (skip) |
| Queue ready but stopped | "Queue ready · N tracks" · ▶ Play |
| Empty queue + stopped | bar is **hidden entirely** |

Tap anywhere on the bar → expand Now Playing sheet.
Tap play/pause icon → toggles without expanding.

### Now Playing sheet (modal overlay)

Full-screen modal that slides up from the mini-player. Designed per
the `docs/design/reference/claude-design-bundle/project/Now Playing
v4.html` reference. Contains: album art, metadata row (Hi-Res badge,
key, BPM, energy), transport, progress bar, lyrics toggle, similar
tracks, queue button, HQPlayer quick-access.

Dismiss: drag-down gesture or chevron-down in header. Sheet
collapses back to mini-player bar.

### More drawer

A **bottom-up sheet** with a vertical list of entries:

- HQPlayer (status, host, port, quick-access to DSP)
- Audio output (`#more/output`) — the Output picker (HARDWARE-TIERS
  §2.6): HQPlayer, local devices via the built-in engine
  (WASAPI / ASIO / CoreAudio; listed only where the backend runs
  natively) with an exclusive-mode toggle, DLNA renderers (auto-scan on
  open + add-by-IP), and "This device" (browser playback). The drawer
  row's hint shows the active output's label from the SSE `output` field.
  Browser-renderer semantics: the tab that taps "This device" becomes
  the renderer (its tap doubles as the autoplay-unlock gesture); a
  newer tab taking over displaces the old one via a `released`
  directive; a closed renderer tab reports the output stopped after a
  short grace, and the queue survives for any output to pick up.
- DSP / Signal Chain (filter, matrix, dither, digital attenuation)
- Profile (identity, account, hardware profile, audio chain)
- Library (`#more/library`) — music path, owned counts, enrichment
  coverage, scan/enrich actions
- Streaming library (`#more/phantoms`) — the catalog the node knows but
  does not own; its own screen because it is its own catalog, with the
  owner's `discovery.phantom_layer` switch, the one explicit removal,
  and the MusicBrainz catalogue that mints the discographies.
  **"Streaming library" is the user-facing name only** — "phantom" stays
  the internal term in the route, the API, the settings key and the CSS,
  and everywhere else in the docs
- About / version

Sheet is dismissed with drag-down or tap-outside. Detail screens
(e.g., HQPlayer config) push **above** the sheet — the sheet stays
behind as a return-to-overview anchor, or slides away if the detail
is nav-heavy.

Right-to-left drawer variant reserved for Phase-2 tablet/desktop
layouts.

---

## URL hash routing

All navigation state lives in `window.location.hash` so browser
back/forward and refresh work natively:

```
#home                        → Home root
#home/artist/<uuid>          → Artist detail, pushed from Home
#home/artist/<uuid>/album/<uuid> → Album detail, pushed from Artist
#home/album/<uuid>/genre/<id>    → Genre detail, pushed from Album
#discovery                   → Discovery root
#discovery/search?q=piano    → Search results, query param
#discovery/album/<uuid>      → Album detail, pushed from Discovery
#discovery/genre/<id>        → Genre detail, pushed from Discovery
#friends                     → Friends list
#friends/chat/<peer>         → Friend chat thread
#more                        → More drawer open
#more/hqplayer               → HQPlayer config screen
#more/dsp                    → DSP / Signal Chain screen
#more/profile                → Profile (own — identity, account, audio chain)
#more/profile/gear/<id>      → Gear item detail (sheet within Profile)
#more/library                → Library (owned counts, enrichment, scan)
#more/phantoms               → Streaming library (phantom counts, enrichment, MB)
#profile/<pubkey-prefix>     → Profile (viewing other, read-only)
#queue                       → Full queue editor
#queue/history/<id>          → Specific historical queue restore
```

### Tab switch semantics

- Switching tabs **resets to tab root** (does not preserve detail
  stack per tab).
- Example: at `#home/artist/<uuid>/album/<uuid>`, tap Discovery tab
  → jump to `#discovery`. Return to Home → back at `#home` root.
  The detail stack is discarded.
- Browser back button reverses hash-history as normal; the history
  includes tab switches and pushes intermixed.
- This is a **deliberate simplification** vs per-tab stack
  preservation (iOS/Spotify-style). Matches the flat web-navigation
  model and uses browser primitives.

### Now Playing overlay in routing

Now Playing is a **modal overlay that does not own a URL**. Opening
it does not push a history entry; closing it does not pop history.
This ensures: if you're on `#discovery/album/<uuid>`, tap mini-player,
expand, close — you're still at `#discovery/album/<uuid>`.
Exception: tapping "Go to artist" or "Go to album" from inside the
sheet **collapses the sheet and pushes** the entity screen in the
current tab stack (changes hash).

---

## Screen inventory

### Root screens (accessible via bottom tabs)

| Screen | Hash | Contents |
|--------|------|----------|
| **Home** | `#home` | 3 horizontal-scroll sections (6 items each, lazy "See all"): Favourite artists · New in library · Recommendations. Optional 4th: Recent queues (queue history). |
| **Discovery** | `#discovery` | Search bar (default visible), advanced filters (collapsed), below: horizontal-scroll shuffle mosaic (infinite, random albums from library) |
| **Friends** | `#friends` | Identity card (invite code), add-by-code form, email-invite form, friends list. (Chat is a pushed detail per-friend.) |
| **More** | `#more` | Bottom-up sheet listing HQPlayer / DSP / Settings / Profile / etc. |

### Pushed detail screens (within a tab stack)

| Screen | Pushed by | Contents |
|--------|-----------|----------|
| **Artist** | Tap on artist name (anywhere) | Hero (name, photo if available), bio (Last.fm), tags, albums grid, popular tracks, similar artists |
| **Album** | Tap on album cover / title | Cover hero, metadata row (year · duration · format badge), genre chips on a separate row (up to 3), tracklist, **Play all** + **+ Queue** actions |
| **Genre** | Tap on a genre chip (from Album / Artist / Discovery) | Hero banner with genre name, description prose, top artists / albums / tracks in genre, related-genre chip strip |
| **Queue** (current playlist) | Queue button on Now Playing sheet, or `#queue` direct | Current queue with playing-track highlighted, drag-reorder, swipe-remove, Clear queue, Shuffle queue (future), summary "N tracks · HH:MM". The full list is rendered — no "and N more" truncation. |
| **Queue history item** | Tap on "Recent queues" row | Snapshot preview + Restore action (loads queue, does not auto-play) |
| **HQPlayer config** | From More | Status, host, port, filter / matrix / dither selector — read/write HQP state |
| **DSP / Signal Chain** | From More or Now Playing "→ HQPlayer" icon | Deep HQP DSP controls: filter, oversampling, dither, digital attenuation stepper, matrix profile |
| **Profile (own)** | From More → Profile entry | Identity card (avatar, name, @login, invite, city, bio), account (email + verify, password, Last.fm, scrobbling), audio chain (gear list), sociability placeholder |
| **Profile (viewing other)** | Tap on a user from friend chat header / friend list / future Discover-people | Read-only identity card, public audio chain (if owner opted in), match indicator (placeholder copy), CTAs (Send message · Add as friend) |
| **Gear item detail** | Tap on a gear card from Profile | Sheet: header (brand · model · category · status), research panel (3 states), AI personalised take (only if agent active), My notes |
| **Friend profile / chat thread** | Tap on friend in list | Chat messages, send-message input, identity info |

### Modal / overlay

| Modal | Trigger | Behaviour |
|-------|---------|-----------|
| **Now Playing mini** | `queue.length > 0 OR hqp active` | Persistent bar above tabs, always visible in valid state, tap → expand |
| **Now Playing expanded** | Tap mini-player bar | Full-screen modal sheet, drag-down to collapse to mini |
| **Queue sheet** | Tap Queue button on Now Playing expanded | Slides up within the sheet context; can also be reached by `#queue` direct |
| **AI FAB chat** | Tap FAB | Sheet with chat messages + session list; closes tap-outside or chevron-down |
| **"+" action menu** | Tap "+" on a track row | Small popover: [Play next] · [Add to end] — two options, explicit |
| **More drawer** | Tap More tab | Bottom sheet with entry list; child screens push above (or replace) |

---

## Now Playing state machine

```
                  ┌──────────────┐
                  │   HIDDEN     │  queue empty, HQP idle
                  │ (no bar)     │
                  └──────┬───────┘
                         │ user adds to queue
                         │ OR starts playback
                         ▼
                  ┌──────────────┐
          ┌──────▶│  MINI        │◀─────────────┐
          │       │  (bar)       │              │
          │       └──────┬───────┘              │
  drag-up │              │ tap bar              │
          │              ▼                      │
          │       ┌──────────────┐              │
          │       │  EXPANDED    │              │
          │       │  (full sheet)│              │
          │       └──────┬───────┘              │
          │              │ drag-down  OR        │ drag-down  OR
          └──────────────┘ chevron-close        │ chevron-close
                                                │
                         ┌──────────────┐       │
                         │  QUEUE VIEW  │───────┘
                         │  (sheet on   │
                         │   top of     │
                         │   expanded)  │
                         └──────────────┘
                         tap queue icon
                         in expanded NP
```

**Invariants**:

- Expanding the sheet does **not** change tab routing.
- Actions inside the sheet that navigate to entities (tap artist,
  tap album) collapse the sheet to mini and push the entity screen
  in the current tab stack.
- "Go to HQPlayer" shortcut inside expanded sheet → collapses sheet,
  navigates to `#more/dsp` (DSP / Signal Chain detail).

---

## Play vs Queue semantics

Sautium makes **curation of a listening queue** a first-class action,
distinct from immediate playback. This reflects audiophile listening
workflow: assemble the evening's flow, review the sequence, then
start.

### Action reference

| Context | Tap on track row | "+" icon on row | Main button |
|---------|------------------|-----------------|-------------|
| **Album detail** | Play album from this track (tracks N → end in queue, replaces) | Menu: [Play next] · [Add to end] | **▶ Play all** (track 1, replaces queue) · **+ Queue** (append all tracks to queue, navigate to Queue screen) |
| **Standalone track** (search result, home feed, Similar, recommendation) | Play now, replaces queue with single-track queue | Menu: [Play next] · [Add to end] | — |
| **Track inside Queue** | Jump playback to this position (reorder pointer) | — | — |

### "+" popover menu

A tap on "+" opens a small popover with exactly two choices:

- **Play next** — insert after currently-playing track
- **Add to end** — append to queue

No long-press gestures, no hidden interactions. Explicit beats clever.

### "Replace queue" safety — queue history

Every "play now, replace queue" action **automatically saves the
previous queue state** into a queue-history store before overwriting.

- Retention: last 5 replaced queues
- Storage: new table `queue_history` — `(id, tracks JSONB, context
  TEXT, created_at TIMESTAMPTZ)`
- Display: "Recent queues" section on Home, summary row per entry
  (e.g., "12 tracks · 48 min · Four Seasons + Hidden Orchestra")
- **Restore action**: tap on historical queue → populates current
  queue, does **not** auto-play; user decides when to press play
- Swipe-delete to remove from history

Evolution (later): AI-named queues ("Evening rainy · calm"), cross-
device sync via P2P.

---

## Home feed structure

Three horizontal-scroll sections by default, 6 items visible each,
lazy-load "See all" into a full screen.

| Section | Content | Source |
|---------|---------|--------|
| **Favourite artists** | Artists with highest listen count in `local_play_stats`, ordered by play frequency | Aggregated from listening history |
| **New in library** | Albums most recently imported, `media_files.file_modified_at DESC` | Scanner-tracked |
| **Recommendations** | "Like what you love, haven't heard yet" — similar to highly-played artists but under-played themselves | CLAP embeddings + `similar_artists` + play counts |
| **Recent queues** *(optional, if queue-history populated)* | Last 3–5 replaced queues | `queue_history` table |

Each section row is horizontally scrollable; tap item navigates to
Artist / Album / Queue-history-item detail within the Home tab stack.

---

## Discovery structure

Mobile-first vertical layout, minimal by default:

```
┌─────────────────────────────┐
│ [ search input          🔍 ] │  ← always visible
│                              │
│ [ mode: sound / lyrics / … ] │  ← 5 mode chips
│                              │
│ ▸ Advanced filters           │  ← collapsed (tap expands)
│                              │
│ ─── below the fold ─────     │
│                              │
│ Shuffle your library         │  ← section label
│ ← cover cover cover cover →  │  ← horizontal-scroll mosaic
│                              │     infinite, random albums
└─────────────────────────────┘
```

**Advanced filters expanded** (via `<details>` or similar toggle):

- BPM range
- Key + Mode
- Vocalist / Gender
- Danceable / Energy
- Instruments (new — multi-select from AudioSet labels)
- Quality tier (Lossy / Lossless / Hi-Res)

When search runs or filters apply, the shuffle mosaic is replaced
by results. Clearing search restores the mosaic.

**Result blocks** — Artists · Albums · Tracks · Genres, each the same
composite query answered at its own grain. A block is a *window* onto
its matches, never the whole set: a title query saturates (72 tracks
named "Casanova" all match perfectly and score identically), so a
first page is a slice of the matches, not the matches. Depth is an
affordance rather than a cap:

- **Artists / Albums** — horizontal rows, infinite-scroll along their
  own axis (a sentinel at the row's end pulls the next page).
- **Tracks** — vertical list plus an explicit **Show more** button.
  Deliberately not infinite-scroll: the block sits above Genres, and a
  list that grew on scroll would push the rest of the results off the
  screen forever.
- **Genres** — wrap-row of pills, not paged.

Ties rank **owned-first**: at equal relevance a track you own beats a
phantom stub you cannot play. Below that the order is total, so paging
never duplicates or skips a row.

The **horizontal** (not vertical) shuffle mosaic is an intentional
choice: keeps the search bar and advanced-filter affordance visible
even while browsing, no vertical-scroll-trap.

---

## AI FAB behaviour

A persistent button in the bottom-left corner. Default glyph: a
minimalist 🤖 / music-note hybrid.

### Visibility rules

- Visible on: Home, Discovery, Friends root, Artist detail, Album
  detail, Genre detail, Profile, Settings, HQPlayer.
- Hidden on: Now Playing expanded sheet, Queue sheet, AI chat sheet
  itself, More drawer (it is a sheet), and **friend chat threads**
  (social conversation context — AI assistance is not relevant
  there).
- General rule: any bottom-sheet or full-screen modal hides the FAB.
  Pushed detail screens keep the FAB unless the screen represents
  a context where AI is not semantically relevant (chat thread is
  the canonical example).

### Interaction

- Tap → bottom sheet with AI chat interface (session list + current
  conversation)
- Passes **invisible context** to the prompt: current screen type and
  entity id (e.g., `{screen: "artist", artist_id: "<uuid>"}`). The AI
  can use this to tailor suggestions.
- Evolution: context-aware greeting ("You're browsing Sade. Want
  something similar but moodier?"), action chips for quick prompts,
  gesture to save conversations by theme.

### Context examples

| Screen when FAB tapped | Context passed |
|-------------------------|----------------|
| Home | `{screen: "home"}` |
| Artist detail | `{screen: "artist", id, name}` |
| Album detail | `{screen: "album", id, title, artist}` |
| HQPlayer config | `{screen: "hqplayer"}` — AI can answer questions about filter settings |

---

## Friends — MVP

The existing UI is kept mostly as-is for this phase:

- Root: identity card + invite forms + friends list
- Tap friend → push chat thread
- Chat interface: message list + input

**Deferred until later**:

- Browsing a friend's library (their artists / albums / queue)
- Shared queues / listening together
- Library-wide music similarity comparison

Integration point with the rest of the app: none beyond the friend
list. Friends tab stays isolated in MVP.

---

## Profile + Audio chain

The Profile screen is the user's **audiophile identity card**. It
lives under the More tab and serves three purposes:

1. **Self-inventory** — own / want / sell / previously-owned gear
   tracking.
2. **AI context** — the gear list feeds the AI chat's personalised
   recommendations (HQPlayer filters, pairings, comparisons).
3. **Future social** — matchmaking and discovery placeholder,
   designed-for but inactive in MVP.

### Sections of the own-profile screen

Top to bottom:

- **Identity card** — avatar (circular), display name (editable,
  friendly form, distinct from @login), `@login + invite code` (mono
  blue), city (optional, editable), bio (3-line max, editable inline).
- **Account** — email + verification badge (✓ / ⚠), change password,
  Last.fm connect/disconnect, scrobbling toggle (enabled only when
  Last.fm is connected).
- **Hardware profile** — read-only: the auto-detected tier
  (full/standard/lite) and what the machine was measured at. Sits with
  the account because it describes THIS node, not the library; it
  governs compute only, never retention (see CLAUDE.md).
- **Audio chain (My setup)** — header with Add button, item list,
  empty-state copy ("Add your audio chain. Search by brand or model —
  we'll fill in details.").

The Sociability placeholder — a disabled "Open to meet other audiophiles"
toggle and a profile-completion bar — was **removed from the screen**: a
control nobody can operate is furniture, and the completion score existed
only to fill its bar. `users.open_to_meet` and the `/api/profile` field
stay, so Phase 2 has its state waiting when discovery is built.

### Audio-chain item flow

**Add — silent input.** Search-box autocomplete against the canonical
`gear_models` DB. Type → Enter → added. **No "No match" UI** — garbage
in is on the user, doesn't pollute the shared DB (see canonical DB
below). If the user has an AI agent active, an additional "Paste
batch" affordance accepts free-text like *"Holo Spring 3 KTE, Meze
Elite, AK Kann Ultra"* and parses it via the agent.

**Card content.** Each item shows: brand, model, category tag, status
badge, and a research-pulled chip when cached (e.g. `R2R · $6000` plus
1–2 sound-signature pills).

**Tap item → Gear item detail** (bottom sheet).

### Canonical gear DB (`gear_models`)

**Lazy canonicalization**, not upfront seed:

- User adds an item not in DB → placeholder entry created with state
  `awaiting_research`.
- Background research worker (AI-powered, WebSearch + WebFetch +
  Claude synthesis) fills in `specs JSONB`, `research_summary`,
  `community_sentiment JSONB`.
- P2P sync distributes completed entries across nodes — same protocol
  as artist bios. One user's research benefits everyone.
- Per-item DB cost is linear to actual demand (~$0.05 per real
  addition), no waste on hypothetical models.

A minimal **targeted seed** of ~200–300 most popular items (HD600/650,
Topping E50, Schiit Modi, Focal Clear, HD800S, etc.) is acceptable to
bootstrap autocomplete from day one (~$3 one-time, separate script).
All beyond seed = lazy.

### Research states

Per gear item:

| State | UI representation |
|-------|-------------------|
| `awaiting_research` | Silent muted "Awaiting research · queued" label. **No spinner as UI noise.** |
| `researching` | Subtle inline indicator. Item card remains useful. |
| `cached` | Full content: prose summary (3-5 lines), structured specs grid (category-specific keys with **mono-blue** values), community sentiment block (large mono-blue score · sample size · key praise / criticism pills), "Updated 2 days ago" + Refresh button (with cooldown indicator). |

Refresh has a cooldown (e.g. 7 days per item) to throttle API budget.

### AI-on vs no-AI users

The gear screen serves both modes:

- **AI-on users** are the **production engine** for community
  knowledge — their agent triggers research-on-add. They also see a
  **personalised take** per gear item: *"Relative to your current
  Bliss KTE + AM5LE, this DAC would shift soundstage wider but lose
  some midrange warmth…"*. Either pre-generated or an "Ask AI" CTA
  opening the chat sheet with the gear pre-loaded as context.
- **No-AI users** are **free consumers** of cached research. They can
  browse, add gear, read prose + structured specs + sentiment, track
  statuses. The personalised-take block is **hidden entirely** for
  them — no greyed-out teaser.

**Design contract**: research production may require AI; research
consumption never does. Do not gate cached content behind agent
state.

### Status badges

| Status | Semantic |
|--------|----------|
| **Own** | Current ownership (default state when adding) |
| **Want** | Wishlist / research target |
| **Sell** | Active listing (placeholder — marketplace inactive in MVP) |
| **Previously-owned** | Past ownership; auto-transition from `sell` on a manual "sold" action |

Visual mapping deferred to Claude Design within the locked palette
(amber / cool blue / sage / terracotta / text shades — no new hues
per `POSITIONING.md` palette discipline).

### Viewing another user's profile (`#profile/<pubkey-prefix>`)

Read-only variant of own profile. Differences:

- CTAs: **Send message** · **Add as friend** (contextual if already
  friends).
- Audio chain visible only if owner has `users.public_gear = true`.
- Match indicator: placeholder copy (`78% taste overlap · 2 shared:
  Bliss KTE · HD800S`) — algorithm is Phase 2.
- Bio + city visible.

The `<pubkey-prefix>` is the first 16 hex chars of the friend's
public key — same routable format as `#friends/chat/<peer>`.

### Privacy

MVP: profile-wide toggle `users.public_gear` (default false).
Per-item privacy granularity is Phase 2.

`users.open_to_meet` (default false) gates the future
Discover-people surface; UI placeholder shows the toggle disabled in
MVP.

---

## Migration strategy — Option D (keep app.js)

Recommended migration approach for this IA:

1. **Keep `backend/static/app.js`** — API calls, state management,
   HQPlayer control, P2P, chat. Business logic is tested through
   real usage; rewriting it would be wasteful and risky.
2. **Rewrite `backend/static/index.html` + `style.css`** to the new
   IA and DS. Shell, navigation, screens, modals — all new markup.
3. **Add URL-hash routing** to `app.js` (new small module) to power
   tab + push navigation.
4. **Extend `app.js` rendering functions** to match new DOM
   structure per screen. Function names (doSearch, playerCmd,
   sendChat) stay stable; their DOM targets change.
5. **New backend endpoints** as needed: queue history CRUD,
   multi-instrument filter, home feed aggregators.
6. **Scanner no-op**: IA changes do not affect scanning /
   enrichment pipeline.

No big-bang rewrite. No parallel v2 directory. The transition is
in-place, one-file-rewrite of the view layer. Old `index.html` is
archived in git history; no need to keep both.

---

## Phase 2 — Desktop / Tablet

Explicitly deferred:

- Separate HTML files per form factor, not a responsive unified
  layout — per `POSITIONING.md` pragmatic trade-off.
- Desktop layout: likely sidebar navigation + two-pane detail view
  (list + detail), richer content density.
- Tablet: may share mobile layout centred, or get its own landscape
  variant.
- Selection via user-agent / viewport-size probe on server OR client
  redirect.

Do not architect Phase-2 UI concerns into Phase-1 code.

---

## Data sources per screen (annex)

Concrete mapping of each visible block on each screen to a data
source: existing DB tables, existing endpoints, external APIs, or
new endpoints to be built. Implementation blueprint — minimal
additional research needed when wiring the UI to the data layer.

Conventions: `(new endpoint)` = backend work required; `(LFM)` =
Last.fm API call; otherwise the source already exists in the DB
or is a thin query over existing data.

### Home

| Block | MVP source | Evolution |
|-------|------------|-----------|
| Favourite artists | `local_play_stats` aggregated by artist | + listening recency weight |
| New in library | `media_files.file_modified_at DESC`, grouped to album | + scanner-assigned "fresh" tag |
| Recommendations | CLAP audio similarity to top-played tracks, filter to artists not yet heard much | + AI assistant contextual blends |
| Recent queues *(if populated)* | `queue_history` table **(new)** + `(new endpoint)` GET `/queue/history` | + cross-device sync via P2P |

### Discovery

| Block | MVP source | Evolution |
|-------|------------|-----------|
| Search input | existing `/search/tracks`, `/search/artists`, `/search/albums`, `/search/genres` | unified `/search?q=&type=` if simpler |
| Mode chips | switches which existing endpoint is called | — |
| Advanced filters | existing `/search/features` (extend to multi-instrument + AND/OR + quality tier) **(endpoint extension)** | saved filter presets |
| Shuffle mosaic | random sample from `albums` (filter to lossless? or all) | bias to under-played, diverse genres |

### Now Playing (mini + expanded)

| Block | MVP source | Evolution |
|-------|------------|-----------|
| Track title / artist / album | `tracks` + `track_artists` + `albums` joins | — |
| Progress / duration | HQPlayer state poll (existing in app.js) | — |
| Quality badge | `media_files.is_lossless` + `bit_depth` + `sample_rate` (Hi-Res = ≥ 96k/24bit lossless) | — |
| Key pill, BPM, energy | `audio_features.key`, `bpm`, `energy` | — |
| Lyrics panel | `track_lyrics` table | + timed-LRC support already partly present |
| Similar tracks | existing `/search/similar?track_id=` (CLAP embedding cosine) | + AI-rerank for diversity |
| Save replaced queue | `queue_history` table **(new)** + `(new endpoint)` POST `/queue/history` | — |

### Artist

| Block | MVP source | Evolution |
|-------|------------|-----------|
| Hero photo | uploaded artist photo (manual curation step), or fallback gradient | + auto-fetch from Last.fm `artist.getInfo` images |
| Bio prose | `artist_bios.bio` (Last.fm imported) | — |
| Tag chips | `artist_tags` (Last.fm) ranked by weight, top 4 | — |
| Albums | albums where this artist appears in `track_artists`, sorted by year | — |
| Popular tracks | `local_play_stats` filtered to this artist's tracks | + Last.fm `artist.getTopTracks` for global popularity |
| Similar artists | `similar_artists` (Last.fm imported) | + BGE-M3 vector similarity on bios |

### Album

| Block | MVP source | Evolution |
|-------|------------|-----------|
| Cover hero | `media_files` cover, embedded or via `cover_id` | — |
| Metadata row | `albums.release_year`, sum of `media_files.duration_seconds`, `media_files.is_lossless` + `bit_depth` for badge | — |
| Genre chips | `track_genres` + `genres.name` aggregated, top 3 by occurrence count | — |
| Tracklist | `tracks` ordered by `track_number` | — |
| Play all / + Queue | existing transport calls in app.js | + queue history snapshot before replace |

### Genre

| Block | MVP source | Evolution |
|-------|------------|-----------|
| Hero banner | most-played album cover in this genre (from `local_play_stats` × `track_genres`); fallback to typography-only with gradient | + pre-curated stock per genre, or AI-generated cached |
| Description | `genres.description` if present, otherwise Last.fm `tag.getInfo` (cached) | + curated wiki-style content |
| Top artists | local play count aggregated per artist within genre | + Last.fm `tag.getTopArtists` for global discovery |
| Top albums | local plays aggregated per album within genre | + `tag.getTopAlbums` (LFM) |
| Top tracks | local plays per track within genre | + `tag.getTopTracks` (LFM) |
| Related genres | **co-occurrence** in library: genres sharing tracks with this one, ranked by shared-track count | + BGE-M3 cosine on `genre_desc_embeddings` (already exist) |

All Genre blocks roll up into a single `(new endpoint)` GET
`/genres/:id` that returns aggregated payload — saves the UI from
3-5 roundtrips per screen open.

### Queue

| Block | MVP source | Evolution |
|-------|------------|-----------|
| Current queue | client-side state in `app.js` + HQPlayer playlist state | + persist server-side for cross-device |
| Drag-reorder | client-side, then PUSH new order to HQPlayer | — |
| Swipe-remove | client-side + HQPlayer remove call | — |
| Summary counts | aggregated client-side from queue contents | — |
| "Clear all" | HQPlayer clear playlist call | + warn-if-unsaved-history |

### Friends (MVP — minimal)

| Block | MVP source | Evolution |
|-------|------------|-----------|
| Identity card | `desktop/node_identity` (existing) | — |
| Friends list | `friends` table | — |
| Add by invite | existing P2P add-friend flow | — |
| Email invite | existing email-verify flow + worker | — |
| Chat thread | `p2p_messages` table + NaCl Box decrypt | — |

### More (drawer + sub-screens)

| Block | MVP source | Evolution |
|-------|------------|-----------|
| HQPlayer status | existing HQP state poll | — |
| HQPlayer config (host/port) | existing settings persistence | + save profiles per location |
| DSP / Signal Chain | existing HQP filter / matrix / dither endpoints | + per-genre auto-profile |
| Library | `/api/settings/library` — owned counts + enrichment coverage over the ENGAGED artist set (`sql_queries.ARTIST_ENGAGED`), so the ratio names the same population the pipeline queues | — |
| Streaming library | `/api/settings/phantoms` — its own endpoint: the counts cost ~0.4 s and the library screen wakes on every scan/enrich tick. Enrichment there is a COUNT, never a ratio: a phantom track has no file, so audio analysis only arrives over P2P and there is no total to complete | + per-source breakdown (MB vs Last.fm vs streaming mint) |
| MusicBrainz catalogue | `/api/settings/musicbrainz/*` — status, auto-update toggle, download/update/delete. Sits on the Phantom screen because the dump is what mints phantom discographies; deleting it stops them updating and touches nothing owned | — |

### Profile (own)

| Block | MVP source | Evolution |
|-------|------------|-----------|
| Identity (avatar, name, login, invite, city, bio) | `users` table extensions (`display_name`, `city`, `bio`, `avatar_cover_id`) **(new)** + `(new endpoint)` GET/PUT `/api/profile` | + avatar upload via existing covers pipeline |
| Account (email, password, Last.fm, scrobbling) | existing flows (email-verify worker, Argon2id password, Last.fm OAuth) | — |
| Hardware profile | `/api/settings/hardware` — auto-detected tier, read-only | — |
| Audio chain (gear list) | `user_gear` table **(new)** joined to `gear_models` **(new)** + `(new endpoint)` GET/POST/DELETE `/api/profile/gear` | + per-item privacy granularity |
| ~~Sociability placeholder~~ | removed from the screen; `users.open_to_meet` kept for Phase 2 | + Phase 2 active feature, when discovery exists |

### Profile (viewing other)

| Block | MVP source | Evolution |
|-------|------------|-----------|
| Public identity | `(new endpoint)` GET `/api/profile/<pubkey>` returning public subset | — |
| Public audio chain | conditional on `users.public_gear = true` | + per-item privacy |
| Match indicator | placeholder copy in MVP | + Phase 2 taste-overlap + shared-gear algorithm |

### Gear item detail

| Block | MVP source | Evolution |
|-------|------------|-----------|
| Header (brand · model · category) | `gear_models` table | — |
| Status pill | `user_gear.status` enum | — |
| Research panel | `gear_models.research_state`, `.research_summary`, `.specs JSONB`, `.community_sentiment JSONB`, `.researched_at` | + iterative research (Run 1 prose → Run 2 structured → Run 3 wiki-style corrections) |
| Personalised take | runtime AI chat call with user's gear + listening history as context — only if agent active | + caching for repeat questions |
| My notes | `user_gear.notes TEXT` | — |

### AI FAB (chat overlay)

| Block | MVP source | Evolution |
|-------|------------|-----------|
| Chat sessions | existing chat persistence in `app.js` | + named sessions, search history |
| Message stream | existing AI provider (Claude / OpenAI) | — |
| Invisible context payload | client-side: `{screen, entity_type, entity_id, route_hash}` injected into prompt | + recent listening, current queue summary |
| Provider selector | existing | — |

### Backend work required (summary)

For Phase-1 implementation:

1. `queue_history` table — `(id UUID, tracks JSONB, context TEXT, created_at TIMESTAMPTZ)`
2. `GET /queue/history` — list last 5 replaced queues
3. `POST /queue/history` — save current queue snapshot before replace
4. `POST /queue/history/<id>/restore` — populate current queue (no auto-play)
5. `GET /genres/:id` — aggregated genre detail payload (description, top
   artists/albums/tracks, related genres)
6. Extend `/search/features` for multi-instrument filter (`instruments=piano,drums&op=AND`)
7. `users` table extensions — `display_name TEXT`, `city TEXT`, `bio TEXT`,
   `avatar_cover_id UUID REFERENCES covers(id)`, `public_gear BOOLEAN DEFAULT FALSE`,
   `open_to_meet BOOLEAN DEFAULT FALSE`
8. `gear_models` table **(new)** — `(id UUID PK, brand TEXT, model TEXT, category TEXT,
   research_state gear_research_state ENUM, research_summary TEXT, specs JSONB,
   community_sentiment JSONB, researched_at TIMESTAMPTZ, refresh_cooldown_days INT
   DEFAULT 7)` — canonicalized via UUID v5 `(brand:model:category)`
9. `user_gear` table **(new)** — `(id UUID PK, user_id, gear_model_id REFERENCES
   gear_models(id), status user_gear_status ENUM (own / want / sell / previously_owned),
   notes TEXT, added_at, status_changed_at)`
10. `(new endpoints)` for Profile:
    - `GET/PUT /api/profile` — own profile read/write
    - `GET /api/profile/<pubkey>` — public profile of another user
    - `GET/POST/DELETE /api/profile/gear` — manage own audio chain
    - `GET /api/gear-models/search?q=` — autocomplete
    - `GET /api/gear-models/<id>` — detail (research summary etc.)
11. **Background research worker** — picks up `gear_models` rows with
    `research_state = 'queued'`, performs WebSearch + WebFetch on
    audiophile sources, runs Claude synthesis, writes back specs +
    summary + sentiment. Triggered only when an AI-on user adds the
    gear; no-AI users still consume cached results via P2P sync.
12. **P2P sync extension** — `gear_models` joins the sync inventory
    (same protocol as artist bios); user-specific tables (`user_gear`,
    `users` profile fields) stay private to each node.
13. Optional: cache layer for Last.fm `tag.getTopArtists/Albums/Tracks` if Evolution
    tier work begins (not Phase-1 critical)

No new ML pipelines required — existing CLAP + BGE-M3 + AST/PaSST
embeddings cover everything. New backend work is database +
aggregation queries, not model training.

---

## Open design decisions (to resolve before implementation)

These are small but merit explicit resolution during first Claude
Design session or implementation:

1. **Mini-player skip button** — include "next track" in mini bar, or
   only in expanded sheet? Compact space matters; "next" is a common
   gesture. Leaning toward: include in mini if there's room.

2. **Queue history retention** — fixed last 5, or user-configurable
   (3 / 5 / 10)? MVP: hardcoded 5.

3. **Shuffle mosaic in Discovery** — loops through all 34k tracks, or
   samples "interesting" (under-played or diverse genres)? MVP:
   pure random, evolve based on use.

4. **Empty states** — distinct copy / illustrations per section, or
   a single house style? MVP: consistent minimal house style ("No
   tracks yet", subtle icon).

5. **Transitions** — slide-from-right on push (iOS), fade, or no
   animation? MVP: subtle fade + minimal slide (~120ms, per
   `--dur-fast`).

6. **FAB position** — bottom-left fixed, or bottom-right (standard
   Material)? User chose **bottom-left** (jivochat-style). Keep
   consistent.

7. _(resolved)_ Genre screen is in **Phase 1**. Promoted from
   optional after the realisation that tappable genre chips logically
   require a destination — leaving them dangling would make them
   decorative, which violates the "respect the content" principle.
