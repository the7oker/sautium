"""Parse structured block payloads from AI responses.

The chat protocol invites the model to emit a `[SAUTIUM_BLOCKS][...]` JSON
marker at the very end of its response, replacing the older flat
`[SAUTIUM_TRACKS]` list. The block list mirrors Discovery's three-block
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

Backwards compatibility: if no `[SAUTIUM_BLOCKS]` marker is present but
a legacy `[SAUTIUM_TRACKS]` marker is, `extract_blocks_with_fallback`
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
    r'\s*\[SAUTIUM_BLOCKS\]\s*(\[.*\])\s*\[/SAUTIUM_BLOCKS\]\s*', re.DOTALL,
)
_BLOCKS_RE_OPEN = re.compile(
    r'\s*\[SAUTIUM_BLOCKS\]\s*(\[.*\])\s*\Z', re.DOTALL,
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


def parse_blocks_payload(json_str: str) -> list[dict]:
    """Parse a raw JSON array (the inside of the `[SAUTIUM_BLOCKS]...
    [/SAUTIUM_BLOCKS]` markers) into normalized block dicts. Used by the
    streaming filter, which gives us the payload directly without
    surrounding markers."""
    try:
        raw = json.loads(json_str)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"Failed to parse SAUTIUM_BLOCKS JSON: {e}")
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for b in raw:
        norm = _normalize_block(b)
        if norm:
            out.append(norm)
    return out


def extract_blocks(text: str) -> list[dict]:
    """Extract the `[SAUTIUM_BLOCKS]` payload. Returns empty list if absent
    or unparseable.
    """
    match = _find_blocks_match(text)
    if not match:
        return []
    return parse_blocks_payload(match.group(1))


def strip_blocks_marker(text: str) -> str:
    """Remove the `[SAUTIUM_BLOCKS]` marker from the visible answer text."""
    match = _find_blocks_match(text)
    if not match:
        return text.strip()
    return (text[:match.start()] + text[match.end():]).strip()


def extract_blocks_with_fallback(text: str) -> tuple[list[dict], str]:
    """Extract blocks, falling back to the legacy `[SAUTIUM_TRACKS]` flat
    list when no `[SAUTIUM_BLOCKS]` marker is present. Returns
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


# ---------------------------------------------------------------------------
# Streaming filter
# ---------------------------------------------------------------------------

class BlocksFilter:
    """Stateful filter for streamed assistant text.

    Sees a sequence of text deltas and produces:
      - clean text deltas with the `[SAUTIUM_BLOCKS]...[/SAUTIUM_BLOCKS]` marker
        elided, safe to forward live to the UI.
      - one JSON payload string when the closing marker is reached,
        ready to feed into `json.loads` + `_normalize_block`.

    Why not just buffer everything and run the regex at the end?
    Because the prose is the bulk of the response (often 100-500
    tokens) and we want to stream it as it generates. The marker is
    a short trailer; we just have to make sure the few characters
    leading up to it don't leak into the visible bubble.

    State machine:
      `before` — accumulating prose, watching for `[SAUTIUM_BLOCKS]`.
                 Emits everything except the trailing few chars (which
                 might be the start of the marker we haven't seen yet).
      `in`     — between markers, accumulating the JSON payload.
      `after`  — past `[/SAUTIUM_BLOCKS]`. Anything more is unexpected; we
                 still emit it as text so nothing silently disappears.
    """

    OPEN = "[SAUTIUM_BLOCKS]"
    CLOSE = "[/SAUTIUM_BLOCKS]"
    HOLD = len(OPEN) - 1  # chars to hold back so we never split a marker

    def __init__(self):
        self._before = ""
        self._payload = ""
        self._state = "before"
        self.full_text = ""  # complete unfiltered text, for fallback parsing

    def feed(self, delta: str) -> list[tuple[str, str]]:
        """Process one text delta. Returns a list of `(kind, value)`
        tuples where kind is `"text"` (forward to UI) or
        `"blocks_json"` (parse + hydrate, then emit a `blocks` event).
        """
        self.full_text += delta
        out: list[tuple[str, str]] = []
        if not delta:
            return out

        if self._state == "after":
            out.append(("text", delta))
            return out

        if self._state == "before":
            self._before += delta
            idx = self._before.find(self.OPEN)
            if idx >= 0:
                pre = self._before[:idx]
                if pre:
                    out.append(("text", pre))
                self._payload = self._before[idx + len(self.OPEN):]
                self._before = ""
                self._state = "in"
                # Fall through to process payload below.
            else:
                if len(self._before) > self.HOLD:
                    safe = self._before[:-self.HOLD]
                    self._before = self._before[-self.HOLD:]
                    out.append(("text", safe))
                return out
        else:  # state == "in" before this call
            self._payload += delta

        # State == "in"; look for closing marker.
        idx = self._payload.find(self.CLOSE)
        if idx >= 0:
            out.append(("blocks_json", self._payload[:idx]))
            tail = self._payload[idx + len(self.CLOSE):]
            self._payload = ""
            self._state = "after"
            if tail:
                out.append(("text", tail))
        return out

    def flush(self) -> list[tuple[str, str]]:
        """End of stream — emit anything still buffered. Held-back
        chars in `before` are real text. An unterminated `in` payload
        is forwarded as JSON so the parser can try to recover the
        list (matches the existing `_BLOCKS_RE_OPEN` fallback).
        """
        out: list[tuple[str, str]] = []
        if self._state == "before" and self._before:
            out.append(("text", self._before))
            self._before = ""
        elif self._state == "in" and self._payload.strip():
            out.append(("blocks_json", self._payload))
            self._payload = ""
        return out
