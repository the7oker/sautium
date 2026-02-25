"""Parse track recommendations from AI responses.

Extracts [DJ_TRACKS][...][/DJ_TRACKS] markers from text.
Shared between Claude Code runner and API providers.
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def extract_tracks(text: str) -> list[dict[str, Any]]:
    """Extract track list from [DJ_TRACKS][...][/DJ_TRACKS] marker."""
    match = re.search(r'\[DJ_TRACKS\]\s*(\[.*?\])\s*\[/DJ_TRACKS\]', text, re.DOTALL)
    if not match:
        return []

    try:
        tracks = json.loads(match.group(1))
        if not isinstance(tracks, list):
            return []
        valid = []
        for t in tracks:
            if isinstance(t, dict) and t.get("id") and t.get("title"):
                valid.append({
                    "id": t["id"],
                    "title": t.get("title", ""),
                    "artist": t.get("artist", "Unknown"),
                    "album": t.get("album", ""),
                })
        return valid
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"Failed to parse DJ_TRACKS JSON: {e}")
        return []


def strip_tracks_marker(text: str) -> str:
    """Remove [DJ_TRACKS]...[/DJ_TRACKS] block from answer text."""
    cleaned = re.sub(r'\s*\[DJ_TRACKS\].*?\[/DJ_TRACKS\]\s*', '', text, flags=re.DOTALL)
    return cleaned.strip()
