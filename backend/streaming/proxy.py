"""In-memory media proxy: serves provider-fetched audio to HQPlayer over plain
http (HQPlayer can't sign HMAC, so this is a SEPARATE plain-http endpoint,
localhost/LAN-reachable, gated by per-track tokens), and owned files by
token to any output that pulls bytes over the network — a DLNA renderer,
an HQPlayer on another machine.

Design points proven during bring-up:
- HQPlayer plays http FLAC sustained in its NATIVE playlist (alongside file://
  owned tracks) — so a preview track is just one more playlist URI.
- A provider fetch takes seconds (search + download + transcode), so we PREFETCH
  the next track off the request path; HQPlayer's GET then hits a ready buffer.
- ONE fetch lane, serial, with no list of its own: when it is free it takes an
  on-demand read first (a consumer's GET, a start parked on a stream), then
  what the canonical queue wants next in ITS order (`picker`, the queue side's
  — playback.substitute), so a slot moved nearer the playhead is fetched
  sooner; a token nobody wants any more is cancelled at the source
  (`cancel`, the provider's fetch context — contract 3).
- HQPlayer probe-then-streams (HEAD → Range GET, sometimes resets) — we support
  HEAD + Range(206) and swallow client resets.

Audio lives in memory only and is dropped behind a sliding window — no rip is
persisted to disk (legal: derived-metadata model, plus casual-copy resistance).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import socket
import sys
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from . import transcode
from .base import (FetchCancelled, FetchContext, FetchedAudio, ProviderError, Refused,
                   StreamProvider, TrackQuery, fetch_context)

logger = logging.getLogger(__name__)

_UNSET_TIMEOUT = object()   # wait_ready sentinel: "use the proxy default"
UNSATISFIABLE = object()    # parse_byte_range: a span that starts past the end
_HIT_TOKENS = 256           # tokens whose requests are remembered
_HITS_PER_TOKEN = 16
# A token the proxy never served or has let go is remembered too — HQPlayer
# asking for a dead one is part of its trace — but apart, few, and cut short:
# no request for one pushes a live token's record out.
_STRAY_TOKENS = 32
_STRAY_HITS = 4
_TOKEN_KEPT = 64            # characters of a token a record keeps
_RANGE_KEPT = 128
# Connections served at once — each a thread — and from one address: HQPlayer
# probes and streams one entry at a time, a renderer pulls one or two; past
# either, a connection is closed as it arrives (where every consumer shows one
# address, as behind Docker Desktop's bridge, the share is what they have
# together). The request line and headers must arrive within _REQUEST_HEAD_S
# in all and _REQUEST_READ_S of each other; the answer's writes wait as long
# as the consumer does (a paused HQPlayer stops reading for as long as it
# likes), so a consumer gone for good keeps its place until the system's TCP
# gives up on it (minutes), as a paused one keeps its own by design.
_CONNECTIONS = 64
_PER_ADDRESS = 32
_REQUEST_READ_S = 10.0
_REQUEST_HEAD_S = 15.0


def shown(token: str) -> str:
    """A token as a log line names it: enough to tell two apart, never one a
    reader of the log could fetch with."""
    return token[:6] + "…"

# Content type by media_files.file_format — the one table every URL-serving
# output reads (DLNA DIDL, the browser route, an HQPlayer elsewhere).
MIME_BY_FORMAT = {
    "FLAC": "audio/flac", "MP3": "audio/mpeg", "WAV": "audio/wav",
    "OGG": "audio/ogg", "M4A": "audio/mp4", "AIFF": "audio/aiff",
    "DSF": "audio/x-dsf", "DFF": "audio/x-dff", "WV": "audio/x-wavpack",
    "APE": "audio/x-ape",
}

@dataclass
class _FileEntry:
    """Disk-backed serving (owned tracks for DLNA renderers/HQPlayer): the
    proxy streams straight from the file handle — never slurped into RAM,
    unlike preview buffers. A CUE slice entry (start is not None) serves the
    cached FLAC cut of [start, end) produced on first GET. Size always
    resolves at serve time (materialize_file): the file can be gone since
    the token was minted."""
    token: str
    path: str
    mime: str
    start: Optional[float] = None
    end: Optional[float] = None
    tags: Optional[dict] = None


@dataclass
class _Entry:
    token: str
    query: TrackQuery
    # Lossless-first fallback chain of (provider, source_id). source_id is the
    # pre-resolved id (skip re-resolution) or None (the provider resolves inside
    # fetch). Tried in order: a provider that RESOLVED but fails to DOWNLOAD (e.g.
    # the lossless provider has the track but no lossless tier for this account/region) cascades to the
    # next, so a resolvable-but-undownloadable track still streams from YouTube.
    chain: list = field(default_factory=list)
    provider: Optional[StreamProvider] = None   # chain[0]'s provider; the actual one once fetched
    ready: threading.Event = field(default_factory=threading.Event)
    audio: Optional[FetchedAudio] = None
    error: Optional[str] = None
    fetch_seconds: Optional[float] = None   # wall time of the provider fetch
    _claimed: bool = False          # a worker is (or has) prepared this entry
    evicted: bool = False           # audio dropped by the RAM budget; refetches on demand
    # What a consumer was told of the bytes at their last fetch: (size, type).
    # HQPlayer probes an entry when it is added and holds its length: a HEAD
    # once the budget dropped the bytes answers from this at once, and a
    # refetch that lands otherwise must be handed to it again (`reshaped`)
    held: Optional[tuple] = None
    reshaped: bool = False
    # The fetch under way: its cancel and its progress (contract 3)
    ctx: Optional[FetchContext] = None
    progress: Optional[float] = None
    # Why the links that refused said so — (provider id, Refused code) — and
    # the reason the failure is shown with: the chain head's code, else the
    # first coded one (None: a plain failure, "couldn't be fetched")
    refusals: list = field(default_factory=list)
    code: Optional[str] = None
    cancelled: bool = False


# A fetch's progress is told in tenths: a row's line, never a repaint a second
_PROGRESS_STEP = 0.1



def parse_byte_range(header: Optional[str], total: int):
    """The span a Range header asks for, (start, end) inclusive: None for
    none, or one a server ignores (a malformed or a multi-range header — the
    whole body answers it); UNSATISFIABLE for one past the end, answered 416
    and never with the whole body: HQPlayer resumes a stream cut short with
    "bytes=<offset>-" and APPENDS what comes back, so the whole file served
    for an offset at its end fed it the file again and again (Embedded
    6.2.3, 2026-10-03). "bytes=-N" is the last N bytes. The media proxy and
    the browser output's media route answer with it alike."""
    if not (header and header.startswith("bytes=")):
        return None
    s, _, en = header[len("bytes="):].partition("-")
    try:
        if not s:
            n = int(en)
            return (max(0, total - n), total - 1) if n > 0 and total else UNSATISFIABLE
        start = int(s)
        end = min(int(en), total - 1) if en else total - 1
    except ValueError:
        return None
    if start >= total:
        return UNSATISFIABLE
    return (start, end) if start <= end else None


