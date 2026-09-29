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

- Mobile web UI (target baseline 360 × 760, locked design-pixel
  scaling per `backend/static/tokens.css`).
- Tablet and desktop are **layout modes of the same UI**, not
  separate pages: one set of screen renderers, and one raw-px
  breakpoint selects `compact` (< 768) or `tablet` (≥ 768, shipped
  2026-09-11); a `desktop` mode is reserved, not designed. See
  §"Layout modes — tablet / desktop".
- The view layer is `index.html` + `style.css` + `app-shell.js`
  (routing and every screen) + `player.js` (transport / SSE) — see
  `backend/static/CLAUDE.md` §"View-layer architecture".

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
    AI  ← AI FAB, bottom-right, above the mini-player
```

### Bottom tab bar (4 slots)

| Icon | Tab | Role |
|------|-----|------|
| 🏠 | Home | Personalised entry: favourite artists, recommendations, new in library, listening history |
| 🔍 | Discovery | Search + advanced filters + shuffle-mosaic |
| 👥 | Friends | Chat + invite flows (MVP == current interface) |
| ☰ | More | HQPlayer / Audio output / Profile / Library / Streaming library / Offline databases / AI assistant / Sync & P2P — bottom-sheet drawer |

### AI — FAB only

AI assistant is **not** a bottom tab. It is a **floating action button** in
the bottom-right corner, shown while at least one AI provider is
configured. Tap opens a chat sheet with the current session list. The chat
receives no screen context: the request carries the message, and the
server adds what is playing. Passing the open screen (`{screen, id}` —
Artist X, Album Y, HQPlayer settings) was designed in 2026-04 and is not
built.

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
| Stopped with a queue | the first queued track · ▶ Play · next |
| Empty queue + stopped | bar is **hidden entirely** |

Tap anywhere on the bar → expand Now Playing sheet.
Tap play/pause icon → toggles without expanding.

### Now Playing sheet (modal overlay)

Full-screen modal that slides up from the mini-player. Designed per
the `docs/design/reference/claude-design-bundle/project/Now Playing
v4.html` reference, with the five-button transport row of `Now Playing
v5.html`. Contains: album art, metadata row (Hi-Res badge,
key, BPM, energy — and, for a streamed 30 s excerpt, a neutral `[30s]`
badge beside the quality badge), a progress bar that seeks on tap or
drag, the transport row (queue · previous · play/pause · next · radio)
and the Similar tracks shelf. The artist and album lines open their
screens. The lyrics button is parked (hidden since 2026-08-24) until the
lyrics sheet exists; HQPlayer is reached from the More drawer, the sheet
has no shortcut to it.

The transport row ends with the **Radio toggle** (`#npRadioBtn`, a
stroke-only transistor-radio glyph where the reference draws Repeat;
since 2026-05-26). It is a status indicator, not a selected pill: on
tints the glyph amber, nothing fills. Its state is the `radio_mode`
field of the status stream, never the tap — see "Radio" under Play vs
Queue semantics.

Dismiss: the chevron-down in the header or Escape (on the tablet also a
tap on the scrim). Sheet collapses back to mini-player bar. A drag-down
gesture was designed and is not built.

### More drawer

A **bottom-up sheet** with a vertical list of entries:

- HQPlayer (status of the selected HQPlayer, quick-access to DSP, and —
  last, for an HQPlayer on another machine — its library: import/sync,
  rescan, forget, with the `hqp_library` guidance trail leading there; the
  connection itself is chosen in Audio output)
- Audio output (`#more/output`) — the Output picker (HARDWARE-TIERS
  §2.6): every HQPlayer the network scan found or the owner added (one
  entry each), local devices via the built-in engine
  (WASAPI / ASIO / CoreAudio; listed only where the backend runs
  natively) with an exclusive-mode toggle, DLNA renderers, and "This
  device" (browser playback); one scan on open + Rescan, and one "Add
  device by address" for HQPlayers and renderers alike. The drawer
  row's hint shows the active output's label from the SSE `output` field.
  Browser-renderer semantics: the tab that taps "This device" becomes
  the renderer (its tap doubles as the autoplay-unlock gesture); a
  newer tab taking over displaces the old one via a `released`
  directive; a closed renderer tab reports the output stopped after a
  short grace, and the queue survives for any output to pick up.
  Below the devices, "Output quality" for DLNA and This device: Lossless
  (FLAC, the default), Opus 192k, Opus 96k — for listening away from
  home; HQPlayer and local outputs are always lossless, and a change
  applies from the next track.
- Profile (identity, account, hardware profile, audio chain)
- Library (`#more/library`) — music path, owned counts, enrichment
  coverage, scan/enrich actions. (Node backup and restore are launcher
  functions — `BACKUP.md`; the Web UI has no backup surface.)
- Streaming library (`#more/phantoms`) — the catalog the node knows but
  does not own; its own screen because it is its own catalog, with the
  owner's `discovery.phantom_layer` switch and the one explicit removal.
  **"Streaming library" is the user-facing name only** — "phantom" stays
  the internal term in the route, the API, the settings key and the CSS,
  and everywhere else in the docs
