"""What the ACTIVE output opens for each queue slot, and the fetch lane's
queue side.

The canonical queue holds identities — the track uuid and where the enqueue
found the bytes (`QueueItem.source`) — and an output switch keeps it. What
changes with the output is the way in: this module recomputes, per slot,
the copy the new output opens ITSELF (`QueueItem.play`, the album page's
rule applied to the queue): a file on this node's disk for any output; the
copy in an HQPlayer's own library when that HQPlayer is the output, and
preferred there over a rip here; else a stream through the media proxy —
the same demo channel a not-owned track streams through, one full listen
then the excerpt. A streamed track queued at the tap is such a slot from
the start (`source` kind `track`, since 2026-11-22): it takes its place in
the queue at once, and its stream comes to it there.

A slot's stream is `pending` until it lands, and the lane brings it (Lane):
the media proxy's one serial fetch worker asks Lane.pick, when it is free,
what the queue wants next — in the QUEUE's order, from the slot the output
plays through LEAD slots after it — so a slot moved nearer is fetched
sooner, one moved away later, and a slot beyond the window waits. The
window's unresolved slots are resolved in one waterfall pass off the lane
(album context kept); a slot whose resolve finds nothing, or whose fetch
fails, is `unplayable` with the reason's code; a landing makes it a `proxy`
stream. The output that parked a play on the slot is told
(`PlayerBackend.slot_ready`) either way, so the play goes on — the stream,
or past the slot. A slot removed cancels its own fetch at the source, and a
replaced queue all of its own (Lane.prune). What a streamed row shows —
waiting, buffering with its progress, ready, failed with why — is read off
the slot's way in and the proxy's entry (Lane.slots) and told as one message
on the page's event stream, never as a queue version: a progress step
repaints no sheet.

HQPlayer probes an http entry when it is ADDED, so a stream it has not got
cannot sit in its playlist: that output holds the queue up to its first
pending slot after the one it plays — its horizon (playback.hqp_backend) —
and a landing there is an append. An owned file HQPlayer cannot open as it
is (an m4a, a CUE slice whose cut is not on disk yet) is pending there the
same way, and the lane prepares it in the same order.
"""

import logging
import threading
from typing import Callable, Optional

from db_pool import db_query
from playback.queue import file_source
from sql_queries import best_rip_order, owned_rank

logger = logging.getLogger(__name__)

# The origins resolved against the track's copies: the rest (a stream, a
# foreign uri) open as they are on every output.
_BY_TRACK = ("file", "hqp", "track")

# Slots after the playing one whose streams are fetched before they are
# reached, and whose bytes the RAM budget never evicts — at album pace 15–20
# minutes of music against a fetch of seconds; a queue nobody listens on past
# the window downloads nothing.
LEAD = 4

# What a failed slot says, by the code it failed with (streaming.base.Refused,
# and the host's own): the gap's words and the log's; the rows say it in the
# page's words from the same code.
REASONS = {
    "region": "not available in this region",
    "no_match": "no recording of this length",
    "no_clip": "no 30-second clip",
    "not_found": "no streaming source has it",
    "nowhere": "no streaming source has it",
    "unknown": "couldn't be fetched",
    "off": "streaming is off on this node",
}

_COPIES_SQL = f"""
    SELECT f.track_id::text AS track_id, f.id, f.location::text AS location, f.hqp_endpoint_id,
           mf.file_path, mf.file_format::text AS file_format,
           mf.cue_start_seconds, mf.cue_end_seconds,
           hf.hqp_path, hf.file_format::text AS hqp_format
    FROM (
        SELECT mf.id, mf.track_id, av.location, av.hqp_endpoint_id, av.absent_since,
               mf.is_lossless, mf.sample_rate, mf.bit_depth
        FROM media_files mf JOIN album_variants av ON av.id = mf.album_variant_id
        WHERE mf.track_id = ANY(%(ids)s::uuid[])
        UNION ALL
        SELECT hf.id, hf.track_id, av.location, av.hqp_endpoint_id, av.absent_since,
               hf.is_lossless, hf.sample_rate, hf.bit_depth
        FROM hqp_library_files hf JOIN album_variants av ON av.id = hf.album_variant_id
        WHERE hf.track_id = ANY(%(ids)s::uuid[])
    ) f
    LEFT JOIN media_files mf ON f.location = 'local' AND mf.id = f.id
    LEFT JOIN hqp_library_files hf ON f.location = 'hqplayer' AND hf.id = f.id
    ORDER BY f.track_id, {owned_rank('f')}, {best_rip_order('f')}
"""