class MediaProxy:
    def __init__(self, port: int, advertised_host: str, *, file_token_key: bytes,
                 bind_host: str = "0.0.0.0", prepare_timeout: float = 120.0):
        # advertised_host: the address a consumer on this machine uses to
        # reach us (it pulls the URL itself), which may differ from bind_host
        # in containerised setups; a consumer elsewhere on the network is
        # handed the address `streaming.media_host` picks for it instead.
        # file_token_key: the node secret the owned-file tokens derive from.
        self.port = port
        self._advertised_host = advertised_host
        self._bind_host = bind_host
        self._prepare_timeout = prepare_timeout
        self._file_token_key = file_token_key
        self._lock = threading.RLock()
        # SEQUENTIAL fetching: one worker, one track at a time. The track
        # wanted next gets the full pipe (best keep-up on a slow link), and
        # one-at-a-time traffic looks like normal listening — a parallel bulk
        # pull is a classic scraping/piracy signal that trips provider abuse
        # detection (account flags / YouTube 403). What it takes is asked when
        # it is free: `_demand` first (a consumer waits on those bytes), then
        # `picker` — the queue's next want, in the queue's order — then
        # `_later` (a wait with no hurry).
        self._demand: list[str] = []
        self._later: list[str] = []
        self._fetch_cv = threading.Condition(self._lock)
        self._fetch_worker = False
        self._woken = 0                 # wakes so far: a wake while the picker ran is not lost
        self._entries: dict[str, _Entry] = {}
        # The queue side's next want (playback.substitute.Lane.pick): a token
        # to fetch, a callable to run in the lane (a CUE cut), or None.
        # Called on the worker with no proxy lock held.
        self.picker: Callable[[], object] = lambda: None
        # Fired (with the _Entry) whenever what a row shows of it moves: the
        # fetch begins, its progress passes a tenth, it ends. Kept generic, as
        # the ready hooks are: the queue side turns it into the rows' states.
        self.state_hooks: list[Callable[["_Entry"], None]] = []
        # Fired (with the query, the provider, its Refused code) for every link
        # that refused with a reason: the resolve remembers it (routers.player)
        self.link_refused: Callable[[TrackQuery, StreamProvider, str], None] = \
            lambda query, provider, code: None
        self._files: dict[str, _FileEntry] = {}          # /file/{token} registry
        # What each /file/ and /preview/ token was actually asked for — the
        # consumer's side of a hand-over, which the HQPlayer playback trace
        # joins on the token (playback.hqp_diagnostics). Newest tokens kept;
        # a token the proxy does not hold, in a small store of its own.
        self._hits: OrderedDict[str, deque] = OrderedDict()
        self._stray_hits: OrderedDict[str, deque] = OrderedDict()
        self._blobs: dict[str, tuple[bytes, str]] = {}   # /art/{token} → (data, mime)
        self._blob_tokens_by_key: dict[str, str] = {}    # cover key → token
        self._httpd: Optional[ThreadingHTTPServer] = None
        # Fired (with the _Entry) once a track is fetched, audio present, BEFORE
        # the entry is marked ready — so a waiter that wakes on it finds the
        # world the hooks maintain (the canonical-queue item) already true.
        # Kept generic so the proxy stays CLAP-agnostic: the enrichment tee is
        # one hook, the queue refresh another.
        self.track_ready_hooks: list[Callable[["_Entry"], None]] = []
        # Fired (with the _Entry, its `error` said) once a fetch ends with no
        # audio, before the entry is marked ready: whoever waits for those
        # bytes in the node hears the fetch is over (a start an output parked)
        self.track_failed_hooks: list[Callable[["_Entry"], None]] = []
        # Whether a chain link may be fetched NOW — the demo policy's gate
        # (streaming/demo.py). A chain is a plan made at resolve time; a link
        # the gate refuses is skipped and the chain cascades on.
        self.link_admissible: Callable[[StreamProvider, TrackQuery], bool] = \
            lambda provider, query: True

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        handler = _make_handler(self)
        self._httpd = _BoundedServer((self._bind_host, self.port), handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True,
                         name="media-proxy").start()
        logger.info("media proxy on %s:%d (advertised host %s)",
                    self._bind_host, self.port, self._advertised_host)

    # ---- owned-file serving (DLNA renderers pull these) -------------------

    def file_token(self, path: str, start: Optional[float] = None,
                   end: Optional[float] = None) -> str:
        """The token /file/ serves (path, start, end) under — a keyed hash,
        not a random draw, so it is the SAME across backend restarts: an
        HQPlayer on another machine keeps a mirrored playlist of these URLs
        for days, and after a restart the restored queue re-registers the
        very tokens it still holds instead of leaving it a list of 404s.
        Unguessable all the same (the node secret is the key), and a token
        is served only while a queue has registered its file — never the
        library at large."""
        canonical = f"{path}\n{start}\n{end}".encode()
        digest = hmac.new(self._file_token_key, canonical, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest[:15]).decode()

    def register_file(self, path: str, mime: str, *,
                      start: Optional[float] = None, end: Optional[float] = None,
                      tags: Optional[dict] = None) -> str:
        """Expose one owned file — or a CUE slice [start, end) of it — at
        /file/{token}. Idempotent per (path, start, end) by construction
        (`file_token`): re-registering returns the same token a renderer may
        still be mid-stream on, and two slices of one image get DISTINCT
        tokens so URI-based auto-advance detection keeps working.

        Bookkeeping only — the disk is read when the token is served. A
        stat here raised out of URL minting on the DLNA loop, where a
        missing file read as the renderer failing, and it could only ever
        vouch for the file at minting time."""
        tok = self.file_token(path, start, end)
        with self._lock:
            if tok not in self._files:
                self._files[tok] = _FileEntry(tok, path, mime,
                                              start=start, end=end, tags=tags)
            return tok

    def file_entry(self, token: str) -> Optional[_FileEntry]:
        with self._lock:
            return self._files.get(token)

    def register_blob(self, key: str, data: bytes, mime: str) -> str:
        """Expose small bytes (cover art for DLNA renderers) at /art/{token}.
        Idempotent per key (cover_id); a small FIFO cap bounds memory to
        roughly one queue's worth of covers."""
        with self._lock:
            tok = self._blob_tokens_by_key.get(key)
            if tok:
                return tok
            tok = secrets.token_urlsafe(12)
            self._blobs[tok] = (data, mime)
            self._blob_tokens_by_key[key] = tok
            while len(self._blob_tokens_by_key) > 48:
                old_key = next(iter(self._blob_tokens_by_key))
                self._blobs.pop(self._blob_tokens_by_key.pop(old_key), None)
            return tok

    def blob_token_for(self, key: str) -> Optional[str]:
        with self._lock:
            return self._blob_tokens_by_key.get(key)

    def blob(self, token: str) -> Optional[tuple[bytes, str]]:
        with self._lock:
            return self._blobs.get(token)

    # ---- RAM budget (playback-position-keyed eviction) -------------------

    @staticmethod
    def _entry_keys(e: _Entry) -> set:
        """Queue identities an entry answers to: its token (a streamed slot's
        way in names it) and ("mf", id) for an owned transcode (HQPlayer's
        slot names it too, but the file is what the queue row is)."""
        keys = {e.token}
        if e.query.media_file_id:
            keys.add(("mf", e.query.media_file_id))
        return keys

    def enforce_budget(self, keep: set, ranked: list, budget_bytes: int) -> int:
        """Bound fetched preview/transcode audio to ``budget_bytes``. ``keep``
        (the playback window) is untouchable; entries matching no known queue
        identity (orphans — rows since removed from the queue) evict first,
        then ``ranked`` order (most evictable first). Evicted entries keep
        their metadata + chain and re-arm: the next wait/GET refetches
        transparently. Returns bytes freed."""
        with self._lock:
            fetched = [e for e in self._entries.values()
                       if e.ready.is_set() and e.audio is not None]
            excess = sum(len(e.audio.data) for e in fetched) - budget_bytes
            if excess <= 0:
                return 0
            rank = {k: i for i, k in enumerate(ranked)}
            known = set(rank) | keep

            def order(e):
                ks = self._entry_keys(e)
                if not (ks & known):
                    return -1                      # orphan — first out
                return min(rank.get(k, len(ranked)) for k in ks)

            freed = 0
            evicted = []
            for e in sorted(fetched, key=order):
                if freed >= excess:
                    break
                if self._entry_keys(e) & keep:
                    continue
                freed += len(e.audio.data)
                self._evict(e)
                evicted.append(e)
        for e in evicted:
            self._said(e)
        return freed

    @staticmethod
    def _evict(e: _Entry) -> None:
        """Drop an entry's audio and re-arm it: metadata + chain stay, the next
        wait/GET refetches transparently. Caller holds _lock."""
        e.audio = None
        e.error = None
        e.code = None
        e.refusals = []
        e.fetch_seconds = None
        e.progress = None
        e.evicted = True
        e._claimed = False
        # Fresh event: late waiters on the old (set) one read audio=None
        # and skip; new waits see un-ready and re-arm the fetch.
        e.ready = threading.Event()

    def drop_audio(self, track_id: str) -> int:
        """Forget the fetched audio of every entry of `track_id` that a
        demo-limited provider served — its one listen is spent (streaming/
        demo.py). Evicted exactly like the budget does, so a replay refetches
        and the admissibility gate routes it past the spent provider. Returns
        the number of entries dropped."""
        if not track_id:
            return 0
        dropped = []
        with self._lock:
            for e in self._entries.values():
                if (e.query.track_id == track_id and e.ready.is_set()
                        and e.audio is not None and e.provider is not None
                        and e.provider.manifest.demo_limited):
                    self._evict(e)
                    dropped.append(e)
        for e in dropped:
            self._said(e)
        return len(dropped)

    def bytes_away(self, token: str) -> bool:
        """Whether the stream `token` names is out of RAM and comes back with
        a fetch — the budget dropped its bytes, or they are on their way or
        not asked for yet. Not one whose fetch failed (its GET answers at
        once, with the error), nor one in hand or unknown here."""
        with self._lock:
            e = self._entries.get(token)
            return e is not None and e.audio is None and e.error is None

    def wanted(self, token: str) -> bool:
        """Whether the lane would fetch `token` if it took it now: known
        here, its bytes neither in hand nor on their way, its fetch not over
        with an error — what the queue side's picker hands the worker."""
        with self._lock:
            e = self._entries.get(token)
            return e is not None and not e._claimed

    def state(self, token: str) -> Optional[tuple]:
        """What a row shows of the stream `token`: (state, progress, code) —
        `waiting` (its fetch not begun: behind another, or its bytes dropped
        by the budget), `buffering` (its fetch runs; progress 0..1 where the
        provider gives it), `ready` (its bytes in hand), `failed` (code: the
        refusal's, None for a plain failure). None for a token not known
        here."""
        with self._lock:
            e = self._entries.get(token)
            if e is None:
                return None
            if e.ready.is_set():
                return ("ready", None, None) if e.audio is not None else ("failed", None, e.code)
            if e._claimed:
                return "buffering", e.progress, None
            return "waiting", None, None

    def demand(self, token: str) -> None:
        """Fetch `token` before anything the queue wants next: a consumer waits
        on those bytes (an output's GET, a start parked on the stream, a
        transcode a mirror waits for). Idempotent."""
        self._submit(token, front=True)

    def wake(self) -> None:
        """What the queue wants next may have moved (an edit, the playhead, a
        resolve that landed): the lane, if idle, asks the picker again."""
        with self._fetch_cv:
            self._woken += 1
            self._start_worker()
            self._fetch_cv.notify()

    def cancel(self, token: str) -> bool:
        """Nobody wants the bytes of `token` any more (the slot that named it
        was removed, the queue replaced): out of the lane if not begun,
        stopped at the source if running — a provider that reads its fetch's
        cancel stops; one that does not is waited out and its bytes
        discarded, never run beside another fetch. Bytes already in hand stay
        for the budget, which takes an orphan first. Every waiter is woken.
        Returns whether a fetch was cancelled."""
        with self._lock:
            e = self._entries.get(token)
            if e is None or e.ready.is_set():
                return False
            for line in (self._demand, self._later):
                if token in line:
                    line.remove(token)
            del self._entries[token]
            e.cancelled = True
            e.error = "cancelled: nobody wants these bytes any more"
            ctx = e.ctx
        if ctx is not None:
            ctx.cancel()
        e.ready.set()
        logger.info("preview: fetch of %s cancelled (%s)", shown(token),
                    "under way" if ctx is not None else "not begun")
        return True

    # ---- entry minting ---------------------------------------------------

    def _ram_index(self) -> dict:
        """track_id → entry for every track already fetched and still in RAM.
        Re-downloading what we hold is pure waste, and repeated identical pulls
        read as scraping to the providers. Caller holds _lock."""
        return {e.query.track_id: e for e in self._entries.values()
                if e.ready.is_set() and e.audio is not None and e.query.track_id}

    def mint(self, query: TrackQuery, chain: list) -> str:
        """A token for one track: `chain` is its lossless-first fallback list
        of ``(provider, source_id)`` (see _Entry). Nothing is fetched yet —
        the lane takes it when the queue wants it (picker) or a consumer asks
        (demand). A track already in RAM, from a provider the chain still
        names, is adopted at once: a re-queued album downloads nothing — but a
        chain built without the demo channel (its listen spent) does not
        inherit the demo channel's bytes."""
        tok = secrets.token_urlsafe(12)
        with self._lock:
            prior = self._ram_index().get(query.track_id)
            if (prior is None or prior.audio is None
                    or prior.provider not in {p for p, _sid in chain}):
                e = _Entry(tok, query, chain=chain, provider=chain[0][0] if chain else None)
            else:
                e = _Entry(tok, query, chain=chain, provider=prior.provider)
                e.audio = prior.audio
                e.held = (len(prior.audio.data), prior.audio.mime)
                e.fetch_seconds = prior.fetch_seconds
                e._claimed = True
                e.ready.set()                    # already in hand — waiters return at once
            self._entries[tok] = e
        return tok

    @staticmethod
    def _seconds(e: _Entry) -> Optional[float]:
        """How much audio the entry holds, in seconds: the clip's own length
        for an excerpt, the catalog length otherwise."""
        if e.audio is not None and e.audio.excerpt and e.audio.seconds:
            return e.audio.seconds
        return e.query.duration

    # ---- materialization: what a GET would have to produce ----------------
    def materialize_file(self, token: str, q: Optional[str],
                         ss: float = 0.0) -> tuple[str, str, int]:
        """The on-disk resource ``/file/{token}?q=&ss=`` serves — the raw
        file, the cached CUE cut, the cached Opus of either, or the cached
        FLAC of a whole file (``q=flac``, for a renderer that lists no format
        of the file's own) — produced NOW when not cached yet. Returns (path,
        mime, size); KeyError for an unknown token, RuntimeError when a CUE
        cut or a FLAC cannot be made (a cut has no lossless fallback — the raw
        image would play the whole disc — and the renderer asked for FLAC
        does not take the raw file), OSError when the file is gone (moved
        since the scan, the library's drive unmounted).

        The DLNA backend calls this before a URL is handed to a renderer:
        KANN probes the URI inside SetAVTransportURI and its control channel
        is frozen until the probe returns, so a first-GET transcode of a long
        track (tens of seconds for a DJ mix) froze every SOAP call and read as
        the renderer leaving the network. The GET handler still materializes
        for consumers that come unannounced (HQPlayer, the browser)."""
        import os
        fe = self.file_entry(token)
        if fe is None:
            raise KeyError(token)
        path, mime = fe.path, fe.mime
        if fe.start is not None:
            path = transcode.flac_slice_path_for_file(
                fe.path, fe.start, fe.end, fe.tags or {})
            mime = transcode.FLAC_MIME
        elif q == transcode.FLAC_TIER:
            path, mime = transcode.flac_path_for_file(path), transcode.FLAC_MIME
        if transcode.wants_opus(q):
            try:
                # For a slice `path` is already the cut, so the Opus tier
                # chains off it with slice-relative ?ss for free.
                opus = transcode.opus_path_for_file(path, q, ss=ss)
                if opus:
                    return opus, transcode.MIME, os.path.getsize(opus)
            except Exception as e:
                logger.warning("opus transcode failed %s — lossless: %s", path, e)
        return path, mime, os.path.getsize(path)

    def materialize_preview(self, token: str, q: Optional[str],
                            ss: float = 0.0) -> Optional[str]:
        """Cached Opus path for a FETCHED preview buffer when ?q asks for it
        (produced now when missing), else None — the lossless bytes are
        served from RAM. Callers wait_ready() first."""
        if not transcode.wants_opus(q):
            return None
        e = self._peek(token)
        if e is None or e.audio is None:
            raise KeyError(token)
        try:
            return transcode.opus_path_for_bytes(token, e.audio.data, q, ss=ss)
        except Exception as ex:
            logger.warning("opus transcode failed for preview %s — lossless: %s",
                           shown(token), ex)
            return None

    # `host` is the address the CONSUMER reaches us at (streaming.media_host
    # picks it per peer); None = the advertised host, right for a consumer on
    # this machine.
    def url_for(self, token: str, *, host: Optional[str] = None) -> str:
        return f"http://{host or self._advertised_host}:{self.port}/preview/{token}"

    def file_url(self, token: str, *, host: Optional[str] = None) -> str:
        return f"http://{host or self._advertised_host}:{self.port}/file/{token}"

    def art_url(self, token: str, *, host: Optional[str] = None) -> str:
        return f"http://{host or self._advertised_host}:{self.port}/art/{token}"

    def record_hit(self, token: str, client: str, method: str,
                   range_header: Optional[str], kind: str, *, held: bool = True) -> dict:
        """Open the record of one request for `token`; the handler stamps
        its status and adds the bytes it writes as they go out. A token the
        proxy does not hold (`held=False`) goes to the strays' store; the
        token and the range are kept cut short either way."""
        token = token[:_TOKEN_KEPT]
        hit = {"ts": time.time(), "client": client, "method": method,
               "range": None if range_header is None else range_header[:_RANGE_KEPT],
               "kind": kind, "status": None, "bytes": 0}
        store, tokens, per = ((self._hits, _HIT_TOKENS, _HITS_PER_TOKEN) if held
                              else (self._stray_hits, _STRAY_TOKENS, _STRAY_HITS))
        with self._lock:
            ring = store.pop(token, None) or deque(maxlen=per)
            ring.append(hit)
            store[token] = ring
            while len(store) > tokens:
                store.popitem(last=False)
        return hit

    def hits(self, token: str) -> list[dict]:
        """The requests for `token`, oldest first — held or not when asked."""
        token = token[:_TOKEN_KEPT]
        with self._lock:
            both = (*self._stray_hits.get(token, ()), *self._hits.get(token, ()))
            return [dict(h) for h in sorted(both, key=lambda h: h["ts"])]

    def chain_summary(self, token: str) -> Optional[dict]:
        """Where a preview token's audio comes from: the provider chain in
        order, the provider that served it, and how the fetch ended."""
        with self._lock:
            e = self._entries.get(token)
        if e is None:
            return None
        return {"chain": [p.manifest.id for p, _ in e.chain],
                "provider": e.provider.manifest.id if e.provider else None,
                "ready": e.ready.is_set(), "error": e.error,
                "excerpt": bool(e.audio is not None and e.audio.excerpt),
                "fetch_seconds": e.fetch_seconds}

    def preview_meta(self, token: str) -> Optional[dict]:
        """Provider metadata for a current-session preview token — Now
        Playing / the queue panel use it because HQPlayer only knows the
        track as 'HTTP stream'. None for an unknown token / expired session."""
        with self._lock:
            e = self._entries.get(token)
        if e is None:
            return None
        # An excerpt reports the clip's own length: the queue, the renderer's
        # DIDL and the tracker are told about THIS buffer, not the recording.
        return {"artist": e.query.artist, "title": e.query.title,
                "album": e.query.album, "album_id": e.query.album_id,
                "provider": e.provider.manifest.id,
                "track_id": e.query.track_id, "cover_url": e.query.cover_url,
                "duration": self._seconds(e), "media_file_id": e.query.media_file_id,
                "excerpt": bool(e.audio is not None and e.audio.excerpt)}

    def wait_ready(self, token: str, timeout=_UNSET_TIMEOUT, *,
                   front: bool = False) -> _Entry:
        """Block until a track is fetched. ``timeout=None`` waits indefinitely
        — safe because every token reaches a terminal ready (fetch success,
        fetch failure, or the cancel's wake-up). ``front`` asks for it before
        anything the queue wants (an on-demand read); without it the lane
        takes it once the queue wants nothing sooner."""
        if timeout is _UNSET_TIMEOUT:
            timeout = self._prepare_timeout
        return self._ensure_ready(token, timeout, front=front)

    # ---- the lane ----------------------------------------------------------
    def _submit(self, token: str, *, front: bool) -> None:
        """Ask the lane for `token`: `front` at the head of the on-demand
        reads (and promoted there from the later ones), else after everything
        the queue wants. Idempotent — a claimed token is a no-op."""
        with self._fetch_cv:
            e = self._entries.get(token)
            if e is None or e._claimed:
                return
            e.evicted = False
            if front:
                if token in self._later:
                    self._later.remove(token)
                if token in self._demand:
                    self._demand.remove(token)
                self._demand.insert(0, token)
            elif token not in self._demand and token not in self._later:
                self._later.append(token)
            self._woken += 1
            self._start_worker()
            self._fetch_cv.notify()

    def _start_worker(self) -> None:
        """Caller holds the lane's condition."""
        if not self._fetch_worker:
            self._fetch_worker = True
            threading.Thread(target=self._fetch_loop, daemon=True,
                             name="preview-fetch").start()

    def _fetch_loop(self) -> None:
        """One worker, one track at a time: an on-demand read first, then the
        queue's next want (the picker, asked with no proxy lock held: it reads
        the queue), then a wait with no hurry; asleep until a wake when there
        is none — a wake while the picker ran is not lost."""
        while True:
            with self._fetch_cv:
                seen = self._woken
                token = self._demand.pop(0) if self._demand else None
            job = token
            if job is None:
                try:
                    job = self.picker()
                except Exception:
                    logger.exception("the lane's picker failed")
                    job = None
            if job is None:
                with self._fetch_cv:
                    job = self._later.pop(0) if self._later else None
                    if job is None:
                        if self._woken == seen:
                            self._fetch_cv.wait()
                        continue
            if callable(job):
                try:
                    job()
                except Exception:
                    logger.exception("a lane job failed")
            else:
                self._prepare(job)

    def _progress(self, e: _Entry, fraction: float) -> None:
        """A provider's progress (its fetch context): told at each tenth."""
        before = e.progress
        e.progress = fraction
        if before is None or int(fraction / _PROGRESS_STEP) != int(before / _PROGRESS_STEP):
            self._said(e)

    def _said(self, e: _Entry) -> None:
        for hook in list(self.state_hooks):
            try:
                hook(e)
            except Exception:
                logger.exception("state hook failed for %s", shown(e.token))

    def _prepare(self, token: str) -> None:
        with self._lock:
            e = self._entries.get(token)
            if e is None or e._claimed:
                return
            e._claimed = True
            e.progress = None
            ctx = e.ctx = FetchContext(on_progress=lambda f: self._progress(e, f))
        self._said(e)
        try:
            # Walk the lossless-first chain: a provider that RESOLVED but can't
            # DOWNLOAD (the lossless provider has the track but no lossless tier for this account) falls
            # through to the next provider (e.g. YouTube) instead of dropping it.
            last_err = None
            for prov, sid in e.chain:
                if ctx.cancelled.is_set():
                    break
                if not self.link_admissible(prov, e.query):
                    last_err = last_err or ProviderError(
                        f"{prov.manifest.id}: demo listen spent")
                    logger.info("preview link refused %s via %s: demo listen spent",
                                shown(token), prov.manifest.id)
                    continue
                # A chain resolved while the provider was offered still names
                # it: one that has stepped aside since is not asked
                aside = prov.stepped_aside()
                if aside is not None:
                    last_err = last_err or ProviderError(
                        f"{prov.manifest.id}: stepped aside ({aside.code})")
                    logger.info("preview link skipped %s via %s: stepped aside (%s)",
                                shown(token), prov.manifest.id, aside.code)
                    continue
                started = time.monotonic()
                try:
                    with fetch_context(ctx):
                        audio = prov.download(sid) if sid else prov.fetch(e.query)
                    if ctx.cancelled.is_set():
                        break                   # waited out, and nobody wants it
                    e.audio = audio
                    e.provider = prov                     # the one that actually served
                    e.fetch_seconds = time.monotonic() - started
                    break
                except FetchCancelled:
                    break
                except Refused as ex:
                    last_err = ex
                    e.refusals.append((prov.manifest.id, ex.code))
                    logger.warning("preview fetch refused %s via %s (%s): %s",
                                   shown(token), prov.manifest.id, ex.code, ex)
                    try:
                        self.link_refused(e.query, prov, ex.code)
                    except Exception:
                        logger.exception("the refusal of %s was not remembered", shown(token))
                except ProviderError as ex:
                    last_err = ex
                    logger.warning("preview fetch failed %s via %s: %s",
                                   shown(token), prov.manifest.id, ex)
            if e.audio is None and last_err is not None and not ctx.cancelled.is_set():
                e.error = str(last_err)
                head = e.chain[0][0].manifest.id if e.chain else None
                codes = dict(e.refusals)
                e.code = codes.get(head) or (e.refusals[0][1] if e.refusals else None)
        except Exception as ex:  # provider bug — surface, don't hang the GET
            e.error = f"unexpected: {ex}"
            logger.error("preview fetch crashed %s: %s", shown(token), ex, exc_info=True)
        finally:
            e.ctx = None
            self._settle(e, ctx)

    def _settle(self, e: _Entry, ctx: FetchContext) -> None:
        """A fetch is over: its hooks told — what landed, or that it failed —
        before the entry is marked ready, so a waiter that wakes finds the
        world they keep already true. A cancelled one lands nothing: cancel()
        took it out and woke its waiters."""
        token = e.token
        if e.cancelled or ctx.cancelled.is_set():
            e.audio = None
            e.ready.set()
        else:
            if e.audio is not None:
                shape = (len(e.audio.data), e.audio.mime)
                e.reshaped = e.held is not None and e.held != shape
                e.held = shape
                for hook in list(self.track_ready_hooks):
                    try:
                        hook(e)
                    except Exception:
                        logger.exception("track-ready hook failed %s", shown(token))
            elif e.error is not None:
                for hook in list(self.track_failed_hooks):
                    try:
                        hook(e)
                    except Exception:
                        logger.exception("track-failed hook failed %s", shown(token))
            e.ready.set()
            self._said(e)

    def _peek(self, token: str) -> Optional[_Entry]:
        """Lookup without triggering a fetch — for a cheap HEAD."""
        with self._lock:
            return self._entries.get(token)

    def _ensure_ready(self, token: str, timeout: Optional[float], *,
                      front: bool = False) -> _Entry:
        with self._lock:
            e = self._entries.get(token)
        if e is None:
            raise KeyError(token)
        if not e._claimed:
            self._submit(token, front=front)
        if not e.ready.wait(timeout):
            raise TimeoutError(f"preview {shown(token)} not ready")
        if e.cancelled:
            raise KeyError(token)
        return e

