"""In-memory media proxy: serves provider-fetched audio to HQPlayer over plain
http (HQPlayer can't sign HMAC nor trust the backend's self-signed TLS, so this
is a SEPARATE plain-http endpoint, localhost/LAN-reachable, gated by per-track
ephemeral tokens).

Design points proven during bring-up:
- HQPlayer plays http FLAC sustained in its NATIVE playlist (alongside file://
  owned tracks) — so a preview track is just one more playlist URI.
- A provider fetch takes seconds (search + download + transcode), so we PREFETCH
  the next track off the request path; HQPlayer's GET then hits a ready buffer.
- Queued work is BOUND to the canonical-queue generation that will hold it, so
  moving to another queue cancels the pending fetches at the source instead of
  downloading an album nobody will hear (`bind` / `retire_generation`).
- HQPlayer probe-then-streams (HEAD → Range GET, sometimes resets) — we support
  HEAD + Range(206) and swallow client resets.

Audio lives in memory only and is dropped behind a sliding window — no rip is
persisted to disk (legal: derived-metadata model, plus casual-copy resistance).
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from . import transcode
from .base import FetchedAudio, ProviderError, StreamProvider, TrackQuery
from .events import preview_events

logger = logging.getLogger(__name__)

_UNSET_TIMEOUT = object()   # wait_ready sentinel: "use the proxy default"

@dataclass
class _FileEntry:
    """Disk-backed serving (owned tracks for DLNA renderers/HQPlayer): the
    proxy streams straight from the file handle — never slurped into RAM,
    unlike preview buffers. A CUE slice entry (start is not None) serves the
    cached FLAC cut of [start, end) produced on first GET, so size resolves
    at serve time."""
    token: str
    path: str
    mime: str
    size: Optional[int] = None
    start: Optional[float] = None
    end: Optional[float] = None
    tags: Optional[dict] = None


@dataclass
class _Entry:
    token: str
    query: TrackQuery
    index: int                      # position in the session, for prefetch/eviction
    # Lossless-first fallback chain of (provider, source_id). source_id is the
    # pre-resolved id (skip re-resolution) or None (the provider resolves inside
    # fetch). Tried in order: a provider that RESOLVED but fails to DOWNLOAD (e.g.
    # Deezer has the track but no FLAC for this account/region) cascades to the
    # next, so a resolvable-but-undownloadable track still streams from YouTube.
    chain: list = field(default_factory=list)
    provider: Optional[StreamProvider] = None   # chain[0]'s provider; the actual one once fetched
    ready: threading.Event = field(default_factory=threading.Event)
    audio: Optional[FetchedAudio] = None
    error: Optional[str] = None
    fetch_seconds: Optional[float] = None   # wall time of the provider fetch
    _claimed: bool = False          # a worker is (or has) prepared this entry
    evicted: bool = False           # audio dropped by the RAM budget; refetches on demand
    # Canonical-queue generation whose filler will place this track. None = the
    # request that made it still owns it (a start_session still pre-buffering, an
    # owned m4a transcode); a bound entry dies with its generation.
    generation: Optional[int] = None


class MediaProxy:
    def __init__(self, port: int, advertised_host: str, bind_host: str = "0.0.0.0",
                 prepare_timeout: float = 120.0):
        # advertised_host: the address HQPlayer uses to reach us (it pulls the
        # URL itself), which may differ from bind_host in containerised setups.
        self.port = port
        self._advertised_host = advertised_host
        self._bind_host = bind_host
        self._prepare_timeout = prepare_timeout
        self._lock = threading.RLock()
        # SEQUENTIAL, in-ORDER fetching: one worker drains an ordered token queue
        # (enqueue order == playlist order). The next-to-play track gets the full
        # pipe (best keep-up on a slow link), and one-at-a-time traffic looks like
        # normal listening — a parallel bulk pull is a classic scraping/piracy
        # signal that trips provider abuse detection (ARL flag / YouTube 403).
        self._fetch_q: list[str] = []
        self._fetch_cv = threading.Condition(self._lock)
        self._fetch_worker = False
        self._entries: dict[str, _Entry] = {}
        self._session: list[str] = []   # ordered tokens of the current preview
        # The canonical-queue generation currently being played, as last reported
        # by the manager. Work bound to any other generation has no consumer left.
        self._live_generation: Optional[int] = None
        self._files: dict[str, _FileEntry] = {}          # /file/{token} registry
        self._file_tokens_by_key: dict[tuple, str] = {}  # (path, start, end) → token
        self._blobs: dict[str, tuple[bytes, str]] = {}   # /art/{token} → (data, mime)
        self._blob_tokens_by_key: dict[str, str] = {}    # cover key → token
        self._httpd: Optional[ThreadingHTTPServer] = None
        # Fired (with the _Entry) once a track is fetched, audio present — the
        # preview-enrichment tee. Kept generic so the proxy stays CLAP-agnostic.
        self.on_track_ready: Optional[Callable[["_Entry"], None]] = None

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self._bind_host, self.port), handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True,
                         name="media-proxy").start()
        logger.info("media proxy on %s:%d (advertised host %s)",
                    self._bind_host, self.port, self._advertised_host)

    # ---- owned-file serving (DLNA renderers pull these) -------------------

    def register_file(self, path: str, mime: str, *,
                      start: Optional[float] = None, end: Optional[float] = None,
                      tags: Optional[dict] = None) -> str:
        """Expose one owned file — or a CUE slice [start, end) of it — at
        /file/{token}. Idempotent per (path, start, end) — re-registering
        returns the existing token (a renderer may still be mid-stream on it),
        and two slices of one image get DISTINCT tokens so URI-based
        auto-advance detection keeps working. Tokens are unguessable; only
        registered files are reachable, never the library at large."""
        import os
        key = (path, start, end)
        with self._lock:
            tok = self._file_tokens_by_key.get(key)
            if tok:
                return tok
            tok = secrets.token_urlsafe(12)
            size = os.path.getsize(path) if start is None else None
            self._files[tok] = _FileEntry(tok, path, mime, size,
                                          start=start, end=end, tags=tags)
            self._file_tokens_by_key[key] = tok
            return tok

    def clear_files(self, keep_paths: Optional[set] = None) -> None:
        """Drop file registrations (queue replaced) except entries whose
        path is in `keep_paths`."""
        with self._lock:
            keep = keep_paths or set()
            for key, tok in list(self._file_tokens_by_key.items()):
                if key[0] not in keep:
                    del self._file_tokens_by_key[key]
                    self._files.pop(tok, None)

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
        """Queue identities an entry answers to: its token (phantom rows carry
        it in source) and ("mf", id) for an owned transcode (the queue row is
        the file item; only the HQP mirror knows the token)."""
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
            for e in sorted(fetched, key=order):
                if freed >= excess:
                    break
                if self._entry_keys(e) & keep:
                    continue
                freed += len(e.audio.data)
                e.audio = None
                e.error = None
                e.fetch_seconds = None
                e.evicted = True
                e._claimed = False
                # Fresh event: late waiters on the old (set) one read audio=None
                # and skip; new waits see un-ready and re-arm the fetch.
                e.ready = threading.Event()
            return freed

    def ensure_buffered(self, keys: list) -> None:
        """Priority-(re)fetch the playback window, first key most urgent —
        window tokens go to the HEAD of the sequential fetch queue, ahead of
        background album fills. Keys are queue identities (see _entry_keys);
        unknown ones are skipped."""
        with self._lock:
            by_mf = {e.query.media_file_id: e.token
                     for e in self._entries.values() if e.query.media_file_id}
            toks = []
            for k in keys:
                tok = by_mf.get(k[1]) if isinstance(k, tuple) else (
                    k if k in self._entries else None)
                if tok:
                    toks.append(tok)
        for tok in reversed(toks):
            self._prefetch(tok, front=True)

    # ---- entry minting ---------------------------------------------------

    def _ram_index(self) -> dict:
        """track_id → entry for every track already fetched and still in RAM.
        Re-downloading what we hold is pure waste, and repeated identical pulls
        read as scraping to the providers. Caller holds _lock."""
        return {e.query.track_id: e for e in self._entries.values()
                if e.ready.is_set() and e.audio is not None and e.query.track_id}

    def _mint(self, token: str, query: TrackQuery, index: int, chain: list,
              in_ram: dict) -> _Entry:
        """A fresh entry, adopting `in_ram` audio when the same track is already
        in hand (a re-streamed or re-queued album downloads nothing). Caller
        holds _lock."""
        prior = in_ram.get(query.track_id)
        if prior is None or prior.audio is None:
            return _Entry(token, query, index, chain=chain,
                          provider=chain[0][0] if chain else None)
        e = _Entry(token, query, index, chain=chain, provider=prior.provider)
        e.audio = prior.audio
        e.fetch_seconds = prior.fetch_seconds
        e._claimed = True
        e.ready.set()                    # already in hand — waiters return at once
        return e

    # ---- session API (called by the player endpoint) --------------------
    def start_session(self, items: list) -> list[str]:
        """Replace the current preview with an ordered track list. Each item is a
        ``(query, chain)`` pair where chain is the lossless-first fallback list of
        ``(provider, source_id)`` (see _Entry). Returns the per-track tokens; build
        URLs with ``url_for``. The tokens start UNBOUND — the caller binds them to
        the queue generation it goes on to build (see ``bind``)."""
        reused = 0
        with self._lock:
            in_ram = self._ram_index()
            for e in self._entries.values():
                if not e.ready.is_set():
                    # Wake every waiter on the dropped set (fillers block
                    # unbounded) — "superseded" is an event, not a timeout.
                    e.error = "superseded by a new preview session"
                    e.ready.set()
            self._entries.clear()
            self._fetch_q.clear()        # drop the previous session's pending fetches
            self._session = []
            for i, (q, chain) in enumerate(items):
                tok = secrets.token_urlsafe(12)
                e = self._mint(tok, q, i, chain, in_ram)
                if e.ready.is_set():
                    reused += 1
                self._entries[tok] = e
                self._session.append(tok)
        # Priority start: fetch ONLY track 0 now (full bandwidth → fastest first
        # track), unless it was reused from RAM. The caller measures its
        # throughput, then calls prefetch_from(1) to fan out the rest
        # concurrently while track 0 buffers and plays. This is rolling-append:
        # HQPlayer HEAD-probes each URI at ADD time, so we add the ready
        # prefix, play, then append the tail as each track lands.
        if self._session and not self._entries[self._session[0]].ready.is_set():
            self._prefetch(self._session[0])
        logger.info("preview session: %d tracks (%d reused from RAM)",
                    len(items), reused)
        preview_events.ping()   # new buffering set → open album page re-fetches
        return list(self._session)

    def prefetch_from(self, start_index: int) -> None:
        """Fan out concurrent (semaphore-bounded) prefetch of the session tail —
        called once track 0 is in hand so the rest download while it plays."""
        with self._lock:
            tail = self._session[start_index:]
        for tok in tail:
            self._prefetch(tok)

    def add_tracks(self, items: list, *, front: bool = False) -> list:
        """Add tracks to the served pool WITHOUT replacing the current album
        session — for queue-appends, so the album and the queued tracks are
        served at once. Each item is a ``(query, chain)`` pair (see start_session).
        Returns the new tokens (prefetched); the caller waits and places them.
        They are NOT part of the rolling-append `_session`; the next
        start_session (a replace-queue play) clears them together with the album,
        and ``bind`` ties them to the queue generation that will hold them.

        ``front`` fetches them AHEAD of everything already pending: a 'play next'
        block is wanted before the current track ends, so it cannot sit behind an
        album that was merely queued at the end (the whole point of 'next')."""
        toks = []
        with self._lock:
            in_ram = self._ram_index()
            for i, (q, chain) in enumerate(items):
                tok = secrets.token_urlsafe(12)
                self._entries[tok] = self._mint(tok, q, i, chain, in_ram)
                toks.append(tok)
        # Reversed for `front`, so head-inserting each one leaves the block in
        # its own order at the head of the fetch queue.
        for tok in (reversed(toks) if front else toks):
            self._prefetch(tok, front=front)
        preview_events.ping()   # queued tracks now buffering → re-fetch
        return toks

    # ---- generation binding (cancellation at the source) -----------------

    def bind(self, tokens: list, generation: int) -> None:
        """Attach tokens to the canonical-queue generation whose filler will
        place them, so retiring that generation cancels them. Binding to an
        ALREADY superseded generation retires immediately — that closes the
        window between submitting tracks and reading the generation they were
        submitted for."""
        with self._lock:
            for tok in tokens:
                e = self._entries.get(tok)
                if e is not None:
                    e.generation = generation
            live = self._live_generation
        if live is not None and generation != live:
            self.retire_generation(live)

    def retire_generation(self, live_generation: int) -> int:
        """Cancel work bound to a superseded queue generation: the user moved to
        another queue, so nothing will ever place these tracks. Their fetches
        leave the queue and their waiters are woken, so a filler blocked on
        `wait_ready` stops at once instead of downloading an album nobody will
        hear (and hammering the provider for it). Entries already holding audio
        stay as RAM-reuse candidates — the budget owns their memory. UNBOUND
        entries belong to the request that made them and are never touched.
        Returns the number of fetches cancelled."""
        with self._lock:
            self._live_generation = live_generation
            cancelled = 0
            for tok, e in list(self._entries.items()):
                if e.generation is None or e.generation == live_generation:
                    continue
                if e.ready.is_set() and e.audio is not None:
                    continue
                if tok in self._fetch_q:
                    self._fetch_q.remove(tok)
                del self._entries[tok]
                e.error = "superseded: the queue moved on"
                e.ready.set()
                cancelled += 1
        if cancelled:
            logger.info("preview: cancelled %d pending fetch(es) — the queue "
                        "moved to generation %d", cancelled, live_generation)
            preview_events.ping()   # those rows stopped buffering → re-fetch
        return cancelled

    def fetch_rtf(self, token: str) -> Optional[float]:
        """Real-time factor of a fetched track: wall seconds spent fetching per
        second of audio. <1 means the pipeline outruns playback on a single
        stream — the channel-speed signal the buffering policy sizes its lead by.
        None until the track is fetched and its duration is known."""
        with self._lock:
            e = self._entries.get(token)
        if e is None or e.fetch_seconds is None or not e.query.duration:
            return None
        return e.fetch_seconds / e.query.duration

    def ready_run(self, tokens: list) -> tuple[list, float, int]:
        """The leading run of already-fetched tokens, without blocking: the
        playable ones (failed fetches skipped), their total audio-seconds, and
        how many tokens the run consumed — the first not-yet-fetched token is
        the buffer boundary. This is what is safe to hand a player right now."""
        with self._lock:
            playable: list = []
            secs = 0.0
            used = 0
            for tok in tokens:
                e = self._entries.get(tok)
                if e is None or not e.ready.is_set():
                    break                       # first un-fetched track = boundary
                used += 1
                if e.audio is None:
                    continue                    # fetched-but-failed → skip, scan on
                playable.append(tok)
                secs += e.query.duration or 0.0
            return playable, secs, used

    def ready_lead(self, from_index: int) -> tuple[list[str], float, int]:
        """`ready_run` over the rolling album session, returning the session
        index to resume filling at."""
        with self._lock:
            tail = self._session[from_index:]
        playable, secs, used = self.ready_run(tail)
        return playable, secs, from_index + used

    def is_buffering(self, track_id: str) -> bool:
        """True if a track with this id is still in-flight in the served pool (an
        entry exists that hasn't finished fetching) — the UI buffering flag. Read
        by /api/albums/{id} so one re-fetch carries buffering with the features.
        A budget-evicted entry is NOT buffering — it refetches on demand and
        would otherwise show an eternal spinner."""
        if not track_id:
            return False
        with self._lock:
            return any(e.query.track_id == track_id and not e.ready.is_set()
                       and not e.evicted
                       for e in self._entries.values())

    def url_for(self, token: str) -> str:
        return f"http://{self._advertised_host}:{self.port}/preview/{token}"

    def file_url(self, token: str) -> str:
        return f"http://{self._advertised_host}:{self.port}/file/{token}"

    def preview_meta(self, uri: str) -> Optional[dict]:
        """Provider metadata for a current-session preview URI — Now Playing /
        the queue panel use it because HQPlayer only knows the track as
        'HTTP stream'. Returns None for non-preview URIs / expired sessions."""
        prefix = f"http://{self._advertised_host}:{self.port}/preview/"
        if not uri.startswith(prefix):
            return None
        token = uri[len(prefix):].split("?", 1)[0]
        with self._lock:
            e = self._entries.get(token)
        if e is None:
            return None
        return {"artist": e.query.artist, "title": e.query.title,
                "album": e.query.album, "album_id": e.query.album_id,
                "provider": e.provider.manifest.id,
                "track_id": e.query.track_id, "cover_url": e.query.cover_url,
                "duration": e.query.duration, "media_file_id": e.query.media_file_id}

    def wait_ready(self, token: str, timeout=_UNSET_TIMEOUT, *,
                   front: bool = False) -> _Entry:
        """Block until a track is fetched (used by the endpoint to absorb the
        first track's startup latency before telling HQPlayer to play).
        ``timeout=None`` waits indefinitely — safe because every enqueued token
        reaches a terminal ready (fetch success, fetch failure, or the
        superseded wake-up when a new session drops the set). ``front``
        priority-fetches (an on-demand read, not a background fill)."""
        if timeout is _UNSET_TIMEOUT:
            timeout = self._prepare_timeout
        return self._ensure_ready(token, timeout, front=front)

    # ---- preparation -----------------------------------------------------
    def _prefetch(self, token: str, *, front: bool = False) -> None:
        """Enqueue a token for the single sequential fetch worker (FIFO = playlist
        order). Idempotent — a claimed token is a no-op. ``front`` puts the token
        at the HEAD of the queue (and promotes an already-queued one): on-demand
        requests and the playback window outrank background album fills."""
        with self._fetch_cv:
            e = self._entries.get(token)
            if e is None or e._claimed:
                return
            e.evicted = False
            if token in self._fetch_q:
                if front and self._fetch_q[0] != token:
                    self._fetch_q.remove(token)
                    self._fetch_q.insert(0, token)
                return
            if front:
                self._fetch_q.insert(0, token)
            else:
                self._fetch_q.append(token)
            if not self._fetch_worker:
                self._fetch_worker = True
                threading.Thread(target=self._fetch_loop, daemon=True,
                                 name="preview-fetch").start()
            self._fetch_cv.notify()

    def _fetch_loop(self) -> None:
        """One worker fetches the queue strictly in order, one track at a time."""
        while True:
            with self._fetch_cv:
                while not self._fetch_q:
                    self._fetch_cv.wait()
                token = self._fetch_q.pop(0)
            self._prepare(token)        # serial — full pipe per track, in order

    def _prepare(self, token: str) -> None:
        with self._lock:
            e = self._entries.get(token)
            if e is None or e._claimed:
                return
            e._claimed = True
        try:
            # Walk the lossless-first chain: a provider that RESOLVED but can't
            # DOWNLOAD (Deezer has the track but no FLAC for this account) falls
            # through to the next provider (e.g. YouTube) instead of dropping it.
            last_err = None
            for prov, sid in e.chain:
                started = time.monotonic()
                try:
                    e.audio = prov.download(sid) if sid else prov.fetch(e.query)
                    e.provider = prov                     # the one that actually served
                    e.fetch_seconds = time.monotonic() - started
                    break
                except ProviderError as ex:
                    last_err = ex
                    logger.warning("preview fetch failed [%d] %s — %s via %s: %s",
                                   e.index, e.query.artist, e.query.title,
                                   prov.manifest.id, ex)
            if e.audio is None and last_err is not None:
                e.error = str(last_err)
        except Exception as ex:  # provider bug — surface, don't hang the GET
            e.error = f"unexpected: {ex}"
            logger.error("preview fetch crashed [%d]: %s", e.index, ex, exc_info=True)
        finally:
            e.ready.set()
            preview_events.ping()   # buffering ended → re-fetch picks up the change
            hook = self.on_track_ready
            if hook is not None and e.audio is not None:
                try:
                    hook(e)
                except Exception:
                    logger.exception("on_track_ready hook failed [%d]", e.index)

    def _peek(self, token: str) -> Optional[_Entry]:
        """Lookup without triggering a fetch or advancing — for cheap HEAD."""
        with self._lock:
            return self._entries.get(token)

    def _ensure_ready(self, token: str, timeout: Optional[float], *,
                      front: bool = False) -> _Entry:
        with self._lock:
            e = self._entries.get(token)
        if e is None:
            raise KeyError(token)
        if not e._claimed:
            self._prefetch(token, front=front)  # enqueue (keeps fetching strictly sequential)
        if not e.ready.wait(timeout):
            raise TimeoutError(f"preview not ready: {e.query.title}")
        return e

    def _advance(self, index: int) -> None:
        """Ensure the next track is fetched. Never evicts on GET — HQPlayer
        GETs every URI at ADD time (not in playback order), so that dropped
        tracks before they were ever played. Memory-bounded eviction lives in
        the manager's playback-position window (enforce_budget)."""
        with self._lock:
            nxt = index + 1
            tok = self._session[nxt] if nxt < len(self._session) else None
        if tok:
            self._prefetch(tok)


def _make_handler(proxy: MediaProxy):
    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.0 (the default): each request gets its own connection that
        # closes after the response. HQPlayer probe-then-streams and resets
        # connections; keep-alive (1.1) desynced those and playback never
        # started. Matches the plain http.server that played reliably in bring-up.

        def log_message(self, fmt, *a):  # quiet by default; -d to trace probes
            logger.debug("proxy %s: %s", self.address_string(), fmt % a)

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
            rng = self.headers.get("Range")
            if not (rng and rng.startswith("bytes=")):
                return None
            try:
                s, _, en = rng[len("bytes="):].partition("-")
                start = int(s) if s else 0
                end = int(en) if en else total - 1
                end = min(end, total - 1)
                if 0 <= start <= end:
                    return start, end
            except ValueError:
                pass
            return None

        def _serve_file(self, fe, *, body: bool):
            import os
            path, mime, size = fe.path, fe.mime, fe.size
            if fe.start is not None:
                # CUE slice: the cached FLAC cut IS the resource — the raw
                # image would play the whole disc, so there is no lossless
                # fallback on failure here, only an error.
                try:
                    path = transcode.flac_slice_path_for_file(
                        fe.path, fe.start, fe.end, fe.tags or {})
                except Exception as e:
                    logger.error("flac slice failed %s [%s, %s): %s",
                                 fe.path, fe.start, fe.end, e)
                    self.send_error(500)
                    return
                mime, size = transcode.FLAC_MIME, os.path.getsize(path)
            q = self._q()
            if transcode.wants_opus(q):
                try:
                    # For a slice `path` is already the cut, so the Opus tier
                    # chains off it with slice-relative ?ss for free.
                    opus = transcode.opus_path_for_file(path, q, ss=self._ss())
                    if opus:
                        path, mime, size = opus, transcode.MIME, os.path.getsize(opus)
                except Exception as e:
                    logger.warning("opus transcode failed %s — lossless: %s",
                                   path, e)
            self._serve_disk_file(path, mime, size, body=body)

        def _serve_disk_file(self, path: str, mime: str, size: int, *, body: bool):
            span = self._parse_range(size)
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
            except (BrokenPipeError, ConnectionResetError):
                pass   # renderers probe-then-reset, like HQPlayer
            except OSError as e:
                logger.error("file serve failed %s: %s", path, e)

        def _opus_for_preview(self, token, e) -> Optional[str]:
            """Cached Opus path for a phantom buffer when ?q asks for it, else
            None (serve the lossless in-memory bytes)."""
            q = self._q()
            if not transcode.wants_opus(q):
                return None
            try:
                return transcode.opus_path_for_bytes(token, e.audio.data, q,
                                                     ss=self._ss())
            except Exception as ex:
                logger.warning("opus transcode failed for preview %s — lossless: %s",
                               token, ex)
                return None

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
            atok = self._art_token()
            if atok is not None:
                self._serve_art(atok, body=False)
                return
            ftok = self._file_token()
            if ftok is not None:
                fe = proxy.file_entry(ftok)
                if fe is None:
                    self.send_error(404, "unknown token")
                    return
                self._serve_file(fe, body=False)
                return
            # HQPlayer HEAD-probes each URI synchronously when it is ADDED and
            # needs Content-Length to accept it (the FLAC size is only known
            # after the fetch). The session prefetches the whole album and the
            # endpoint waits before adding, so these HEADs return immediately.
            tok = self._token()
            if tok is None:
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
            opus = self._opus_for_preview(tok, e)
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
            atok = self._art_token()
            if atok is not None:
                self._serve_art(atok, body=True)
                return
            ftok = self._file_token()
            if ftok is not None:
                fe = proxy.file_entry(ftok)
                if fe is None:
                    self.send_error(404, "unknown token")
                    return
                self._serve_file(fe, body=True)
                return
            tok = self._token()
            if tok is None:
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
            proxy._advance(e.index)
            opus = self._opus_for_preview(tok, e)
            if opus:
                import os
                self._serve_disk_file(opus, transcode.MIME,
                                      os.path.getsize(opus), body=True)
                return
            data = e.audio.data
            total = len(data)
            rng = self.headers.get("Range")
            start, end = 0, total - 1
            partial = False
            if rng and rng.startswith("bytes="):
                try:
                    s, _, en = rng[len("bytes="):].partition("-")
                    start = int(s) if s else 0
                    end = int(en) if en else total - 1
                    end = min(end, total - 1)
                    partial = 0 <= start <= end
                except ValueError:
                    partial = False
            if partial:
                self._send_headers(e, end - start + 1, full=False, body=True,
                                   start=start, end=end, total=total)
                self._write(data[start:end + 1])
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

        def _write(self, chunk: bytes):
            # HQPlayer probes then resets connections — that's normal, not an error.
            try:
                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler
