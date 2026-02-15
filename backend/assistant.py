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
- If the user references a previous recommendation or conversation, use the conversation history for context.
- If player state info is provided (currently playing track, playlist), use it to answer questions like "what's playing now", "play something similar to this", "what album is this", etc.
- When recommending "more like this" or "similar to what's playing", reference the currently playing track."""

CLAUDE_MODEL = "claude-sonnet-4-20250514"
TRANSLATE_MODEL = "claude-haiku-4-5-20251001"


def _has_cyrillic(text: str) -> bool:
    """Check if text contains Cyrillic characters."""
    return any('\u0400' <= ch <= '\u04FF' for ch in text)


def _translate_query(query: str) -> Optional[str]:
    """
    Translate a Cyrillic query to English using Claude Haiku.
    Returns English translation or None on failure.
    Handles artist names, album titles, and music terminology correctly.
    """
    if not _has_cyrillic(query):
        return None

    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        resp = client.messages.create(
            model=TRANSLATE_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": query}],
            system=(
                "Translate the user's music-related query from Ukrainian/Russian to English. "
                "Pay special attention to artist names, album titles and band names — "
                "use their correct English/original spellings (e.g. 'клаус шульце' → 'Klaus Schulze', "
                "'бітлз' → 'The Beatles', 'пінк флойд' → 'Pink Floyd'). "
                "Keep the query intent intact. Reply ONLY with the English translation, nothing else."
            ),
        )
        translated = resp.content[0].text.strip()
        logger.info(f"Query translated: '{query}' → '{translated}'")
        return translated
    except Exception as e:
        logger.warning(f"Query translation failed: {e}")
        return None


def _transliterate_ua_to_latin(text: str) -> str:
    """
    Transliterate Ukrainian Cyrillic to Latin for artist name matching.
    Simple mapping for common names like "клаус шульц" → "klaus schulz"
    """
    ua_to_lat = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'h', 'ґ': 'g', 'д': 'd', 'е': 'e',
        'є': 'ye', 'ж': 'zh', 'з': 'z', 'и': 'y', 'і': 'i', 'ї': 'yi', 'й': 'y',
        'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r',
        'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch',
        'ш': 'sh', 'щ': 'shch', 'ь': '', 'ю': 'yu', 'я': 'ya',
        'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'H', 'Ґ': 'G', 'Д': 'D', 'Е': 'E',
        'Є': 'Ye', 'Ж': 'Zh', 'З': 'Z', 'И': 'Y', 'І': 'I', 'Ї': 'Yi', 'Й': 'Y',
        'К': 'K', 'Л': 'L', 'М': 'M', 'Н': 'N', 'О': 'O', 'П': 'P', 'Р': 'R',
        'С': 'S', 'Т': 'T', 'У': 'U', 'Ф': 'F', 'Х': 'Kh', 'Ц': 'Ts', 'Ч': 'Ch',
        'Ш': 'Sh', 'Щ': 'Shch', 'Ь': '', 'Ю': 'Yu', 'Я': 'Ya',
    }

    result = []
    for char in text:
        result.append(ua_to_lat.get(char, char))
    return ''.join(result)


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
            if not artist_name:
                continue
            name_lower = artist_name.lower()
            # Short names (< 4 chars) must match as whole word to avoid false positives
            # e.g. "En" matching "recommEnd", "Air" matching "repAIR"
            if len(name_lower) < 4:
                if re.search(r'\b' + re.escape(name_lower) + r'\b', query_lower):
                    filters["artist"] = artist_name
                    break
            elif name_lower in query_lower:
                filters["artist"] = artist_name
                break

        # Fuzzy match if no exact match — use trigram similarity
        # Extract 2-3 word combinations from query and try fuzzy matching
        if "artist" not in filters:
            # Transliterate if query contains Cyrillic
            search_query = query
            if any('\u0400' <= char <= '\u04FF' for char in query):
                # Normalize Ukrainian case endings before transliteration
                # Replace genitive endings: "клауса" → "клаус", "шульца" → "шульц"
                normalized = re.sub(r'\b(\w+)а\b', r'\1', query)  # remove trailing 'а'
                search_query = _transliterate_ua_to_latin(normalized)
                logger.info(f"Query contains Cyrillic, normalizing and transliterating: '{query}' → '{normalized}' → '{search_query}'")

            # Extract all 2-3 word combinations as potential artist names
            words = search_query.lower().split()
            artist_candidates = []

            # 2-word combinations
            for i in range(len(words) - 1):
                artist_candidates.append(" ".join(words[i:i+2]))

            # 3-word combinations (for names like "Klaus Schulze's U.S.O.")
            for i in range(len(words) - 2):
                artist_candidates.append(" ".join(words[i:i+3]))

            # Try fuzzy matching for each candidate and pick best match
            best_match = None
            best_similarity = 0.4  # threshold (raised to avoid false matches like "shchos z" → "Shio-Z")

            for candidate in artist_candidates:
                sql = text("""
                    SELECT name, similarity(name, :query) as sim FROM artists
                    WHERE similarity(name, :query) > :threshold
                    ORDER BY similarity(name, :query) DESC
                    LIMIT 1
                """)
                fuzzy = db.execute(sql, {"query": candidate, "threshold": best_similarity}).fetchone()
                if fuzzy and fuzzy[1] > best_similarity:
                    best_match = (fuzzy[0], fuzzy[1], candidate)
                    best_similarity = fuzzy[1]

            if best_match:
                filters["artist"] = best_match[0]
                logger.info(f"Fuzzy match: candidate='{best_match[2]}' matched artist='{best_match[0]}' with similarity={best_match[1]:.3f}")
    except Exception as e:
        logger.debug(f"Artist extraction failed: {e}")

    # Album name — try to detect album references in query
    # Look for patterns like "альбом X", "album X", or known album titles
    # Capture until end of sentence or next clause
    album_pattern = re.search(
        r'(?:альбом[уі]?\s+|album\s+)["«]?([^"»,!?]+?)["»]?(?:\s*$|[,!?.])',
        query, re.IGNORECASE,
    )
    if album_pattern:
        album_name = album_pattern.group(1).strip()
        if album_name and len(album_name) > 2:
            filters["album_hint"] = album_name
            logger.info(f"Detected album reference: '{album_name}'")

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


def _is_catalog_query(original_query: str, translated_query: Optional[str]) -> bool:
    """
    Detect if the user is asking for a catalog listing (all albums, discography, etc.)
    rather than a recommendation.
    """
    catalog_patterns = [
        # Ukrainian
        r'всі\s+альбом',
        r'все\s+альбом',
        r'які\s+альбом',
        r'які.*в\s+мене\s+є',
        r'що\s+є\s+в\s+мене',
        r'що\s+в\s+мене\s+є',
        r'скільки\s+альбомів',
        r'покажи.*альбом',
        r'дискографі[яюї]',
        r'список\s+альбомів',
        # Russian
        r'все\s+альбом',
        r'какие\s+альбом',
        r'дискографи[яюи]',
        r'покажи.*альбом',
    ]
    translated_patterns = [
        r'all\s+albums?',
        r'every\s+album',
        r'show.*albums?',
        r'list.*albums?',
        r'what\s+albums?',
        r'how\s+many\s+albums?',
        r'discography',
        r'what\s+do\s+i\s+have',
        r'catalog',
        r'complete\s+collection',
    ]

    q_lower = original_query.lower()
    for pat in catalog_patterns:
        if re.search(pat, q_lower):
            return True

    # Check English patterns against both original and translated queries
    for q in [original_query, translated_query]:
        if q:
            t_lower = q.lower()
            for pat in translated_patterns:
                if re.search(pat, t_lower):
                    return True

    return False


def _get_artist_discography(db: Session, artist_name: str) -> List[Dict[str, Any]]:
    """
    Get full discography for an artist: all albums with track counts and durations.
    Returns album-level data, no track limit.
    """
    sql = text("""
        SELECT al.id, al.title as album_title, al.release_year, al.quality_source,
               COUNT(t.id) as track_count,
               COALESCE(SUM(t.duration_seconds), 0) as total_duration,
               al.label, al.catalog_number
        FROM albums al
        JOIN tracks t ON t.album_id = al.id
        JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        WHERE a.name = :artist
        GROUP BY al.id, al.title, al.release_year, al.quality_source, al.label, al.catalog_number
        ORDER BY al.release_year NULLS LAST, al.title
    """)
    rows = db.execute(sql, {"artist": artist_name}).fetchall()

    albums = []
    for row in rows:
        total_sec = float(row.total_duration)
        mins = int(total_sec // 60)
        albums.append({
            "album_id": row.id,
            "title": row.album_title,
            "year": row.release_year,
            "quality_source": row.quality_source,
            "track_count": int(row.track_count),
            "total_duration_min": mins,
            "label": row.label,
        })

    return albums


def _format_album_context(
    albums: List[Dict[str, Any]],
    artist_name: str,
) -> str:
    """Format album listing into text block for Claude's context."""
    if not albums:
        return f"No albums found for artist '{artist_name}' in the library."

    lines = [f"Complete discography of {artist_name} in the library ({len(albums)} albums/releases):\n"]
    for i, a in enumerate(albums, 1):
        year = f" ({a['year']})" if a.get("year") else ""
        quality = a.get("quality_source", "CD")
        duration = f"{a['total_duration_min']} min" if a.get("total_duration_min") else "?"
        label = f" [{a['label']}]" if a.get("label") else ""

        line = (
            f'{i}. "{a["title"]}"{year} | {quality} | '
            f'{a["track_count"]} tracks, {duration}{label}'
        )
        lines.append(line)

    return "\n".join(lines)


