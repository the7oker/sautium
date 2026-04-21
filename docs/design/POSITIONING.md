# Sautium — Positioning Document

_v1 · 2026-04-19 · source of truth for Claude Design work_

---

## Name

**Sautium** = `Sauti` (Swahili: voice / sound) + `-ium`.

Three readings, with one dominant:

- **Medium** (primary) — a guide into the world of music: it reads your
  desires, dives into your memories, bridges you and your library.
- _Place_ (secondary) — auditorium / podium connotation, a space for listening.
- _Element_ (secondary) — titanium / vibranium feel, a component of your
  audio system.

Brand work should foreground the _medium_ aspect.

---

## Audience

### Primary — Mature Collector

- 30+, Windows-first, owns an offline FLAC library of 5k–50k tracks.
- Walked the path: cassettes → MP3 piracy → FLAC pirating → paid
  Bandcamp / label direct purchases.
- Audiophile-savvy: knows the difference between DSD and PCM, tunes
  HQPlayer themselves, owns a DAC + amplifier + streamer.
- Does **not** use Spotify as a primary listening source.

### Secondary — Entry Audiophile

- Building an offline library from scratch (0–5k tracks).
- Wants an AI guide to their own discoveries, not willing to invest
  days in manual cataloguing.

### Tertiary (served, not targeted) — Indie creators / niche labels

- Musicians and small labels who want to find their audience through
  music-similarity discovery.
- Served as a **side effect** of the outdoor P2P network, **not** a
  flagship flow in MVP.

### Not for

- Casual Spotify-style listeners who do not own a library.
- Social-media-driven "infinite feed" discoverers.
- People who want gamified progress (streaks, points, badges).

---

## Product essence: Indoor + Outdoor

Sautium is two halves that share one foundation.

### Indoor — Your library, your audio workflow

- AI search across your own collection: surface forgotten artists,
  semantic queries ("piano with rain", "aggressive 90s industrial").
- Recommendations grounded in audio analysis + metadata.
- **HQPlayer remote control via mobile web UI** — the core workflow is
  _"I'm on the couch, the amp and streamer are in another room, I run
  everything from my phone without standing up"_.
- **Mobile-first is not an option — it is mandatory.** At least 80% of
  indoor interaction happens from a phone, horizontally on a couch,
  one-handed. Desktop is the secondary surface.

### Outdoor — Low-intensity social

- Find people with similar vectorized music interests (similarity of
  listening/library, **not** a feed of posts).
- Chat with them about music, explore their libraries.
- Discover new music through other people's collections.
- **No social feed in MVP.** The model is **contact-driven, not
  feed-driven**: "stay in touch", not "live in the app".
- Feed-like affordances may arrive later for creators (tertiary group),
  but are explicitly out of scope for the current UI iteration.

---

## Tone & Vibe

Three anchor words: **затишок · комфорт · сучасні технології**
(coziness · comfort · modern technology).

Expanded:

- **Quiet professional instrument** — Roon-like restraint, respects the
  content. Album art large, metadata precise, nothing blinks.
- **Minimalist but not boring** — warm dark palette, not a cold
  Linear/Vercel slate.
- **Functional without a labyrinth** — new features feel like
  _"discoveries"_, not additional menu tabs. Progressive disclosure:
  the surface stays simple, depth is revealed when asked for.
- **No design noise** — no decorative gradients, no neon accents, no
  motion gimmicks. The stillness of an evening listening session.

---

## Anti-patterns

- SaaS-dashboard vibe (Linear / Stripe style).
- Gamification — streaks, points, badges, achievements.
- Social feed in MVP (reserved for future creator-oriented work).
- Infinite scroll **in social contexts** (feeds, notifications).
- Tab / screen labyrinths — every core action reachable in ≤ 2 taps.
- Desktop-first mindset — everything must work from a phone, lying down.

---

## Selective yes: where infinite scroll earns its place

**Discovery only.** The user case is _"nothing appeals tonight, I don't
know what to listen to"_ → a random mosaic of album covers → flick
through → recognise something half-forgotten → one tap → play.

This is not a social feed. It is **memory recall from your own
library**, served visually.

---

## Design principles (DS-level directives)

