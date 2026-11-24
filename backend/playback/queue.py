"""
The Sautium-canonical play queue (HARDWARE-TIERS §2.6).

Single source of truth for what is queued, in what order, with full
display metadata. Output backends render it (local engine) or mirror it
one-way (HQPlayer's native playlist); `/api/player/playlist` serves its
`payload()` directly — no player round-trip, no cache.

`version` is bumped on every payload-visible change and embedded in each
SSE status payload as `playlist_version`, so clients refetch
deterministically. `generation` is bumped whenever a NEW queue starts
(replace / radio clear): a radio fill captures it and appends nothing to a
queue the user moved off. A streamed track is a slot from the tap
(items_for_streamed): what its row shows of its stream is told apart from
the version (playback.substitute.Lane.slots).
"""

import logging
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

from db_pool import db_query as _db_query
from hqplayer_client import file_path_to_uri
from sql_queries import HERE, best_rip_order

logger = logging.getLogger(__name__)

def gone_prefixes() -> list:
    """scanner.gone_prefixes() — the stored-path prefixes of the folders
    that do not answer now — as the play path reads it: the list is that
    module's recorded observation, so a process that never loaded the
    scanner has looked at no folder and holds none (the backend looks at
    its start, PlaybackManager.init_from_settings). Never an import here:
    the first one reads the catalogue, and this runs under the queue's
    lock."""
    scanner = sys.modules.get("scanner")
    return scanner.gone_prefixes() if scanner is not None else []


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
      {"kind": "track"} — the track itself: queued with no copy here (a
                        streamed track, items_for_streamed), or its file
                        left the library with no other copy here
                        (rebind_origins): every output opens whatever copy
                        it reaches, else a stream
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
    # A slot an automatic fill queued (radio, Play similar) — nobody tapped
    # it — played whole or not at all: it streams only from a full-length
    # link (the excerpt is never in its chain, so a slot no full link serves
    # fails and is walked past, never a 30 s clip), and one queued as a file
    # of the owner's streams at all only while his switch lets radio stream
    # (radio.streamed) — with its file not here it is otherwise a gap, its
    # file again when that opens (playback.substitute._stream_play). A fill
    # never spends a demo listen of his own album behind his back.
    whole: bool = False
    # How the ACTIVE output opens this slot when that differs from `source`
    # (playback.substitute, recomputed on every output switch): a `file` /
    # `hqp` copy the new output opens itself, a `proxy` stream that stands
    # in for a copy it cannot, `pending` while that stream is on its way,
    # `unplayable` when nothing can serve the slot here. None = as `source`
    # says. `source` is the enqueue-time origin, which no output switch
    # touches, so the way back to the output that opened it natively is
    # free; it changes only when the file it names leaves the library
    # (CanonicalQueue.rebind). A file that does not open here now — its
    # copy away, its folder not answering — is never a way in: the slot's
    # `play` is a stream until it does (CanonicalQueue.reread).
    play: Optional[dict] = None

    def opener(self) -> dict:
        """What the active output reads to open this slot."""
        return self.play or self.source

    @property
    def stream_token(self) -> Optional[str]:
        """The media proxy's token of a provider's stream this slot plays —
        queued as one, or its way in (an owned file's transcode is none):
        every reader of "is this slot a stream" asks this, never `preview`,
        which says only how the slot was queued."""
        src = self.opener()
        return src.get("token") if src.get("kind") == "proxy" and not src.get("transcode") else None


