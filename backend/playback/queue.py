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
from hqplayer_client import file_path_to_uri
from sql_queries import best_rip_order

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
      {"kind": "hqp",   "path": <hqp_path>, "format": <file_format>,
                        "endpoint": <hqp_endpoints id>}
                        — a file held in an HQPlayer's own library: that
                        HQPlayer opens it as file://<path>; no other output
                        can (it is played as a stream there)
      {"kind": "proxy", "token": <media-proxy token>}
      {"kind": "uri",   "uri": <verbatim>}   — foreign/out-of-library
      {"kind": "track"} — its file left the library and no other copy of
                        the track is here (rebind_origins): every output
                        opens whatever copy it reaches, else a stream
    """
    track_id: Optional[str]
    media_file_id: Optional[int]
    source: dict
    title: str = ""
    artist: str = ""
    album: str = ""
    album_id: Optional[str] = None   # albums.id this slot came from; see TrackQuery.album_id
    track_number: Optional[int] = None
    duration_seconds: Optional[float] = None
    cover_id: Optional[str] = None
    cover_url: Optional[str] = None
    preview: bool = False
    provider: Optional[str] = None
    # A 30 s excerpt, not the recording: shown as [30s], never a listen, and
    # `duration_seconds` is the clip's own length.
    excerpt: bool = False
    # How the ACTIVE output opens this slot when that differs from `source`
    # (playback.substitute, recomputed on every output switch): a `file` /
    # `hqp` copy the new output opens itself, a `proxy` stream that stands
    # in for a copy it cannot, `pending` while that stream is on its way,
    # `unplayable` when nothing can serve the slot here. None = as `source`
    # says. `source` is the enqueue-time origin, which no output switch
    # touches, so the way back to the output that opened it natively is
    # free; it changes only when the file it names leaves the library
    # (CanonicalQueue.rebind).
    play: Optional[dict] = None

    def opener(self) -> dict:
        """What the active output reads to open this slot."""
        return self.play or self.source


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

    def index_of(self, item: QueueItem) -> Optional[int]:
        """1-based slot of THIS item object — identity, not equality: the same
        track can legitimately sit in the queue twice. None once it is gone."""
        with self._lock:
            for i, it in enumerate(self._items):
                if it is item:
                    return i + 1
            return None

    def refresh_proxy_items(self, token: str, *, provider: Optional[str],
                            excerpt: bool, duration_seconds: Optional[float]) -> bool:
        """Bring every slot streaming `token` up to date with what the proxy
        actually holds for it (the media-proxy track-ready hook). A stream's
        item is built once, when its buffer is first ready; a refetch may
        land on another provider or, as an excerpt, another length — and an
        output reads the item at load. True when anything changed (the
        version is bumped, so the next status tick carries it)."""
        with self._lock:
            changed = False
            for it in self._items:
                if not any(s.get("kind") == "proxy" and s.get("token") == token
                           for s in (it.source, it.play or {})):
                    continue
                new = (provider, excerpt, duration_seconds)
                if new != (it.provider, it.excerpt, it.duration_seconds):
                    it.provider, it.excerpt, it.duration_seconds = new
                    changed = True
            if changed:
                self._version += 1
            return changed

    def reresolve(self, resolve) -> int:
        """Recompute how the ACTIVE output opens every slot (`QueueItem.play`)
        — an output switch keeps the queue, and each output opens its own
        copy of a track (playback.substitute.native_plays). `resolve` maps
        the items, in order, to their `play` (None = as `source` says).
        Returns the number of slots left `pending` a stream."""
        with self._lock:
            plays = list(resolve(list(self._items)))
            for it, play in zip(self._items, plays):
                it.play = play
            self._version += 1
            return sum(1 for p in plays if p and p.get("kind") == "pending")

    def rebind(self, origins, plays) -> list[QueueItem]:
        """The file layer changed under the queue (PlaybackManager.
        rebind_files). `origins` maps the items, in order, to what each must
        change (rebind_origins): None where nothing does, else the fields to
        set — none when only its way in named a gone file. Every item that
        changed has its `play` re-read through `plays` (the active output's
        native_plays), or cleared with no output attached: the attach
        re-reads every slot. Items change in place, so the playing one stays
        the object the backends and the tracker hold. Returns the items that
        changed; the version is bumped when any did."""
        with self._lock:
            items = list(self._items)
            moved = []
            for it, change in zip(items, origins(items)):
                if change is None:
                    continue
                for name, value in change.items():
                    setattr(it, name, value)
                moved.append(it)
            if moved:
                new_plays = plays(moved) if plays is not None else [None] * len(moved)
                for it, play in zip(moved, new_plays):
                    it.play = play
                self._version += 1
            return moved

    def set_play(self, item: "QueueItem", play: Optional[dict]) -> Optional[int]:
        """A substitute landed on (or failed for) this very item: its 1-based
        slot, or None when the item left the queue meanwhile."""
        with self._lock:
            for i, it in enumerate(self._items):
                if it is item:
                    it.play = play
                    self._version += 1
                    return i + 1
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

    def reorder_by_track_ids(self, order: list[str]) -> None:
        """Reorder to `order` (universal track UUIDs — the one identity
        BOTH owned and phantom items carry; media_file_id is None for
        phantoms, which made radio queues unreorderable). The endpoint has
        already validated the permutation. Duplicate ids keep their
        relative order via per-id buckets."""
        with self._lock:
            by_id: dict[Optional[str], list[QueueItem]] = {}
            for it in self._items:
                by_id.setdefault(it.track_id, []).append(it)
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
        # `play` names how the active output opens the slot when that is not
        # what `source` says (playback.substitute): a stream standing in for a
        # held copy shows as such, a slot still waiting for one as pending.
        play = (item.play or {}).get("kind")
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
                "play": play,
            }
        if item.preview or play == "proxy":
            return {
                "id": None,
                "title": item.title or "Unknown",
                "track_number": None,
                "artist": item.artist or "Unknown",
                "album": item.album or "",
                "duration_seconds": (item.duration_seconds
                                     or resolved_durations.get(item.track_id)),
                "cover_id": item.cover_id,
                "preview": True,
                "provider": item.provider,
                "excerpt": item.excerpt,
                "track_id": item.track_id,
                "cover_url": item.cover_url,
                "provider_cover_url": resolved_artwork.get(item.track_id),
                "index": idx,
                "play": play,
            }
        return {
            "id": None,
            "title": item.title or "Unknown",
            "track_number": None,
            "artist": item.artist or "Unknown",
            "album": item.album or "",
            "duration_seconds": item.duration_seconds,
            "cover_id": item.cover_id,
            "cover_url": item.cover_url,
            "track_id": item.track_id,
            "index": idx,
            "play": play,
        }


# -- item constructors -----------------------------------------------------

_MEDIA_ITEM_SQL = """
    SELECT mf.id, mf.file_path, mf.file_format, t.id::text AS track_uuid,
           t.title, mf.track_number, mf.duration_seconds,
           mf.cue_start_seconds, mf.cue_end_seconds,
           mf.cover_id::text AS cover_id, al.cover_url, a.name AS artist, al.title AS album,
           al.id::text AS album_id
    FROM media_files mf
    JOIN tracks t ON mf.track_id = t.id
    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
    JOIN artists a ON ta.artist_id = a.id
    LEFT JOIN album_variants av ON mf.album_variant_id = av.id
    LEFT JOIN albums al ON av.album_id = al.id
    WHERE mf.id = ANY(%(ids)s)