def _handle_catalog_query(
    db: Session,
    original_query: str,
    translated: Optional[str],
    filters: Dict[str, Any],
    history: Optional[List[Dict[str, str]]] = None,
    player_context: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Handle catalog-type queries (all albums, discography) with album-level retrieval.
    Bypasses the normal track-level retrieval pipeline.
    """
    artist_name = filters["artist"]
    retrieval_log = []

    if translated:
        retrieval_log.append({
            "source": "Query translation",
            "description": f"Переклад запиту: '{original_query}' → '{translated}'",
            "count": 0,
        })

    retrieval_log.append({
        "source": "Catalog query detected",
        "description": f"Запит на дискографію/каталог артиста '{artist_name}'",
        "count": 0,
    })

    # Get full discography
    albums = _get_artist_discography(db, artist_name)
    logger.info(f"Catalog query: found {len(albums)} albums for '{artist_name}'")

    retrieval_log.append({
        "source": "Artist discography (DB)",
        "description": f"Знайдено {len(albums)} альбомів/релізів '{artist_name}'",
        "count": len(albums),
    })

    # Format album context
    album_context = _format_album_context(albums, artist_name)

    # Artist bio context
    artist_context = ""
    ctx = _get_artist_context(db, artist_name)
    if ctx:
        artist_context = f"\n\nArtist Information:\n{ctx}"
        retrieval_log.append({
            "source": "Artist bio (Last.fm)",
            "description": f"Біографія та схожі артисти для '{artist_name}'",
            "count": 1,
        })

    # Library summary
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

    # Player context
    player_section = ""
    if player_context:
        player_section = f"\n\n{player_context}"

    user_message = f"""User query: {original_query}{player_section}

This is a catalog/discography query. Here is the complete album listing from the library:

{album_context}{artist_context}{lib_summary}

Please present the full discography listing to the user. Group by album type if useful (studio albums, singles/EPs, deluxe editions). Mention quality sources (CD, Vinyl, Hi-Res) and years."""

    # Build messages
    messages = []
    if history:
        for msg in history:
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})

    # Call Claude
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    logger.info(f"Catalog query: calling Claude ({CLAUDE_MODEL}) with {len(albums)} albums context")

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=messages,
    )

    answer = response.content[0].text

    # Build tracks_data from albums for UI (compact representation)
    tracks_data = [
        {
            "id": a["album_id"],
            "title": a["title"],
            "artist": artist_name,
            "album": a["title"],
            "similarity": None,
        }
        for a in albums
    ]

    return {
        "answer": answer,
        "tracks": tracks_data,
        "query": original_query,
        "model": CLAUDE_MODEL,
        "tracks_retrieved": len(albums),
        "filters_detected": filters,
        "track_reference": None,
        "retrieval_log": retrieval_log,
    }


def ask_assistant(
    db: Session,
    query: str,
    limit: int = 20,
    history: Optional[List[Dict[str, str]]] = None,
    player_context: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Enhanced RAG pipeline: multi-source retrieval, enriched context, Claude.

    Args:
        db: Database session.
        query: Natural language question about the music library.
        limit: Max tracks to retrieve for context.
        history: Optional conversation history (list of {role, content} dicts).
        player_context: Optional string with current HQPlayer state (now playing, playlist).

    Returns:
        Dict with answer, tracks, query, model, tracks_retrieved, and metadata.
    """
    if not settings.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY is not configured")

    # 0. Translate Cyrillic queries to English for better search
    original_query = query
    search_query = query
    translated = _translate_query(query)
    if translated:
        search_query = translated

    # 0b. Detect catalog queries (all albums, discography, etc.)
    # Extract filters early to check for artist
    filters = _extract_filters(db, search_query)
    if not filters.get("artist") and translated:
        orig_filters = _extract_filters(db, original_query)
        if orig_filters.get("artist"):
            filters.update(orig_filters)

    if _is_catalog_query(original_query, translated) and filters.get("artist"):
        return _handle_catalog_query(
            db, original_query, translated, filters,
            history=history, player_context=player_context,
        )

    # 1. Multi-source retrieval
    all_tracks: Dict[int, Dict[str, Any]] = {}
    retrieval_log: List[Dict[str, Any]] = []  # Track which sources contributed

    if translated:
        retrieval_log.append({
            "source": "Query translation",
            "description": f"Переклад запиту: '{original_query}' → '{translated}'",
            "count": 0,
        })

    # 1a. Check if user references a specific track for similarity search
    ref_track_id = _detect_track_reference(db, search_query)
    if ref_track_id:
        try:
            sim_results = search_similar_tracks(db, ref_track_id, limit=limit)
            count = 0
            for t in sim_results.get("results", []):
                all_tracks[t["id"]] = t
                count += 1
            retrieval_log.append({
                "source": "CLAP audio similarity",
                "description": f"Знайдено {count} треків схожих за звучанням (ref track_id={ref_track_id})",
                "count": count,
            })
            logger.info(f"Track similarity search (ref={ref_track_id}) returned {count} tracks")
        except Exception as e:
            logger.warning(f"Track similarity search failed: {e}")

    # 1b. PRIMARY: Hybrid search (text semantic + CLAP audio)
    try:
        hybrid_results = search_hybrid(db, search_query, limit=limit, min_similarity=0.2)
        count = 0
        for t in hybrid_results.get("results", []):
            if t["id"] not in all_tracks:
                all_tracks[t["id"]] = t
                count += 1
        retrieval_log.append({
            "source": "Hybrid search (CLAP audio + text embeddings)",
            "description": f"CLAP audio embeddings + text semantic search повернули {hybrid_results.get('count', 0)} треків, {count} нових додано",
            "count": count,
        })
        logger.info(f"Hybrid search returned {hybrid_results.get('count', 0)} tracks")
    except Exception as e:
        logger.warning(f"Hybrid search failed, falling back: {e}")
        # Fallback to text semantic only
        try:
            text_results = search_by_text_semantic(
                db, search_query, limit=limit, min_similarity=0.2
            )
            count = 0
            for t in text_results.get("results", []):
                if t["id"] not in all_tracks:
                    all_tracks[t["id"]] = t
                    count += 1
            retrieval_log.append({
                "source": "Text semantic search (fallback)",
                "description": f"Text embeddings (all-MiniLM-L6-v2) повернули {text_results.get('count', 0)} треків, {count} нових",
                "count": count,
            })
            logger.info(f"Text semantic search returned {text_results.get('count', 0)} tracks")
        except Exception as e2:
            logger.warning(f"Text semantic search also failed: {e2}")

    # 1c. Metadata search if we can extract filters
    # (filters already extracted above before catalog query check)
    logger.info(f"Extracted filters from query: {filters}")

    if filters:
        try:
            # Use higher limit for metadata when artist is detected to capture more albums
            meta_limit = min(limit * 2, 40) if "artist" in filters else limit
            meta_results = search_by_metadata(db, filters=filters, limit=meta_limit)
            count = 0
            for t in meta_results.get("results", []):
                if t["id"] not in all_tracks:
                    t["similarity"] = 0.85
                    all_tracks[t["id"]] = t
                    count += 1
            filter_desc = ", ".join(f"{k}={v}" for k, v in filters.items() if k != "album_hint")
            retrieval_log.append({
                "source": "Metadata DB search",
                "description": f"PostgreSQL ILIKE фільтр ({filter_desc}) повернув {meta_results.get('count', 0)} треків, {count} нових",
                "count": count,
            })
            logger.info(
                f"Metadata search (filters={filters}) returned "
                f"{meta_results.get('count', 0)} tracks"
            )

            # Album sampling: ensure all albums by the artist are represented
            # Pick one representative track per album to give Claude full discography visibility
            if "artist" in filters:
                try:
                    sample_sql = text("""
                        SELECT DISTINCT ON (al.id)
                               t.id, t.title, t.track_number, t.duration_seconds,
                               a2.name as artist, al.title as album, g.name as genre,
                               al.quality_source, al.release_year, 0.85 as similarity
                        FROM tracks t
                        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
                        JOIN artists a2 ON ta.artist_id = a2.id
                        JOIN albums al ON t.album_id = al.id
                        LEFT JOIN track_genres tg ON t.id = tg.track_id
                        LEFT JOIN genres g ON tg.genre_id = g.id
                        WHERE a2.name = :artist
                        ORDER BY al.id, t.track_number
                    """)
                    sample_rows = db.execute(sample_sql, {"artist": filters["artist"]}).fetchall()

                    sample_added = 0
                    for row in sample_rows:
                        track = {}
                        for k, v in dict(row._mapping).items():
                            if hasattr(v, 'as_integer_ratio'):
                                track[k] = float(v)
                            else:
                                track[k] = v
                        if track["id"] not in all_tracks:
                            all_tracks[track["id"]] = track
                            sample_added += 1
                    if sample_added:
                        logger.info(f"Album sampling: added {sample_added} tracks (1 per album) for '{filters['artist']}'")
                        retrieval_log.append({
                            "source": "Album sampling",
                            "description": f"По 1 треку з кожного альбому '{filters['artist']}' — {sample_added} нових додано для повноти дискографії",
                            "count": sample_added,
                        })
                except Exception as e2:
                    logger.warning(f"Album sampling query failed: {e2}")

        except Exception as e:
            logger.warning(f"Metadata search failed: {e}")

    # 1d. If a specific album is referenced, use its CLAP embedding as anchor
    # to find the most sonically similar tracks across the artist's catalog
    if filters.get("album_hint"):
        album_hint = filters["album_hint"]
        artist_filter = filters.get("artist", "")

        # 1d-i. Add the referenced album's own tracks to context
        try:
            album_sql = text("""
                SELECT t.id, t.title, t.track_number, t.duration_seconds,
                       a2.name as artist, al.title as album, g.name as genre,
                       al.quality_source, 0.95 as similarity
                FROM tracks t
                JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
                JOIN artists a2 ON ta.artist_id = a2.id
                JOIN albums al ON t.album_id = al.id
                LEFT JOIN track_genres tg ON t.id = tg.track_id
                LEFT JOIN genres g ON tg.genre_id = g.id
                WHERE al.title ILIKE :album_hint
                  AND (:artist = '' OR a2.name = :artist)
                ORDER BY t.track_number
                LIMIT 20
            """)
            album_rows = db.execute(album_sql, {
                "album_hint": f"%{album_hint}%",
                "artist": artist_filter,
            }).fetchall()

            album_track_ids = []
            added = 0
            for row in album_rows:
                track = {}
                for k, v in dict(row._mapping).items():
                    if hasattr(v, 'as_integer_ratio'):
                        track[k] = float(v)
                    else:
                        track[k] = v
                album_track_ids.append(track["id"])
                if track["id"] not in all_tracks:
                    all_tracks[track["id"]] = track
                    added += 1
            if added:
                retrieval_log.append({
                    "source": "Album reference lookup",
                    "description": f"Прямий пошук альбому '{album_hint}' додав {added} треків",
                    "count": added,
                })
                logger.info(f"Added {added} tracks from referenced album '{album_hint}'")
        except Exception as e:
            album_track_ids = []
            logger.warning(f"Album hint search failed: {e}")

        # 1d-ii. CLAP similarity: rank ALL artist tracks by audio similarity
        # to the referenced album (averaged embedding)
        if album_track_ids and artist_filter:
            try:
                placeholders = ", ".join(str(int(tid)) for tid in album_track_ids)
                sim_sql = text(f"""
                    WITH album_avg AS (
                        SELECT avg(e.vector) as avg_vec
                        FROM tracks t
                        JOIN embeddings e ON t.embedding_id = e.id
                        WHERE t.id IN ({placeholders})
                    )
                    SELECT t.id, t.title, t.track_number, t.duration_seconds,
                           a2.name as artist, al.title as album, g.name as genre,
                           al.quality_source,
                           round((1 - (e.vector <=> (SELECT avg_vec FROM album_avg)))::numeric, 4) as similarity
                    FROM tracks t
                    JOIN embeddings e ON t.embedding_id = e.id
                    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
                    JOIN artists a2 ON ta.artist_id = a2.id
                    JOIN albums al ON t.album_id = al.id
                    LEFT JOIN track_genres tg ON t.id = tg.track_id
                    LEFT JOIN genres g ON tg.genre_id = g.id
                    WHERE a2.name = :artist
                      AND t.id NOT IN ({placeholders})
                    ORDER BY e.vector <=> (SELECT avg_vec FROM album_avg)
                    LIMIT :lim
                """)
                sim_rows = db.execute(sim_sql, {
                    "artist": artist_filter,
                    "lim": limit * 3,  # get more, will be trimmed later
                }).fetchall()

                sim_added = 0
                for row in sim_rows:
                    track = {}
                    for k, v in dict(row._mapping).items():
                        if hasattr(v, 'as_integer_ratio'):
                            track[k] = float(v)
                        else:
                            track[k] = v
                    if track["id"] not in all_tracks:
                        all_tracks[track["id"]] = track
                        sim_added += 1
                    else:
                        # Update similarity if CLAP score is available and old was a placeholder
                        existing = all_tracks[track["id"]]
                        if existing.get("similarity", 0) == 0.85:
                            existing["similarity"] = float(track["similarity"])

                retrieval_log.append({
                    "source": "CLAP album similarity",
                    "description": (
                        f"Cosine similarity до '{album_hint}' ранжувала "
                        f"{len(sim_rows)} треків {artist_filter}, {sim_added} нових додано"
                    ),
                    "count": sim_added,
                })
                logger.info(
                    f"CLAP album similarity: ranked {len(sim_rows)} tracks by similarity "
                    f"to '{album_hint}', {sim_added} new added"
                )
            except Exception as e:
                logger.warning(f"CLAP album similarity search failed: {e}")

    # 1e. Deduplicate quality variants (prefer CD over Vinyl/Hi-Res/MP3)
    # HQPlayer can't play files with [Vinyl]/[TR24]/[MP3] brackets in paths
    quality_priority = {"CD": 0, "Hi-Res": 1, "Vinyl": 2, "MP3": 3}
    seen_track_keys = {}  # (artist, title, album_base) -> (track_id, quality_priority)
    ids_to_remove = []
    for tid, t in all_tracks.items():
        key = (t.get("artist", ""), t.get("title", ""), t.get("album", ""))
        qs = t.get("quality_source", "CD")
        prio = quality_priority.get(qs, 0)
        if key in seen_track_keys:
            existing_tid, existing_prio = seen_track_keys[key]
            if prio < existing_prio:
                # Current track has better priority, remove old one
                ids_to_remove.append(existing_tid)
                seen_track_keys[key] = (tid, prio)
            else:
                # Existing track has better priority, remove current
                ids_to_remove.append(tid)
        else:
            seen_track_keys[key] = (tid, prio)
    for tid in ids_to_remove:
        all_tracks.pop(tid, None)
    if ids_to_remove:
        logger.info(f"Deduplicated {len(ids_to_remove)} quality variants (kept CD over Vinyl/Hi-Res)")

    # 2. Enrich tracks with Last.fm data, audio features, tags
    track_ids = list(all_tracks.keys())
    enrichment = _get_track_enrichment(db, track_ids)

    # Count enrichment sources
    enrichment_stats = {"audio_features": 0, "tags": 0, "popularity": 0, "artist_bio": 0}
    for tid, enrich in enrichment.items():
        if enrich.get("bpm") or enrich.get("key_mode") or enrich.get("danceability") is not None:
            enrichment_stats["audio_features"] += 1
        if enrich.get("album_tags") or enrich.get("artist_tags"):
            enrichment_stats["tags"] += 1
        if enrich.get("listeners") or enrich.get("playcount"):
            enrichment_stats["popularity"] += 1

    enrichment_parts = []
    if enrichment_stats["audio_features"]:
        enrichment_parts.append(f"audio features (BPM, key, mood, instruments) для {enrichment_stats['audio_features']} треків")
    if enrichment_stats["tags"]:
        enrichment_parts.append(f"Last.fm теги для {enrichment_stats['tags']} треків")
    if enrichment_stats["popularity"]:
        enrichment_parts.append(f"Last.fm popularity для {enrichment_stats['popularity']} треків")
    if enrichment_parts:
        retrieval_log.append({
            "source": "Enrichment",
            "description": "Збагачення контексту: " + "; ".join(enrichment_parts),
            "count": len(track_ids),
        })

    # Debug: Log artists in retrieved tracks
    artists_in_context = {}
    for track in all_tracks.values():
        artist = track.get("artist", "Unknown")
        artists_in_context[artist] = artists_in_context.get(artist, 0) + 1
    logger.info(f"Artists in retrieved tracks (before ranking): {artists_in_context}")

    # 3. Sort by similarity, then boost by popularity
    tracks = sorted(
        all_tracks.values(),
        key=lambda t: t.get("similarity") or 0,
        reverse=True,
    )[:40]  # wider net for re-ranking

    tracks = _boost_by_popularity(tracks, enrichment)
    tracks = tracks[:30]  # final cap

    # 3b. For artist-specific queries, ensure every album has at least 1 playable track
    # Add back album sample tracks that were cut by the cap
    if "artist" in filters:
        artist_name = filters["artist"]
        track_ids_in_final = {t["id"] for t in tracks}
        albums_covered = {t.get("album") for t in tracks if t.get("artist") == artist_name}
        missing = [
            t for t in all_tracks.values()
            if t.get("artist") == artist_name
            and t.get("album") not in albums_covered
            and t["id"] not in track_ids_in_final
        ]
        # Deduplicate: pick one per missing album
        seen_albums = set()
        for t in missing:
            album = t.get("album")
            if album and album not in seen_albums:
                tracks.append(t)
                seen_albums.add(album)
        if seen_albums:
            logger.info(f"Added {len(seen_albums)} tracks from albums missing in final list: {seen_albums}")

    # Debug: Log artists in final context sent to Claude
    final_artists = {}
    for track in tracks:
        artist = track.get("artist", "Unknown")
        final_artists[artist] = final_artists.get(artist, 0) + 1
    logger.info(f"Artists in final context (top {len(tracks)}): {final_artists}")

    # 4. Build enriched context
    track_context = _format_track_context(tracks, enrichment)

    # Debug: Log first 3 tracks in formatted context
    if tracks:
        logger.info(f"First 3 tracks in formatted context:")
        for i, t in enumerate(tracks[:3], 1):
            logger.info(f"  {i}. {t.get('artist')} - {t.get('album')} - {t.get('title')}")

    # Artist-specific context if query mentions a known artist
    artist_context = ""
    if filters.get("artist"):
        ctx = _get_artist_context(db, filters["artist"])
        if ctx:
            artist_context = f"\n\nArtist Information:\n{ctx}"
            retrieval_log.append({
                "source": "Artist bio (Last.fm)",
                "description": f"Біографія та схожі артисти для '{filters['artist']}'",
                "count": 1,
            })

        # Always add compact album listing so Claude sees full discography
        albums = _get_artist_discography(db, filters["artist"])
        if albums:
            album_listing = _format_album_context(albums, filters["artist"])
            artist_context += f"\n\n{album_listing}"

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

    # Player state context (now playing, playlist)
    player_section = ""
    if player_context:
        player_section = f"\n\n{player_context}"

    user_message = f"""User query: {original_query}{player_section}

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
        "query": original_query,
        "model": CLAUDE_MODEL,
        "tracks_retrieved": len(tracks),
        "filters_detected": filters,
        "track_reference": ref_track_id,
        "retrieval_log": retrieval_log,
    }
