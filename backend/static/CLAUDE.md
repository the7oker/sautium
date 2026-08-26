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