"""


def file_source(r: dict) -> dict:
    """The `file` source of a media_files row (file_path, file_format, the
    CUE bounds)."""
    source = {"kind": "file", "path": r["file_path"], "format": r["file_format"]}
    if r.get("cue_start_seconds") is not None:
        # CUE image slice — every backend must consume [cue_start, cue_end)
        # as its own resource, never the raw path. Bounds live in the source
        # dict so queue persistence round-trips them without dataclass churn.
        source["cue_start"] = float(r["cue_start_seconds"])
        source["cue_end"] = (float(r["cue_end_seconds"])
                             if r["cue_end_seconds"] is not None else None)
    return source


def _item_from_media_row(r: dict) -> QueueItem:
    return QueueItem(
        track_id=r["track_uuid"],
        media_file_id=r["id"],
        source=file_source(r),
        title=r["title"],
        artist=r["artist"],
        album=r.get("album") or "",
        album_id=r.get("album_id"),
        track_number=r["track_number"],
        duration_seconds=(float(r["duration_seconds"])
                          if r["duration_seconds"] is not None else None),
        cover_id=r["cover_id"],
        # the album's own URL only where no file carries a cover — the rule
        # every album surface follows (coverUrl() prefers the URL)
        cover_url=None if r["cover_id"] else r.get("cover_url"),
    )


# The owned slots' bindings against the file layer, in one pass over the
# queue: which slots name a media_files row that is gone — as their origin or
# as their way in — and, for a gone origin, the best live copy of the track
# (one on the album the slot was queued from first) and whether the track
# itself is still in the catalogue.
_REBIND_SQL = f"""
    WITH q AS (
        SELECT s.*,
               s.mid IS NOT NULL AND NOT EXISTS (
                   SELECT 1 FROM media_files mf WHERE mf.id = s.mid) AS origin_gone,
               s.pmid IS NOT NULL AND NOT EXISTS (
                   SELECT 1 FROM media_files mf WHERE mf.id = s.pmid) AS play_gone
        FROM unnest(%(mids)s::int[], %(pmids)s::int[], %(tids)s::uuid[], %(aids)s::uuid[])
             WITH ORDINALITY AS s(mid, pmid, track_id, album_id, slot)
    )
    SELECT q.slot, q.origin_gone,
           EXISTS (SELECT 1 FROM tracks t WHERE t.id = q.track_id) AS track_kept,
           c.id, c.file_path, c.file_format, c.duration_seconds,
           c.cue_start_seconds, c.cue_end_seconds
    FROM q
    LEFT JOIN LATERAL (
        SELECT mf.id, mf.file_path, mf.file_format::text AS file_format,
               mf.duration_seconds, mf.cue_start_seconds, mf.cue_end_seconds
        FROM media_files mf
        JOIN album_variants av ON av.id = mf.album_variant_id
        WHERE q.origin_gone AND mf.track_id = q.track_id
        ORDER BY (av.album_id = q.album_id) DESC NULLS LAST, {best_rip_order('mf')}
        LIMIT 1
    ) c ON true
    WHERE q.origin_gone OR q.play_gone
