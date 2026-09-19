# Database Architecture - Sautium

## Overview

Sautium uses a **hybrid approach** for metadata storage:
- **Normalized tables** for the metadata itself
- **A fetch ledger** (`external_metadata`) recording which external source was
  asked for what, and what it answered — including "nothing"

---

## Normalized Tables

All five tables below are **node-local**: no seal columns, never on the P2P
wire, in a share file or in the seed bundle (since 2026-09-19 — Last.fm's
API terms do not allow redistributing its answers). Every node fetches its
own by name; only `artist_bios.listeners` is read as a local rarity proxy.

### 1. `similar_artists` - Artist Similarity Relationships

```sql
similar_artists:
  artist_id → artists (who)
  similar_artist_id → artists (similar to whom)
  match_score (0.0-1.0)
  source ('lastfm', 'musicbrainz')
```

**Purpose**: Many-to-many artist relationships with similarity scores
**Sources**: Last.fm (73,171 rows). Minted neighbours become phantom artists —
see `docs/design/PHANTOM-DISCOVERY.md`
**Use cases**:
- "Find artists similar to X"
- "Show me music like Y"
- Recommendation engine

---

### 2. `tags` + `artist_tags` - Universal Tag System

```sql
tags:
  id, name (UNIQUE)

artist_tags:
  artist_id → artists
  tag_id → tags
  weight (0-100)  -- relevance score
  source ('lastfm', 'user')
```

**Purpose**: Flexible tagging system for artists (can be extended to albums, tracks)
**Tag types**: genres, moods, eras, styles, demographics
**Sources**: Last.fm (285,188 rows over 23,548 tags); user tags (future)
**Use cases**:
- "Find all artists tagged as 'psychedelic'"
- "Show me 70s krautrock artists"
- Genre/mood-based recommendations

---

### 3. `artist_bios` - Artist Biographies

```sql
artist_bios:
  artist_id → artists
  source ('lastfm', 'musicbrainz', 'wikipedia')
  summary (short, 1-2 paragraphs)
  content (full biography)
  url (source link)
  listeners, playcount (Last.fm specific)
```

**Purpose**: Artist biographical information with popularity stats
**Sources**: Last.fm (36,843 rows); MusicBrainz, Wikipedia (future)
**Use cases**:
- Display artist info in UI
- Rank by popularity (listeners/playcount)
- Text embeddings for semantic search (Phase 2)

---

### 4. `genre_descriptions` - Genre Information

```sql
genre_descriptions:
  genre_id → genres
  source ('lastfm', 'wikipedia')
  summary (short description)
  content (full description with history)
  url (source link)
  reach (Last.fm popularity metric)
```

**Purpose**: Detailed genre/style descriptions
**Sources**: Last.fm (2,308 rows); Wikipedia (future)
**Use cases**:
- Display genre info to users
- Text embeddings for genre-based search
- Understanding genre relationships

---

## Fetch Ledger

### `external_metadata` - what was asked, and what came back

```sql
external_metadata:
  entity_type metadata_entity_type  -- artist | album | track | genre
  entity_id   TEXT                  -- polymorphic, NOT an FK: uuid for
                                    -- artist/album/track, int for genre
  source      VARCHAR(50)           -- 'lastfm' | 'genius' | 'lrclib' | ...
  metadata_type metadata_kind       -- bio | info | stats | lyrics | description
  data        JSONB                 -- the response, or what identifies it
  fetch_status fetch_status         -- success | not_found | error
  UNIQUE (entity_type, entity_id, source, metadata_type)
```

**Purpose**: the memory that keeps enrichment idempotent. The metadata itself
lives in the normalized tables above; this table records the *call* — which
source was asked about which entity, and whether it had anything. The value is
mostly in the negatives: an artist with no Last.fm bio, a track LRCLIB has
never heard of. Without the row, every pass of the background loop would ask
again, and the rate limiter would spend its budget re-learning the same
absence (CLAUDE.md § "Each step checks its own precondition").

Written by `backend/lastfm.py`, `backend/lrclib.py`, `backend/genius.py`.

**Measured 2026-09-18** — 79,118 rows, of which 42,984 are `not_found`:

| source | metadata_type | success | not_found | error |
|---|---|---|---|---|
| lrclib | lyrics | 32,772 | 21,932 | 138 |
| genius | lyrics | 2,993 | 17,881 | — |
| lastfm | stats | — | 3,060 | — |
| lastfm | bio | 83 | 111 | 40 |
| lastfm | info | 92 | — | — |
| lastfm | description | — | 16 | — |

