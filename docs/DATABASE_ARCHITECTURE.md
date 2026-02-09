# Database Architecture - Music AI DJ

## Overview

Music AI DJ uses a **hybrid approach** for metadata storage:
- **Normalized tables** for well-understood, frequently-queried data
- **Staging table** (`external_metadata`) for new/experimental metadata

---

## Normalized Tables

### 1. `similar_artists` - Artist Similarity Relationships

```sql
similar_artists:
  artist_id → artists (who)
  similar_artist_id → artists (similar to whom)
  match_score (0.0-1.0)
  source ('lastfm', 'spotify', 'musicbrainz')
```

**Purpose**: Many-to-many artist relationships with similarity scores
**Sources**: Last.fm (current), Spotify (future)
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
  source ('lastfm', 'spotify', 'user')
```

**Purpose**: Flexible tagging system for artists (can be extended to albums, tracks)
**Tag types**: genres, moods, eras, styles, demographics
**Sources**: Last.fm (current), Spotify genres (future), user tags (future)
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
**Sources**: Last.fm (current), MusicBrainz (future), Wikipedia (future)
**Use cases**:
- Display artist info in UI
- Rank by popularity (listeners/playcount)
- Text embeddings for semantic search (Phase 2)

---

### 4. `genre_descriptions` - Genre Information

```sql
genre_descriptions:
  genre_id → genres
  source ('lastfm', 'wikipedia', 'spotify')
  summary (short description)
  content (full description with history)
  url (source link)
  reach (Last.fm popularity metric)
```

**Purpose**: Detailed genre/style descriptions
**Sources**: Last.fm (current), Wikipedia (future)
**Use cases**:
- Display genre info to users
- Text embeddings for genre-based search
- Understanding genre relationships

---

## Staging Table

### `external_metadata` - Experimental/New Metadata

```sql
external_metadata:
  entity_type ('artist', 'album', 'track', 'genre')
  entity_id (FK to respective table)
  source ('lastfm', 'spotify', 'musicbrainz', 'wikipedia')
  metadata_type (e.g., 'audio_features', 'lyrics', 'credits')
  data (JSONB - flexible structure)
  fetch_status ('success', 'not_found', 'error')
```

**Purpose**:
- Temporary storage for new metadata types
- Quick integration of new API sources
- Experiment with data structure before normalization
- Iterate on schema design

**Workflow**:
```
New API → external_metadata (JSONB) → analyze structure → design schema → migrate to normalized table
```

**Current status**: Empty (0 records) - ready for new integrations

**Examples of future use**:
- Spotify audio features (danceability, energy, tempo, etc.)
- MusicBrainz detailed credits (producers, engineers, etc.)
- Wikipedia structured data
- Lyrics from various sources

---

## Design Philosophy

### When to use normalized tables:
✅ Data structure is well-understood
✅ Frequently queried
✅ Relationships with other entities
✅ Need efficient indexes
✅ Need data integrity (FK constraints)

**Examples**: artist_bios, tags, similar_artists

### When to use external_metadata (staging):
✅ Exploring new API source
✅ Structure not yet clear
✅ Experimental features
✅ Rapid prototyping
✅ One-time data collection

**Examples**: Initial Spotify integration, testing new API endpoints

---

## Migration Pattern

All existing metadata has been normalized:

| Metadata Type | From | To |
|---------------|------|-----|
| Similar artists | `external_metadata` JSONB | `similar_artists` table |
| Artist tags | `external_metadata` JSONB | `tags` + `artist_tags` |
| Artist bios | `external_metadata` JSONB | `artist_bios` |
| Genre descriptions | `external_metadata` JSONB | `genre_descriptions` |

**Result**:
- `external_metadata`: 0 records (clean slate)
- Normalized tables: 332 records total
- Ready for Phase 2 (Spotify, MusicBrainz integration)

---

## Benefits of Current Architecture

1. **Performance**: Efficient queries with proper indexes
2. **Data Integrity**: Foreign key constraints, CASCADE DELETE
3. **No Duplication**: Each tag/genre stored once
4. **Type Safety**: Proper column types (INTEGER, DECIMAL, TEXT)
5. **Multi-Source**: Can aggregate data from multiple sources
6. **Flexibility**: `external_metadata` for rapid experimentation
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

### Phase 2 - Spotify Integration
New metadata types to explore in `external_metadata`:
- Audio features (danceability, energy, valence, tempo, etc.)
- Spotify genres (different from Last.fm tags)
- Track popularity scores
- Album release types (album, single, compilation)

Once structure is clear → normalize into dedicated tables:
- `track_audio_features`
- `spotify_genres` (or merge with `tags`)
- `track_popularity`

### Phase 3+ - Additional Sources
- MusicBrainz: detailed credits, recording info
- Wikipedia: structured data, infoboxes
- User-generated: custom tags, ratings, notes

---

## Summary

**Current State**: Fully normalized database with staging table for future growth
**Tables**: 5 normalized + 1 staging
**Records**: 332 normalized, 0 staging
**Status**: ✅ Ready for Phase 2 integrations