"""


def rebind_origins(items: list[QueueItem]) -> list[Optional[dict]]:
    """What each slot must change now that files have left the library
    (CanonicalQueue.rebind). A slot whose own file row is gone takes the best
    live copy of its track, one on the album it was queued from first; with
    none left here it becomes the track itself (`{"kind": "track"}`: the
    output opens whatever copy it reaches, else streams it like any
    phantom); a track that went with its file has left the catalogue, and
    the slot stays under its old name as an inert foreign entry. A slot whose
    way in (`play`) named a gone file changes nothing else — `{}`, its play
    is re-read. None for every other slot."""
    out: list[Optional[dict]] = [None] * len(items)
    bound = [(i, it) for i, it in enumerate(items)
             if (it.source.get("kind") == "file" and it.media_file_id is not None)
             or (it.play or {}).get("media_file_id") is not None]
    if not bound:
        return out
    rows = _db_query(_REBIND_SQL, {
        "mids": [it.media_file_id if it.source.get("kind") == "file" else None
                 for _, it in bound],
        "pmids": [(it.play or {}).get("media_file_id") for _, it in bound],
        "tids": [it.track_id for _, it in bound],
        "aids": [it.album_id for _, it in bound],
    })
    for r in rows:
        i, it = bound[r["slot"] - 1]
        if not r["origin_gone"]:
            out[i] = {}
        elif r["id"] is not None:
            change = {"media_file_id": r["id"], "source": file_source(r)}
            if r["duration_seconds"] is not None:
                change["duration_seconds"] = float(r["duration_seconds"])
            out[i] = change
        elif r["track_kept"]:
            out[i] = {"media_file_id": None, "source": {"kind": "track"}}
        else:
            out[i] = {"track_id": None, "media_file_id": None,
                      "source": {"kind": "uri", "uri": file_path_to_uri(it.source["path"])}}
    return out


def items_for_media_ids(ids: list[int]) -> list[QueueItem]:
    """Full-metadata QueueItems for owned media files, order-preserving.
    THE single constructor every owned enqueue path goes through."""
    if not ids:
        return []
    rows = _db_query(_MEDIA_ITEM_SQL + " ORDER BY array_position(%(ids)s, mf.id)",
                     {"ids": ids})
    return [_item_from_media_row(r) for r in rows]


_HQP_ITEM_SQL = """
    SELECT hf.id, hf.hqp_path, hf.file_format, av.hqp_endpoint_id,
           t.id::text AS track_uuid, t.title, hf.track_number, hf.duration_seconds,
           (SELECT mf.cover_id::text FROM media_files mf
            JOIN album_variants av2 ON av2.id = mf.album_variant_id
            WHERE av2.album_id = al.id AND mf.cover_id IS NOT NULL LIMIT 1) AS cover_id,
           al.cover_url, a.name AS artist, al.title AS album, al.id::text AS album_id
    FROM hqp_library_files hf
    JOIN tracks t ON hf.track_id = t.id
    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
    JOIN artists a ON ta.artist_id = a.id
    JOIN album_variants av ON hf.album_variant_id = av.id
    JOIN albums al ON av.album_id = al.id