The ledger is node-local: it describes this node's own calls, so it rides in a
node backup but in no share file and in no sync category
(`docs/design/BACKUP.md` § Data classes).

---

## Design Philosophy

### When to use normalized tables:
✅ Data structure is well-understood
✅ Frequently queried
✅ Relationships with other entities
✅ Need efficient indexes
✅ Need data integrity (FK constraints)

**Examples**: artist_bios, tags, similar_artists

### When to write a ledger row:
✅ An external source was asked about an entity
✅ ...especially when it answered "nothing" — that is the row that saves the
   next pass a call
✅ The step that asked can read it back as its own precondition

**Examples**: `lastfm.enrich_bios` marking an artist Last.fm has no bio for;
`lrclib` / `genius` marking a track neither service carries.

---

## Migration Pattern

All existing metadata has been normalized:

| Metadata Type | From | To |
|---------------|------|-----|
| Similar artists | `external_metadata` JSONB | `similar_artists` table |
| Artist tags | `external_metadata` JSONB | `tags` + `artist_tags` |
| Artist bios | `external_metadata` JSONB | `artist_bios` |
| Genre descriptions | `external_metadata` JSONB | `genre_descriptions` |

**Result** (2026-09-18): the normalized tables hold the data — 285,188
artist_tags, 73,171 similar_artists, 36,843 artist_bios, 36,358 track_stats,
2,308 genre_descriptions — and `external_metadata` holds the 79,118 fetch
records behind them.

---

## Benefits of Current Architecture

1. **Performance**: Efficient queries with proper indexes
2. **Data Integrity**: Foreign key constraints, CASCADE DELETE
3. **No Duplication**: Each tag/genre stored once
4. **Type Safety**: Proper column types (INTEGER, DECIMAL, TEXT)
5. **Multi-Source**: Can aggregate data from multiple sources
6. **Idempotence**: the `external_metadata` ledger, so a re-run costs no calls
7. **Extensibility**: Easy to add new sources to existing tables
8. **Clear Schema**: Self-documenting structure

---

## Query Examples

### Find artists similar to Klaus Schulze
```sql
SELECT a2.name, sa.match_score
FROM artists a1
JOIN similar_artists sa ON a1.id = sa.artist_id
JOIN artists a2 ON sa.similar_artist_id = a2.id
WHERE a1.name = 'Klaus Schulze'
ORDER BY sa.match_score DESC;
```

### Find all artists tagged "psychedelic"
```sql
SELECT a.name, at.weight
FROM artists a
JOIN artist_tags at ON a.id = at.artist_id
JOIN tags t ON at.tag_id = t.id
WHERE t.name = 'psychedelic'
ORDER BY at.weight DESC;
```

### Top artists by popularity
```sql
SELECT a.name, ab.listeners, ab.playcount
FROM artists a
JOIN artist_bios ab ON a.id = ab.artist_id
WHERE ab.source = 'lastfm'
ORDER BY ab.listeners DESC
LIMIT 10;
```

### Artists with both tags: "electronic" AND "ambient"
```sql
SELECT a.name
FROM artists a
WHERE EXISTS (
    SELECT 1 FROM artist_tags at
    JOIN tags t ON at.tag_id = t.id
    WHERE at.artist_id = a.id AND t.name = 'electronic'
)
AND EXISTS (
    SELECT 1 FROM artist_tags at
    JOIN tags t ON at.tag_id = t.id
    WHERE at.artist_id = a.id AND t.name = 'ambient'
);
```

---

## Future Considerations

Audio features are derived here, not bought: `audio_features` comes from
librosa + CLAP over the file itself (`backend/audio_analysis.py`), and
instruments from the AST + PaSST ensemble — no catalog API is in that path,
and none is planned.

Sources still open:
- MusicBrainz: detailed credits, recording info (the `mb_*` dump layer)
- Wikipedia: structured data, infoboxes
- User-generated: custom tags, ratings, notes

---

## Summary

**Current State**: normalized metadata tables plus the `external_metadata`
fetch ledger that keeps their enrichment idempotent.

This file covers the Last.fm enrichment layer only. The whole schema — every
table, index and enum — is `desktop/migrations/001_initial.sql`, which is the
single readable source of truth (CLAUDE.md § Migration & DB Workflow).