def _file_play(r: dict) -> dict:
    return {**file_source(r), "media_file_id": r["id"]}


def _stream_play(item) -> dict:
    """A slot that only a stream serves: the stream the lane brings it — on
    its way, landed, or found it could not — kept across every re-read of
    the way in (a mirroring, an output switch: a stream is every output's),
    else a stream to come."""
    play = item.play or {}
    if play.get("lane") or (play.get("kind") == "pending" and not play.get("prepare")):
        return play
    return {"kind": "pending"}


def _prepared(item, play: Optional[dict]) -> Optional[dict]:
    """On HQPlayer: an owned file it cannot open as it is — an m4a (it reads
    no MP4 container) when streaming is up to transcode it, a CUE slice whose
    cut is not on disk yet — is pending a preparation of the lane, in the
    queue's order; the one the lane made is kept. Any other way in as
    `play` says (None: as `source`)."""
    src = play if play is not None else item.source
    if src.get("kind") != "file":
        return play
    from streaming.local import TRANSCODE_FORMATS
    held = item.play or {}
    same = held.get("file", held).get("path") == src.get("path")
    if (src.get("format") or "").upper() in TRANSCODE_FORMATS:
        from streaming import service as streaming_service
        if not streaming_service.is_enabled():
            return play                 # nothing transcodes it: the file, as it was
        if same and (held.get("transcode") or held.get("transcode_failed")
                     or held.get("prepare") == "transcode"):
            return held
        return {"kind": "pending", "prepare": "transcode",
                "file": {**src, "media_file_id": src.get("media_file_id", item.media_file_id)}}
    if src.get("cue_start") is not None:
        from config import settings
        from streaming import transcode
        if same and (held.get("cut") or held.get("prepare") == "cut"):
            return held
        if transcode.flac_slice_cached(settings.translate_to_local_path(src["path"]),
                                       src["cue_start"], src.get("cue_end")):
            return play
        return {"kind": "pending", "prepare": "cut", "file": src}
    return play


def native_plays(items: list, output_id: Optional[str],
                 endpoint_id: Optional[int]) -> list:
    """Per item, in order: how this output opens it when that is not what
    `source` says — a `file` / `hqp` copy it opens itself, a stream when
    only a stream can serve it (`pending` until it lands, the stream the
    lane brought it once it has) — or None to open `source` as is. Streams
    and foreign uris are as they are on every output; owned items are
    looked up once for the whole queue, every copy of every track in one
    query."""
    owned = [it for it in items
             if it.track_id and it.source.get("kind") in _BY_TRACK]
    by_track: dict = {}
    if owned:
        for r in db_query(_COPIES_SQL, {"ids": sorted({it.track_id for it in owned}),
                                        "hqp_id": endpoint_id}):
            by_track.setdefault(r["track_id"], []).append(r)
    out = []
    for it in items:
        src = it.source
        kind = src.get("kind")
        if kind not in _BY_TRACK or not it.track_id:
            out.append(None)
            continue
        rows = by_track.get(it.track_id, [])
        local = next((r for r in rows if r["location"] == "local"), None)
        if output_id == "hqplayer":
            own = next((r for r in rows if r["location"] == "hqplayer"
                        and r["hqp_endpoint_id"] == endpoint_id), None)
            if kind == "hqp" and src.get("endpoint") == endpoint_id:
                out.append(None)          # its own held file, as queued
            elif own is not None:
                # this HQPlayer's copy outranks a rip here, as on the album page
                out.append({"kind": "hqp", "path": own["hqp_path"], "format": own["hqp_format"],
                            "endpoint": endpoint_id})
            elif kind == "file":
                out.append(_prepared(it, None))
            elif local is not None:
                out.append(_prepared(it, _file_play(local)))
            else:
                # held at another HQPlayer, its file gone, or never one here:
                # streamed on this HQPlayer too, its playlist ending before it
                # until it lands (hqp_backend's horizon)
                out.append(_stream_play(it))
        else:
            if kind == "file":
                out.append(None)
            elif local is not None:
                out.append(_file_play(local))
            else:
                out.append(_stream_play(it))
    return out