1. **Mobile-first.** Baseline 360–390px; desktop is an enhancement.
   Touch targets ≥ 44pt. One-handed reach matters.
2. **Warm dark foundation.** Dark with a warm undertone (coffee /
   cognac / amber-tinged), **not** obsidian black.
3. **Typography bi-family.** One sans for UI prose; one mono for numeric
   data (BPM 128.00, sample rate 96kHz / 24bit, duration 4:37). 4–5
   sizes max.
4. **Album art first.** Covers are the dominant visual actor. UI is the
   frame around them.
5. **Breathing, not packed.** Spacing is generous — coziness over
   density. For data-heavy views, offer an explicit "expand" mode rather
   than cramming everything at default.
6. **Warm dominant + cool technical accent.** Warm amber primary for
   emotion / brand / CTA. Cool muted-blue secondary reserved for
   technical numeric readouts and focus states — never for brand or
   emotion. Discipline: roughly 90% warm / 10% cool.
7. **Respect the content.** Don't truncate metadata gratuitously, don't
   crop covers, show full durations.
8. **Icon conventions follow established mobile idioms (iOS / Material).**
   Vertical ellipsis (⋮) for kebab / overflow menus, horizontal (⋯) only
   when it genuinely means "more text" or "truncated". Chevrons for
   navigation (not arrows), standard transport glyphs (play / pause /
   skip), hamburger only when the full drawer pattern is warranted.
   Deviate only when a deliberate subversion serves a product principle
   — convention-literacy is a free UX win, and breaking it must be paid
   for with a clear reason.
9. **Sautium is a remote, not a playback device.** The phone / web UI
   does not produce audio itself — audio flows through HQPlayer (on a
   separate machine) → DAC → amplifier → speakers. Therefore no UI
   affordance may imply device-local control: no system volume slider,
   no equaliser applied to web-UI output, no "audio output device"
   picker on the phone, no mute toggle. Controls that have no technical
   effect on the actual signal chain must not appear at all, even if
   they are conventional in generic music players — their presence is
   a UI lie.

---

## Colour palette (v1)

### Primary accent

| Token | Hex | Role |
|-------|------|------|
| **Accent — amber** | `#E8B06F` | Primary brand, CTAs, nav highlight, active track, emotional signals. _"Golden hour / evening lamp glow"._ |

### Technical secondary

| Token | Hex | Role |
|-------|------|------|
| **Accent — cool blue** | `#4A7FA7` | Audio-technical readouts (BPM, bit depth, sample rate, DSP state), waveform / spectrogram, focus rings, selection outlines. _"McIntosh VU blue meters"._ |

The cool blue is the **direct HSL complement of the amber**
(H≈32° ↔ H≈212°), saturation-matched at ~70%. The complementary pair
is intentional: **warm = emotion, cool = precision**. Oscillation
between them is a signature Sautium semantic — vintage tube glow for
feel, calibrated instrument blue for numbers.

### Foundation & text

| Token | Hex | Role |
|-------|------|------|
| **Foundation dark** | `#1B1714` | Page background. Coffee-near-black, warm undertone. |
| **Card surface** | `#2A2420` | Cards, raised surfaces, modals. |
| **Text primary** | `#EDE2D4` | Primary body / headings. Parchment cream, not sterile white. |
| **Text secondary** | `#A69B8E` | Metadata, captions, secondary labels. Warm gray. |

### Semantic

| Token | Hex | Role |
|-------|------|------|
| **Positive** | `#8DA77B` | "New / success" states. Sage moss — nature's "good". |
| **Negative** | `#C1564E` | Errors, destructive warnings. Terracotta — warm but alarming. |

### Palette discipline (DS rules)

1. **Warm ~90% / cool ~10%** proportion across any given screen.
2. **Cool blue appears only where the semantic is data/precision** —
   never brand, never CTA, never decoration.
3. **Both accents share saturation ~65–70%** — they balance rather than
   compete.
4. **Cool blue never stands alone** — always in proximity to warm
   elements, so the overall composition stays warm.
5. **Never introduce a third hue as accent.** Additional tones must come
   from the warm analogous family (honey, cognac, terracotta variants).

---

## Surface scope for Claude Design (MVP)

