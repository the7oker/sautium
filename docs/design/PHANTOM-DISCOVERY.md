# Phantom Discovery — Music Beyond the Local Catalog

> **Status: largely SHIPPED — this file is now a design-rationale + status
> record, not an open proposal.** Live: Phases 0–3 (phantom artists, albums,
> tracks), the MB-anchored canonicalization foundation, missing-albums,
> phantom tracklists (`album_tracks`), CAA covers, phantom similar-artist
> discovery, the provider→HQPlayer streaming preview (D4 / Phase 6,
> shipped 2026-06-28), and source-agnostic listening tracking of streamed
> phantom plays (history/stats/scrobble + Home-shelf sessions, 2026-06-30).
> Phase 5 followed: the Discovery engine treats owned and phantom rows as
> corpus layers (`corpus=owned|phantom|all`, chosen as a bridge, not a WHERE
> filter), so phantom entities are searchable alongside local ones.
> Phase 4 was **superseded** rather than built: the skeleton does not travel
> as rows — the network ships MB slices, and each node mints its own phantom
> albums and tracklists from them (see D6). What does travel is the analysis
> hanging off those rows, through sync and carry.
> **Remaining:** Stage C (text-similarity). The
> "Phases" table and "Design decisions" below are the original plan, kept for
> rationale; "Implementation status" records what actually landed.
> **Origin:** proposed 2026-06-01 by Valerii.
> **Relates to:** `DISCOVERY-SEARCH-ENGINE.md` (the engine that will
> surface phantom entities alongside local ones),
> `reference_artist_photos.md` (Deezer integration pattern + throttle).

## Goal

Turn Sautium from "a manager of *my* library" into "an engine that
discovers music I don't yet own, but that matches my taste."

Concretely: start persisting **similar artists that are absent from
the local FLAC catalog** ("phantom" artists), enrich them from
external sources, surface them in the UI as semi-transparent tiles
*after* the artists physically present in the library, and use them
as the basis for recommending material the user does not own.

A second, lower-risk payoff falls out of the same data-collection
work: for artists the user **does** own, fetch the full discography
and show **new albums the user is missing**.

## Vocabulary

- **Local entity** — artist/album/track with at least one row in
  `media_files` (a physical file on disk).
- **Phantom entity** — an artist/album/track row that exists for
  enrichment/discovery purposes but has **no** `media_files`. Same
  table, same UUID space, no physical file.

## The architecture already supports this (schema is permissive)

Investigated 2026-06-01. The schema does **not** tie enrichment to
physical files — locality is enforced by *code*, not constraints:

- `artists` (`desktop/migrations/001_initial.sql:43-57`) has **no**
  `is_local`/`has_files` flag and **no** FK to `media_files`/`tracks`.
  An artist row can exist with zero tracks.
- `albums` (`:59-71`) has no FK to artists and no requirement for
  `album_variants`/`media_files`.
- `tracks` (`:73-78`) is referenced *by* `media_files.track_id`, not
  the reverse — a track with no file is legal.
- All enrichment tables — `artist_bios`, `artist_tags`,
  `similar_artists`, `album_descriptions` — FK only to the
  logical entity, never to a file, and all carry a `source` column
  for provenance.
- `similar_artists` (`:307-317`): both `artist_id` and
  `similar_artist_id` FK to `artists(id)`; they require the artist
  **row** to exist, **not** any `media_files`.

**What enforces the bound today (the engagement gates):**

- `backend/lastfm.py` `enrich_artist` — bio-time similars are fetched
  for OWNED artists only (`track_artists JOIN media_files`).
- `backend/lastfm.py` `backfill_similar` — the engagement gate: owned
  file OR a completed, unskipped listen (`listening_history`); run as
  the background `similar` step, it is how listened phantoms
  (catalog-less mode) get their similars.
- `desktop/sync_client.py` `_engaged_artist_uuids` — the sync client
  filters the similars pull by the same engagement predicate before
  requesting anything from a peer.

### Why UUID v5 makes phantom→local seamless

`backend/uuid_utils.py` derives every shareable UUID
deterministically from the normalized name:

```
artist_uuid(name)         = uuid5(NS, "artist:{normalize(name)}")
album_uuid(title, artist) = uuid5(NS, "album:{normalize(artist)}:{normalize(title)}")
track_uuid(title, artist) = uuid5(NS, "song:{normalize(artist)}:{normalize(title)}")
```

A phantom artist and the *same* artist once its FLAC is later ripped
collapse to **one** UUID. Enrichment collected on the phantom
auto-attaches to the real files on import — no migration, no
dedup. Phantom→local is an **upgrade, not a conflict**. The same
holds for phantom albums/tracks.

## Design decisions

### D1. Local vs phantom: derive, don't denormalize (initially)

There is no `is_local` flag. Two options:

1. **Derived** — `EXISTS (SELECT 1 FROM track_artists ta JOIN
   media_files mf ON mf.track_id = ta.track_id WHERE ta.artist_id =
   a.id)`. Always correct, nothing to maintain.
2. **Denormalized `is_local boolean`** on `artists`, maintained by
   the existing post-import classifier hooks
   (`_update_artist_gender` / `_update_artist_is_vocalist`).

**Decision: derive first** (CLAUDE.md "push computation into SQL",
"trust internal guarantees"). Expose `is_local` as a computed field
in API responses so the renderer can dim phantom tiles. Add the
denormalized flag only if profiling shows the EXISTS is hot on a
real screen.

Note the **natural safety**: almost every existing query already
JOINs `track_artists`, so phantom artists do **not** leak into
search/stats/Discovery by default. The work is the *opposite* —
deliberately *including* phantoms on the Similar-artists and Genre
screens.

### D2. Eager vs lazy enrichment: artist eager, albums/tracks lazy

Eagerly enriching full discographies explodes:
`~2550 local artists × ~20 similar × ~10 albums × ~10 tracks` ≈
millions of phantom rows and an enormous external-API bill.

- **Background enrichment stores only**: the phantom **artist** row +
  `artist_bios` + `similar_artists` links + `artist_tags`. Cheap,
  bounded.
- **Albums/tracks of a phantom are fetched on demand** when the user
  opens that artist's screen, then cached.
- **Bound the graph to 1 hop from engagement**: similars are fetched
  only for artists with an owned file or a completed human listen. No
  *unconditional* recursion — a minted stub becomes a seed only when a
  human completes a listen on it, so expansion is linear in listening,
  never combinatorial (that is the trap).

### D3. External sources

| Need                    | Primary            | Why |
|-------------------------|--------------------|-----|
| Bio / summary           | Last.fm            | Already integrated; good prose. |
| Albums + tracklists     | **Deezer**         | Already integrated (`routers/covers.py`), public JSON API, no OAuth, normalized titles, album art. |
| Canonical release IDs   | MusicBrainz        | Release groups distinguish an album from its reissues/deluxe editions; free, no auth. |
| Listening counts        | ListenBrainz       | `lb_recording` / `lb_artist` per MBID (CC0, lower bounds), from the statistics dump or P2P slices — since 2026-09-20; Last.fm's `track_stats` is gone. |

Last.fm tags/tracklists are noisy and duplicated — **do not** use
Last.fm for tracklists. Streaming services are intentionally **not**
data-storage sources (their ToS restrict retaining metadata); they
appear only in the *preview* path (D4), never the *metadata* path.

Reuse the **global throttle + cooldown** already protecting Deezer
in `routers/covers.py` — Deezer/Last.fm ban the IP under concurrent
load. (See `reference_artist_photos.md`; do not remove that throttle.)

### D4. Phantom preview: audition through the user's own chain

A phantom track has no file, so it cannot enter the queue as a `file://`
URI. Instead of a passive deep-link to a streaming service, the preview
routes the audio into the user's own hi-fi chain: audition transport is
**provider-based** — a `StreamProvider` fetches the audio, the in-memory
proxy serves it to the output (HQPlayer, DLNA or the browser), and Now
Playing tracks it like any owned file. YouTube ships in core; DRM
providers are bring-your-own and out of tree (`backend/streaming/`).
Preview quality is flagged in the UI (lossless / lossy) so it is never
mistaken for the local hi-res experience. Shipped 2026-06-28; the earlier
virtual-cable / live-input design is superseded.
Premature to spike HQP live-input now.