class CanonicalQueue:
    def __init__(self):
        self._lock = threading.RLock()
        self._items: list[QueueItem] = []
        self._version = 0
        self._generation = 0
        self._pending = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def version(self) -> int:
        return self._version

    def reading(self):
        """The queue's lock, for a reader whose read must not straddle a
        swap of a slot's way in (an output's layout of what it holds)."""
        return self._lock

    @property
    def generation(self) -> int:
        return self._generation

    @contextmanager
    def mutation(self):
        """A mutation in flight: the output mirrors it first and this queue
        commits it after (PlaybackManager._mutation), so until it ends the
        output's playlist and this queue differ by design — whoever compares
        the two (HQPlayer's poller) holds its verdict meanwhile."""
        with self._lock:
            self._pending += 1
        try:
            yield
        finally:
            with self._lock:
                self._pending -= 1

    def view(self) -> tuple[list[QueueItem], int, bool]:
        """The items, the version and whether a mutation is in flight, read
        together: what the output reports is judged against one state."""
        with self._lock:
            return list(self._items), self._version, self._pending > 0

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

    def slot_of(self, index: int, track_id: Optional[str]) -> Optional[int]:
        """The slot a tap on a queue sheet means: `index` while it holds the
        track the sheet showed there (`track_id`), else the slot nearest it
        that holds that track — the queue moved under a sheet not refreshed
        yet (a replace, the radio, another device) — or None once the queue
        no longer holds it. An entry with no track (None) names the slot."""
        with self._lock:
            if track_id is None:
                return index if 1 <= index <= len(self._items) else None
            slots = [s for s, it in enumerate(self._items, 1) if it.track_id == track_id]
            return min(slots, key=lambda s: (abs(s - index), s)) if slots else None

    def slots_streaming(self, token: str) -> list[int]:
        """The slots whose way in is the media proxy's stream `token`."""
        with self._lock:
            return [s for s, it in enumerate(self._items, 1)
                    if it.opener().get("kind") == "proxy" and it.opener().get("token") == token]

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
                if not any(s.get("kind") in ("proxy", "pending") and s.get("token") == token
                           for s in (it.source, it.play or {})):
                    continue
                new = (provider, excerpt,
                       duration_seconds if duration_seconds is not None else it.duration_seconds)
                if new != (it.provider, it.excerpt, it.duration_seconds):
                    first = (it.play or {}).get("kind") == "pending"
                    shown = new[1:] != (it.excerpt, it.duration_seconds)
                    it.provider, it.excerpt, it.duration_seconds = new
                    # A first landing is the `slots` message's to tell, unless
                    # it changes what the row shows (an excerpt's own length);
                    # a refetch that lands otherwise always does
                    changed = changed or not first or shown
            if changed:
                self._version += 1
            return changed

    def reresolve(self, resolve, hold: Optional[QueueItem] = None) -> int:
        """Recompute how the ACTIVE output opens every slot (`QueueItem.play`)
        — an output switch keeps the queue, and each output opens its own
        copy of a track (playback.substitute.native_plays). `resolve` maps
        the items, in order, to their `play` (None = as `source` says).
        `hold` — a slot the output plays on through this re-read (HQPlayer
        rebuilt around its position) — keeps its way in whatever was read,
        as in reread. Returns the number of slots left `pending` a stream."""
        with self._lock:
            plays = [it.play if it is hold else play
                     for it, play in zip(self._items, resolve(list(self._items)))]
            for it, play in zip(self._items, plays):
                _set_play(it, play)
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
                    _set_play(it, play)
                self._version += 1
            return moved

    def reread(self, pick, plays, hold: Optional[QueueItem] = None) -> tuple[list[QueueItem], bool]:
        """What opens here changed under the queue (PlaybackManager.
        reach_changed): the way in of every slot `pick` names is read again
        through `plays` (the active output's native_plays), the origin
        untouched. A slot whose way in is a file again keeps nothing the
        stream told of its length (an excerpt's 30 s). `hold` — the slot the
        output sounds — keeps its way in whatever was read. Returns the
        items whose way in changed, and whether `hold`'s would have; the
        version is bumped when any did."""
        with self._lock:
            chosen = [it for it in self._items if pick(it)]
            if not chosen:
                return [], False
            changed, owed = [], False
            for it, play in zip(chosen, plays(chosen)):
                if play == it.play:
                    continue
                if it is hold:
                    owed = True
                    continue
                _set_play(it, play)
                changed.append(it)
            if changed:
                _file_lengths(changed)
                self._version += 1
            return changed, owed

    def swap_play(self, item: "QueueItem", expected: Optional[dict], play: Optional[dict], *,
                  bump: bool = True, before=None) -> bool:
        """This very item's way in becomes `play` — if it still is `expected`
        (the same dict) and the item is still queued: a stream that landed, or
        failed, on a slot whose way in nothing re-read meanwhile. `bump`: a
        change the playlist shows (a landing, a failure) moves the version;
        the lane's own marks on a pending slot do not."""
        with self._lock:
            if item.play is not expected or not any(it is item for it in self._items):
                return False
            if before is not None:
                before(item)        # told under the same lock, just before (Lane._settle)
            _set_play(item, play)
            if bump:
                self._version += 1
            return True

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

    def reorder(self, order: list[QueueItem]) -> None:
        """Reorder to `order` — the queue's own items, each once, by
        identity (a track queued twice is two slots); the endpoint has
        validated the permutation against the items it read, and the
        manager applies it only to those items (apply_reorder)."""
        with self._lock:
            self._items = list(order)
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

    def payload(self, gap=None, states=None) -> dict:
        """The exact `/api/player/playlist` JSON shape the UI has always
        consumed (owned / preview / foreign row variants). `gap` — the
        active output's (PlayerBackend.gap) — names each slot that output
        does not hold; `states` — the lane's (Lane.slots) — what each
        streamed row shows of its stream, as the `slots` message says it
        after."""
        with self._lock:
            tracks = [self._row(it, idx) for idx, it in enumerate(self._items)]
            gaps = [gap(it) for it in self._items] if gap is not None else []
            version = self._version
        for row, why in zip(tracks, gaps):
            if why is not None:
                row["gap"] = why
        for st in (states() if states is not None else []):
            row = tracks[st["index"] - 1] if st["index"] <= len(tracks) else None
            if row is not None and row.get("track_id") == st["track_id"]:
                row["stream"] = {k: v for k, v in st.items()
                                 if k not in ("index", "track_id", "album_id")}
        return {"tracks": tracks, "count": len(tracks), "version": version}

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
        if item.preview or play == "proxy" or item.source.get("kind") == "track":
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


