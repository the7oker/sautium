"""What the ACTIVE output opens for each queue slot.

The canonical queue holds identities — the track uuid and where the enqueue
found the bytes (`QueueItem.source`) — and an output switch keeps it. What
changes with the output is the way in: this module recomputes, per slot,
the copy the new output opens ITSELF (`QueueItem.play`, the album page's
rule applied to the queue): a file on this node's disk for any output; the
copy in an HQPlayer's own library when that HQPlayer is the output, and
preferred there over a rip here; else a stream through the media proxy,
which stands in for a copy the output cannot reach — the same demo channel
a not-owned track streams through, one full listen then the excerpt.

Streams are resolved LAZILY, a lead window ahead of the playhead
(`Substitutes.maintain` runs on every status tick and right after the
switch), submitted to the proxy front of the fetch queue, and land on the
slot as `play` when buffered; the output that parked a play on that slot is
told (`PlayerBackend.slot_ready`). Nothing is fetched for a queue nobody
listens on past the window, and a queue that moves on retires its tokens
through the proxy's generation binding.

The HQPlayer output mirrors its playlist one entry per slot and HQPlayer
fetches an http entry at ADD time, so a stream it has not buffered cannot
sit in that playlist: on that output only native copies are resolved, and a
slot only a stream could serve (a file held at another HQPlayer with no rip
here) stays unplayable there, named as such.

A slot whose file left the library with no other copy here (`source` kind
`track`, playback.queue.rebind_origins) has no copy of its own: every output
resolves it as it resolves a copy held elsewhere.
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

# Slots ahead of the playhead whose streams are fetched before they are
# reached — enough to absorb a provider's resolve and download at album
# pace, few enough that a queue nobody listens on costs nothing.
LEAD = 3

_COPIES_SQL = f"""
    SELECT f.track_id::text AS track_id, f.id, f.location::text AS location, f.hqp_endpoint_id,
           mf.file_path, mf.file_format::text AS file_format,
           mf.cue_start_seconds, mf.cue_end_seconds,
           hf.hqp_path, hf.file_format::text AS hqp_format
    FROM (
        SELECT mf.id, mf.track_id, av.location, av.hqp_endpoint_id,
               mf.is_lossless, mf.sample_rate, mf.bit_depth
        FROM media_files mf JOIN album_variants av ON av.id = mf.album_variant_id
        WHERE mf.track_id = ANY(%(ids)s::uuid[])
        UNION ALL
        SELECT hf.id, hf.track_id, av.location, av.hqp_endpoint_id,
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


def native_plays(items: list, output_id: Optional[str],
                 endpoint_id: Optional[int]) -> list:
    """Per item, in order: how this output opens it when that is not what
    `source` says — a `file` / `hqp` copy it opens itself, `pending` when
    only a stream can serve it — or None to open `source` as is. Streams and
    foreign uris are as they are on every output; owned items are looked up
    once for the whole queue, every copy of every track in one query."""
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
                out.append(None)
            elif local is not None:
                out.append(_file_play(local))
            else:
                out.append({"kind": "unplayable",
                            "reason": ("held in another HQPlayer's library" if kind == "hqp"
                                       else "its file left the library")})
        else:
            if kind == "file":
                out.append(None)
            elif local is not None:
                out.append(_file_play(local))
            else:
                out.append({"kind": "pending"})
    return out


class Substitutes:
    """The streams that stand in for copies the active output cannot reach —
    resolved a lead window ahead of the playhead, landed on their slots as
    they buffer. `notify(index)` reaches the active output (slot_ready)."""

    def __init__(self, queue, notify: Callable[[int], None]):
        self._queue = queue
        self._notify = notify
        self._lock = threading.Lock()
        self._epoch = 0           # bumped on every output switch: an older fetch lands nothing
        self._submitted: set = set()

    def reset(self) -> None:
        with self._lock:
            self._epoch += 1
            self._submitted.clear()

    def maintain(self, idx) -> None:
        """Submit the pending slots within LEAD of the playhead that no
        fetch holds yet. Cheap on the status thread: a snapshot and a set
        lookup; the work runs on its own thread."""
        anchor = idx if isinstance(idx, int) and idx >= 1 else 1
        with self._lock:
            epoch = self._epoch
            window = [(i + 1, it) for i, it in enumerate(self._queue.snapshot())
                      if anchor <= i + 1 <= anchor + LEAD
                      and (it.play or {}).get("kind") == "pending"
                      and id(it) not in self._submitted]
            if not window:
                return
            for _, it in window:
                self._submitted.add(id(it))
        threading.Thread(target=self._fetch, args=(window, epoch, self._queue.generation),
                         daemon=True, name="queue-substitutes").start()

    def _fetch(self, window: list, epoch: int, generation: int) -> None:
        from streaming import service as streaming_service
        # The resolve and the query builders live with the play endpoints.
        from routers.player import _phantom_track_query, _resolve_waterfall
        if not streaming_service.is_enabled() or not streaming_service.providers_preferred():
            for _, it in window:
                self._settle(it, {"kind": "unplayable", "reason": "streaming is off on this node"}, epoch)
            return
        proxy = streaming_service.get_proxy()
        queries, slots = [], []
        for _, it in window:
            q = _phantom_track_query(it.track_id, it.album_id)
            if q is None:
                self._settle(it, {"kind": "unplayable", "reason": "nothing to stream it from"}, epoch)
                continue
            queries.append(q)
            slots.append(it)
        if not queries:
            return
        chains = _resolve_waterfall(queries)
        pairs, waiting = [], []
        for it, q, chain in zip(slots, queries, chains):
            if not chain:
                self._settle(it, {"kind": "unplayable", "reason": "no provider has it"}, epoch)
            else:
                pairs.append((q, chain))
                waiting.append(it)
        if not pairs:
            return
        tokens = proxy.add_tracks(pairs, front=True)
        proxy.bind(tokens, generation)
        for it, tok in zip(waiting, tokens):
            try:
                e = proxy.wait_ready(tok, timeout=None)
            except KeyError:
                return            # retired: the queue moved on
            if e.audio is None:
                self._settle(it, {"kind": "unplayable", "reason": "the stream could not be fetched"}, epoch)
                continue
            self._settle(it, {"kind": "proxy", "token": tok}, epoch)

    def _settle(self, item, play: dict, epoch: int) -> None:
        with self._lock:
            if epoch != self._epoch:
                return                # another output since: its own resolve rules
        index = self._queue.set_play(item, play)
        if index is None:
            return
        if play["kind"] == "proxy":
            logger.info("queue slot %d streams %s — %s", index, item.artist, item.title)
            self._notify(index)
        else:
            logger.info("queue slot %d unplayable here: %s (%s — %s)", index,
                        play["reason"], item.artist, item.title)
