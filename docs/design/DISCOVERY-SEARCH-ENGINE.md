# Discovery — Modular Search Engine (v2)

> **Status:** design brief, agreed in the 2026-06-30 redesign session; not yet
> implemented.
> **Supersedes:** the v1 brief (2026-04-28). v1 assumed two axes
> (target × dimensions), owned-only results, track-grain genre, and no
> cross-script search. All four assumptions are wrong now — see below.
> **Scope:** this engine **replaces the entire search layer** — the five
> `discovery.py` endpoints, the `search.py` functions, **and** the MCP search
> tools (`mcp/assistant_server.py`: `search_semantic/similar/artists/albums/`
> `tracks/genres/lyrics`, `play_similar`). One search core; Discovery UI and
> the AI agents are both clients of it.

## Why a rewrite

The search layer is **three parallel implementations of the same filters**:
`discovery.py::_filter_clauses`, `search.py::_apply_filters`, and the MCP
tools' own SQL — already diverging (`instruments ?|` vs `?`, different key
sets). Adding one dimension means touching all three. This is the forbidden
"two parallel patch sites" anti-pattern, tripled.

Beyond duplication, four structural problems:

1. **Result-entity mismatch.** `Vocal+Female` (two artist-level filters)
   returns hundreds of *tracks*; the user wants the *female vocal artists*.
   Target entity must be selectable, with lower-level filters promoted via
   `EXISTS`/aggregation.
2. **Owned-only is obsolete.** Sautium is not bounded by the physical library
   (phantoms from similar-artists discovery + streamable tracks). Today: 37 000
   owned tracks vs **2.96 M phantom**; the network will only grow this.
3. **No cross-script search.** Canonicalization renames artists to their native
   script (Cyrillic, CJK). There is no way to find `Высоцкий` from a Latin
   keyboard. No romanized form exists anywhere in the DB today.
4. **Must scale.** 3 M rows now, plausibly 100 M as the P2P network grows. The
   engine must lean on indexes + query-time narrowing, not Python post-filtering.

## Core principle — the engine is a structured API, not a parser

The engine accepts **already-structured** parameters:
`(target_entity, relevance_sources + seeds, filters, corpus)`. It does **not**
parse natural language. Intent decomposition — turning *"romantic saxophone
with female vocals"* into `semantic_text="romantic saxophone"` +
`filter(instrument=saxophone, gender=female, vocalist=vocal)` — is done by the
**AI-chat**, which is just another client. The two first-class clients:

- **Discovery UI** — chips/inputs fill the structured params directly; an
  optional free-text box feeds the semantic source.
- **AI agents (MCP)** — the LLM decomposes the user's phrase and calls the
  engine with structured params.

A composite query is natively `hard filters ⊕ optional semantic text`. The same
contract serves chip-mode (precise) and phrase-mode (via chat) — the only
difference is who filled the fields.

## The four axes

The v1 brief had two axes. Reality has four, and their **independence** is the
architecture.

### Axis 1 — Entity target

`artist` · `album` · `track` *(+ future: `stream-hit`)*. Each declares in an
**entity registry**: primary key, name field, default ordering, base SELECT/FROM
joined to its dependencies, and how it surfaces a representative cover /
`media_file_id` for tile rendering.

### Axis 2 — Relevance source (where the score comes from)

A **relevance-source registry**. Each source produces a query vector or a
fuzzy score and has a **coverage** (which rows it can possibly match).
Crucially, coverage is a **row property** (is this row enriched?), **not** an
ownership property — see Axis 4.

| Source | Mechanism | Grain | Today's coverage |
|---|---|---|---|
| `name-latin-fuzzy` | trigram + levenshtein on `name_latin` | artist/album/track | all rows (once backfilled) |
| `clap-text` | CLAP text→audio cosine (HNSW) | track | rows with a CLAP vector |
| `clap-seed` | CLAP **track-vector**→audio cosine | track | rows with a CLAP vector |
| `lyrics-vector` | BGE-M3 lyrics cosine, MAX over chunks | track | rows with lyrics embeddings |
| `bio-vector` | BGE-M3 artist-bio cosine | artist | artists with bio embeddings |
| `genre-desc-vector` | BGE-M3 genre-description cosine | genre | genres with desc embeddings |
| `composed-text-vector` | BGE-M3 over composed track metadata | track | **built, currently unread — see below** |
| `browse` | no relevance, default ordering | any | all rows |