**The demo policy (2026-09-17).** The core channel is an acquaintance
tool, not a free replacement for a streaming service: a track streams
from it in full at most **once** — the listen is spent when playback
passes 90 % (`demo_plays`, life data: backed up and merged, never synced)
— and from then on the track plays as the catalog's own **30 s excerpt**
(the core `deezer_preview` provider: Deezer public API, no auth), which
is also what plays when the core channel is missing or has nothing.
A bring-your-own full-length provider is not limited. Every phantom
album page wears `[demo]`; a row that will play as an excerpt wears
`30s`; Now Playing shows `[30s]` beside the quality badge while one
plays. An excerpt is not the recording: it is never analysed
(`PreviewEnricher` refuses it before any provenance row) and never a
listen (no history, stats, scrobble or now-playing). The ledger governs
what is FETCHED — the resolve leaves the spent channel out of the
track's chain, the proxy refuses it at fetch time, and the spent buffer
is dropped when its listen ends; what an output has buffered for itself
(a browser blob, a renderer's cache) is beyond reach by design.

### D5. Acquisition: "Buy on Bandcamp", not a wishlist

A wishlist needs its own management surface, and at $5–10/album on
Bandcamp the friction of "save for later" is not worth a new screen.
Replace the wishlist concept with a direct **[Buy on Bandcamp]**
action on phantom album/track tiles. Acquisition is one click, not a
list to curate.

**The link is resolved, not searched (2026-09-17).** A search deep-link
keyed on artist + album often landed on nothing, and there is nothing
better to ask Bandcamp for: its official API serves labels and merch
partners only, and the site — search, album pages, all of it — answers
a non-browser client with a bot challenge. MusicBrainz carries the
exact page as a release-URL relationship (~800k Bandcamp urls; the
importer userscript is how most independent releases enter MB), so the
dump keeps three tables filtered to `*.bandcamp.com` (`mb_url`,
`mb_l_release_url`, `mb_l_artist_url`; a slice node receives them in
the artist's v3 slice) and the album endpoint answers `buy.state`:

- `album` — the release group's own page (earliest-dated release,
  `/album/` over `/track/`, so every node picks the same link);
- `artist` — no release link, but a credited artist has a Bandcamp
  page: the link is the shop's `/music` grid, never the root MB stores,
  because a root redirects to whatever release the artist featured
  (Buy then "opened a different album");
- `absent` — the local facts cover the record and name no page: the
  button is disabled;
- `unknown` — the facts are not here yet (a carried phantom whose
  artist's slice has not landed, a record newer than the dump): the
  search fallback stays.

Bandcamp is the only store kept. MB links other download shops under
the same generic relationship types (Beatport, 7digital, a few thousand
HDtracks pages in three URL generations), but one store means one
button state, and the space a store allowlist would save is under a
percent of the dump — the saving that matters is filtering `url` at
all (22M rows → 0.8M).

### D6. P2P propagation is the multiplier

The strongest leverage: phantom enrichment **propagates over the
Sautium network** instead of every node hammering Deezer/Last.fm.
One node enriches an artist's discography; every peer receives it on
sync. This is a genuine network-effect moat.

> Revised 2026-09-19: this holds for audio analysis and the canon layer
> only. Last.fm answers — bios, tags, similars, stats, genre descriptions —
> are node-local (Last.fm's API terms do not allow redistribution), so every
> node fetches its own; the network effect is the analysis.

> Revised again: it propagates, but NOT the way this section proposed. The
> structural rows never enter the sync inventory. Two mechanisms replaced
> that plan, and the split is deliberate:
>
> - **The skeleton is replicated as source material, not as rows.** A dump
>   node answers an artist name with raw `mb_*` rows — the matched artists
>   plus their whole discography subtree — signed under its own key
>   (`desktop/p2p/mb_slice_queries.py`). The requester inserts them into its
>   own `mb_*` tables and runs the ordinary canon pipeline, so it *derives*
>   the same phantom albums and tracklists instead of trusting a peer's
>   version of them. ListenBrainz counts replicate through a second,
>   separate slice family on the same pattern.
> - **The analysis rides sync and carry**, keyed on track UUIDs that already
>   exist locally: segments, audio features and recording bindings. Carry's
>   answer rule states the invariant — *"structural categories are never
>   asked for (the skeleton is already here, MB-canonical)"*
>   (`sync_queries.wanted_tracks`) — and "a recording this node has no row
>   for is a recording it never cared about" is what keeps a push from
>   minting anything.
>
> Why this is better than importing rows: identity stays derivable (every
> node computes the same UUID v5 from the same MB facts), a peer cannot
> invent an album into your catalogue, and the `source='p2p:<nodeid>'`
> trust-but-verify apparatus below is unnecessary for structure — only the
> analysis needs seals, and it has them.

### D7. Text embeddings give phantoms semantic search for free

Phantoms have no audio, so no CLAP embedding is possible — but a
**BGE-M3 text embedding from bio + tags is**. That makes
"find me ambient artists I don't own" work via text similarity even
without audio. This feeds directly into the planned Discovery
engine: a phantom is just another row the engine can target and
score (`DISCOVERY-SEARCH-ENGINE.md` §4).

## UI

- **Artist screen → Similar artists**: render local similars first,
  then phantom similars as **semi-transparent tiles** (the `is_local`
  field from D1 drives the dim state).
- **Genre screen**: same — locals first, phantoms dimmed after.
- **Phantom artist screen**: bio + (lazily-fetched) albums/tracks.
  **No transport/Play** (nothing local to play). Instead:
  - `[▶ Preview → HQPlayer]` (D4),
  - `[Buy on Bandcamp]` (D5).
- **Local artist screen → new releases**: a "New albums you don't
  own" shelf, diffed from the fetched discography (see Phase 1).

## Phases

| Phase | Scope | Risk | Why this order |
|-------|-------|------|----------------|
| **0** | Add derived `is_local` to artist/genre/similar API responses; render dimmed phantom tiles (no phantoms exist yet → no visual change, but the plumbing lands). | low | De-risks the renderer before data arrives. |
| **1** | **New albums for *local* artists.** Fetch full discography (Deezer + MusicBrainz) for owned artists, diff vs `albums`, surface "missing albums" shelf. | low | Validates the album/track source on *known* artists; immediate value; no phantom rows yet. |
| **2** | **Phantom similar artists (eager).** Relax `lastfm.py:348,519`; background-enrich phantom artist + bio + tags + similar links, bounded to 1 hop from local. | med | Core feature; bounded by D2. |
| **3** | **Phantom albums/tracks (lazy).** On phantom-artist-screen open, fetch + cache discography. | med | Storage-safe via laziness. |
| **4** | ~~**P2P propagation.** Relax sync inventory joins; `source='p2p:*'`; post-import handling.~~ **Superseded** by MB slices + carry — see D6. | med | Network effect; depends on phantom data existing. |
| **5** | **Phantom semantic search.** BGE-M3 text embeddings for phantoms; wire into Discovery engine. | med | Depends on Discovery engine landing. |
| **6** | **Provider→HQPlayer preview (D4).** | high | Most uncertainty; ship last, after open questions resolved. |

## Implementation status

**Phase 1 — shipped (2026-06-01).** New albums for local artists,
Deezer-only with heuristic dedup. Hybrid freshness model as designed:
background monthly + fetch-on-view daily, gated by `artists.last_album_sync`.

- Schema: `artists.deezer_id`, `artists.last_album_sync`, `albums.cover_url`
  (+ `idx_artists_last_album_sync`). Staged in `001_initial.sql` and the ORM.
- `backend/deezer_discography.py` — Deezer client (search + paginated albums).
- `backend/discography.py` — `release_match_key` (heuristic reissue/own
  collapse), `sync_artist_discography` (idempotent, persists phantom
  albums via `album_artists` + `cover_url`, never clobbers owned rows),
  `fetch_new_albums`.
- `backend/background_enrichment.py` — `_step_sync_discographies`, scope =
  artists listened-to in 6mo with stale data, 20/batch, shares the Deezer
  cooldown with photo lookups.
- `backend/routers/artists.py` — GET returns `new_albums` + `new_albums_stale`;
  `POST /{id}/sync-discography` is the daily-gated fetch-on-view refresh.
- Frontend — dimmed `.is-unowned` "Missing albums" shelf after Albums
  (the artist's releases absent from the catalog, not just *new* ones),
  amber "Buy" → the MB-resolved Bandcamp page (D5; a search only while the
  facts are not local, disabled when they name no page), in-place stale
  refresh (no flicker).

**Observed on first run (the iteration signal):** `release_match_key`
collapses reissues/editions cleanly (remaster + deluxe → one row; live
stays distinct), and the owned-skip is exact. The open problem is
**release-type noise**: Deezer labels DJ-mixes, compilations, live and
remix albums all as `record_type: 'album'`, so a prolific artist's shelf
mixes genuine studio albums with "fabric presents…", "Late Night Tales",
"… Remixed", "… Tour (Live)", and VA compilations the artist merely
appears on. Title-keyword filtering is fragile (legit albums use those
words). The clean fix is **MusicBrainz release-group `primary-type` /
`secondary-types`** (Album vs Compilation/Live/Remix/DJ-mix/Soundtrack) —
not for dedup (Deezer match-key handles that) but for *classification*.
This is the concrete, evidence-backed case for adding MusicBrainz as the
next iteration, deferred until we decide it's worth the second client.

**Phase 1 rebuilt on the MB dump — shipped (2026-06-12).** The "next
iteration" above landed as a full source swap once the local MusicBrainz
dump + canonicalization (`artist_mbids`) matured: Deezer dismantled
(`deezer_discography.py` deleted, `artists.deezer_id` dropped, 2994
Deezer-era phantom rows purged), discography now derives from
`mb_local.fetch_album_release_groups` for **canonized artists only**.

- **Classification solved at the source:** primary type ∈ {Album, EP};
  any disqualifying secondary type (Compilation/Live/Remix/DJ-mix/…)
  drops the group. `Soundtrack` deliberately passes — an artist-credited
  film score is a first-class studio release (VA soundtracks never enter:
  the fetch is per artist credit).
- **Own-check is two-channel:** release-group MBID equality
  (`albums.musicbrainz_id`, exact) + `release_match_key` title fallback,
  including MB *release* (regional/variant) titles — the same channel
  `mb_audit` overlap-verifies through.
- **Sync is a reconcile, not an append:** upsert newly-missing groups,
  unlink rows that stopped being missing (ripped since / MB reclassified
  / revoked canonization), GC unlinked phantoms, clear a stale external
  `cover_url` once an album becomes owned. Phantom rows now carry
  `albums.musicbrainz_id` = the rg MBID; across artists the rg MBID is
  the identity (a collaboration synced from the other member only gains
  an `album_artists` link).
- **Covers: Cover Art Archive, zero server-side fetching.** The CAA
  image API is explicitly unlimited, so `cover_url` stores the canonical
  `…/release-group/{mbid}/front-500` URL and the browser resolves it
  (tile `onerror` hides a 404). No throttle apparatus at all.
- **Background step re-enabled** (`_step_sync_discographies`): pure
  local-DB work now, 50 canonized artists per batch, oldest-stale first.
  `python /app/discography.py` backfills the whole canonized set.
- **Year:** `release_year` is NULL until a dump refresh loads the newly
  staged `mb_release_country`/`mb_release_unknown_country` tables
  (`first_year` = MIN release date; `release_group_meta` lives in the
  *derived* dump archive the loader doesn't stream — checked against
  `ExportAllTables`).

**Stage B — phantom tracklists — shipped (2026-06-12).** Phantom albums
now carry their canonical MB tracklist as first-class `tracks` +
`track_artists` rows linked through the new **`album_tracks`** junction
(album_id, track_id, disc, position, recording_mbid; PK = album/disc/
position slot). Owned albums keep the authoritative
`album_variants → media_files` chain — a track is *owned* iff it has
`media_files`, mirroring the album discriminator, and a later rip
collapses onto the same `track_uuid` row (gains files, no migration).

- **Canonical release pick:** among `fetch_release_tracklists(rg)` —
  Official status first, then most complete tracklist, then lowest gid
  (deterministic re-pick). `mb_local.fetch_release_tracklists` now
  returns per-release `status`.
- **Idempotent persist:** `execute_values` batches; tracks ON CONFLICT
  DO NOTHING (never clobber an owned title), slot upsert on the PK.
  Re-syncs skip albums that already have a tracklist.
- **Reconcile extended:** orphan phantom tracks of the artist are
  deleted; enrichment rows cascade. "Orphan" is one rule for every
  deleter of tracks (`canon.identity.ORPHAN_TRACK_SQL`, 2026-09-24): no
  file, slot, analysis, lyrics or life data — a listened phantom loses its
  album, never its listens.
- **Reload guard:** a pg advisory lock (`MB_LOAD_LOCK_KEY`) is held by
  `stream_load` for the whole TRUNCATE+COPY loop; the sync try-locks it
  and returns `status="mb_loading"` (not stamped) — without this, a
  view mid-reload would derive an empty discography and strip the
  artist's shelf. Cross-process by design (DB-level).
- **Flood gates** (the albums-leak lesson applied to ~770k phantom
  tracks): Last.fm track-stats candidates, lyrics batch (already
  media-joined), BGE-M3 text-embedding candidates, assistant-prompt library
  count, `library_stats.total_tracks` — all gated on
  `EXISTS media_files`. P2P sync inventory is safe by construction
  (tracks rows are never synced; enrichment is requested for the
  importer's local tracks only).
- **Index drift fixed live:** `idx_track_artists_artist_id`,
  `idx_album_artists_artist_id`, `idx_similar_artists_similar` (001
  declared them; the live DB pre-dated the lines).

## Artist canonicalization (MB-anchored) — a foundation this feature needs

Surfaced while hardening discography (2026-06-01). The discography
owned-check and the planned phantom-similar work both assume a *clean
canonical artist identity*. Two things break that:

1. **Collaboration mis-normalization.** Sautium's Pass2 keeps ad-hoc
   collabs ("GMO & Dense", "DJ Snake & Lil Jon") as single `verified_band`
   rows because its Last.fm check only splits when a component clears
   `>=1000 listeners` (`normalize_artists.py:671`) — niche artists never
   do. Deezer/MB have no entity for such combos, so a blind name search
   returns the wrong artist → risk of attributing a wrong discography.
2. **Non-canonical names fragment identity.** Diacritics ("Tomas Dvorak"
   vs "Tomáš Dvořák"), abbreviations ("H. Mancini" vs "Henry Mancini"),
   transliterations ("Vladimir Vissotski" vs "Владимир Высоцкий"), and
   distributor junk (case, "(garbage)" suffixes — observed in the wild on
   Sundial Aeon releases) each yield a different `artist_uuid`.

### Decisions

- **North star = canonical UUID for sync.** Identity stays name-derived
  `uuid5(normalize(canonical_name))` — available immediately and syncable
  *without* waiting for the slow MB phase, and it still works when MB has
  no match. `uuid5(MBID)` was **rejected**: most names are already correct
  (so we'd gain nothing for the majority and lose the no-MBID case).
  Display script (Latin vs Cyrillic) doesn't matter; an alias table can
  carry variants later if anyone objects.
- **MB is the authority, used asynchronously — never blocks sync.** MB's
  signal is *structural* (entity exists or not) and threshold-free, so it
  works for niche artists where Last.fm's listener count fails. Verified
  live: "GMO & Dense"/"DJ Snake & Lil Jon" → MB empty (collab); "Simon &
  Garfunkel" → score-100 Group (real). MB **aliases** bridge the
  non-canonical names: "Tomas Dvorak"→Floex(alias Tomáš Dvořák),
  "Vladimir Vissotski"→Владимир Высоцкий, "H. Mancini"→Henry Mancini.
  So MB resolution is a **4-in-1**: collab split decision, canonicalisation,
  duplicate merge, and the MBID the discography classifier needs.
- **The owned-album overlap is the confidence anchor**, not a flat score%.
  A candidate MB artist is the right one iff its release-groups overlap the
  Sautium artist's owned albums (by `release_match_key`). This disambiguates
  same-name artists (3× "Tomáš Dvořák" → only the one whose release you own)
  and blocks false merges. Gates are **action-specific**: store-MBID /
  diacritic-rename → overlap≥1 + decent score + clear runner-up gap;
  **merge** two rows → both overlap-verified to the same MBID; **split** a
  compound → MB-empty for the whole name + every component resolves.
- **Auto-act only at high confidence; leave the rest as-is.** No review UI
  (the user may not know the right answer either). Uncertain → no-op, marked
  attempted so we don't re-query.
- **`artist_aliases` (1:1, synced) is the convergence layer.** Collab
  1:many decomposition stays in `artist_members`. Identity name-derived +
  aliases synced over P2P means nodes converge by sharing the dirty→canonical
  map; a node that ran MB reaches the same canonical UUID independently.

### Rollout

1. **Foundation — SHIPPED (2026-06-01).** `artist_aliases` table + ORM +
   `backend/artist_aliases.py` (`resolve_alias`/`record_alias`). Wired the
   resolver into `scanner.get_or_create_artist` (a rescan of an aliased
   variant now converges instead of re-fragmenting) and `record_alias` into
   the clean-pass merge+rename branches (`normalize_artists.py`) so existing
   normalization is durable. Empty table = exact prior behaviour (safe).
   `resolve_artist` still to wire into Last.fm-similar + P2P import.
2. **Read-only MB audit — NEXT.** Per-artist MB resolve + overlap, report
   `name → MB canonical, MBID, score, overlap, proposed action
   (keep/rename/merge/split/unsure)`, mutate nothing. Calibrate the gates on
   real data (how often overlap confirms, how many dups, how many MB-unknown).
3. **MB background pass.** At the calibrated confidence, write aliases +
   MBID (overlap-anchored). Pure async enhancement; 1 req/s, own UA + 503
   cooldown. Priority (4 tiers): listened → enriched → similar-of-listened
   → rest by file mtime; gate `last_mb_sync IS NULL`, artists with ≥1 owned
   album. Once an artist has its MBID, discography sync uses it for
   release-group classification (the original record_type-noise fix).
4. **Sync MBID as enriched data.** Most artists match their normalized
   name, so `artists.musicbrainz_id` (+ `artist_aliases`) should sync over
   P2P as part of enrichment — one node's MB resolution spares every other
   node the lookup, cutting MB-service load network-wide. `last_mb_sync` is
   per-node state, not synced. Touches the sync layer (`sync_queries.py`,
   `routers/sync.py`, `sync_client.py`).

## Risks

- **Volume explosion** — mitigated by D2 (artist eager, albums/tracks
  lazy, 1-hop bound). Never recurse the similarity graph.
- **External rate limits** — Deezer ~50 req/5s; route all phantom
  enrichment through the existing global throttle/cooldown. **Log
  what was skipped** — silent truncation reads as "fully enriched"
  when it isn't.
- **P2P data poisoning** — `source='p2p:<nodeid>'`, trust-but-verify;
  never merge peer enrichment into first-party `source` rows.
- **Phantom leakage into local-only views** — audit any query that
  does *not* JOIN `track_artists`/`media_files`; those are the only
  places phantoms can wrongly appear (playback, stats, "your
  library" counts). Most existing queries are naturally safe.
- **D4 quality/ToS** — lossy preview + reverse-engineered client;
  scoped to a single-user appliance, flagged in UI, sequenced last.

## Out of scope

- **P2P transfer of audio.** Not a capability of this project and
  not a planned one. Phantom discovery stands on its own — enrichment,
  preview and buy.
- Recursive similarity graph (similar-of-similar) — see D2.
- A streaming service as a *metadata storage* source — D3.

## References

- Schema: `desktop/migrations/001_initial.sql` (artists `:43`, albums
  `:59`, tracks `:73`, media_files `:191`, enrichment `:281-395`,
  similar_artists `:307`).
- Locality filters that were relaxed in Phase 2: `backend/lastfm.py`.
- UUID formulas: `backend/uuid_utils.py:27-54`.
- Sync inventory joins (structure stays out by design, D6):
  `desktop/p2p/sync_queries.py`, `backend/routers/sync.py`.
- Slice replication, the mechanism that replaced Phase 4:
  `desktop/p2p/mb_slice_queries.py`, `desktop/mb_slice_client.py`.
- Playback path (local-file only): `mcp/assistant_server.py:751-1005`
  (`file_path_to_uri` → `playlist_add`).
- Deezer integration + throttle: `backend/routers/covers.py`,
  `reference_artist_photos.md`.
- Discovery engine that surfaces phantoms: `DISCOVERY-SEARCH-ENGINE.md`.