"""


def _item_from_hqp_row(r: dict) -> QueueItem:
    return QueueItem(
        track_id=r["track_uuid"],
        media_file_id=None,
        source={"kind": "hqp", "path": r["hqp_path"], "format": r["file_format"],
                "endpoint": r["hqp_endpoint_id"]},
        title=r["title"],
        artist=r["artist"],
        album=r.get("album") or "",
        album_id=r.get("album_id"),
        track_number=r["track_number"],
        duration_seconds=(float(r["duration_seconds"])
                          if r["duration_seconds"] is not None else None),
        cover_id=r["cover_id"],
        # No bytes here to read a cover from: the album's own URL (the Cover
        # Art Archive front a held album is given) is what the mini-player
        # and Now Playing show, as the album page does — unless a local rip
        # of the album carries a cover.
        cover_url=None if r["cover_id"] else r.get("cover_url"),
    )


def items_for_hqp_ids(ids: list[int]) -> list[QueueItem]:
    """QueueItems for files held at an HQPlayer (hqp_library_files ids),
    order-preserving — the album's cover when a local rip carries one, else
    the album's cover URL."""
    if not ids:
        return []
    rows = _db_query(_HQP_ITEM_SQL + " WHERE hf.id = ANY(%(ids)s) ORDER BY array_position(%(ids)s, hf.id)",
                     {"ids": ids})
    return [_item_from_hqp_row(r) for r in rows]


def items_for_hqp_paths(paths: list[str], endpoint_id: Optional[int]) -> dict[str, QueueItem]:
    """QueueItems for files held at the HQPlayer `endpoint_id` names, keyed
    by hqp_path — the reverse mapping when adopting a playlist HQPlayer
    still holds after a backend restart."""
    if not paths or endpoint_id is None:
        return {}
    rows = _db_query(_HQP_ITEM_SQL + " WHERE hf.hqp_path = ANY(%(paths)s) "
                     "AND av.hqp_endpoint_id = %(e)s",
                     {"paths": paths, "e": endpoint_id})
    return {r["hqp_path"]: _item_from_hqp_row(r) for r in rows}


def items_for_owned_rows(rows: list[dict]) -> list[QueueItem]:
    """QueueItems for file rows from both places files live, in the rows'
    order: `id` is a media_files id when `location` is 'local' (or absent —
    a plain media row), an hqp_library_files id when 'hqplayer'
    (sql_queries.ALBUM_FILES)."""
    loc = lambda r: r.get("location") or "local"
    local = {it.media_file_id: it
             for it in items_for_media_ids([r["id"] for r in rows if loc(r) == "local"])}
    hqp_ids = [r["id"] for r in rows if loc(r) == "hqplayer"]
    held = dict(zip(hqp_ids, items_for_hqp_ids(hqp_ids)))
    out = []
    for r in rows:
        it = local.get(r["id"]) if loc(r) == "local" else held.get(r["id"])
        if it is not None:
            out.append(it)
    return out


def items_for_file_spans(spans: list[tuple]) -> dict[tuple, QueueItem]:
    """QueueItems for owned media files keyed by (file_path, cue_start) —
    the reverse mapping used when adopting an existing HQPlayer playlist on
    attach. Keyed by the span, not the path alone: the N virtual rows of a
    CUE image share one path and are distinct tracks."""
    paths = sorted({p for p, _ in spans})
    if not paths:
        return {}
    rows = _db_query(
        _MEDIA_ITEM_SQL.replace("WHERE mf.id = ANY(%(ids)s)",
                                "WHERE mf.file_path = ANY(%(ids)s)"),
        {"ids": paths})
    out: dict[tuple, QueueItem] = {}
    for r in rows:
        start = r.get("cue_start_seconds")
        out[(r["file_path"], float(start) if start is not None else None)] = \
            _item_from_media_row(r)
    return out


def item_for_proxy_token(token: str) -> Optional[QueueItem]:
    """QueueItem for a media-proxy stream. Phantom tracks become preview
    items; an OWNED m4a served through the proxy (HQPlayer transcode)
    resolves back to its owned item. None when the proxy has no metadata
    for the token (never fetched)."""
    from streaming import service as streaming_service
    proxy = streaming_service.get_proxy()
    if proxy is None:
        return None
    meta = streaming_service.preview_meta(token)
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
        album_id=meta.get("album_id"),
        duration_seconds=meta.get("duration"),
        cover_url=meta.get("cover_url"),
        preview=True,
        provider=meta.get("provider"),
        excerpt=bool(meta.get("excerpt")),
    )