def streamed(item) -> bool:
    """A slot whose row shows a stream's state: a not-owned track the lane
    brings it (an owned file's preparation shows none)."""
    if item.media_file_id is not None or item.source.get("kind") == "file":
        return False
    play = item.opener()
    kind = play.get("kind")
    return kind in ("pending", "proxy") or (kind == "unplayable" and bool(play.get("lane")))


class Lane:
    """The fetch lane's queue side: what the proxy's one worker takes next
    (pick), the window's resolve, what landed or failed on which slot, the
    tokens nobody wants any more (prune), and what each streamed row shows
    (slots). `notify(index)` reaches the active output (slot_ready);
    `anchor()` is the slot the output plays or Play would start; `changed()`
    says the rows' states moved; `failed(item, index, code)` hears a slot
    that failed."""

    def __init__(self, queue, notify: Callable[[int], None], anchor: Callable[[], int],
                 changed: Callable[[], None], failed: Callable[[object, int, str], None]):
        self._queue = queue
        self._notify = notify
        self._anchor = anchor
        self._changed = changed
        self._on_failed = failed
        self._lock = threading.Lock()
        self._minted: set = set()       # the tokens this lane minted, while a slot may name them
        self._proxy = None

    def attach(self, proxy) -> None:
        """The media proxy this lane feeds: its worker asks pick, its fetches
        tell landed / failed / their progress — before any other hook, so the
        slot streams by the time an output hears of the landing."""
        if self._proxy is proxy:
            return
        self._proxy = proxy
        proxy.picker = self.pick
        proxy.track_ready_hooks.insert(0, self._landed)
        proxy.track_failed_hooks.insert(0, self._failed)
        proxy.state_hooks.append(lambda e: self._changed())

    # -- what the lane takes next ------------------------------------------

    def _window(self, items: list) -> list:
        if not items:
            return []
        anchor = max(1, min(self._anchor(), len(items)))
        return [(s, items[s - 1]) for s in range(anchor, min(len(items), anchor + LEAD) + 1)]

    def maintain(self) -> None:
        """The queue or the playhead moved: the window's unresolved slots are
        resolved (off the lane, one waterfall pass), and the lane, if idle,
        looks again. Cheap on the status thread: a snapshot of LEAD slots."""
        proxy = self._proxy
        if proxy is None:
            return
        todo = []
        for _s, it in self._window(self._queue.snapshot()):
            play = it.play
            if (play is not None and play.get("kind") == "pending" and not play.get("prepare")
                    and not play.get("token") and not play.get("resolving")):
                marked = {"kind": "pending", "resolving": True}
                if self._queue.swap_play(it, play, marked, bump=False):
                    todo.append((it, marked))
        if todo:
            threading.Thread(target=self._resolve, args=(todo,), daemon=True,
                             name="queue-resolve").start()
        proxy.wake()

    def pick(self):
        """The proxy worker's next job, in the queue's order from the playing
        slot through the window: a token to fetch (a stream resolved, a
        transcode to make, a landed stream whose bytes the budget dropped),
        a CUE cut to make, or None."""
        proxy = self._proxy
        for _s, it in self._window(self._queue.snapshot()):
            play = it.play or {}
            kind = play.get("kind")
            tok = play.get("token")
            if kind == "pending" and play.get("prepare") == "transcode" and not tok:
                tok = self._mint_transcode(it, play)
            if kind in ("pending", "proxy") and tok and proxy.wanted(tok):
                return tok
            if kind == "pending" and play.get("prepare") == "cut" and not play.get("cutting"):
                marked = {**play, "cutting": True}
                if self._queue.swap_play(it, play, marked, bump=False):
                    return lambda it=it, marked=marked: self._cut(it, marked)
            if kind is None and it.source.get("kind") == "proxy" and proxy.wanted(it.source["token"]):
                return it.source["token"]
        return None

    def _mint_transcode(self, item, play: dict) -> Optional[str]:
        """The token of an owned m4a's transcode for HQPlayer, minted at its
        turn (streaming.local)."""
        from config import settings
        from streaming.base import TrackQuery
        src = play["file"]
        # artist/title are unused for display here: the playlist renders the
        # slot from its media_file_id, an owned row
        q = TrackQuery(artist="", title="", media_file_id=src.get("media_file_id"))
        tok = self._proxy.mint(q, [(_local_transcoder(),
                                    settings.translate_to_local_path(src["path"]))])
        new = {**play, "token": tok, "transcode": True}
        if not self._queue.swap_play(item, play, new, bump=False):
            self._proxy.cancel(tok)
            return None
        with self._lock:
            self._minted.add(tok)
        return tok

    def _cut(self, item, play: dict) -> None:
        """Make a CUE slice's cut for HQPlayer, in the lane (a whole album's
        cuts never hold a mirror up): handed as the file once it is on disk —
        or as the whole image, said, where it cannot be made
        (hqp_backend._owned_play_uri)."""
        from config import settings
        from streaming import transcode
        src = play["file"]
        try:
            transcode.flac_slice_path_for_file(
                settings.translate_to_local_path(src["path"]), src["cue_start"], src.get("cue_end"),
                {"title": item.title, "artist": item.artist, "album": item.album,
                 "track": item.track_number})
        except Exception as e:
            logger.error("cue slice for %s [%s, %s) not made — HQPlayer gets the whole image: %s",
                         src["path"], src.get("cue_start"), src.get("cue_end"), e)
        self._settle(item, play, {**src, "cut": True, "lane": True})

    # -- the window's resolve ------------------------------------------------

    def _resolve(self, todo: list) -> None:
        try:
            self._resolve_now(todo)
        except Exception:
            # A resolve that broke (the database away, a provider's bug) says
            # nothing of the tracks: the slots are to come again, and the next
            # look at the window resolves them anew
            logger.exception("the resolve of %d queued slot(s) failed", len(todo))
            for it, marked in todo:
                self._queue.swap_play(it, marked, {"kind": "pending"}, bump=False)

    def _resolve_now(self, todo: list) -> None:
        from streaming import service as streaming_service
        # The resolve and the query builders live with the play endpoints.
        from routers.player import _phantom_track_query, _refused_code, _resolve_waterfall
        if not streaming_service.is_enabled() or not streaming_service.providers_preferred():
            for it, marked in todo:
                self._fail(it, marked, "off")
            return
        proxy = self._proxy
        queries, slots = [], []
        for it, marked in todo:
            q = _phantom_track_query(it.track_id, it.album_id)
            if q is None:
                self._fail(it, marked, "nowhere")
                continue
            queries.append(q)
            slots.append((it, marked))
        if not queries:
            return
        chains = _resolve_waterfall(queries)
        by_track: dict = {}
        for (it, marked), q, chain in zip(slots, queries, chains):
            if not chain:
                self._fail(it, marked, _refused_code(it.track_id) or "nowhere")
                continue
            # A track queued twice waits for one fetch
            tok = by_track.get(it.track_id)
            if tok is None:
                tok = by_track[it.track_id] = proxy.mint(q, chain)
                with self._lock:
                    self._minted.add(tok)
            new = {"kind": "pending", "token": tok}
            if not self._queue.swap_play(it, marked, new, bump=False):
                continue            # the slot left the queue, or its way in changed
            if (proxy.state(tok) or ("",))[0] == "ready":
                # In RAM already (a re-queued album): it lands now
                self._settle(it, new, {"kind": "proxy", "token": tok, "lane": True})
        self.prune()
        self._changed()
        proxy.wake()

    # -- what the proxy tells ---------------------------------------------

    def _landed(self, e) -> None:
        """The proxy's track-ready hook: every slot naming this token is told
        what landed — the provider that served it, an excerpt's own length (a
        refetch may land on another provider, or as an excerpt: an output
        reads the item at load) — then every slot pending it streams it now,
        and an output parked on one goes."""
        excerpt = bool(e.audio.excerpt)
        self._queue.refresh_proxy_items(
            e.token, provider=e.provider.manifest.id if e.provider else None, excerpt=excerpt,
            duration_seconds=(e.audio.seconds if excerpt and e.audio.seconds else e.query.duration))
        for it, play in self._pending_on(e.token):
            land = {"kind": "proxy", "token": e.token, "lane": True}
            if play.get("transcode"):
                land.update(transcode=True, file=play["file"])
            self._settle(it, play, land)

    def _failed(self, e) -> None:
        """The proxy's track-failed hook: every slot pending this token is
        unplayable here, with the code its fetch failed with."""
        for it, play in self._pending_on(e.token):
            if play.get("transcode"):
                # An m4a HQPlayer cannot have transcoded: the file itself, as
                # before a transcode existed (it says why it does not open it)
                self._settle(it, play, {**play["file"], "lane": True, "transcode_failed": True})
            else:
                self._fail(it, play, e.code or "unknown")

    def _pending_on(self, token: str) -> list:
        return [(it, it.play) for it in self._queue.snapshot()
                if (it.play or {}).get("kind") == "pending" and it.play.get("token") == token]

    def _fail(self, item, expected: dict, code: str) -> None:
        index = self._settle(item, expected, {"kind": "unplayable", "reason": REASONS[code],
                                              "code": code, "lane": True})
        if index is not None:
            self._on_failed(item, index, code)

    def _settle(self, item, expected: dict, play: dict) -> Optional[int]:
        """The slot's way in is `play` now — if it still was `expected`: the
        output that parked on it hears so (slot_ready). Its 1-based slot, or
        None when it left the queue or moved on meanwhile."""
        if not self._queue.swap_play(item, expected, play):
            return None
        index = self._queue.index_of(item)
        if index is None:
            return None
        if play["kind"] == "unplayable":
            logger.info("queue slot %d unplayable here: %s", index, play["reason"])
        else:
            logger.debug("queue slot %d ready — %s — %s", index, item.artist, item.title)
        self._changed()
        self._notify(index)
        return index

    # -- tokens nobody wants -----------------------------------------------

    def prune(self) -> None:
        """Cancel the fetch of every token this lane minted that no slot names
        any more — a removed slot's, a replaced queue's — at the source; bytes
        in hand stay for the budget, which takes an orphan first."""
        proxy = self._proxy
        if proxy is None:
            return
        named = set()
        for it in self._queue.snapshot():
            for d in (it.source, it.play or {}):
                if d.get("token"):
                    named.add(d["token"])
        with self._lock:
            gone = self._minted - named
            self._minted &= named
        for tok in gone:
            proxy.cancel(tok)

    # -- what the rows show ------------------------------------------------

    def slots(self) -> list:
        """Every streamed slot's state, in queue order: {index, track_id,
        album_id (the album it was queued from), state, progress?, reason?} — `waiting` (its fetch not begun: beyond the
        window, behind another in the lane, unresolved, its bytes dropped by
        the budget), `buffering` (its fetch runs; progress where the provider
        gives it), `ready`, `failed` (reason: the code). "Found in the
        catalog" is waiting, never buffering."""
        proxy = self._proxy
        out = []
        for index, it in enumerate(self._queue.snapshot(), 1):
            if not streamed(it):
                continue
            src = it.opener()
            row = {"index": index, "track_id": it.track_id, "album_id": it.album_id}
            if src.get("kind") == "unplayable":
                row.update(state="failed", reason=src.get("code") or "unknown")
            else:
                tok = src.get("token")
                st = proxy.state(tok) if (proxy is not None and tok) else None
                if st is None:
                    row["state"] = "waiting"
                else:
                    state, progress, code = st
                    row["state"] = state
                    if progress is not None:
                        row["progress"] = round(progress, 1)
                    if state == "failed":
                        row["reason"] = code or "unknown"
            out.append(row)
        return out


_transcoder = None


def _local_transcoder():
    """One local transcode provider per process (it holds no state)."""
    global _transcoder
    if _transcoder is None:
        from streaming.local import LocalTranscodeProvider
        _transcoder = LocalTranscodeProvider()
    return _transcoder