- Offline databases (`#more/databases`) — the bulk open-data dumps a node
  may hold locally instead of asking the network for every fact: the
  MusicBrainz catalogue and the ListenBrainz listening statistics
  (2026-09-21), lyrics later. Each is opt-in, sized in gigabytes, and runs
  the same `dump_job.DumpJob` shape, so the screen is one block renderer
  over `DUMP_FAMILIES`. Its own section rather than rows on Library or
  Streaming library: the dumps are neither the user's files nor a catalog
  of music — they are reference data the whole node reads, and the set of
  them grows
- AI assistant (`#more/ai`) — which agent answers the chat and its sign-in
- Sync & P2P (`#more/sync`) — the network's state, the P2P settings, the
  notices

The drawer's header carries the title and, on its baseline at the right
edge, a link to the website (sautium.net). The About screen that held the
version, the licence and the links out was removed 2026-09-26 to keep the
drawer short; the site's footer carries the guides, the privacy page, the
licence, the source and the third-party notices. Every link to the site
carries `#node=<origin>` — the origin THIS device reached the node by, in
the URL fragment, so the site can offer "Open my Sautium" without the
address ever reaching a server log or a Referer (`rel="noreferrer"` on the
links plus the page-wide `no-referrer` policy). The same `siteLink()`
helper feeds the header link and the three contextual "Learn more" rows:
Audio output → the phone-as-speaker guide, Sync & P2P → the privacy page,
Streaming library → the streaming-library guide. A link to sautium.net
navigates in place — the site is the other half of this system, and its
"Open my Sautium" brings the user back; any other outbound link opens a
new tab (since 2026-09-22). The drawer never grows
past the viewport: it stops a strip short of the top and its rows scroll
inside it.

The Web UI carries the Sautium mark as favicon and touch icon and a
web-app manifest (`site.webmanifest`, `display: standalone`, since
2026-09-22), so "Add to Home Screen" installs it under its own name and
opens it without the browser's chrome — a pushed screen's own chevron
(`goBack`) is then the visible way back.

The drawer is dismissed by a tap on the scrim, its handle or its close
button, by the More tab again, or by any other tab. A row closes the
drawer and navigates to its section; on the tablet, Back from a section
reopens the menu. The HQPlayer row is listed only while HQPlayer is the
selected output.

The drawer's rows are one DOM node in every layout mode: `compact`
shows them as this bottom drawer, `tablet` as a centred card over a
scrim. See §"Layout modes — tablet / desktop".

---

## URL hash routing

The URL stays hash-shaped, so refresh and deep links work, but since
2026-09-25 the router never assigns `location.hash`: a push is
`history.pushState` followed by `render()`, and Back / Forward arrive as
`popstate` (the iOS 27 reason is in `backend/static/CLAUDE.md`
§"Navigation and screen architecture"). A hash that names no screen is
replaced by `#home`. On the tablet a More window also stamps its depth
into `history.state`. The routes:

```
#home                        → Home root
#discovery                   → Discovery root (query and filters are screen state, not URL)
#<tab>/artist/<uuid>         → Artist detail
#<tab>/artist/<uuid>/<mbid>  → Artist detail, one namesake selected
#<tab>/album/<uuid>          → Album detail
#<tab>/release-group/<uuid>  → Release group (its editions)
#<tab>/genre/<uuid>          → Genre detail
#<tab>/session/<uuid>        → Listening session
#friends                     → Friends list
#friends/chat/<peer>         → Friend chat thread
#more                        → redirects to #home (the drawer toggles in place, it is not a route)
#more/hqplayer               → HQPlayer screen
#more/output                 → Audio output (the Output picker)
#more/profile                → Profile (own — identity, account, audio chain)
#more/gear/<id>              → Gear item detail, pushed from Profile
#more/gear-system            → System analysis (pair matrix), from Profile
#more/gear-advisor           → Upgrade advisor, from Profile
#more/library                → Library (owned counts, enrichment, scan)
#more/phantoms               → Streaming library (phantom counts, enrichment)
#more/databases              → Offline databases (MusicBrainz, ListenBrainz)
#more/ai                     → AI assistant (agent, sign-in)
#more/sync                   → Sync & P2P (network state, settings, notices)
#profile/<pubkey-prefix>     → Profile (viewing other, read-only)
```

An entity screen nests one level under the tab it was opened from
(`<tab>` = home, discovery, friends, more); the path never accumulates —
an album opened from an artist is `#home/album/<uuid>`. The queue, like
Now Playing, is an overlay without a URL.

### Tab switch semantics

- Switching tabs **resets to tab root** (does not preserve detail
  stack per tab).
- Example: at `#home/album/<uuid>`, tap Discovery tab
  → jump to `#discovery`. Return to Home → back at `#home` root.
  The detail stack is discarded.
- Browser back button reverses hash-history as normal; the history
  includes tab switches and pushes intermixed.