`clap-text` and `clap-seed` are **the same vector-KNN path** with a different
*origin* of the query vector (encode-text vs read-the-playing-track's-vector).
They must be unified, not two functions. `clap-seed` is the "context overlay"
feature: take the playing track's CLAP vector, apply dimension constraints
(`key`, `genre`, …) → *"more like this, but in a minor key / in genre X"*.

### Axis 3 — Dimensions (hard filters)

A **dimension registry**. Each declares: entity level, SQL clause template
(named params), kind (`exact`/`range`/`set`/`aggregable`), required joins
(lazy), and a promotion strategy (how it filters when the target is a higher
level — typically `EXISTS`).

| Dimension | Level | Notes |
|---|---|---|
| `vocalist` | artist | `artists.is_vocalist`. **The only reliable vocal signal** — `audio_features.vocal_instrumental` is broken (35 848/37 515 = "instrumental"). |
| `gender` | artist | `artists.gender` |
| `bpm_min/max` | track | `audio_features.bpm` (99.8% populated) |
| `key`, `mode` | track | 100% populated |
| `energy` | track | `energy_db` buckets low/mid/high (100%) |
| `danceable` | track | `danceability >= 0.5` (100%) |
| `moods` | track | `audio_features.moods` JSONB, **100% populated**, fixed 6-label set (happy/calm/sad/aggressive/dark/energetic). Ready-made; not yet exposed. |
| `instruments` | track | `audio_features.instruments` JSONB `?|`. **Sparse by catalog nature** (54% empty — ambient). Valid filter where present; pair with `clap` for recall. |
| `genre` | **album** | **album-grain now** via `album_genres` (track_genres dropped). `EXISTS (album_genres ⋈ genres)`. Present on phantoms too (mb source). |
| `year` | album | `albums.release_year` |
| `quality` | file | `media_files.is_lossless` + `sample_rate` + `bit_depth` (lossy/lossless/hi-res) |

### Axis 4 — Corpus layer

`owned` · `phantom` · `streamable`. **Orthogonal** to the other three.

- **Ownership is derived, never a flag**: owned ⟺ `EXISTS media_files`. Phantom
  track ⟺ no `media_files` (hangs off `album_tracks`). Phantom album ⟺ no
  `album_variants` (carries `cover_url`, `musicbrainz_id`).
- **Signal presence is a row property, not a corpus property.** A phantom row
  *can* have a CLAP vector / lyrics / features — it gets them via streaming
  auto-enrichment (incl. lyrics) and, in future, P2P. The engine JOINs on
  vector/feature **presence**, never on ownership. Today's signals happen to be
  mostly on owned rows (+~515 preview-analyzed phantoms); that is a data
  snapshot, not a design boundary.
- **Corpus is an explicit search control** (`owned`/`phantom`/`all`), not an
  engine decision. **Default `all`**; fall back to `owned` only if testing shows
  weak performance at scale.
- Every result carries an `owned`/`phantom` attribute; the UI dims phantoms
  (already semi-transparent). Output is **mixed and ranked by relevance**, not
  split into separate blocks — the user wants the best results; the limit +
  ranking handle volume.
- **Flood** (e.g. `key=C minor` → hundreds of equal-relevance rows) is an
  *under-constrained-query* problem, not a corpus one — restricting to owned
  doesn't fix it (owned alone still floods). The fix is letting the user narrow
  (add genre, …). The engine must provide a **stable secondary tiebreak**
  (play_count / recency) so order is sane before narrowing.

## Cross-script search (transliteration)

Canonicalization renames artists to native script; there is **no romanized form
anywhere in the DB** today. We generate it.

- **New field `name_latin`** on `artists` (and a Latin search form on
  `albums.title` / `tracks.title` — transliterate **all** entities; phantom
  growth is gradual, same as artists) + a **GIN trigram index** on it.
- **Bidirectional phonetic bridge via symmetric normalization** — the *identical*
  `latinize()` function is applied at **write time** (into `name_latin`) and at
  **query time** (to the query string); matching is plain `pg_trgm` +
  `levenshtein` over `name_latin`. This is the Lucene/Elasticsearch/Solr
  textbook recipe.
  - `мадонна` → `madonna` (0 edits to "Madonna").
  - query `Vysotskiy` ↔ stored `vysotskiy` (from `Высоцкий`) — exact / 1-edit.
- **Stack** (all permissive — the PyInstaller `.exe` bundles deps, so GPL is
  disqualifying): **`anyascii`** (ISC) as the universal symmetric folder
  (BGN-phonetic Cyrillic + perfect accented-Latin fold), with script-dispatched
  CJK overrides **`pypinyin`** (MIT, Chinese) / **`cutlet`** (MIT, Japanese
  Hepburn) / **`koroman`** (MIT, Korean RR). CJK is **first-class** — the
  open-source audience may be majority-Asian. Reject `Unidecode`/`pykakasi`/
  `transliterate` (GPL) and `PyICU` (heavy native build, ISO-9 default,
  kanji-as-Chinese). No in-DB transliteration (`icu_ext` exists but is VOLATILE,
  ISO-9, and mis-handles kanji).
- **Romanization standard = BGN/PCGN English-phonetic** (`я→ya`, `х→kh`,
  `щ→shch`, `-ий→-y`). Reject ISO-9/GOST (diacritics break trigram tokenization;
  4-5 edits, beyond the `levenshtein≤2` gate). Fuzzy absorbs intra-family
  variance (`ya↔ia`, `y↔i`, `shch↔sch` — 1 edit each).
- **One canonical `name_latin`** for Cyrillic/accented-Latin (fuzzy collapses
  the ≤2-edit variance). **Multiple forms** only for **CJK**, where variants are
  *different words* (中田 = Nakata **or** Nakada) that fuzzy cannot bridge —
  a one-to-many alias table fuzzy-matched alongside `name_latin`.
- `name_latin` is **the base search channel for all scripts** (one code path);
  the original `name` contributes a precision bonus on native-script exact
  match. (Open: the original-`name` trigram may become redundant.)

### Domain split — the engine READS the field; scan/canon FILLS it

The engine never transliterates; it only queries `name_latin`. Population is a
**separate domain**:

- **Algorithmic transliteration** (the stack above) is the **base layer** — it
  works with **zero external dependencies**, so search functions without
  MusicBrainz. This is mandatory.
- **Canonicalization** is the **enrichment layer**. MusicBrainz's human-curated
  locale-tagged Latin aliases are the ideal precision source for hard CJK names
  — but MB is optional (the dump isn't loaded on every node), so it enriches
  `name_latin` **only when present, never as a dependency**. Canonicalization
  will keep evolving (future: P2P canonicalization for nodes that don't want to
  download MB).

Refactoring scan/canon to populate `name_latin` is done **as part of this engine
work** (the implementer understands the requirement best), but it stays
architecturally in the scan/canon domain, not the engine.

## Tools, sources, and score composition

The engine is driven by **search tools** — one per UI control (text search,
Vocalist, Gender, Energy, Instruments, …). Each tool declares its **sources**
and its **targets** (entities its results can resolve to). Active tools union
their targets; for each target the engine JOINs every active source in and
builds one query. `Tool = { sources: [Source], targets: [entity...] }`.

A **source** carries a score contract — this is how heterogeneous signals mix:

    Source = {
      score_sql,      # SQL expr → raw score in [0,1]  (e.g. similarity(a.name_latin, :q))
      floor, ceil,    # normalization bounds (below)
      weight,         # importance in the cross-tool sum
      is_gate,        # binary filter: 0/1, pure WHERE, contributes no rank
      level, needs_join, bridge_hint,
    }

**Normalization — calibrated per-source min-max, NOT rank fusion.** A raw `0.5`
means different things per source (CLAP text→audio rarely exceeds 0.4; BGE
text-text easily hits 0.7), so raw scores aren't comparable. Each source declares
two empirical bounds and maps to a shared scale:

    norm = clamp( (raw − floor) / (ceil − floor), 0, 1 )

`floor` = the relevance threshold (already de-facto our per-search
`min_similarity`); `ceil` = the typical "strong match" (≈90th percentile of good
matches). After norm, `0.5` means the same everywhere. This keeps **magnitude**
(an exact name match → `norm 1.0` dominates) and is stable across queries (bounds
are empirical, not per-query min-max). RRF (rank fusion) was rejected: it discards
magnitude and exists for black-box systems whose scales you can't see — we own our
sources, so calibrating is strictly better.

**Combination (two levels):**

    norm_i   = clamp((raw_i − floor_i)/(ceil_i − floor_i), 0, 1)
    tool_rel = GREATEST(norm over the tool's relevance sources)   # OR within a tool
    final    = Σ_tools  weight_t × tool_rel_t                      # sum across tools
       WHERE  Π(gates)=1  AND  each relevance raw ≥ its floor
       ORDER BY final DESC, <stable tiebreak: play_count / recency>

Everything is SQL-expressible (`clamp` = LEAST/GREATEST), so scoring, filtering,
and ordering stay in the query — no pulling rows into Python to sort. **Calibrating
`floor`/`ceil` is a separate, iterable layer (Phase 2): constants in the source
registry, tuned from the real score distribution — not an architecture change.**

## Bridges — how a source reaches a target

A source lives on some table; the target is another entity. The engine connects
them through **bridge joins** from a **static, corpus-aware edge registry** — NOT
an auto-path over the FK graph. Why static: `track↔album` has TWO edges —
`album_tracks` (phantom; 1 hop) and `media_files→album_variants` (owned; 2 hops).
An FK-graph BFS picks the shorter `album_tracks` and silently loses **87% of owned
tracks** (only 4 972 / 37 000 owned appear in `album_tracks`). Topology ≠ semantics:
the FK graph doesn't know `media_files` is owned-only and `album_tracks` is
phantom-only. So edges are declared explicitly with corpus semantics; path
composition (shortest, dedup of shared tables) runs BFS over the declared edges.
`corpus` therefore **selects a bridge** (owned→media_files, phantom→album_tracks,
all→both), not just a WHERE filter. Any path through `media_files` is owned-only,
so a file-level tool (quality) is physically inapplicable to phantoms — dropped for
now; it returns as a **two-source** tool (file-tier for owned, stream-tier for
phantom: YouTube lossy / Deezer lossless).

**`corpus='all'` branches only at the `media_files` boundary, not the whole query.**
The owned/phantom split is LOCAL — only `track↔album` differs; `artist↔track`,
`artist↔album`, `album↔genre` are corpus-agnostic. So compose a **shared CTE head**
(the corpus-agnostic prefix — e.g. the matching album/artist ids) and **two thin
tails** off it (owned via `media_files`, phantom via `album_tracks`), UNION'd — the
head computes once. UNION vs LEFT-JOIN-both for the tails is decided by `EXPLAIN`,
not theory. A deferred option: extend `album_tracks` to owned rows too, making
`track↔album` a single corpus-agnostic 1-hop and removing the split — at the cost of
duplicating the owned link (`media_files` keeps the physical edition). Not needed if
the CTE-head/tails path performs.

## `composed-text-vector` decision (revive vs delete)

`text_embeddings` (36 955 rows, ~all owned tracks; BGE-M3 over
`title+artist+album+year+lossless+genres+top-10 artist tags`) is **built and
HNSW-indexed but read by nothing** — only its builder references it. It is the
**only per-track vector combining genres+tags+year**, so it can match *"German
synth-pop, 80s"* where CLAP (sound), bio (artist-level) and genre-desc
(genre-level) cannot. **Lean: revive it as a relevance source**, but review its
composition (`lossless` is semantic noise). **Resolve when finalizing the
relevance-source set** — delete only if bio + genre-desc prove to cover it.

## Streaming search (future)

Streaming has **zero schema footprint** (100% runtime: `backend/streaming/`,
YouTube core + Deezer BYO-plugin lossless). The future *"search Deezer for a
track/album/artist"* idea needs **no new engine**: the `StreamProvider` contract
gains a `search()` method; results are **minted as phantom rows on-demand** (the
way `_store_similar_artists` mints phantoms today); the engine then searches them
as ordinary phantoms. Lay an **"entity source = DB vs provider"** abstraction now
so this slots in later without rework. The Deezer catalog is huge — mint top-N
per query, never en masse.

## Proposed phasing

*(Order proposed, not yet locked — Phase 0 has standalone value and is the
natural start.)*

| Phase | Scope |
|---|---|
| **0** | **Transliteration + `name_latin`** — standalone value, independent of the engine. Field + GIN trgm index, `latinize()` dispatch module, backfill, wire into scan/canon population. Point the existing `/artists` at it for an immediate cross-script win. |
| **1** | **Engine skeleton + registries** on `target=track` (simplest): entity registry, dimension registry, query builder, one relevance source, fixture tests. |
| **2** | **Unify vector sources** — `clap-text` + `clap-seed` (one path), `lyrics`, `bio`; absorb `_similar_by_track_embedding`; resolve `composed-text-vector`. |
| **3** | **Entity targets `artist`/`album`** — cross-level promotion (EXISTS / aggregation). Also: **album/track title aliases** (CJK multi-form + human file tags) — copy the `artist_name_aliases` pattern to `album_title_aliases` / `track_title_aliases` (separate FK-CASCADE tables, **not** a polymorphic `name_aliases` — poly loses the FK and canon rewrites album/track UUIDs — and **not** a `text[]` column — `gin_trgm_ops` needs a normalized/unnested table). Deferred here because the alias search channel only lands with album/track targets; populating earlier is dead capital. |
| **4** | **Corpus layers** — owned/phantom/streamable control, mixed ranking, owned attribute, stable tiebreak. |
| **5** | **Absorb all clients** — cut over the `discovery.py` endpoints and the MCP search tools to the engine; wire AI-chat decomposition. |
| **6** | **UI** (Claude Design handoff) — corpus control, new dimensions (genre/mood/year), composite chip+text. |
| **7** *(future)* | **Streaming search** — `provider.search()` → mint phantom. |

## Out of scope (first iteration)

- Pagination beyond `limit`.
- Real-time index (DB is fast enough at current scale; revisit near 100 M).
- Saved searches / smart playlists.

## Constraints

- **Push computation into SQL** — filtering, ranking, scoring, top-N, text
  normalization all in-query. Transliteration is the one write-time exception
  (it's normalization, not matching; matching stays in SQL).
- **No third filter implementation** — the dimension registry is the single
  source of truth; `_filter_clauses` and `_apply_filters` both die.
- **Don't migrate the JSONB** (`instruments`/`moods`) — `?|` already works.
- Browse mode (no relevance source) must stay useful — default ordering per
  entity lives in the entity registry.

## Reference — current-state files (to be absorbed)

- `backend/routers/discovery.py` — 5 endpoints + `_filter_clauses` (dies).
- `backend/search.py` — `search_by_text/lyrics/...`, `_similar_by_track_embedding`,
  `_apply_filters` (dies).
- `mcp/assistant_server.py` — the `search_*` / `play_similar` MCP tools.
- `backend/static/app-shell.js` — `wireDiscoveryFilters`, `appendFilterParams`,
  `runUnifiedSearch`, `runFilterOnlyBrowse` (frontend filter UI).
- `backend/text_embeddings.py` — builder for the currently-unread
  `composed-text-vector`.
