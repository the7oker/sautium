# Web UI conventions (`backend/static/`)

The web UI is served directly by FastAPI. **Vanilla HTML + CSS + JS, no
build step, no npm, no framework** — we rejected a React migration in
favour of this simplicity, and Claude Design's handoff format is plain
HTML anyway (see `docs/design/reference/claude-design-bundle/README.md`).

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

Above 768px the **frame** changes, not the scale — see "Layout modes"
below. (Until the frame ships, `tokens.css` still centres `body` at
468px there; that rule goes away with the frame, because `max-width`
on `body` never contained the fixed chrome — on a monitor the nav and
the sheets spanned the whole viewport around a 468px column.)

**Why not pure fluid scaling?** Fluid scaling at all widths inflates
the design on phones wider than the 360 baseline (a 19px reference
title becomes ~23px on a 443px viewport), which contradicts pixel-
perfect parity with the reference HTML and pushes meta-row content
past the screen edge. Locking the size keeps both the typography
spec and horizontal layout predictable.

### Layout modes — tablet / desktop

_Contract decided 2026-09-11 (`docs/design/INFORMATION-ARCHITECTURE.md`
§"Layout modes" has the chrome mapping and the reasoning). Status: the
compact chrome is what is implemented today; the frame PR turns this
section into code. Tablet artboards live under
`docs/design/reference/wide-layout/`; the desktop is a later cycle._

One DOM, modes selected purely by raw-px media queries: `compact`
(< 768, today's mobile chrome) and `tablet` (≥ 768: left nav rail, the
mini-player as a bar right of the rail, the AI FAB above it, every
sheet as a centred 360px card over a scrim). The type scale stays
locked. A `desktop` mode (docked right panel, `--panel-w`) is reserved,
not designed.

Every chrome offset derives from one set of per-mode variables — this
is the only place the breakpoint numbers and the chrome geometry live:

```css
:root {                                  /* defaults = compact */
  --layout-mode: compact;                /* read by JS, never by CSS layout */
  --nav-h: calc(56 * var(--px));         /* bottom nav; 0 on the tablet */
  --nav-w: 0px;                          /* nav rail; 80 on the tablet */
  --player-h: calc(60 * var(--px));      /* mini-player bar */
  --fab-clear: calc(72 * var(--px));     /* the FAB stays on the tablet */
  --panel-w: 0px;                        /* reserved for the desktop's docked panel */
  --content-max: none;
}
@media (min-width: 768px) { :root { --layout-mode: tablet; --nav-h: 0px; --nav-w: calc(80 * var(--px)); } }

body { --player-h-active: 0px; --fab-clear-active: var(--fab-clear); --panel-w-active: 0px; }
body.has-miniplayer { --player-h-active: var(--player-h); }
body.no-fab         { --fab-clear-active: 0px; }
body { --chrome-bottom: calc(var(--nav-h) + var(--player-h-active) + var(--safe-bottom)); }
#app { padding: 0 var(--panel-w-active) calc(var(--chrome-bottom) + var(--fab-clear-active)) var(--nav-w); }
```

On the tablet `#more/<section>` routes are **modal**: `render()` keeps
the underlying route root mounted and renders the section into a window
(scrim + centred card, `--window-w` 360px for the seven first-level
sections, `--window-w-wide` ~560px for the gear screens) appended to
`body`; Back removes the window without re-rendering what is beneath.
The More drawer is the same DOM as on the phone, styled as a
content-height centred card with the card header (title + close).

On the tablet the three sheets keep `inset: 0` (that is the scrim) and
their inner screen becomes the card: `width: calc(360 * var(--px))`,
`max-height: calc(100% - 48px)`, centred, `border-radius: var(--radius-lg)`,
`box-shadow: var(--shadow-4)`, and `overflow-y: auto` moves from the
sheet to the screen so the card scrolls as a whole, cover included,
exactly like the phone. `--shadow-4` is the fourth elevation step
(warm ambient drop plus a 1px light rim), added to `tokens.css` with
the frame.

Rules:

- **CSS owns the mode.** No `matchMedia`, no resize listener. Anything
  positioned against the chrome (FAB, guide puck, sticky filter bar,
  drawer, chat screen height) references `--chrome-bottom` /
  `--nav-w` / `--panel-w-active` — never its own copy of the sum.
- **JS asks, never decides.** A read of the live mode
  (`getComputedStyle(document.documentElement).getPropertyValue('--layout-mode')`)
  is allowed only where `[hidden]` would otherwise force a mobile-only
  behaviour. The tablet cycle needs none: a scrim tap closing a card is
  the same listener in every mode (in `compact` the screen covers the
  scrim, so it never fires).
- **One DOM per chrome element.** The nav, the More rows, the
  mini-player and each sheet exist once; a mode repositions them.
- **Document scroll stays.** The frame is fixed chrome plus variables,
  not a grid with an inner scroller.
- The compact rendering must not change when a wider mode is added:
  `scripts/ui-shots.mjs --compare` against the 360 baseline is the
  proof.

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
5. **Visual verify before declaring done.** Open both screens in a
   browser at 360px, and at the width of every tablet / desktop
   artboard the screen has, compare side by side. The user expects
   pixel-perfect parity; "approximately right" is a bug.
   `scripts/ui-shots.mjs` renders every route at each width into a
   contact sheet; `--compare` against a baseline proves the 360
   rendering did not move when only a wider mode was meant to change.

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

### Guidance trail — pointing at a pending action

A node knows what still needs a human (`GET /api/settings/guidance`
returns a list of task ids); the view layer knows where each one
lives. `guide` in `app-shell.js` paints an active task on **every**
element carrying `data-guide="<task-id>"`, so the user follows one
amber dot down the hierarchy — More tab → drawer row → the control
itself — instead of hunting for it. `data-guide="*"` marks a step
that stands for "any open task", which is all the tab bar can
honestly say from outside the drawer.

To point at something new:

1. Add the task id to `_guidance_state()` in
   `backend/routers/settings.py`, with the rule for when it is done.
   The rule belongs there, next to the workers that satisfy it —
   three surfaces re-deriving it from raw counters would drift, and
   a trail whose steps disagree points nowhere.
2. Put `data-guide="<id>"` on each element along the path.
3. Call `guide.paint()` after that markup is built (renderers do
   this at the end; the drawer does it on open).

A task whose completion is a judgement rather than a state we can
read (visiting a screen IS the whole of it) is retired with
`guide.seen(id)` and must be listed in `_GUIDANCE_DISMISSIBLE`.

A control that sits past the fold also gets `data-guide-scroll`.
That attaches the **puck**: a handle that rides the bottom of the
viewport while the target is out of sight, lands exactly on it as it
scrolls into view, and slides under it — the control it points at is
the thing that hides it. Tapping it scrolls the target to the puck's
own resting place, so the button arrives under the finger that asked
for it. It is `position: sticky`, never a scroll listener: the rail
is an out-of-flow strip from the top of the screen down to the
target's centre, and a sticky box cannot leave its containing block,
so the puck physically cannot drift past the target. The one
measurement is the rail's height, re-read by a `ResizeObserver`.
The guided control needs an opaque background for the tuck to read
(see `.btn-secondary.is-guided`).

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
