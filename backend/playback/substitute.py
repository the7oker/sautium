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
from playback.queue import file_source, gone_prefixes
from sql_queries import HERE, best_rip_order, owned_rank

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
    "excerpt_only": "only a 30-second excerpt streams here",
    "unknown": "couldn't be fetched",
    "off": "streaming is off on this node",
    "not_here": "its file is not here now",
}

# Every copy of the tracks asked about: the tracks named (`ids`) and the ones
# the files named (`mids`) belong to NOW — a file slot is read by what it is,
# its media_files row: the canon rewrites a track's uuid under a queue that
# still holds the old one (canon.identity._update_track_uuid).
_COPIES_SQL = f"""
    WITH t AS (
        SELECT unnest(%(ids)s::uuid[]) AS id
        UNION
        SELECT o.track_id FROM media_files o WHERE o.id = ANY(%(mids)s::int[])
    )
    SELECT f.track_id::text AS track_id, f.id, f.location::text AS location, f.hqp_endpoint_id,
           f.here,
           mf.file_path, mf.file_format::text AS file_format,
           mf.cue_start_seconds, mf.cue_end_seconds,
           hf.hqp_path, hf.file_format::text AS hqp_format
    FROM (
        SELECT mf.id, mf.track_id, av.location, av.hqp_endpoint_id, {HERE} AS here,
               mf.is_lossless, mf.sample_rate, mf.bit_depth
        FROM media_files mf JOIN album_variants av ON av.id = mf.album_variant_id
        WHERE mf.track_id = ANY(ARRAY(SELECT id FROM t))
        UNION ALL
        SELECT hf.id, hf.track_id, av.location, av.hqp_endpoint_id, true,
               hf.is_lossless, hf.sample_rate, hf.bit_depth
        FROM hqp_library_files hf JOIN album_variants av ON av.id = hf.album_variant_id
        WHERE hf.track_id = ANY(ARRAY(SELECT id FROM t))
    ) f
    LEFT JOIN media_files mf ON f.location = 'local' AND mf.id = f.id
    LEFT JOIN hqp_library_files hf ON f.location = 'hqplayer' AND hf.id = f.id
    ORDER BY f.track_id, {owned_rank('f')}, {best_rip_order('f')}
"""


def by_copies(item) -> bool:
    """A slot whose way in is read against its track's copies
    (native_plays)."""
    return bool(item.track_id) and item.source.get("kind") in _BY_TRACK


def streams() -> bool:
    """Whether a slot with no file here can stream on this node now:
    streaming is up and a provider stands in its order — what a play route
    asks before it queues a stream."""
    from streaming import service as streaming_service
    return streaming_service.is_enabled() and bool(streaming_service.providers_preferred())


def _file_play(r: dict) -> dict:
    return {**file_source(r), "media_file_id": r["id"]}


def _fill_streams() -> bool:
    """Whether an automatic fill's slot may stream: the owner's switch
    (radio.streamed), while streaming is up — radio's own rule, read where
    radio reads it."""
    from routers.player import _radio_streams
    return _radio_streams()


