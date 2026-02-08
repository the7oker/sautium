"""
AI Assistant for Music AI DJ.
RAG pipeline: retrieve relevant tracks, build context, query Claude for recommendations.
"""

import logging
from typing import Any, Dict, List, Optional

import anthropic
from sqlalchemy import text
from sqlalchemy.orm import Session

from config import settings
from search import search_by_text, search_by_metadata

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an AI music DJ assistant for a personal FLAC music library. \
Your job is to recommend tracks from the library based on the user's request.

Rules:
- Only recommend tracks from the provided library list below. Never invent tracks.
- Include track titles and artists in your recommendations.
- Explain why each recommendation matches the request.
- If no tracks match well, say so honestly.
- Be concise but explain your reasoning.
- You can comment on audio quality (CD, Vinyl, Hi-Res) when relevant.
- Format track references as: "Title" by Artist."""

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
        "nu jazz", "nu-jazz",
    ]
    for genre in genre_keywords:
        if genre in query_lower:
            filters["genre"] = genre
            break

    # Artist names — check against DB for known artists mentioned in query
    try:
        rows = db.execute(text("SELECT DISTINCT name FROM artists")).fetchall()
        for row in rows:
            artist_name = row[0]
            if artist_name and artist_name.lower() in query_lower:
                filters["artist"] = artist_name
                break
    except Exception as e:
        logger.debug(f"Artist extraction failed: {e}")

    # Year patterns
    import re

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

    return filters


def _format_track_context(tracks: List[Dict[str, Any]]) -> str:
    """Format retrieved tracks into a text block for Claude's context."""
    if not tracks:
        return "No tracks were found matching the search criteria."

    lines = []
    for i, t in enumerate(tracks, 1):
        duration = ""
        if t.get("duration_seconds"):
            mins = int(t["duration_seconds"] // 60)
            secs = int(t["duration_seconds"] % 60)
            duration = f" | Duration: {mins}:{secs:02d}"

        sim = ""
        if t.get("similarity") is not None:
            sim = f" | Relevance: {t['similarity']:.2f}"

        lines.append(
            f"Track {i}: \"{t.get('title', '?')}\" by {t.get('artist', 'Unknown')}"
            f" | Album: {t.get('album', '?')}"
            f" | Genre: {t.get('genre', 'N/A')}"
            f" | Quality: {t.get('quality_source', '?')}"
            f"{duration}{sim}"
        )

    return "\n".join(lines)


def ask_assistant(
    db: Session,
    query: str,
    limit: int = 20,
) -> Dict[str, Any]:
    """
    RAG pipeline: retrieve tracks, build context, call Claude.

    Args:
        db: Database session.
        query: Natural language question about the music library.
        limit: Max tracks to retrieve for context.

    Returns:
        Dict with answer, tracks, query, model, and tracks_retrieved count.
    """
    if not settings.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY is not configured")

    # 1. Retrieve tracks using multiple strategies
    all_tracks: Dict[int, Dict[str, Any]] = {}  # track_id -> track dict

    # Always run text-to-audio similarity search (CLAP)
    try:
        text_results = search_by_text(db, query, limit=limit, min_similarity=0.3)
        for t in text_results.get("results", []):
            all_tracks[t["id"]] = t
        logger.info(f"Text search returned {text_results.get('count', 0)} tracks")
    except Exception as e:
        logger.warning(f"Text search failed: {e}")

    # Also run metadata search if we can extract filters
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
        except Exception as e:
            logger.warning(f"Metadata search failed: {e}")

    # Deduplicated and capped track list
    # Sort by similarity (text search results first, then metadata-only)
    tracks = sorted(
        all_tracks.values(),
        key=lambda t: t.get("similarity") or 0,
        reverse=True,
    )[:30]

    # 2. Build context
    track_context = _format_track_context(tracks)

    user_message = f"""User query: {query}

Here are the tracks from the library that may be relevant:

{track_context}

Based on these tracks, please answer the user's query with specific recommendations."""

    # 3. Call Claude
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    logger.info(f"Calling Claude ({CLAUDE_MODEL}) with {len(tracks)} tracks context")

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    answer = response.content[0].text

    # 4. Return structured response
    return {
        "answer": answer,
        "tracks": tracks,
        "query": query,
        "model": CLAUDE_MODEL,
        "tracks_retrieved": len(tracks),
    }
