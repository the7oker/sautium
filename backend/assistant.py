"""
AI Assistant for Music AI DJ.
Enhanced RAG pipeline: multi-source retrieval, enriched context, Claude recommendations.
"""

import logging
import math
import re
from typing import Any, Dict, List, Optional

import anthropic
from sqlalchemy import text
from sqlalchemy.orm import Session

from config import settings
from search import (
    search_by_text,
    search_by_text_semantic,
    search_by_metadata,
    search_hybrid,
    search_similar_tracks,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an AI music DJ assistant for a personal FLAC music library. \
Your job is to recommend tracks from the library based on the user's request.

Rules:
- IMPORTANT: Always respond in the same language as the user's query. If they ask in Ukrainian, respond in Ukrainian. If in English, respond in English.
- Only recommend tracks that appear in the provided library context below. Never invent tracks.
- Include track titles and artists in your recommendations.
- Explain why each recommendation matches the request, referencing genre, mood, style, tags, or audio characteristics.
- Use audio features when available: BPM, key, instruments, mood, danceability, vocal/instrumental status.
- If popularity data is available (listeners/plays), you may mention it to highlight popular picks or hidden gems.
- Use artist bio info and tags when they help explain why a track fits.
- If the user asks about a specific artist, use the artist context provided.
- If no tracks match well, say so honestly and suggest what the library does have.
- Be concise but insightful. Show your music knowledge.
- You can comment on audio quality (CD, Vinyl, Hi-Res) when relevant.
- Format track references as: "Title" by Artist (Album).
- When listing multiple recommendations, briefly explain each choice.
- If the user references a previous recommendation or conversation, use the conversation history for context."""

CLAUDE_MODEL = "claude-sonnet-4-20250514"


def _extract_filters(db: Session, query: str) -> Dict[str, Any]:
    """
    Extract metadata filters from a natural language query.
    Simple keyword matching — Claude does the heavy reasoning.
    """
    filters = {}
    query_lower = query.lower()

    # Quality source keywords
    quality_map = {
        "vinyl": "Vinyl",
        "hi-res": "Hi-Res",
        "hi res": "Hi-Res",
        "hires": "Hi-Res",
        "mp3": "MP3",
    }
    for keyword, quality in quality_map.items():
        if keyword in query_lower:
            filters["quality_source"] = quality
            break

    # Genre keywords
    genre_keywords = [
        "blues", "jazz", "electronic", "ambient", "rock", "classical",
        "metal", "folk", "soul", "funk", "reggae", "hip hop", "hip-hop",
        "pop", "country", "r&b", "punk", "disco", "house", "techno",
        "nu jazz", "nu-jazz", "idm", "krautrock", "berlin school",
        "trip-hop", "trip hop", "downtempo",
    ]
    for genre in genre_keywords:
        if genre in query_lower:
            filters["genre"] = genre
            break

    # Artist names — check against DB for known artists mentioned in query
    # Support both exact match and transliteration (e.g., "клаус шульц" → "klaus schulze")
    try:
        rows = db.execute(text("SELECT DISTINCT name FROM artists")).fetchall()
        for row in rows:
            artist_name = row[0]
            if artist_name and artist_name.lower() in query_lower:
                filters["artist"] = artist_name
                break

        # Fuzzy match if no exact match — use trigram similarity
        if "artist" not in filters:
            sql = text("""
                SELECT name FROM artists
                WHERE similarity(name, :query) > 0.3
                ORDER BY similarity(name, :query) DESC
                LIMIT 1
            """)
            fuzzy = db.execute(sql, {"query": query}).fetchone()
            if fuzzy:
                filters["artist"] = fuzzy[0]
    except Exception as e:
        logger.debug(f"Artist extraction failed: {e}")

    # Year patterns
    # "from the 1970s" / "70s" / "80s"
    decade_match = re.search(r'\b(19)?(\d0)s\b', query_lower)
    if decade_match:
        decade_prefix = decade_match.group(1) or "19"
        decade = int(decade_prefix + decade_match.group(2))
        filters["year_from"] = decade
        filters["year_to"] = decade + 9

    # Explicit years: "from 1985" / "before 2000"
    year_match = re.search(r'\b(19|20)\d{2}\b', query)
    if year_match and "year_from" not in filters:
        year = int(year_match.group())
        if "before" in query_lower or "until" in query_lower:
            filters["year_to"] = year
        elif "after" in query_lower or "since" in query_lower:
            filters["year_from"] = year

    # Audio feature keywords
    # Vocal/instrumental
    if any(w in query_lower for w in ["instrumental", "no vocals", "without vocals"]):
        filters["vocal"] = "instrumental"
    elif any(w in query_lower for w in ["with vocals", "vocal", "singing"]):
        filters["vocal"] = "vocal"

    # Danceability
    if any(w in query_lower for w in ["danceable", "dance music", "dance track"]):
        filters["danceable"] = True

    # Key detection: "in D minor", "in the key of A", "key of F#"
    key_match = re.search(r'(?:in|key of)\s+([A-G]#?)\s*(major|minor|m)?', query, re.IGNORECASE)
    if key_match:
        filters["key"] = key_match.group(1)
        mode_str = key_match.group(2)
        if mode_str:
            filters["mode"] = "minor" if mode_str.lower() in ("minor", "m") else "major"

    # Instrument detection
    instrument_keywords = [
        "piano", "guitar", "drums", "bass", "saxophone", "violin",
        "trumpet", "flute", "organ", "cello", "harmonica", "harp",
        "clarinet", "trombone", "accordion", "synthesizer",
    ]
    for inst in instrument_keywords:
        if inst in query_lower:
            # Map to full label names
            inst_map = {
                "guitar": "acoustic guitar",  # could be either, default to acoustic
                "bass": "bass guitar",
                "drums": "drums and percussion",
                "violin": "violin and strings",
                "synthesizer": "keyboards and synthesizer",
            }
            filters["instrument"] = inst_map.get(inst, inst)
            break

    # BPM hints
    if any(w in query_lower for w in ["fast", "upbeat", "uptempo", "high energy"]):
        filters["bpm_min"] = 120
    elif any(w in query_lower for w in ["slow", "downtempo", "laid back", "chill"]):
        filters["bpm_max"] = 100

    return filters


def _detect_track_reference(db: Session, query: str) -> Optional[int]:
    """
    Detect if the user is referencing a specific track for similarity search.
    Patterns: "similar to X", "like X", "something like X by Y"

    Returns track_id if found, None otherwise.
    """
    query_lower = query.lower()

    # Patterns that suggest "find similar to this track"
    patterns = [
        r'similar to ["\']?(.+?)["\']?\s+by\s+(.+?)(?:\s*$|\s*[,.])',
        r'like ["\']?(.+?)["\']?\s+by\s+(.+?)(?:\s*$|\s*[,.])',
        r'similar to ["\'](.+?)["\']',
        r'like ["\'](.+?)["\']',
        r'something (?:similar to|like) ["\']?(.+?)["\']?\s+by\s+(.+?)(?:\s*$|\s*[,.])',
    ]

    for pattern in patterns:
        match = re.search(pattern, query_lower)
        if match:
            groups = match.groups()
            if len(groups) == 2:
                track_title, artist_name = groups
            else:
                track_title = groups[0]
                artist_name = None

            # Search database for matching track
            if artist_name:
                sql = text("""
                    SELECT t.id FROM tracks t
                    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
                    JOIN artists a ON ta.artist_id = a.id
                    WHERE LOWER(t.title) LIKE :title AND LOWER(a.name) LIKE :artist
                    LIMIT 1
                """)
                row = db.execute(sql, {
                    "title": f"%{track_title.strip()}%",
                    "artist": f"%{artist_name.strip()}%",
                }).fetchone()
            else:
                sql = text("""
                    SELECT t.id FROM tracks t
                    WHERE LOWER(t.title) LIKE :title
                    LIMIT 1
                """)
                row = db.execute(sql, {"title": f"%{track_title.strip()}%"}).fetchone()

            if row:
                logger.info(f"Detected track reference: track_id={row[0]} from query")
                return row[0]

    return None


def _get_track_enrichment(db: Session, track_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """
    Fetch enrichment data for a set of tracks in a single efficient query.
    Returns dict of track_id -> enrichment data (tags, popularity, album year, etc.)
    """
    if not track_ids:
        return {}

    enrichment = {tid: {} for tid in track_ids}

    # 1. Track stats (popularity)
    try:
        stats_sql = text("""
            SELECT track_id, listeners, playcount
            FROM track_stats
            WHERE track_id = ANY(:ids) AND source = 'lastfm'
        """)
        rows = db.execute(stats_sql, {"ids": track_ids}).fetchall()
        for row in rows:
            enrichment[row[0]]["listeners"] = row[1]
            enrichment[row[0]]["playcount"] = row[2]
    except Exception as e:
        logger.debug(f"Track stats query failed: {e}")

    # 2. Album info (year, tags)
    try:
        album_sql = text("""
            SELECT t.id, al.release_year,
                   string_agg(DISTINCT tg.name, ', ' ORDER BY tg.name) as album_tags
            FROM tracks t
            JOIN albums al ON t.album_id = al.id
            LEFT JOIN album_tags atg ON al.id = atg.album_id
            LEFT JOIN tags tg ON atg.tag_id = tg.id
            WHERE t.id = ANY(:ids)
            GROUP BY t.id, al.release_year
        """)
        rows = db.execute(album_sql, {"ids": track_ids}).fetchall()
        for row in rows:
            if row[1]:
                enrichment[row[0]]["year"] = row[1]
            if row[2]:
                enrichment[row[0]]["album_tags"] = row[2]
    except Exception as e:
        logger.debug(f"Album info query failed: {e}")

    # 3. Artist tags
    try:
        artist_tags_sql = text("""
            SELECT t.id,
                   string_agg(DISTINCT tg.name, ', ' ORDER BY tg.name) as artist_tags
            FROM tracks t
            JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
            JOIN artist_tags atg ON ta.artist_id = atg.artist_id
            JOIN tags tg ON atg.tag_id = tg.id
            WHERE t.id = ANY(:ids)
            GROUP BY t.id
        """)
        rows = db.execute(artist_tags_sql, {"ids": track_ids}).fetchall()
        for row in rows:
            if row[1]:
                enrichment[row[0]]["artist_tags"] = row[1]
    except Exception as e:
        logger.debug(f"Artist tags query failed: {e}")

    # 4. Audio features
    try:
        af_sql = text("""
            SELECT track_id, bpm, key, mode, vocal_instrumental, danceability,
                   instruments, moods
            FROM audio_features WHERE track_id = ANY(:ids)
        """)
        rows = db.execute(af_sql, {"ids": track_ids}).fetchall()
        for row in rows:
            enrichment[row.track_id]["bpm"] = row.bpm
            enrichment[row.track_id]["key_mode"] = f"{row.key} {row.mode}" if row.key else None
            enrichment[row.track_id]["vocal"] = row.vocal_instrumental
            enrichment[row.track_id]["danceability"] = row.danceability
            if row.instruments:
                top3 = sorted(row.instruments.items(), key=lambda x: -x[1])[:3]
                enrichment[row.track_id]["instruments"] = ", ".join(k for k, v in top3)
            if row.moods:
                top_mood = max(row.moods.items(), key=lambda x: x[1])
                enrichment[row.track_id]["mood"] = top_mood[0]
    except Exception as e:
        logger.debug(f"Audio features query failed: {e}")

    return enrichment


def _get_artist_context(db: Session, artist_name: str) -> Optional[str]:
    """Get enriched context for a specific artist (bio, tags, similar)."""
    try:
        sql = text("""
            SELECT a.name, ab.summary,
                   string_agg(DISTINCT tg.name, ', ') as tags
            FROM artists a
            LEFT JOIN artist_bios ab ON a.id = ab.artist_id
            LEFT JOIN artist_tags atg ON a.id = atg.artist_id
            LEFT JOIN tags tg ON atg.tag_id = tg.id
            WHERE LOWER(a.name) LIKE :name
            GROUP BY a.id, a.name, ab.summary
            LIMIT 1
        """)
        row = db.execute(sql, {"name": f"%{artist_name.lower()}%"}).fetchone()
        if not row:
            return None

        parts = [f"Artist: {row[0]}"]
        if row[1]:
            # Strip HTML from Last.fm bio
            bio = re.sub(r'<[^>]+>', '', row[1]).strip()
            parts.append(f"Bio: {bio[:400]}")
        if row[2]:
            parts.append(f"Tags: {row[2]}")

        # Similar artists
        sim_sql = text("""
            SELECT a2.name, sa.match_score
            FROM similar_artists sa
            JOIN artists a ON sa.artist_id = a.id
            JOIN artists a2 ON sa.similar_artist_id = a2.id
            WHERE LOWER(a.name) LIKE :name
            ORDER BY sa.match_score DESC
            LIMIT 5
        """)
        sim_rows = db.execute(sim_sql, {"name": f"%{artist_name.lower()}%"}).fetchall()
        if sim_rows:
            similar = [f"{r[0]} ({r[1]:.0%})" for r in sim_rows]
            parts.append(f"Similar artists: {', '.join(similar)}")

        return "\n".join(parts)

    except Exception as e:
        logger.debug(f"Artist context failed: {e}")
        return None


def _popularity_score(listeners: Optional[int], playcount: Optional[int]) -> float:
    """
    Calculate a normalized popularity score (0-1) from Last.fm stats.
    Uses log scale since popularity follows power law distribution.
    """
    if not listeners and not playcount:
        return 0.0

    l = listeners or 0
    p = playcount or 0

    # Log-scale normalization (most tracks have 10-100k listeners, max ~300k)
    listener_score = min(math.log10(max(l, 1)) / 6.0, 1.0)  # log10(1M) = 6
    play_score = min(math.log10(max(p, 1)) / 7.0, 1.0)  # log10(10M) = 7

    return 0.6 * listener_score + 0.4 * play_score


def _format_track_context(
    tracks: List[Dict[str, Any]],
    enrichment: Dict[int, Dict[str, Any]],
) -> str:
    """Format retrieved tracks into enriched text block for Claude's context."""
    if not tracks:
        return "No tracks were found matching the search criteria."

    lines = []
    for i, t in enumerate(tracks, 1):
        tid = t.get("id")
        enrich = enrichment.get(tid, {})

        # Basic info
        duration = ""
        if t.get("duration_seconds"):
            mins = int(t["duration_seconds"] // 60)
            secs = int(t["duration_seconds"] % 60)
            duration = f" | {mins}:{secs:02d}"

        year = ""
        if enrich.get("year"):
            year = f" ({enrich['year']})"

        sim = ""
        if t.get("similarity") is not None:
            sim = f" | Relevance: {t['similarity']:.2f}"

        line = (
            f'{i}. "{t.get("title", "?")}" by {t.get("artist", "Unknown")}'
            f" | Album: {t.get('album', '?')}{year}"
            f" | {t.get('quality_source', '?')}{duration}{sim}"
        )

        # Enrichment details
        details = []

        # Audio features line
        af_parts = []
        if enrich.get("bpm"):
            af_parts.append(f"BPM: {enrich['bpm']:.0f}")
        if enrich.get("key_mode"):
            af_parts.append(f"Key: {enrich['key_mode']}")
        if enrich.get("vocal"):
            af_parts.append(enrich["vocal"].capitalize())
        if enrich.get("danceability") is not None:
            af_parts.append(f"Danceability: {enrich['danceability']:.2f}")
        if af_parts:
            details.append(" | ".join(af_parts))

        if enrich.get("instruments"):
            details.append(f"Instruments: {enrich['instruments']}")

        if enrich.get("mood"):
            details.append(f"Mood: {enrich['mood']}")

        # Tags (combine artist + album, deduplicate)
        all_tags = set()
        for tag_str in [enrich.get("artist_tags", ""), enrich.get("album_tags", "")]:
            for tag in tag_str.split(", "):
                if tag.strip():
                    all_tags.add(tag.strip())
        if all_tags:
            details.append(f"Tags: {', '.join(sorted(all_tags))}")

        # Genre from track (if not already in tags)
        genre = t.get("genre")
        if genre and genre.lower() not in {tag.lower() for tag in all_tags}:
            details.append(f"Genre: {genre}")
        elif genre and not all_tags:
            details.append(f"Genre: {genre}")

        # Popularity
        listeners = enrich.get("listeners")
        playcount = enrich.get("playcount")
        if listeners or playcount:
            pop_parts = []
            if listeners:
                pop_parts.append(f"{listeners:,} listeners")
            if playcount:
                pop_parts.append(f"{playcount:,} plays")
            details.append(f"Popularity: {', '.join(pop_parts)}")

        if details:
            line += "\n   " + " | ".join(details)

        lines.append(line)

    return "\n".join(lines)


def _boost_by_popularity(
    tracks: List[Dict[str, Any]],
    enrichment: Dict[int, Dict[str, Any]],
    popularity_weight: float = 0.15,
) -> List[Dict[str, Any]]:
    """
    Re-rank tracks by combining similarity score with popularity.
    Popularity boost is subtle (default 15%) to avoid always recommending popular tracks.
    """
    if not tracks:
        return tracks

    boosted = []
    for t in tracks:
        t = dict(t)  # copy
        tid = t.get("id")
        enrich = enrichment.get(tid, {})

        original_sim = t.get("similarity") or 0.0
        pop = _popularity_score(enrich.get("listeners"), enrich.get("playcount"))

        # Combined score: (1 - w) * similarity + w * popularity
        t["similarity"] = round(
            (1 - popularity_weight) * original_sim + popularity_weight * pop,
            4,
        )
        boosted.append(t)

    boosted.sort(key=lambda x: x.get("similarity", 0), reverse=True)
    return boosted


def ask_assistant(
    db: Session,
    query: str,
    limit: int = 20,
    history: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """
    Enhanced RAG pipeline: multi-source retrieval, enriched context, Claude.

    Args:
        db: Database session.
        query: Natural language question about the music library.
        limit: Max tracks to retrieve for context.
        history: Optional conversation history (list of {role, content} dicts).

    Returns:
        Dict with answer, tracks, query, model, tracks_retrieved, and metadata.
    """
    if not settings.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY is not configured")

    # 1. Multi-source retrieval
    all_tracks: Dict[int, Dict[str, Any]] = {}

    # 1a. Check if user references a specific track for similarity search
    ref_track_id = _detect_track_reference(db, query)
    if ref_track_id:
        try:
            sim_results = search_similar_tracks(db, ref_track_id, limit=limit)
            for t in sim_results.get("results", []):
                all_tracks[t["id"]] = t
            logger.info(
                f"Track similarity search (ref={ref_track_id}) returned "
                f"{sim_results.get('count', 0)} tracks"
            )
        except Exception as e:
            logger.warning(f"Track similarity search failed: {e}")

    # 1b. PRIMARY: Hybrid search (text semantic + CLAP audio)
    try:
        hybrid_results = search_hybrid(db, query, limit=limit, min_similarity=0.2)
        for t in hybrid_results.get("results", []):
            if t["id"] not in all_tracks:
                all_tracks[t["id"]] = t
        logger.info(f"Hybrid search returned {hybrid_results.get('count', 0)} tracks")
    except Exception as e:
        logger.warning(f"Hybrid search failed, falling back: {e}")
        # Fallback to text semantic only
        try:
            text_results = search_by_text_semantic(
                db, query, limit=limit, min_similarity=0.2
            )
            for t in text_results.get("results", []):
                if t["id"] not in all_tracks:
                    all_tracks[t["id"]] = t
            logger.info(f"Text semantic search returned {text_results.get('count', 0)} tracks")
        except Exception as e2:
            logger.warning(f"Text semantic search also failed: {e2}")

    # 1c. Metadata search if we can extract filters
    filters = _extract_filters(db, query)
    if filters:
        try:
            meta_results = search_by_metadata(db, filters=filters, limit=limit)
            for t in meta_results.get("results", []):
                if t["id"] not in all_tracks:
                    all_tracks[t["id"]] = t
            logger.info(
                f"Metadata search (filters={filters}) returned "
                f"{meta_results.get('count', 0)} tracks"
            )

            # If artist was detected via fuzzy match, add top tracks from that artist directly
            if "artist" in filters and len(all_tracks) < 10:
                try:
                    artist_tracks = db.execute(text("""
                        SELECT t.id, t.title, t.track_number, t.duration_seconds,
                               a2.name as artist, al.title as album, g.name as genre,
                               al.quality_source, 0.9 as similarity
                        FROM tracks t
                        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
                        JOIN artists a2 ON ta.artist_id = a2.id
                        JOIN albums al ON t.album_id = al.id
                        LEFT JOIN track_genres tg ON t.id = tg.track_id
                        LEFT JOIN genres g ON tg.genre_id = g.id
                        WHERE a2.name = :artist
                        ORDER BY al.title, t.track_number
                        LIMIT :limit
                    """), {"artist": filters["artist"], "limit": 30}).fetchall()

                    for row in artist_tracks:
                        track = {k: v for k, v in dict(row._mapping).items()}
                        if track["id"] not in all_tracks:
                            all_tracks[track["id"]] = track
                    logger.info(f"Added {len(artist_tracks)} tracks directly from artist: {filters['artist']}")
                except Exception as e2:
                    logger.warning(f"Direct artist track query failed: {e2}")

        except Exception as e:
            logger.warning(f"Metadata search failed: {e}")

    # 2. Enrich tracks with Last.fm data
    track_ids = list(all_tracks.keys())
    enrichment = _get_track_enrichment(db, track_ids)

    # 3. Sort by similarity, then boost by popularity
    tracks = sorted(
        all_tracks.values(),
        key=lambda t: t.get("similarity") or 0,
        reverse=True,
    )[:40]  # wider net for re-ranking

    tracks = _boost_by_popularity(tracks, enrichment)
    tracks = tracks[:30]  # final cap

    # 4. Build enriched context
    track_context = _format_track_context(tracks, enrichment)

    # Artist-specific context if query mentions a known artist
    artist_context = ""
    if filters.get("artist"):
        ctx = _get_artist_context(db, filters["artist"])
        if ctx:
            artist_context = f"\n\nArtist Information:\n{ctx}"

    # Library summary for context
    lib_summary = ""
    try:
        row = db.execute(text("SELECT * FROM library_stats")).fetchone()
        if row:
            hours = int((row[4] or 0) / 3600)
            lib_summary = (
                f"\n\nLibrary overview: {row[2]:,} tracks, {row[0]:,} artists, "
                f"{row[1]:,} albums, ~{hours} hours of music."
            )
    except Exception:
        pass

    user_message = f"""User query: {query}

Here are the tracks from the library that may be relevant:

{track_context}{artist_context}{lib_summary}

Based on these tracks and their metadata, please answer the user's query with specific recommendations."""

    # 5. Build message list (with optional conversation history)
    messages = []
    if history:
        for msg in history:
            messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": user_message})

    # 6. Call Claude
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    logger.info(f"Calling Claude ({CLAUDE_MODEL}) with {len(tracks)} tracks context")

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=messages,
    )

    answer = response.content[0].text

    # 7. Return structured response
    return {
        "answer": answer,
        "tracks": tracks,
        "query": query,
        "model": CLAUDE_MODEL,
        "tracks_retrieved": len(tracks),
        "filters_detected": filters,
        "track_reference": ref_track_id,
    }
