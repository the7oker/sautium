"""Parse structured block payloads from AI responses.

The chat protocol invites the model to emit a `[DJ_BLOCKS][...]` JSON
marker at the very end of its response, replacing the older flat
`[DJ_TRACKS]` list. The block list mirrors Discovery's three-block
layout — artists, albums, tracks — so the chat UI can render the
same visual contract as the Discovery screen.

Schema accepted by `extract_blocks`:

    [
      {"kind": "artist",
       "items": [{"artist_id": "<uuid>"}, ...]},
      {"kind": "album",
       "items": [{"album_id": "<uuid>"}, ...]},
      {"kind": "tracks",
       "items": [{"id": <media_file_id:int>}, ...]}
    ]

`kind` is canonicalized — singular "artist"/"album"/"track" and plural
forms are both accepted ("artists" → "artist" etc). Items missing
their identifying field are dropped silently. The caller is expected
to hydrate the items (cover_id, names, year, ...) via
`entity_hydration.hydrate_*` before persisting or returning them.

Backwards compatibility: if no `[DJ_BLOCKS]` marker is present but
a legacy `[DJ_TRACKS]` marker is, `extract_blocks_with_fallback`
synthesizes a single tracks block from the flat list.
"""

import json
import logging
import re
from typing import Any

from tools.track_parser import extract_tracks, strip_tracks_marker

logger = logging.getLogger(__name__)


# Greedy match — block JSON contains nested arrays/objects.
_BLOCKS_RE_CLOSED = re.compile(
    r'\s*\[DJ_BLOCKS\]\s*(\[.*\])\s*\[/DJ_BLOCKS\]\s*', re.DOTALL,
)
_BLOCKS_RE_OPEN = re.compile(
    r'\s*\[DJ_BLOCKS\]\s*(\[.*\])\s*\Z', re.DOTALL,
)

_KIND_CANONICAL = {
    "artist": "artist", "artists": "artist",
    "album": "album", "albums": "album",
    "track": "tracks", "tracks": "tracks",
}


def _find_blocks_match(text: str) -> re.Match | None:
    return _BLOCKS_RE_CLOSED.search(text) or _BLOCKS_RE_OPEN.search(text)


def _normalize_block(b: Any) -> dict | None:
    """Validate one `{kind, items}` entry. Drops malformed items
    inside the block but keeps the block itself if at least one
    item survives.
    """
    if not isinstance(b, dict):
        return None
    kind = _KIND_CANONICAL.get(str(b.get("kind", "")).strip().lower())
    raw_items = b.get("items")
    if not kind or not isinstance(raw_items, list):
        return None

    items: list[dict] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        if kind == "artist":
            aid = it.get("artist_id") or it.get("id")
            if aid:
                items.append({"artist_id": str(aid)})
        elif kind == "album":
            aid = it.get("album_id") or it.get("id")
            if aid:
                items.append({"album_id": str(aid)})
        elif kind == "tracks":
            tid = it.get("id") or it.get("media_file_id") or it.get("track_id")
            try:
                tid_int = int(tid) if tid is not None else None
            except (TypeError, ValueError):
                tid_int = None
            if tid_int:
                items.append({"id": tid_int})

    if not items:
        return None
    return {"kind": kind, "items": items}


def extract_blocks(text: str) -> list[dict]:
    """Extract the `[DJ_BLOCKS]` payload. Returns empty list if absent
    or unparseable.
    """
    match = _find_blocks_match(text)
    if not match:
        return []
    try:
        raw = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"Failed to parse DJ_BLOCKS JSON: {e}")
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for b in raw:
        norm = _normalize_block(b)
        if norm:
            out.append(norm)
    return out


def strip_blocks_marker(text: str) -> str:
    """Remove the `[DJ_BLOCKS]` marker from the visible answer text."""
    match = _find_blocks_match(text)
    if not match:
        return text.strip()
    return (text[:match.start()] + text[match.end():]).strip()


def extract_blocks_with_fallback(text: str) -> tuple[list[dict], str]:
    """Extract blocks, falling back to the legacy `[DJ_TRACKS]` flat
    list when no `[DJ_BLOCKS]` marker is present. Returns
    `(blocks, clean_text)` with both markers stripped.
    """
    clean = text
    blocks = extract_blocks(clean)
    if blocks:
        return blocks, strip_blocks_marker(clean)

    # Legacy flat tracks list → wrap as a single tracks block. We do
    # not try to auto-group into albums — the user's preference is
    # that grouping is the model's job.
    legacy = extract_tracks(clean)
    clean = strip_tracks_marker(clean)
    if legacy:
        ids: list[dict] = []
        for t in legacy:
            try:
                ids.append({"id": int(t["id"])})
            except (TypeError, ValueError, KeyError):
                continue
        if ids:
            return [{"kind": "tracks", "items": ids}], clean
    return [], clean