def _set_play(item: QueueItem, play: Optional[dict]) -> None:
    """The slot's way in is `play` now; one that is no provider's stream any
    more keeps nothing a stream told of itself (its provider, an excerpt's
    flag: the play tracker would count no listen of the file it opens now).
    Caller holds the queue's lock."""
    item.play = play
    if not item.preview and (play is None or play.get("kind") != "proxy" or play.get("transcode")):
        item.provider, item.excerpt = None, False


def _file_lengths(items: list[QueueItem]) -> None:
    """Each of `items` whose way in is a file of this node shows that file's
    length — a stream's landing wrote its own (CanonicalQueue.
    refresh_proxy_items). Caller holds the queue's lock."""
    by_file = {}
    for it in items:
        src = it.opener()
        if src.get("kind") == "file":
            by_file.setdefault(src.get("media_file_id") or it.media_file_id, []).append(it)
    by_file.pop(None, None)
    if not by_file:
        return
    for r in _db_query("SELECT id, duration_seconds FROM media_files "
                       "WHERE id = ANY(%(ids)s) AND duration_seconds IS NOT NULL",
                       {"ids": list(by_file)}):
        for it in by_file[r["id"]]:
            it.duration_seconds = float(r["duration_seconds"])


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
# itself is still in the catalogue. A file of a copy that is away
# (album_variants.absent_since) counts as gone while a present copy of the
# track is here: the slot moves to it — a copy that opens now first
# (sql_queries.HERE); with none, it keeps its file as its origin, and its
# way in is read as any slot's whose file does not open here now
# (playback.substitute.native_plays: a stream).
#
# The track asked about is the one the slot's FILE belongs to now (`track`),
# the slot's own `track_id` only where that file's row is gone: the canon
# rewrites a track's uuid under the queue (canon.identity._update_track_uuid
# — a rename, a merge, a split) with no event of the queue's, and a slot
# read by the uuid its enqueue saw would find no copy and no track. A slot
# whose file's track is another now takes that uuid (`track_moved`): play
# tracking keys on it, and a listen against a uuid that is gone is no row —
# the rewrite wakes this pass itself, at its commit.
_AWAY_FILE = """EXISTS (SELECT 1 FROM media_files af
                     JOIN album_variants av ON av.id = af.album_variant_id
                     WHERE af.id = {m} AND av.absent_since IS NOT NULL)
            AND EXISTS (SELECT 1 FROM media_files pf
                        JOIN album_variants pv ON pv.id = pf.album_variant_id
                        WHERE pf.track_id = s.track AND pv.absent_since IS NULL)"""