def _stream_play(item, fill_streams: Callable[[], bool] = _fill_streams) -> dict:
    """A slot that only a stream serves: the stream the lane brings it — on
    its way, landed, or found it could not — kept across every re-read of
    the way in (a mirroring, an output switch: a stream is every output's),
    else a stream to come.

    THE one place a slot becomes a stream by a read of its way in, so the
    one place that asks who queued it: a slot an automatic fill queued as
    the owner's own copy (`whole`, its origin a file or an HQPlayer's) is
    no stream for want of that copy unless radio may stream
    (`fill_streams`: his switch) — and then whole or not at all, the lane's
    rule for every `whole` slot. Otherwise it is a gap of its slot — not
    here, said on its row, walked past — until a read finds its copy."""
    play = item.play or {}
    if item.whole and item.source.get("kind") != "track" and not fill_streams():
        return {"kind": "unplayable", "reason": REASONS["not_here"], "code": "not_here",
                "lane": True}
    if play.get("code") in ("off", "not_here"):
        return {"kind": "pending"}      # streaming was off, or not a fill's to use: a re-read asks again
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
        # The cut is the lane's, which runs on the media proxy's worker: a
        # node with streaming off, or HQPlayer on this machine, may have
        # started no proxy yet
        from streaming import service as streaming_service
        try:
            streaming_service.ensure_proxy()
        except RuntimeError as e:
            logger.warning("no media proxy to cut CUE slices in the lane (%s): HQPlayer is "
                           "handed each cut as the mirror makes it", e)
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
    query.

    A copy that is not here now (sql_queries.HERE: away, or under a folder
    that does not answer) is never a way in: a slot whose own file is one
    takes the best copy that is here, else streams — its origin (`source`)
    untouched, so the same read gives None again once the file opens.

    A slot that is a file is read by its file (`media_file_id`), and its
    other copies are those of the track that file belongs to now: the
    slot's `track_id` is what the enqueue read, and the canon may have
    rewritten the track's uuid since."""
    owned = [it for it in items if by_copies(it)]
    by_track: dict = {}
    track_of: dict = {}             # a slot's own file -> the track it belongs to now
    if owned:
        mids = sorted({it.media_file_id for it in owned
                       if it.source.get("kind") == "file" and it.media_file_id is not None})
        for r in db_query(_COPIES_SQL, {"ids": sorted({it.track_id for it in owned}), "mids": mids,
                                        "hqp_id": endpoint_id, "gone": gone_prefixes()}):
            if r["location"] == "local":
                track_of[r["id"]] = r["track_id"]
            if r["here"]:
                by_track.setdefault(r["track_id"], []).append(r)
    fills: list = []

    def fill_streams() -> bool:
        """The owner's switch, read once for the queue and only when a
        fill's slot asks."""
        if not fills:
            fills.append(_fill_streams())
        return fills[0]

    out = []
    for it in items:
        src = it.source
        kind = src.get("kind")
        if not by_copies(it):
            out.append(None)
            continue
        track = (track_of.get(it.media_file_id) if kind == "file" else None) or it.track_id
        rows = by_track.get(track, [])
        local = next((r for r in rows if r["location"] == "local"), None)
        # the slot's own file, where that opens now
        own_file = kind == "file" and any(
            r["location"] == "local" and r["id"] == it.media_file_id for r in rows)
        if output_id == "hqplayer":
            own = next((r for r in rows if r["location"] == "hqplayer"
                        and r["hqp_endpoint_id"] == endpoint_id), None)
            if kind == "hqp" and src.get("endpoint") == endpoint_id:
                out.append(None)          # its own held file, as queued
            elif own is not None:
                # this HQPlayer's copy outranks a rip here, as on the album page
                out.append({"kind": "hqp", "path": own["hqp_path"], "format": own["hqp_format"],
                            "endpoint": endpoint_id})
            elif own_file:
                out.append(_prepared(it, None))
            elif local is not None:
                out.append(_prepared(it, _file_play(local)))
            else:
                # held at another HQPlayer, no file of it here now, or never
                # one here: streamed on this HQPlayer too, its playlist
                # ending before it until it lands (hqp_backend's horizon)
                out.append(_stream_play(it, fill_streams))
        else:
            if own_file:
                out.append(None)
            elif local is not None:
                out.append(_file_play(local))
            else:
                out.append(_stream_play(it, fill_streams))
    return out


def streamed(item) -> bool:
    """A slot whose row shows a stream's state: its way in is a stream the
    lane brings it — a not-owned track, or an owned one with no file here
    now (an owned file's preparation for HQPlayer shows none)."""
    play = item.opener()
    if play.get("prepare") or play.get("transcode") or play.get("cut") or play.get("kind") == "file":
        return False                # a copy here, prepared for HQPlayer: no stream
    kind = play.get("kind")
    return kind in ("pending", "proxy") or (kind == "unplayable" and bool(play.get("lane")))