class _BoundedServer(ThreadingHTTPServer):
    """A thread per connection, _CONNECTIONS of them at most and
    _PER_ADDRESS from one address: one past either is closed as it arrives,
    its thread never started."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(_CONNECTIONS)
        self._lock = threading.Lock()
        self._by_address: dict[str, int] = {}

    def process_request(self, request, client_address) -> None:
        address = client_address[0]
        with self._lock:
            taken = (self._by_address.get(address, 0) < _PER_ADDRESS
                     and self._slots.acquire(blocking=False))
            if taken:
                self._by_address[address] = self._by_address.get(address, 0) + 1
        if not taken:
            logger.debug("media proxy: no connection left for %s — closed", address)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(address)
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(client_address[0])

    def handle_error(self, request, client_address) -> None:
        """A connection that ended under its request — the consumer gone, or
        its head past the deadline — is said at debug; anything else is an
        error, with its traceback in the log (not on stderr)."""
        error = sys.exc_info()[1]
        if isinstance(error, ConnectionError):
            logger.debug("media proxy: %s's connection ended: %s", client_address[0], error)
        else:
            logger.error("media proxy: a request from %s failed", client_address[0], exc_info=True)

    def _release(self, address: str) -> None:
        with self._lock:
            left = self._by_address[address] - 1
            if left:
                self._by_address[address] = left
            else:
                del self._by_address[address]
        self._slots.release()


def _make_handler(proxy: MediaProxy):
    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.0 (the default): each request gets its own connection that
        # closes after the response. HQPlayer probe-then-streams and resets
        # connections; keep-alive (1.1) desynced those and playback never
        # started. Matches the plain http.server that played reliably in bring-up.

        # The request line and headers: each read waits _REQUEST_READ_S at
        # most (StreamRequestHandler sets it on the socket), and the whole of
        # them _REQUEST_HEAD_S (setup's timer) — a head that keeps coming
        # slowly holds no place for good; the answer, once it begins
        # (_answering), waits for the consumer as long as it takes.
        timeout = _REQUEST_READ_S

        # The /file/ or /preview/ request being served, as the proxy records
        # it (record_hit): its status and the bytes that actually went out.
        hit: Optional[dict] = None

        def log_message(self, fmt, *a):  # quiet by default; -d to trace probes
            logger.debug("proxy %s: %s", self.address_string(), fmt % a)

        def setup(self) -> None:
            super().setup()
            self._head_timer = threading.Timer(_REQUEST_HEAD_S, self._head_late)
            self._head_timer.daemon = True
            self._head_timer.start()

        def _head_late(self) -> None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError as e:                      # it ended on its own meanwhile
                logger.debug("proxy %s: the head's deadline met an ended connection: %s",
                             self.address_string(), e)

        def finish(self) -> None:
            self._head_timer.cancel()
            super().finish()

        def _answering(self) -> None:
            self._head_timer.cancel()
            self.connection.settimeout(None)

        def send_response(self, code, message=None):
            if self.hit is not None:
                self.hit["status"] = code
            super().send_response(code, message)

        def _open_hit(self, token: str, kind: str, *, held: bool) -> None:
            self.hit = proxy.record_hit(token, self.client_address[0], self.command,
                                        self.headers.get("Range"), kind, held=held)

        def _token(self) -> Optional[str]:
            if not self.path.startswith("/preview/"):
                self.send_error(404)
                return None
            return self.path[len("/preview/"):].split("?", 1)[0]

        def _file_token(self) -> Optional[str]:
            if not self.path.startswith("/file/"):
                return None
            return self.path[len("/file/"):].split("?", 1)[0]

        def _q(self) -> Optional[str]:
            """Opus quality tier from the URL (?q=opus_192). DLNA mints it
            from the global setting; HQPlayer never does → its previews stay
            lossless through the very same /preview route."""
            if "?" not in self.path:
                return None
            from urllib.parse import parse_qs, urlparse
            return parse_qs(urlparse(self.path).query).get("q", [None])[0]

        def _ss(self) -> float:
            """Server-side seek offset (?ss=N seconds) — Opus is re-encoded
            from N so renderers that can't time-seek VBR Opus (KANN) seek by
            reloading the URL at the offset."""
            if "?" not in self.path:
                return 0.0
            from urllib.parse import parse_qs, urlparse
            try:
                return float(parse_qs(urlparse(self.path).query).get("ss", ["0"])[0])
            except ValueError:
                return 0.0

        # DLNA renderers gate playability on these headers (OP=01: Range
        # seek supported). Harmless for HQPlayer, so both routes send them.
        def _dlna_headers(self):
            self.send_header("contentFeatures.dlna.org",
                             "DLNA.ORG_OP=01;DLNA.ORG_FLAGS="
                             "01700000000000000000000000000000")
            self.send_header("transferMode.dlna.org", "Streaming")

        def _parse_range(self, total: int):
            return parse_byte_range(self.headers.get("Range"), total)

        def _unsatisfiable(self, total: int):
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _serve_file(self, token: str, *, body: bool):
            fe = proxy.file_entry(token)
            self._open_hit(token, "opus" if transcode.wants_opus(self._q())
                           else "cut" if fe is not None and fe.start is not None
                           else "flac" if self._q() == transcode.FLAC_TIER
                           else "file", held=fe is not None)
            try:
                path, mime, size = proxy.materialize_file(
                    token, self._q(), self._ss())
            except KeyError:
                self.send_error(404, "unknown token")
                return
            except Exception as e:
                logger.error("file token %s not servable: %s", shown(token), e)
                self.send_error(500)
                return
            self._serve_disk_file(path, mime, size, body=body)

        def _serve_disk_file(self, path: str, mime: str, size: int, *, body: bool):
            span = self._parse_range(size)
            if span is UNSATISFIABLE:
                self._unsatisfiable(size)
                return
            if span:
                start, end = span
                self.send_response(206)
            else:
                start, end = 0, size - 1
                self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if span:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self._dlna_headers()
            self.end_headers()
            if not body:
                return
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        chunk = f.read(min(262144, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                        if self.hit is not None:
                            self.hit["bytes"] += len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass   # renderers probe-then-reset, like HQPlayer
            except OSError as e:
                logger.error("file serve failed %s: %s", path, e)

        def _opus_for_preview(self, token) -> Optional[str]:
            """Cached Opus path for a phantom buffer when ?q asks for it, else
            None (serve the lossless in-memory bytes)."""
            return proxy.materialize_preview(token, self._q(), self._ss())

        def _art_token(self) -> Optional[str]:
            if not self.path.startswith("/art/"):
                return None
            return self.path[len("/art/"):].split("?", 1)[0]

        def _serve_art(self, token: str, *, body: bool):
            blob = proxy.blob(token)
            if blob is None:
                self.send_error(404, "unknown token")
                return
            data, mime = blob
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400, immutable")
            self.end_headers()
            if body:
                self._write(data)

        def do_HEAD(self):
            self._answering()
            atok = self._art_token()
            if atok is not None:
                self._serve_art(atok, body=False)
                return
            ftok = self._file_token()
            if ftok is not None:
                self._serve_file(ftok, body=False)
                return
            # HQPlayer HEAD-probes each URI synchronously when it is ADDED and
            # needs Content-Length to accept it (the FLAC size is only known
            # after the fetch). The session prefetches the whole album and the
            # endpoint waits before adding, so these HEADs return immediately —
            # and one the RAM budget dropped since answers from what it was
            # told then, never waiting on a refetch: HQPlayer's probe of a tail
            # repair or a reconverge waited out whole fetches on its control
            # thread (live, 45 s on one add). One whose fetch failed is
            # answered with its error, as its GET is.
            tok = self._token()
            if tok is None:
                return
            held = proxy._peek(tok)
            self._open_hit(tok, "opus" if transcode.wants_opus(self._q()) else "preview",
                           held=held is not None)
            if (held is not None and held.audio is None and held.error is None
                    and held.held is not None and not transcode.wants_opus(self._q())):
                self.send_response(200)
                self.send_header("Content-Type", held.held[1])
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(held.held[0]))
                self._dlna_headers()
                self.end_headers()
                return
            try:
                e = proxy._ensure_ready(tok, proxy._prepare_timeout, front=True)
            except KeyError:
                self.send_error(404, "unknown token")
                return
            except TimeoutError:
                self.send_error(504, "preview preparation timed out")
                return
            if e.error or e.audio is None:
                self.send_error(502, "provider could not fetch this track")
                return
            opus = self._opus_for_preview(tok)
            if opus:
                import os
                self._serve_disk_file(opus, transcode.MIME,
                                      os.path.getsize(opus), body=False)
                return
            self.send_response(200)
            self.send_header("Content-Type", e.audio.mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(e.audio.data)))
            self._dlna_headers()
            self.end_headers()

        def do_GET(self):
            self._answering()
            atok = self._art_token()
            if atok is not None:
                self._serve_art(atok, body=True)
                return
            ftok = self._file_token()
            if ftok is not None:
                self._serve_file(ftok, body=True)
                return
            tok = self._token()
            if tok is None:
                return
            self._open_hit(tok, "opus" if transcode.wants_opus(self._q()) else "preview",
                           held=proxy._peek(tok) is not None)
            try:
                e = proxy._ensure_ready(tok, proxy._prepare_timeout, front=True)
            except KeyError:
                self.send_error(404, "unknown token")
                return
            except TimeoutError:
                self.send_error(504, "preview preparation timed out")
                return
            if e.error or e.audio is None:
                self.send_error(502, "provider could not fetch this track")
                return
            opus = self._opus_for_preview(tok)
            if opus:
                import os
                self._serve_disk_file(opus, transcode.MIME,
                                      os.path.getsize(opus), body=True)
                return
            data = e.audio.data
            total = len(data)
            span = self._parse_range(total)
            if span is UNSATISFIABLE:
                self._unsatisfiable(total)
            elif span:
                start, end = span
                self._send_headers(e, end - start + 1, full=False, body=True,
                                   start=start, end=end, total=total)
                self._write(memoryview(data)[start:end + 1])     # the span, not a copy of it
            else:
                self._send_headers(e, total, full=True, body=True)
                self._write(data)

        def _send_headers(self, e, length, *, full, body, start=0, end=0, total=0):
            self.send_response(200 if full else 206)
            self.send_header("Content-Type", e.audio.mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if not full:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self._dlna_headers()
            self.end_headers()

        def _write(self, data: bytes):
            # HQPlayer probes then resets connections — that's normal, not an error.
            # In slices, so the hit record counts what has gone out while a
            # slow reader is still taking a whole track's buffer.
            view = memoryview(data)
            try:
                for start in range(0, len(view), 262144):
                    piece = view[start:start + 262144]
                    self.wfile.write(piece)
                    if self.hit is not None:
                        self.hit["bytes"] += len(piece)
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler
