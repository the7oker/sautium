"""
The Sautium-canonical play queue (HARDWARE-TIERS §2.6).

Single source of truth for what is queued, in what order, with full
display metadata. Output backends render it (local engine) or mirror it
one-way (HQPlayer's native playlist); `/api/player/playlist` serves its
`payload()` directly — no player round-trip, no cache.

`version` is bumped on every payload-visible change and embedded in each
SSE status payload as `playlist_version`, so clients refetch
deterministically. `generation` is bumped whenever a NEW queue starts
(replace / radio clear): background fillers capture it and stop appending
the instant the user moves to another queue.
"""

import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

from db_pool import db_query as _db_query

logger = logging.getLogger(__name__)

# Provider-resolved track durations for phantom tracks MusicBrainz has NO length
# for — DISPLAY ONLY, deliberately never written to album_tracks.length_ms. That
# column is MB-canonical IDENTITY: a provider duration written there would
# circularly self-confirm the very match it was derived from, defeating the
# resolve/enrichment length-gate and letting wrong-recording features (cover /
# remix / DJ-mix) poison the shared feature pool. So the resolved duration lives
# only in memory (recomputed by the availability resolve) and is surfaced solely
# for display. Keyed by track_id; resets on restart.
resolved_durations: dict[str, float] = {}

# Provider album art for streamed tracks, keyed by track_id — a fallback for the
# CAA cover, which 404s for ~a quarter of phantom release-groups (no front art in
# MusicBrainz). DISPLAY-only, in-memory; the streamed provider always has a cover.
resolved_artwork: dict[str, str] = {}


@dataclass
class QueueItem:
    """One queued track. `track_id` (UUID) is the source-agnostic identity —
    owned AND phantom rows carry it, and play tracking keys on it;
    `media_file_id` is the optional physical file (owned only). `source`
    tells a backend how to reach the audio:
      {"kind": "file",  "path": <db file_path>, "format": <file_format>}
      {"kind": "proxy", "token": <media-proxy token>}
      {"kind": "uri",   "uri": <verbatim>}   — foreign/out-of-library
    """
    track_id: Optional[str]
    media_file_id: Optional[int]
    source: dict
    title: str = ""
    artist: str = ""
    album: str = ""
    track_number: Optional[int] = None
    duration_seconds: Optional[float] = None
    cover_id: Optional[str] = None
    cover_url: Optional[str] = None
    preview: bool = False
    provider: Optional[str] = None


class CanonicalQueue:
    def __init__(self):
        self._lock = threading.RLock()
        self._items: list[QueueItem] = []
        self._version = 0
        self._generation = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def version(self) -> int:
        return self._version

    @property
    def generation(self) -> int:
        return self._generation

    def snapshot(self) -> list[QueueItem]:
        with self._lock:
            return list(self._items)

    def item_at(self, index_1b: Optional[int]) -> Optional[QueueItem]:
        with self._lock:
            if isinstance(index_1b, int) and 1 <= index_1b <= len(self._items):
                return self._items[index_1b - 1]
            return None

    # -- mutations (serialized by the manager's mutate lock) ----------------

    def replace(self, items: list[QueueItem]) -> int:
        """New queue = new generation (retires any running filler)."""
        with self._lock:
            self._items = list(items)
            self._generation += 1
            self._version += 1
            return self._generation

    def append(self, items: list[QueueItem]) -> None:
        with self._lock:
            self._items.extend(items)
            self._version += 1

    def insert_after(self, index_1b: int, items: list[QueueItem]) -> None:
        with self._lock:
            self._items[index_1b:index_1b] = items
            self._version += 1

    def remove(self, index_1b: int) -> Optional[QueueItem]:
        with self._lock:
            if 1 <= index_1b <= len(self._items):
                item = self._items.pop(index_1b - 1)
                self._version += 1
                return item
            return None

    def reorder_by_media_ids(self, order: list[int]) -> None:
        """Reorder to `order` (media_file_ids; the endpoint has already
        validated it as a permutation of the current owned-only queue).
        Duplicate ids keep their relative order via per-id buckets."""
        with self._lock:
            by_id: dict[Optional[int], list[QueueItem]] = {}
            for it in self._items:
                by_id.setdefault(it.media_file_id, []).append(it)
            new_items = []
            for mid in order:
                bucket = by_id.get(mid)
                if bucket:
                    new_items.append(bucket.pop(0))
            for bucket in by_id.values():
                new_items.extend(bucket)
            self._items = new_items
            self._version += 1

    def clear_for_radio(self, current_index: Optional[int]) -> int:
        """Radio start: keep only the playing slot (the seed plays on while
        the batch flows in behind). New generation — supersedes any filler."""
        with self._lock:
            if isinstance(current_index, int) and 1 <= current_index <= len(self._items):
                self._items = [self._items[current_index - 1]]
            else:
                self._items = []
            self._generation += 1
            self._version += 1
            return self._generation

    # -- payload -------------------------------------------------------------

    def payload(self) -> dict:
        """The exact `/api/player/playlist` JSON shape the UI has always
        consumed (owned / preview / foreign row variants)."""
        with self._lock:
            tracks = [self._row(it, idx) for idx, it in enumerate(self._items)]
        return {"tracks": tracks, "count": len(tracks)}

    @staticmethod
    def _row(item: QueueItem, idx: int) -> dict:
        if item.media_file_id is not None:
            return {
                "id": item.media_file_id,
                "track_id": item.track_id,
                "title": item.title,
                "track_number": item.track_number,
                "artist": item.artist,
                "album": item.album or "",
                "duration_seconds": item.duration_seconds,
                "cover_id": item.cover_id,
                "index": idx,
            }
        if item.preview:
            return {
                "id": None,
                "title": item.title or "Unknown",
                "track_number": None,
                "artist": item.artist or "Unknown",
                "album": item.album or "",
                "duration_seconds": (item.duration_seconds
                                     or resolved_durations.get(item.track_id)),
                "cover_id": None,
                "preview": True,
                "provider": item.provider,
                "track_id": item.track_id,
                "cover_url": item.cover_url,
                "provider_cover_url": resolved_artwork.get(item.track_id),
                "index": idx,
            }
        return {
            "id": None,
            "title": item.title or "Unknown",
            "track_number": None,
            "artist": item.artist or "Unknown",
            "duration_seconds": None,
            "cover_id": None,
            "index": idx,
        }


# -- item constructors -----------------------------------------------------

_MEDIA_ITEM_SQL = """
    SELECT mf.id, mf.file_path, mf.file_format, t.id::text AS track_uuid,
           t.title, mf.track_number, mf.duration_seconds,
           mf.cover_id::text AS cover_id, a.name AS artist, al.title AS album
    FROM media_files mf
    JOIN tracks t ON mf.track_id = t.id
    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
    JOIN artists a ON ta.artist_id = a.id
    LEFT JOIN album_variants av ON mf.album_variant_id = av.id
    LEFT JOIN albums al ON av.album_id = al.id
    WHERE mf.id = ANY(%(ids)s)
"""


def _item_from_media_row(r: dict) -> QueueItem:
    return QueueItem(
        track_id=r["track_uuid"],
        media_file_id=r["id"],
        source={"kind": "file", "path": r["file_path"], "format": r["file_format"]},
        title=r["title"],
        artist=r["artist"],
        album=r.get("album") or "",
        track_number=r["track_number"],
        duration_seconds=(float(r["duration_seconds"])
                          if r["duration_seconds"] is not None else None),
        cover_id=r["cover_id"],
    )


def items_for_media_ids(ids: list[int]) -> list[QueueItem]:
    """Full-metadata QueueItems for owned media files, order-preserving.
    THE single constructor every owned enqueue path goes through."""
    if not ids:
        return []
    rows = _db_query(_MEDIA_ITEM_SQL + " ORDER BY array_position(%(ids)s, mf.id)",
                     {"ids": ids})
    return [_item_from_media_row(r) for r in rows]


def items_for_file_paths(paths: list[str]) -> dict[str, QueueItem]:
    """QueueItems for owned media files keyed by file_path — the reverse
    mapping used when adopting an existing HQPlayer playlist on attach."""
    if not paths:
        return {}
    rows = _db_query(
        _MEDIA_ITEM_SQL.replace("WHERE mf.id = ANY(%(ids)s)",
                                "WHERE mf.file_path = ANY(%(ids)s)"),
        {"ids": paths})
    return {r["file_path"]: _item_from_media_row(r) for r in rows}


def item_for_proxy_token(token: str) -> Optional[QueueItem]:
    """QueueItem for a media-proxy stream. Phantom tracks become preview
    items; an OWNED m4a served through the proxy (HQPlayer transcode)
    resolves back to its owned item. None when the proxy has no metadata
    for the token (never fetched)."""
    from streaming import service as streaming_service
    proxy = streaming_service.get_proxy()
    if proxy is None:
        return None
    meta = streaming_service.preview_meta(proxy.url_for(token))
    if not meta:
        return None
    if meta.get("media_file_id"):
        items = items_for_media_ids([meta["media_file_id"]])
        return items[0] if items else None
    return QueueItem(
        track_id=meta.get("track_id"),
        media_file_id=None,
        source={"kind": "proxy", "token": token},
        title=meta.get("title") or "",
        artist=meta.get("artist") or "",
        album=meta.get("album") or "",
        duration_seconds=meta.get("duration"),
        cover_url=meta.get("cover_url"),
        preview=True,
        provider=meta.get("provider"),
    )