class Lane:
    """The fetch lane's queue side: what the proxy's one worker takes next
    (pick), the window's resolve, what landed or failed on which slot, the
    tokens nobody wants any more (prune), and what each streamed row shows
    (slots). `notify(item)` reaches the active output (slot_ready);
    `anchor()` is the slot the output plays or Play would start; `changed()`
    says the rows' states moved; `failed(item, index, code)` hears a slot
    that failed; `arriving(item)` is told just before a slot's stream (or
    preparation) becomes its way in — an output that holds the queue only
    as it was handed it (HQPlayer) marks one it does not hold before any
    reader can count it."""

    def __init__(self, queue, notify: Callable[[object], None], anchor: Callable[[], int],
                 changed: Callable[[], None], failed: Callable[[object, int, str], None],
                 arriving: Callable[[object], None] = lambda item: None):
        self._queue = queue
        self._notify = notify
        self._arriving = arriving
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
        from streaming import service as streaming_service
        up = streaming_service.is_enabled()
        if up and (proxy is None or not streaming_service.providers_preferred()):
            # every provider aside or cooling down for now: the window waits,
            # asked again at the next look (the status tick)
            if proxy is not None:
                proxy.wake()
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
            # Off the status thread, as every settle is: with streaming off
            # the resolve says so on the rows ("off", never kept: a re-read of
            # the way in, or streaming coming up — revive — asks again)
            threading.Thread(target=self._resolve, args=(todo,), daemon=True,
                             name="queue-resolve").start()
        if proxy is not None:
            proxy.wake()

    def revive(self) -> None:
        """Streaming came up (streaming.service): the slots it failed as off
        are to come again, and the window looks anew."""
        for it in self._queue.snapshot():
            play = it.play
            if play is not None and play.get("code") == "off":
                self._queue.swap_play(it, play, {"kind": "pending"})
        self._changed()
        self.maintain()

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
        with self._lock:
            swapped = self._queue.swap_play(item, play, new, bump=False)
            if swapped:
                self._minted.add(tok)
        if not swapped:
            self._proxy.cancel(tok)
            return None
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
        if not streaming_service.is_enabled():
            for it, marked in todo:
                self._fail(it, marked, "off")
            if streaming_service.is_enabled():
                self.revive()       # it came up as these failed: revive skipped them
            return
        if not streaming_service.providers_preferred():
            # every provider aside since the look that marked them: to come again
            for it, marked in todo:
                self._queue.swap_play(it, marked, {"kind": "pending"}, bump=False)
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
        named: set = set()
        for (it, marked), q, chain in zip(slots, queries, chains):
            if not chain:
                self._fail(it, marked, _refused_code(it.track_id) or "nowhere")
                continue
            if it.whole:
                # Radio's: a full-length link or nothing — a demo listen spent
                # since the batch was built leaves the excerpt alone, and that
                # is no way in for this slot
                chain = [(p, sid) for p, sid in chain if not p.manifest.excerpt]
                if not chain:
                    self._fail(it, marked, "excerpt_only")
                    continue
            # A track queued twice waits for one fetch — of the same chain
            key = (it.track_id, it.whole)
            tok = by_track.get(key)
            if tok is None:
                tok = by_track[key] = proxy.mint(q, chain)
            # Named and kept under the lane's lock: a prune between the two
            # would cancel a token a slot names
            with self._lock:
                if self._queue.swap_play(it, marked, {"kind": "pending", "token": tok}, bump=False):
                    named.add(tok)
                    self._minted.add(tok)
        for tok in set(by_track.values()) - named:
            proxy.cancel(tok)       # every slot it was for left, or its way in changed
        for tok in named:
            e = proxy._peek(tok)
            if e is not None and e.ready.is_set() and e.audio is not None:
                # In RAM already (a re-queued album): it lands now, told as
                # any landing is — its provider, an excerpt's own length
                self._landed(e)
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
        if not e.query.media_file_id:
            # an owned file's transcode tells the slot nothing: it is the file
            excerpt = bool(e.audio.excerpt)
            self._queue.refresh_proxy_items(
                e.token, provider=e.provider.manifest.id if e.provider else None, excerpt=excerpt,
                duration_seconds=(e.audio.seconds if excerpt and e.audio.seconds
                                  else e.query.duration))     # None keeps the slot's own
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
        # The output's mark (`arriving`) and the swap are one step under the
        # queue's lock: no reader — an HQPlayer mirror, a canary tick — sees
        # the slot landed and unmarked. The row's state is the `slots`
        # message's, never a queue version: a landing repaints no sheet
        before = self._arriving if play["kind"] != "unplayable" else None
        if not self._queue.swap_play(item, expected, play, bump=False, before=before):
            return None
        index = self._queue.index_of(item)
        if index is None:
            return None
        if play["kind"] == "unplayable":
            logger.info("queue slot %d unplayable here: %s", index, play["reason"])
        else:
            logger.debug("queue slot %d ready — %s — %s", index, item.artist, item.title)
        self._notify(item)
        self._changed()
        return index

    # -- tokens nobody wants -----------------------------------------------

    def prune(self) -> None:
        """Cancel the fetch of every token this lane minted that no slot names
        any more — a removed slot's, a replaced queue's — at the source; bytes
        in hand stay for the budget, which takes an orphan first."""
        proxy = self._proxy
        if proxy is None:
            return
        # The lane's lock across the read and the subtraction: a resolve names
        # its tokens on their slots and keeps them under the same lock
        with self._lock:
            named = set()
            for it in self._queue.snapshot():
                for d in (it.source, it.play or {}):
                    if d.get("token"):
                        named.add(d["token"])
            gone = self._minted - named
            self._minted &= named
        for tok in gone:
            proxy.cancel(tok)

    # -- what the rows show ------------------------------------------------

    def slots(self) -> list:
        """Every streamed slot's state, in queue order: {index, track_id,
        album_id (the album it was queued from), whole? (radio's: never an excerpt),
        state, progress?, reason?} — `waiting` (its fetch not begun: beyond the
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
            if it.whole:
                row["whole"] = True
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