Priority order for the first DS iteration:

1. **Design system tokens** — colours, type scale, spacing scale,
   component vocab (buttons, chips, cards, sliders, tables).
2. **Discovery tab** — stress-test of density. Most active development
   area; will receive the upcoming instrument filter (built on
   AST+PaSST data in `audio_features.instruments`).
3. **Now Playing / HQPlayer remote** — the defining mobile flow. The
   proof that "phone from the couch" actually works.
4. **Library / Artist / Album pages** — the reading-heavy surfaces.
5. **Friends & Chat** — outdoor MVP surface. Minimal, contact-driven.
6. **Settings** — lowest priority, mostly forms and toggles.

Each surface gets functional parity with the current prototype first,
visual upgrade second, new features (like the instrument filter) third.

---

## Out of scope for this DS iteration

- Creator profiles / artist-claim flows — tertiary audience, not in MVP
  design.
- Social feed — design hooks are NOT pre-baked. We build similarity
  discovery only.
- Voice interface (Whisper + TTS) — on the roadmap but not part of
  visual DS right now.
- File-sharing UI (libtorrent, Phase P5) — also future.

---

## Open questions, resolved

| Question | Decision |
|----------|----------|
| Name aspect to foreground | **Medium** (guide into music) |
| Primary endpoint | **Web**. Launcher exists only to solve web-security / local-server / filesystem-access problems. |
| Creator features in MVP design | **Ignored.** No hooks, no stubs. Will be handled as a separate design cycle later. |
| Accent colour | Amber `#E8B06F` primary + cool blue `#4A7FA7` technical secondary. |
| "Deep sky blue" personal preference | Retained in spirit — cool blue secondary is the de-saturated HSL complement of amber, exactly the axis the preference pointed to. |

---

## Reference gallery (for Claude Design context)

### Core — audiophile library + remote

1. **Roon** (desktop + mobile remote) — the gold standard for
   offline-library UI. Dark, rich metadata, beautiful album art,
   sophisticated but warm. **Primary reference.**
2. **Audirvana Studio** — warmer macOS-like dark theme. Apple-influenced
   aesthetic, good at "calm".
3. **Roon Remote (mobile)** — how to build a remote control from the
   couch. Touch-first, large controls, one-handed flow.

### Secondary — library management / personal media

4. **Plex** (music section) — own library with cover art, sensible
   categorisation.
5. **Jellyfin / Navidrome** — open-source self-hosted music servers.
   Spiritual siblings. Study their failure modes (often overloaded,
   "NAS admin UI" aesthetic) to avoid them.
6. **HQPlayer Desktop** — we integrate with it. Reference for
   audio-technical controls (filter, oversampling, dither). **Not** a
   visual reference — HQP looks like Windows 2000.

### Tertiary — discovery and light social (outdoor)

7. **Last.fm (new mobile)** — scrobble flow, similarity-based
   recommendations, light social without feed obsession. Reference for
   "contact-driven, not feed-driven".
8. **Bandcamp (mobile + desktop)** — audiophile-respectful community,
   purchase-driven, artist-forward. Reference for the creator-friendly
   side.
9. **Tidal Desktop** — modern-streamer aesthetic. Borrow visual ideas
   for discovery surfaces; we are **not** a streamer in function.

### Inspiration for the mobile-remote feel

10. **Spotify Car Thing** (discontinued) — minimal remote-only device.
    Scale-up mental model: our web UI on a phone is a "Car Thing for the
    couch".
11. **Apple Music mobile** — touch target sizing, navigation hierarchy,
    cover-art-first layout.

### Anti-references (look and do the opposite)

- **Spotify Home** — algorithm-feed overload, streaks, noise.
- **YouTube Music** — chaotic hierarchy, weak metadata layout.
- **foobar2000** — functionally powerful, UI designed for 2005
  programmers.

---

_Next artifacts to produce inside Claude Design:_

- Design tokens (colours, type scale, spacing, radii) encoded in the
  DS format.
- Component library v1 (buttons, chips, cards, album tile, track row,
  metadata pill, slider, toggle, tab bar).
- Discovery tab mobile + desktop comp set.
- Now Playing / HQPlayer remote mobile comp set.