_REBIND_SQL = f"""
    WITH q AS (
        SELECT s.*,
               s.mid IS NOT NULL AND (NOT EXISTS (
                   SELECT 1 FROM media_files mf WHERE mf.id = s.mid)
                   OR {_AWAY_FILE.format(m='s.mid')}) AS origin_gone,
               s.pmid IS NOT NULL AND (NOT EXISTS (
                   SELECT 1 FROM media_files mf WHERE mf.id = s.pmid)
                   OR {_AWAY_FILE.format(m='s.pmid')}) AS play_gone
        FROM (SELECT u.*, COALESCE((SELECT mf.track_id FROM media_files mf WHERE mf.id = u.mid),
                                   u.track_id) AS track
              FROM unnest(%(mids)s::int[], %(pmids)s::int[], %(tids)s::uuid[], %(aids)s::uuid[])
                   WITH ORDINALITY AS u(mid, pmid, track_id, album_id, slot)) s
    )
    SELECT q.slot, q.origin_gone, q.track::text AS track,
           q.track IS DISTINCT FROM q.track_id AS track_moved,
           EXISTS (SELECT 1 FROM tracks t WHERE t.id = q.track) AS track_kept,
           c.id, c.file_path, c.file_format, c.duration_seconds,
           c.cue_start_seconds, c.cue_end_seconds
    FROM q
    LEFT JOIN LATERAL (
        SELECT mf.id, mf.file_path, mf.file_format::text AS file_format,
               mf.duration_seconds, mf.cue_start_seconds, mf.cue_end_seconds
        FROM media_files mf
        JOIN album_variants av ON av.id = mf.album_variant_id
        WHERE q.origin_gone AND mf.track_id = q.track
        ORDER BY (NOT {HERE}), (av.album_id = q.album_id) DESC NULLS LAST,
                 {best_rip_order('mf')}
        LIMIT 1
    ) c ON true
    WHERE q.origin_gone OR q.play_gone OR q.track IS DISTINCT FROM q.track_id
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
    is re-read. A slot whose file belongs to another track now (the canon
    rewrote the uuid) takes that track's uuid, with whatever else it
    changes. None for every other slot."""
    out: list[Optional[dict]] = [None] * len(items)
    bound = [(i, it) for i, it in enumerate(items)
             if (it.source.get("kind") == "file" and it.media_file_id is not None)
             or (it.play or {}).get("media_file_id") is not None]
    if not bound:
        return out
    rows = _db_query(_REBIND_SQL, {
        "gone": gone_prefixes(),
        "mids": [it.media_file_id if it.source.get("kind") == "file" else None
                 for _, it in bound],
        "pmids": [(it.play or {}).get("media_file_id") for _, it in bound],
        "tids": [it.track_id for _, it in bound],
        "aids": [it.album_id for _, it in bound],
    })
    for r in rows:
        i, it = bound[r["slot"] - 1]
        moved = {"track_id": r["track"]} if r["track_moved"] else {}
        if not r["origin_gone"]:
            out[i] = moved
        elif r["id"] is not None:
            change = {**moved, "media_file_id": r["id"], "source": file_source(r)}
            if r["duration_seconds"] is not None:
                change["duration_seconds"] = float(r["duration_seconds"])
            out[i] = change
        elif r["track_kept"]:
            out[i] = {**moved, "media_file_id": None, "source": {"kind": "track"}}
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


def items_for_streamed(queries: list, whole: bool = False) -> list[QueueItem]:
    """QueueItems for streamed tracks — one per TrackQuery (streaming.base),
    in order: the track itself (`{"kind": "track"}`), its way in a stream to
    come — what a tap on a not-owned album or track commits, all its slots in
    one mutation, before any provider was asked. Its way in is a stream to
    come on every output (the tap streams what has no copy here; an output
    that holds one reads it again as it mirrors, playback.substitute), which
    the fetch lane brings in the queue's order. `duration_seconds` is the
    catalog's length on the album it was queued from, else the one the
    resolve recovered for a track the catalog gives none (display only).
    `whole`: streamed from a full-length link or not at all (radio's)."""
    return [QueueItem(
        track_id=q.track_id,
        media_file_id=None,
        source={"kind": "track"},
        play={"kind": "pending"},
        title=q.title or "",
        artist=q.artist or "",
        album=q.album or "",
        album_id=q.album_id,
        duration_seconds=q.duration or resolved_durations.get(q.track_id),
        cover_url=q.cover_url,
        whole=whole,
    ) for q in queries if q.track_id]


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
