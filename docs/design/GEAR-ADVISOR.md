# Gear Advisor — system analysis & upgrade strategy

Status: research worker (Phase 2) **implemented 2026-07-12**; pair engine and
upgrade advisor designed, not yet built. This document is the source of truth
for the feature's stance, layers and roadmap. The design was validated by a
live end-to-end experiment before any code was written (see §Experiment).

---

## Stance: evidentialism

The audiophile world is split into warring camps (objectivists: "SINAD is
truth", subjectivists: "ears over instruments"). Sautium joins neither. Every
claim gets a **weight proportional to the quality of evidence behind it** and
a **provenance label**:

- **Objectivist in method** — physics and audibility thresholds are the
  customs office every claim passes through. A claim that contradicts
  measurements gets flagged or dropped regardless of how many owners repeat it.
- **Subjectivist in subject** — aggregated owner experience is *data*, not
  noise: QC statistics, pair synergies, consensus on unmeasurable qualities
  (resolution, stage). Always attributed, always with sample size.
- **Agnostic in verdict** — the engine never says "better". It shows where the
  difference is measurable, whether it crosses an audibility threshold, what
  the community believes (and how the camps split), and what any of it means
  for *this user's* library. The decision stays with the owner.

Subjective claims fall into three baskets: **consistent with measurements** /
**beyond existing metrics** (admissible, labeled) / **contradicts physics**
(shown with skepticism or dropped). Never average the three voices into one
score — a single "8.7/10" is the one output both camps would rightly despise.

Key design rule from the experiment: **headline specs never rank sound.**
SINAD-ranking puts a $9 dongle above a NOS R2R flagship. Deterministic specs
answer *functional* questions only (power, impedance interaction, format
chains, noise floors); every measured delta is classified against
psychoacoustic thresholds ("below audibility" is a first-class verdict).

## The three data layers

1. **Deterministic facts** — specs and measurements (datasheets, ASR,
   GoldenSound, oratory1990/squig-class FR databases). Stored in `gear_specs`
   (EAV with `source_url` provenance). Pair compatibility is computed from
   this layer with **no LLM at runtime**.
2. **Community consensus** — praise/criticism terms, camps, pair synergies.
   Stored in `gear_sentiment_terms` + aggregate columns. Pair-level synergy is
   researched on demand and cached (future), never mass-scraped.
3. **User context** — the unfair advantage no forum has: sample-rate
   distribution of actual listening, genre profile (sub-bass share!), measured
   DR percentiles of the library, playback outputs in use. Turns generic specs
   into personal verdicts ("your 85.8% of listening is ≤48k", "your p90 DR of
   31.5 dB hits Susvara's 114 dB driver ceiling at avg ≥82 dB").

Source hierarchy is **category-dependent**: for electronics,
`datasheet > measurements > forums`; for headphones/IEMs the hierarchy is
**inverted** — community measurement databases beat manufacturer sheets
(Susvara's official sheet is four values; its measurement corpus is vast).
Measurement rigs (GRAS 43AG / 45CA / B&K 5128) must never be mixed silently.

## Data model (already in 001_initial.sql)

`gear_brands` / `gear_models` (research_state: queued → researching → cached |
failed) / `gear_spec_attributes` (canonical EAV catalog, UUID v5 from key,
`seeded` flag separates hand-curated from AI-proposed) / `gear_specs` /
`gear_technologies` (+ `gear_model_technologies`) / `gear_sentiment_terms` /
`user_gear` (own | want | sell | previously_owned, `public_gear` opt-in).
All ids are UUID v5 → P2P-mergeable across nodes.

## Research worker (Phase 2 — implemented)

`backend/gear_research_worker.py`. Event-driven per project rules:
`POST /api/profile/gear` NOTIFYs `gear_research` after inserting a queued
row; the worker holds a LISTEN connection (same pattern as the chat SSE
listener), drains the whole queue on wake-up, and re-queues orphaned
'researching' rows on startup. Idempotent: every write is upsert-shaped.

AI call, in preference order:

1. **Claude Code CLI** (`call_claude_code(..., timeout_seconds=480,
   mcp=False)`) — built-in WebSearch/WebFetch, subscription-billed, the same
   runner the AI assistant chat and ai_canon use. No MCP config: research needs the
   web, not the library.
2. **Anthropic API fallback** with the server-side `web_search` tool
   (pause_turn continuation loop, max 12 searches) when Claude Code is absent
   or returns no parseable JSON.

The prompt is `gear_research_prompt.build_prompt()` — canonical-catalog
injection, reuse-or-propose attribute keys, citations required
(`source_url`), "omit rather than guess". Persistence enforces the same:
specs without `source_url` are skipped; unknown attribute keys without an
accompanying `new_attributes` definition are skipped; AI-proposed attributes
and technologies land with `seeded=FALSE` for later review.

`failed` rows stay failed — research burns real tokens/time, so retry is a
deliberate user action (future Retry button), never an automatic loop.

Cost/latency envelope per model: minutes, not seconds (a full manual run of
one model took 6–14 min with a deep multi-product prompt; the focused
single-model prompt runs shorter). This is why results are cached forever
(`researched_at`) and why P2P sharing of research results matters later.

## Deterministic pair engine (Phase 3 — next)

Pure computation over `gear_specs`, no LLM. The threshold table IS the
product:

- Output impedance vs load: 1/8 rule **against the impedance-curve minimum**,
  not the nominal (AM5LE: "26 Ω" nominal, measured min 14.3 Ω); computed FR
  interaction across the Z-swing, classified against ~1 dB midband threshold.
- SPL headroom: required voltage/power for ~110 dB peaks from sensitivity
  (prefer measured dB/V over datasheet dB/mW), against source capability;
  crest-factor check against the library's DR percentiles (layer 3).
- Gain staging: DAC output vs amp max input; documented level offsets
  (Cyan 2 DSD −6 dB) surface as A/B level-match warnings.
- Format chains: max PCM/DSD per hop, OS-dependent ceilings (native DSD512
  Windows-only on some DACs vs Linux NAA → DoP limit), NOS-only DACs marking
  the upsampler as an architectural dependency.
- Electrostatic domain check: bias voltage + connector = hard incompatibility
  with conventional amps (energizer required).

Verdict grammar: ✓ pass / ⚠ pass-with-caveat / ✗ conflict / ⌀ no data — each
with the numbers shown and a provenance tier per number
(DS datasheet / M measured / D derived / F forum).

## Upgrade strategy (Phase 4 — designed)

The core audiophile question: "how do I improve what I have for sane money."

1. **Plateau diagnosis first.** Run the pair engine over every link; where the
   delta to best-in-budget is below audibility, say "plateau — money goes
   elsewhere". (The experiment's verdict for the reference rig: DAC and amp
   are measured plateaus; all budget flows to transducers.)
2. **Transducers-first** with deterministic exceptions (underpowered planar →
   amp first; hiss on sensitive IEMs → source first; format wall → DAC).
3. **Genre-weighted frontier, not a ranking.** Budget slider → Pareto frontier
   of candidates; each card carries the three voices + "improves X% of your
   library, regresses Y%" computed from genre shares. Anti-recommendations
   (where NOT to spend) are first-class output.
4. Candidate sources, cheap-to-expensive funnel:
   - measurement registries (squig/crinacle DBs, ASR review index,
     oratory1990 list) — structured, cover cold start;
   - own P2P catalog (`gear_models` researched by any node) — grows into the
     primary source;
   - need-driven search (the diagnosed gap queries curves and sentiment
     terms, not "top-10" listicles);
   - co-ownership graph across nodes (`public_gear`) — collaborative
     filtering, network-effect source;
   - user wishlist (`status='want'`) — "check my candidate" mode;
   - (later) street/used prices — a tier of its own ($6k MSRP vs $4.1k street
     changes frontier position).
   Deterministic filters run before expensive research; only finalists get
   the full three-voice treatment. Undisclosed candidates stay visible as
   "not researched" — no silent truncation.

## Experiment (2026-07-12) — reference output

Manual end-to-end run of the whole pipeline on the maintainer's real rig
(6 parallel research agents, source hierarchy enforced, no bot-protection
bypass, pair engine as a script, report in Sautium DS):
https://claude.ai/code/artifact/f16b32f7-75fa-4bb6-a8be-59209fd42ace

Findings that define the quality bar:

- **Blind test passed**: the engine independently reproduced the owner's
  prior conclusion on Chord Qutest — measured top-class yet a *system
  downgrade for this chain* (RCA-only vs balanced chain; Linux NAA → DoP256
  ceiling; 768k feed bypasses the WTA stage the price pays for).
- Deterministic layer caught non-obvious numbers: SRM-T8000 ≈113 dB ceiling
  on SR-X9000; Susvara driver bottoming (114 dB) × library p90 DR 31.5 dB →
  avg ≤82 dB; stock-pad Elite sub-bass ≈ HD650-class, hybrid pads fix it for
  free.
- Sentiment layer carried facts absent from any datasheet: spritzer's T8000
  teardown verdict, HiFiMAN QC lottery, KANN Ultra low-Z clipping (ASR),
  AM5LE measured 2.0 dB L/R mismatch (N=1).
- Context flipped verdicts: the SBAF "HD650 dequalifies flagships" thesis is
  measured-true in mids (0.93 vs 2.73 dB RMS vs Harman, same rig) and
  measured-false below 40 Hz for this library's sub-bass genres.
- 7 honest gaps documented; one (AM5LE impedance curve) was later closed via
  VPN and the pair verdict recomputed — the lifecycle the worker must
  eventually support (`re-research when a previously unreachable source
  appears`).

Method limits to encode in the worker: SBAF is snippet-only (bot protection
respected), exact ASR SINAD figures live in dashboard images, some measurement
hosts are region-blocked, head-fi renders JS-only.

## UI surfaces (per INFORMATION-ARCHITECTURE)

- Trigger moment: "component added → analysis card arrives" (SSE push).
- System screen: chain rails with per-pair verdict rows (status stripe,
  mono+blue numbers, tier chips).
- A-vs-B compare: category-specific (headphones: overlaid FR from one rig,
  banded deltas vs audibility; electronics: functional unlocks + threshold
  verdicts + camp split).
- Upgrade path: budget slider → frontier cards.
- The experiment report doubles as the visual reference for all of these.

## Roadmap

1. ✅ Schema + catalog UI + research prompt (pre-existing).
2. ✅ Research worker (this change).
3. Pair engine (`gear_pairs` or computed-on-read from specs) + thresholds
   table + System screen.
4. "Component added → card" trigger + AI-sheet entry point.
5. Upgrade advisor: plateau diagnosis + frontier + genre weighting.
6. P2P: sync researched gear facts (same signing/karma rails as audio
   analytics), co-ownership graph, pair-synergy cache sharing.
7. Refresh policy: re-research staleness (`researched_at` TTL per category),
   re-research on newly reachable sources, Retry button for `failed`.
