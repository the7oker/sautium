"""Parse track recommendations from AI responses.

Extracts [SAUTIUM_TRACKS][...][/SAUTIUM_TRACKS] markers from text.
Shared between Claude Code runner and API providers.
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


_TRACKS_RE_CLOSED = re.compile(r'\s*\[SAUTIUM_TRACKS\]\s*(\[.*\])\s*\[/SAUTIUM_TRACKS\]\s*', re.DOTALL)
_TRACKS_RE_OPEN = re.compile(r'\s*\[SAUTIUM_TRACKS\]\s*(\[.*\])\s*\Z', re.DOTALL)


def _find_tracks_block(text: str) -> re.Match | None:
    """Match [SAUTIUM_TRACKS][...][/SAUTIUM_TRACKS] greedily.

    Greedy match is required because track titles can themselves contain
    `]` (e.g. "Bon Voyage [Live Hamburg 1981]"); a non-greedy `.*?` would
    truncate the JSON at the first inner bracket.
    """
    return _TRACKS_RE_CLOSED.search(text) or _TRACKS_RE_OPEN.search(text)


def extract_tracks(text: str) -> list[dict[str, Any]]:
    """Extract track list from [SAUTIUM_TRACKS][...][/SAUTIUM_TRACKS] marker.
    Closing tag [/SAUTIUM_TRACKS] is optional for robustness.
    """
    match = _find_tracks_block(text)
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
        logger.warning(f"Failed to parse SAUTIUM_TRACKS JSON: {e}")
        return []


def strip_tracks_marker(text: str) -> str:
    """Remove [SAUTIUM_TRACKS]...[/SAUTIUM_TRACKS] block from answer text.
    Closing tag [/SAUTIUM_TRACKS] is optional for robustness.
    """
    match = _find_tracks_block(text)
    if not match:
        return text.strip()
    return (text[:match.start()] + text[match.end():]).strip()