- This is a **deliberate simplification** vs per-tab stack
  preservation (iOS-style). Matches the flat web-navigation
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
| **Home** | `#home` | Four horizontal-scroll shelves in this order: Favourite artists · Recommendations · New in my collection (hidden while the node owns nothing) · Listening history (the queues this node played and, since 2026-09-24, cards generated from the imported Last.fm history; hidden while empty). No "See all": a shelf pages along its own row. |
| **Discovery** | `#discovery` | Search bar (default visible), advanced filters (collapsed), below: horizontal-scroll shuffle mosaic (infinite, random albums from library) |
| **Friends** | `#friends` | Identity card (invite code), add-by-code form, email-invite form, friends list. (Chat is a pushed detail per-friend.) |
| **More** | — (a drawer, not a route) | Bottom-up sheet listing HQPlayer · Audio output · Profile · Library · Streaming library · Offline databases · AI assistant · Sync & P2P |

### Pushed detail screens (within a tab stack)

| Screen | Pushed by | Contents |
|--------|-----------|----------|
| **Artist** | Tap on artist name (anywhere) | Hero (name, photo if available), bio (Last.fm), tags, albums grid, popular tracks, similar artists, Last.fm credit end-cap |
| **Album** | Tap on album cover / title | Cover hero, metadata row (year · duration · format badge), genre chips on a separate row (up to 3), tracklist, **Play all** + **+ Queue** actions. A streaming-library (phantom) album wears a neutral `[demo]` badge in the metadata row beside the streamed-quality badge, its actions are **Stream all** · **Buy** · **+ Queue**, and a row that will play as a 30 s excerpt carries a `30s` tag beside its duration |
| **Genre** | Tap on a genre chip (from Album / Artist / Discovery) | Hero (the top artist's photo behind the genre name), counts line, description prose, Popular tracks, Artists grid, credit end-cap (Last.fm; ListenBrainz when its counts ranked the tracks). Top albums and a related-genre strip were designed and are not built |
| **Queue** (current playlist) | Queue button on Now Playing's transport row | The canonical queue with the playing row highlighted (an equaliser glyph where the other rows carry ×), drag-reorder by the handle, × to remove a row, summary "N tracks · time left". A tap jumps to the row (on the playing row it toggles play/pause); a long press plays the row from its start. The full list is rendered — no "and N more" truncation. Clear and Shuffle were designed and are not built |
| **Listening session** | Tap on a Listening history card | Artists, the tracklist grouped by album, Play / Queue replaying the stored slots; a card generated from the Last.fm history carries a last.fm chip linking the owner's library |
| **HQPlayer** | From More (the row is listed while HQPlayer is the selected output) or the gear on the selected HQPlayer row in Audio output | Connection (read-only: the HQPlayer's name, address · version · platform, a state dot; a tap leads to Audio output, where another one is picked or added — no editor here since 2026-09-28), Output (mode, rate, volume ±1 dB, the DSP speed factor), Filter (the active filter with the full-list picker, favourites, dither / modulator), Matrix profile, and — last, for an HQPlayer on another machine — Library (import / sync, rescan, forget). Every knob opens the app's own picker, not the OS select dialog (2026-09-26). There is no separate DSP screen |
| **Profile (own)** | From More → Profile entry | Identity card (avatar, name, @login, invite, city, bio), account (email + verify, password, Last.fm — the connected username links to its Last.fm page, scrobbling, Listening history), hardware profile, audio chain (gear list) |
| **Profile (viewing other)** | Tap on a user from friend chat header / friend list / future Discover-people | Read-only identity card and CTAs (Send message · Add as friend). The audio chain is not shown — the screen says sharing is private for now |
| **Gear item detail** | Tap on a gear card from Profile | Routed screen (`#more/gear/<id>`): header (brand · model · category · status), research panel (3 states), AI personalised take (only if agent active), My notes |
| **Friend profile / chat thread** | Tap on friend in list | Chat messages, send-message input, identity info |

### Modal / overlay

| Modal | Trigger | Behaviour |
|-------|---------|-----------|
| **Now Playing mini** | `queue.length > 0 OR hqp active` | Persistent bar above tabs, always visible in valid state, tap → expand |
| **Now Playing expanded** | Tap mini-player bar | Full-screen modal sheet (a centred card on the tablet); the chevron or Escape collapses it to mini |
| **Queue sheet** | Tap Queue button on Now Playing expanded | Full-screen overlay stacked over Now Playing (a centred card on the tablet); closed by its × or Escape. It owns no URL |
| **AI FAB chat** | Tap FAB | Sheet with chat messages + session list; closes tap-outside or chevron-down |
| **"+" inline bar** | Tap "+" on a track row, or + Queue on an album | "Add to: [Next] · [End]" inside the row; the same "+", turned ×, closes it |
| **More drawer** | Tap More tab | Bottom sheet with entry list; a row closes it and opens its section |

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
          │              │ tap bar              │
          │              ▼                      │
          │       ┌──────────────┐              │
          │       │  EXPANDED    │              │
          │       │  (full sheet)│              │
          │       └──────┬───────┘              │
          │              │ chevron-close        │ × close
          └──────────────┘ OR Escape            │ OR Escape
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

---

## Play vs Queue semantics

Sautium makes **curation of a listening queue** a first-class action,
distinct from immediate playback. This reflects audiophile listening
workflow: assemble the evening's flow, review the sequence, then
start.

### Action reference

| Context | Tap on track row | "+" icon on row | Main button |
|---------|------------------|-----------------|-------------|
| **Album detail** | Play that track alone (replaces the queue) | Inline bar: Add to: [Next] · [End] | **▶ Play all** (replaces the queue with the album) · **+ Queue** (the same inline bar for the whole album; the screen stays) |
| **Standalone track** (search result, home feed, Similar, recommendation) | Play now, replaces queue with single-track queue | Inline bar: Add to: [Next] · [End] | — |
| **Track inside Queue** | Jump playback to this position (on the playing row: play/pause); a long press plays the row from its start | — | — |

### "+" inline bar

A tap on "+" turns the row into an inline bar — "Add to: [Next] [End]" —
and the "+" into the × that closes it:

- **Next** — insert after currently-playing track
- **End** — append to queue

No hidden interactions on track rows. The one long press is on a Queue
row: it plays the row from its start. Explicit beats clever.

### Radio

Radio is a third verb beside Play and Queue: "keep playing things like
this". It is a mode of the queue, switched by the Radio toggle on Now
Playing, and its seed is always the track that is playing — owned or
streamed.

| Action | What happens to the queue |
|--------|---------------------------|
| **Radio on** | The playing track plays on, uninterrupted. Every other slot of the queue, before it and behind it, is replaced by similar tracks, which arrive in the background: owned ones at once, streamed ones as they buffer. When more than the current track would be lost, a confirm asks first ("Start radio?" — the replaced queue is archived to Listening history like any replace). |
| **While on** | Three or fewer tracks ahead of the playhead, another batch is appended, seeded from the track playing at that moment — the station drifts and never repeats a track. |
| **"+" / Queue album while on** | Appends as usual; radio stays on. |
| **Any "play now, replace queue"** (a track, an album, a session, the assistant's pick) | Replaces the queue and turns radio off. |
| **Radio off** | The queue stays as it is and stops growing. |

A seed that has no audio analysis yet (a stream that started seconds
ago) cannot start a station: the toggle stays off and a toast says so.
An output that is not reachable answers with the output-unavailable
dialog, as every play action does. A radio queue is reordered, trimmed
and jumped in like any other.

In Listening history a station is a card titled by its seed and labelled
"Radio"; opening it shows the queue the station had built, and Play
replays that as a fixed list — it does not start a station.

### "Replace queue" safety — queue history

_Shipped as `listening_sessions` / `session_tracks` — the Home "Listening
history" shelf (archived on every destructive play, replayed, not restored);
the `queue_history` table below was never built. Since 2026-09-24 the shelf
also carries cards generated from the imported Last.fm history._

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

Four horizontal-scroll shelves, in this order, each filled by its own
endpoint as it resolves. There is no "See all": a shelf pages along its own
row.

| Section | Content | Source |
|---------|---------|--------|
| **Favourite artists** | Artists with the most recent listening time: a completed listen weighs its duration × exp(−age / 90 days), age counted from the newest listen, two years at most (since 2026-09-24) — imported Last.fm listens included; seed-pick artists trail | Aggregated from listening history |
| **Recommendations** | Albums, owned and streaming-library alike, nearest in CLAP space to the records listened to in the 60 days before the newest listen; never played first, then not played for 90 days. Every clock runs from the newest listen, so the shelf a listener left is the shelf they return to (2026-09-24). Rebuilt when the listening history changes (a `sautium_listens` NOTIFY, since 2026-09-26), not per visit; seed picks fill a cold start | CLAP embeddings + `listening_history` |
| **New in my collection** *(hidden while the node owns nothing)* | Albums by their newest file here; an album held only in the HQPlayer's library is dated by its copy's first sighting and wears an HQPlayer badge (since 2026-09-27) — a copy of an album already on disk is not news | `album_variants.file_modified_at` |
| **Listening history** *(hidden while empty)* | Queues this node played, newest first, consecutive replays collapsed; cards generated from imported Last.fm listens (sittings, album runs) | `listening_sessions` (`source` sautium / lastfm) |

Each section row is horizontally scrollable; tap item navigates to
Artist / Album / Listening-session detail within the Home tab stack.

---

## Discovery structure

Mobile-first vertical layout, minimal by default:

```
┌─────────────────────────────┐
│ [ search input          🔍 ] │  ← always visible
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

**Advanced filters expanded**:

- Search in — the five scopes, the first row of the panel: Names & titles ·
  Artist bios · Sound · Lyrics · MusicBrainz (the blueprint drew them as
  mode chips under the search field)
- Context: Similar to now playing (the two-tier similarity radio drifts on)
- BPM range
- Key + Mode
- Vocalist / Gender
- Danceable / Energy
- Instruments (multi-select from AudioSet labels)
- Genre and Artist typeaheads

A quality tier filter (Lossy / Lossless / Hi-Res) was designed and is not
built.

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

A persistent button in the bottom-right corner. Default glyph: the
letters "AI".

### Visibility rules

- Shown only while at least one AI provider is configured.
- Visible on: Home, Discovery, Artist / Album / Genre detail, and the
  More sections on the phone.
- Hidden on: Now Playing expanded sheet, Queue sheet, AI chat sheet
  itself, More drawer (it is a sheet), the whole **Friends tab** — the
  list, chat threads, a peer's profile (social conversation context —
  AI assistance is not relevant there), a More window on the tablet,
  and Discovery while its advanced filters are open (their sticky Apply
  bar takes the same corner).
- General rule: any bottom-sheet or full-screen modal hides the FAB.
  Pushed detail screens keep the FAB unless the screen represents
  a context where AI is not semantically relevant (chat thread is
  the canonical example).

### Interaction

- Tap → bottom sheet with AI chat interface (session list + current
  conversation)
- Designed, not built: an **invisible context** passed to the prompt —
  current screen type and entity id (e.g., `{screen: "artist",
  artist_id: "<uuid>"}`). Today the request carries the message alone and
  the server adds what is playing.
- Evolution: context-aware greeting ("You're browsing Sade. Want
  something similar but moodier?"), action chips for quick prompts,
  gesture to save conversations by theme.

### Context examples (designed, not built)

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
  Last.fm is connected), and — when connected — the imported listening
  history: listens imported, scrobbles waiting — those the canon is still
  placing apart from those MusicBrainz cannot place (an unknown artist, an
  unknown title) — the last sync, Sync, Remove imported (2026-09-24). The
  row is labelled "From Last.fm", the name linking the owner's Last.fm
  library (since 2026-09-26, in place of a separate credit line).
- **Hardware profile** — read-only: the auto-detected tier
  (full/standard/lite, shown by its display name — Curator / Standard /
  Lite — since 2026-09-21) and what the machine was measured at. Sits with
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

**Tap item → Gear item detail** (a routed screen, `#more/gear/<id>`).

### Canonical gear DB (`gear_models`)

**Lazy canonicalization**, not upfront seed:

- User adds an item not in DB → placeholder entry created with state
  `awaiting_research`.
- Background research worker (AI-powered, WebSearch + WebFetch +
  Claude synthesis) fills in `specs JSONB`, `research_summary`,
  `community_sentiment JSONB`.
- P2P sync of completed entries, so that one user's research benefits
  everyone, is designed and not built: the sync carries no gear category
  (the blueprint modelled it on artist bios, which left the wire on
  2026-09-19).
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
- The audio chain is not shown: the screen says sharing is private for
  now. `users.public_gear` holds the state for Phase 2, and the match
  indicator (`78% taste overlap · 2 shared: …`) belongs to that phase.
- Bio + city visible.

The `<pubkey-prefix>` is the first 16 hex chars of the friend's
public key — same routable format as `#friends/chat/<peer>`.

### Privacy

MVP: profile-wide toggle `users.public_gear` (default false).
Per-item privacy granularity is Phase 2.

`users.open_to_meet` (default false) gates the future
Discover-people surface; no control shows it (the placeholder toggle was
removed).

---

## Migration strategy — Option D (keep app.js)

_Historical — completed by 2026-05. `app.js` was retired once every
screen had been rebuilt; the view layer is now the files listed
under Scope (`backend/static/CLAUDE.md` §"View-layer architecture")._

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

## Layout modes — tablet / desktop

Decided 2026-09-11 (the tablet frame was settled the same evening),
replacing the April plan of "separate HTML files per form factor".
That plan predates the rebuild that turned every screen into a
renderer inside `app-shell.js`; forking those renderers per form factor
would triple the maintenance surface for no product gain. Instead the
**same DOM** is laid out differently per viewport width. The **type
scale stays locked** (`--px == 1px` above 360, so 13 / 15 / 20 / 32 px
render 1:1 on a tablet or a monitor); what changes is the frame.

| Mode | Width | Catches | Frame |
|---|---|---|---|
| `compact` | < 768 | every phone portrait, small landscape phones | this document's mobile chrome, unchanged |
| `tablet` | ≥ 768 | iPad portrait 768–1024 and landscape 1024–1366, large phones landscape | left **nav rail** (80, icon + caption), the **mini-player** as a bar right of the rail, the AI FAB above it; every sheet opens as a **centred card** over a scrim |
| `desktop` | later | laptops and monitors | not designed yet — three directions are parked on the frame canvas; the frame reserves a docked right panel (`--panel-w`) for it |

Tablet artboards are drawn at 768×1024 and 1024×768 (the smallest
iPad): wider iPads get more content, never more chrome. Portrait and
landscape share one frame; only the content width differs. A
permanently docked Now Playing panel was tried for landscape and
rejected as visually overloaded at 1024.

Chrome mapping:

| Surface | `compact` | `tablet` |
|---|---|---|
| Bottom nav | tab bar | nav rail (icon + caption) — same `<nav>` / `.nav-tab` DOM |
| More drawer | bottom drawer | centred card (content-height, the card header: title + close); same rows, one DOM node |
| More sections (`#more/*`) | full screens | **windows** (form sheets) that replace the More card: the phone screen in a centred window — 360px for the first-level sections, wider (~560px) for the gear screens reached from Profile; back returns to the menu, close dismisses, the content behind stays. These routes are *modal* on the tablet: the underlying screen stays mounted and Back removes the window |
| Mini-player | bar above the nav | bar at the bottom, right of the rail |
| AI FAB | bottom-right | bottom-right, above the bar |
| Now Playing | full-screen sheet | the same sheet at its native 360px as a centred card over a scrim; the chevron, a scrim tap or Escape closes it; it scrolls as a whole, cover included, exactly like the phone |
| Queue | full-screen over Now Playing | centred card in the same slot, stacked over Now Playing |
| AI (master-detail) | full-screen, list ⇄ chat | centred card, list ⇄ chat unchanged |
| Bottom sheets (add-gear style, HQP pickers) | bottom sheet | centred dialog |
| `<dialog>` confirms | centred, top layer | same |
| Shelves (Home, Artist) | run under the right edge | the same — a cut tile is the scroll affordance, never a gutter |

Floating cards and windows sit on a fourth elevation step, `--shadow-4`
(a warm ambient drop plus a 1px light rim so the edge reads on the
dark scrim), added to `tokens.css` with the frame.

Screens on the tablet (decided with the artboards under
`docs/design/reference/wide-layout/frame/`):

| Screen | Tablet treatment |
|---|---|
| Home | unchanged; shelves run under the right edge |
| Album / Release group | the header changes: the cover (280px) sits beside the title, edition picker, year, chips and Play / Queue; in landscape that column stays on the left while the tracklist scrolls on the right. Track rows, disc labels and Similar albums are the phone's |
| Listening session | the album header pattern (cover beside title, meta, Play / Album / Queue), track rows below |
| Artist, Genre | unchanged: hero, chips, bio at a 640px measure, shelves under the right edge, track rows; the genre's artist grid gains columns (auto-fill of 96px tiles) |
| Discovery | unchanged at content width |
| Friends | a centred 640px column (form-like content must not stretch) |
| Chat thread | a centred 720px column, bubbles capped at 60% of it |
| More sections | windows, see above |

Rules that keep this one UI rather than two:

- **CSS owns the mode.** Breakpoints are raw-px media queries in
  `tokens.css`; every chrome offset derives from one set of per-mode
  variables (the contract is in `backend/static/CLAUDE.md` §"Layout
  modes"). No `matchMedia`, no resize listener.
- **JS asks, never decides.** A read of the live mode is allowed only
  where `[hidden]` would otherwise force a mobile-only behaviour;
  the tablet cycle needs none — a scrim tap closing a card is the
  same listener in every mode (in `compact` the screen covers the
  scrim, so it never fires).
- **One DOM per chrome element.** The nav, the More rows, the
  mini-player and each sheet exist once; a mode repositions them.
- **Document scroll stays.** The frame is fixed chrome plus
  variables, not a grid with an inner scroller — the scroll model,
  sticky elements and iOS behaviour of the compact layout are
  untouched.
- Every screen is verified at 360 and at the widths of its tablet
  artboards; the compact rendering must not change
  (`scripts/ui-shots.mjs --compare` against a baseline is the proof).

Known costs of the unified approach (accepted): a true list + detail
split (Friends list beside a chat thread) needs a renderer extracted
from the screen that owns `#app`; horizontal shelves that become
wrapping grids must drop the shelf-rooted infinite-scroll observer;
three container conventions (`.screen`, `.discovery-screen`,
`.detail-screen`) each carry their own `--content-max`.

---

## Data sources per screen (annex)

> **Historical blueprint (2026-04).** It mapped each block to a data source
> before the screens were wired, and the mapping has since been built — the
> endpoint names drifted along the way (search is now one engine behind
> `/api/discovery/search`, genres live under `/api/genres/{id}`, the profile
> and gear routes under `/api/profile/*`). Read it for *what feeds a screen*,
> and the code for *which route says it*. The one block never built is Recent
> queues: there is no `queue_history` table.

Conventions: `(new endpoint)` = backend work required at the time; `(LFM)` =
Last.fm API call; otherwise the source already exists in the DB
or is a thin query over existing data.

### Home

| Block | MVP source | Evolution |
|-------|------------|-----------|
| Favourite artists | `listening_history` completed listens by primary artist | recency weight shipped 2026-09-24: τ 90 days from the newest listen, two-year window |
| Recommendations | multi-seed CLAP kNN from the records of the 60 days before the newest listen, never-played first, then forgotten for 90 days; the ranking is rebuilt on `NOTIFY sautium_listens` (since 2026-09-26), not per visit | + AI assistant contextual blends |
| New in my collection | `album_variants.file_modified_at` (denormalised from `media_files`), the newest LOCAL file first; an album held only at the HQPlayer is dated by its copy and badged (since 2026-09-27) | + scanner-assigned "fresh" tag |
| Listening history | `listening_sessions` + `session_tracks`, GET `/api/home/listening-history`; imported cards from `playback.sessions.rebuild_imported_sessions` | + cross-device via the life merge |

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
| Progress / duration | the status tick on `/api/events`, whichever output is active; a tap or drag on the bar seeks (POST `/api/player/seek`) | — |
| Quality badge | `media_files.is_lossless` + `bit_depth` + `sample_rate` (Hi-Res = ≥ 96k/24bit lossless); a streamed track: the serving provider's tier (`/now-playing-detail`), plus `[30s]` from the status `excerpt` flag | — |
| Key pill, BPM, energy | `audio_features.key`, `bpm`, `energy` | — |
| Lyrics panel | `track_lyrics` table; the button is parked (hidden since 2026-08-24) until the sheet exists, GET `/api/player/lyrics/{media_file_id}` is there | + timed-LRC support already partly present |
| Similar tracks | GET `/api/player/similar/{track_uuid}` — the two-tier scorer radio drifts on (`track_similarity.py`), owned and streamable rows mixed, deterministic (since 2026-07-13; the blueprint named `/search/similar`, a mean-cosine lookup) | — |
| Radio toggle | state: `radio_mode` in the status stream; POST `/api/player/radio/start` (`track_id` of an owned seed or `track_uuid` of a streamed one) and `/radio/stop` | — |
| Save replaced queue | `queue_history` table **(new)** + `(new endpoint)` POST `/queue/history` | — |

### Artist

| Block | MVP source | Evolution |
|-------|------------|-----------|
| Hero photo | uploaded artist photo (manual curation step), or fallback gradient | + auto-fetch from Last.fm `artist.getInfo` images |
| Bio prose | `artist_bios.bio` (Last.fm imported) | — |
| Tag chips | `artist_tags` (Last.fm) ranked by weight, top 4 | — |
| Albums | albums where this artist appears in `track_artists`, sorted by year | — |
| Popular tracks | ListenBrainz listen counts (`track_mbids ⋈ lb_recording`, owned and phantom tracks alike) first, then `local_play_stats` for tracks ListenBrainz does not know; a phantom artist's slice is asked of the network when the page opens and the block fills in place | — |
| Similar artists | `similar_artists` (Last.fm imported) | + BGE-M3 vector similarity on bios |
| Credit end-cap | `artist_bios.url` — `data from Last.fm` linking to this artist's Last.fm page (required by clause 2.7 of the Last.fm API terms wherever their data is displayed; hidden when the artist has no Last.fm row) · `listening statistics from ListenBrainz` whenever its counts ranked something on the page | — |

### Album

| Block | MVP source | Evolution |
|-------|------------|-----------|
| Cover hero | `media_files` cover, embedded or via `cover_id` | — |
| Metadata row | `albums.release_year`, sum of `media_files.duration_seconds`, `media_files.is_lossless` + `bit_depth` for badge; a phantom album: `[demo]` always, the streamed mix (`/phantom-availability` `quality`) for the badge, its `excerpt` list for the row tags | — |
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
| Credit end-cap | `genre_descriptions.url` — `data from Last.fm` linking to this genre's Last.fm tag page. Same clause-2.7 requirement as the Artist screen | — |

All Genre blocks roll up into a single `(new endpoint)` GET
`/genres/:id` that returns aggregated payload — saves the UI from
3-5 roundtrips per screen open.

### Queue

| Block | MVP source | Evolution |
|-------|------------|-----------|
| Current queue | the canonical queue on the server: GET `/api/player/playlist`, re-read when the status stream's `playlist_version` moves | — |
| Drag-reorder | POST `/api/player/reorder` (the new order of track UUIDs — the one identity owned and streamed slots share) | — |
| Remove (× on a row) | POST `/api/player/remove` (index + track identity) | — |
| Jump | POST `/api/player/jump` | — |
| Summary counts | aggregated client-side from queue contents: tracks and the time left | — |

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
| HQPlayer screen | GET `/api/hqplayer/state`; knobs POST `/api/hqplayer/config`, `/volume`; which HQPlayer is driven is chosen in Audio output (`PUT /api/settings/output`, the registry is `hqp_endpoints`, since 2026-09-28); its library block: `/api/settings/library/hqp-sync`, `/hqp-rescan`, `/hqp-forget` | + per-genre auto-profile |
| Audio output | POST `/api/player/outputs/scan` (one sweep for DLNA renderers and HQPlayers), POST `/api/player/outputs/add` (by address), `PUT /api/settings/output` | — |
| Library | `/api/settings/library` — owned counts + enrichment coverage over the ENGAGED artist set (`sql_queries.ARTIST_ENGAGED`), so the ratio names the same population the pipeline queues | — |
| Streaming library | `/api/settings/phantoms` — its own endpoint: the counts cost ~0.4 s and the library screen wakes on every scan/enrich tick. Enrichment there is a COUNT, never a ratio: a phantom track has no file, so audio analysis only arrives over P2P and there is no total to complete | + per-source breakdown (MB vs Last.fm) |
| Offline databases | `/api/settings/databases` — every dump family's section in one round-trip, so the screen's wake refresh is one request; writes go to `/api/settings/{musicbrainz,listenbrainz}/*` (auto-update toggle, download/update/delete). `/{family}/status` adds the disk budget and exists for the assistant's `*_dump_status` tools | + a lyrics dump |

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
| Public identity | GET `/api/profile/by-pubkey/{pubkey_prefix}` returning the public subset | — |
| Public audio chain | not shown ("sharing is private for now"); `users.public_gear` waits for Phase 2 | + per-item privacy |
| Match indicator | not shown | + Phase 2 taste-overlap + shared-gear algorithm |

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
| Invisible context payload | not built: the request is `{message}` and the server adds what is playing (`chat._get_player_context`) | + `{screen, entity_type, entity_id, route_hash}`, recent listening, current queue summary |
| Provider selector | existing | — |

### Backend work required (summary)

The Phase-1 list as written in 2026-04. Items 5–12 are built; 1–4 are not,
and 13 stayed optional.

1. ❌ `queue_history` table — `(id UUID, tracks JSONB, context TEXT, created_at TIMESTAMPTZ)`
2. ❌ `GET /queue/history` — list last 5 replaced queues
3. ❌ `POST /queue/history` — save current queue snapshot before replace
4. ❌ `POST /queue/history/<id>/restore` — populate current queue (no auto-play)
5. ✅ `GET /genres/:id` — aggregated genre detail payload (description, top
   artists/albums/tracks, related genres)
6. ✅ Multi-instrument filter — landed as an engine tool, not an extension of
   the old endpoint
7. ✅ `users` table extensions — `display_name TEXT`, `city TEXT`, `bio TEXT`,
   `avatar_cover_id UUID REFERENCES covers(id)`, `public_gear BOOLEAN DEFAULT FALSE`,
   `open_to_meet BOOLEAN DEFAULT FALSE`
8. ✅ `gear_models` table **(new)** — `(id UUID PK, brand TEXT, model TEXT, category TEXT,
   research_state gear_research_state ENUM, research_summary TEXT, specs JSONB,
   community_sentiment JSONB, researched_at TIMESTAMPTZ, refresh_cooldown_days INT
   DEFAULT 7)` — canonicalized via UUID v5 `(brand:model:category)`
9. ✅ `user_gear` table **(new)** — `(id UUID PK, user_id, gear_model_id REFERENCES
   gear_models(id), status user_gear_status ENUM (own / want / sell / previously_owned),
   notes TEXT, added_at, status_changed_at)`
10. ✅ `(new endpoints)` for Profile (now under `/api/profile/*`):
    - `GET/PUT /api/profile` — own profile read/write
    - `GET /api/profile/by-pubkey/{pubkey_prefix}` — public profile of another user
    - `GET/POST/DELETE /api/profile/gear` — manage own audio chain
    - `GET /api/gear-models/search?q=` — autocomplete
    - `GET /api/gear-models/<id>` — detail (research summary etc.)
11. ✅ **Background research worker** (`backend/gear_research_worker.py`) — picks up `gear_models` rows with
    `research_state = 'queued'`, performs WebSearch + WebFetch on
    audiophile sources, runs Claude synthesis, writes back specs +
    summary + sentiment. Triggered only when an AI-on user adds the
    gear; no-AI users still consume cached results via P2P sync.
12. ⏳ **P2P sync extension** — `gear_models` joins the sync inventory;
    not built (the sync carries no gear category). The blueprint modelled
    it on artist bios, which left the wire on 2026-09-19. User-specific
    tables (`user_gear`, `users` profile fields) stay private to each node.
13. Optional: a cache layer for Last.fm `tag.getTopArtists/Albums/Tracks`
    (not Phase-1 critical, and Last.fm answers are node-local since
    2026-09-19, so any such cache stays off the wire)

No new ML pipelines required — existing CLAP + BGE-M3 + AST/PaSST
embeddings cover everything. New backend work is database +
aggregation queries, not model training.

---

## Open design decisions — resolved by implementation

Small calls listed before the UI was built; the shipped UI is the answer to
each. Kept for the reasoning.

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
   Material)? User chose **bottom-left** (jivochat-style) in 2026-04;
   shipped bottom-right.

7. _(resolved)_ Genre screen is in **Phase 1**. Promoted from
   optional after the realisation that tappable genre chips logically
   require a destination — leaving them dangling would make them
   decorative, which violates the "respect the content" principle.
