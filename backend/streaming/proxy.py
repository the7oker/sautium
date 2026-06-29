"""In-memory media proxy: serves provider-fetched audio to HQPlayer over plain
http (HQPlayer can't sign HMAC nor trust the backend's self-signed TLS, so this
is a SEPARATE plain-http endpoint, localhost/LAN-reachable, gated by per-track
ephemeral tokens).

Design points proven during bring-up:
- HQPlayer plays http FLAC sustained in its NATIVE playlist (alongside file://
  owned tracks) — so a preview track is just one more playlist URI.
- A provider fetch takes seconds (search + download + transcode), so we PREFETCH
  the next track off the request path; HQPlayer's GET then hits a ready buffer.
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

from .base import FetchedAudio, ProviderError, StreamProvider, TrackQuery
from .events import preview_events

logger = logging.getLogger(__name__)

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

    # ---- session API (called by the player endpoint) --------------------
    def start_session(self, items: list) -> list[str]:
        """Replace the current preview with an ordered track list. Each item is a
        ``(query, chain)`` pair where chain is the lossless-first fallback list of
        ``(provider, source_id)`` (see _Entry). Returns the per-track tokens; build
        URLs with ``url_for``."""
        with self._lock:
            self._entries.clear()
            self._fetch_q.clear()        # drop the previous session's pending fetches
            self._session = []
            for i, (q, chain) in enumerate(items):
                tok = secrets.token_urlsafe(12)
                self._entries[tok] = _Entry(tok, q, i, chain=chain,
                                            provider=chain[0][0] if chain else None)
                self._session.append(tok)
        # Priority start: fetch ONLY track 0 now (full bandwidth → fastest first
        # track). The caller measures its throughput, then calls prefetch_from(1)
        # to fan out the rest concurrently while track 0 buffers and plays. This
        # is rolling-append: HQPlayer HEAD-probes each URI at ADD time, so we add
        # the ready prefix, play, then append the tail as each track lands.
        if self._session:
            self._prefetch(self._session[0])
        logger.info("preview session: %d tracks", len(items))
        preview_events.ping()   # new buffering set → open album page re-fetches
        return list(self._session)

    def prefetch_from(self, start_index: int) -> None:
        """Fan out concurrent (semaphore-bounded) prefetch of the session tail —
        called once track 0 is in hand so the rest download while it plays."""
        with self._lock:
            tail = self._session[start_index:]
        for tok in tail:
            self._prefetch(tok)

    def add_tracks(self, items: list) -> list:
        """Add tracks to the served pool WITHOUT replacing the current album
        session — for queue-appends, so the album and the queued tracks are
        served at once. Each item is a ``(query, chain)`` pair (see start_session).
        Returns the new tokens (prefetched);
        the caller waits and appends them to HQPlayer. They are NOT part of the
        rolling-append `_session`; the next start_session (a replace-queue play)
        clears them together with the album, in step with HQPlayer's queue clear."""
        toks = []
        with self._lock:
            for i, (q, chain) in enumerate(items):
                tok = secrets.token_urlsafe(12)
                self._entries[tok] = _Entry(tok, q, i, chain=chain,
                                            provider=chain[0][0] if chain else None)
                toks.append(tok)
        for tok in toks:
            self._prefetch(tok)
        preview_events.ping()   # queued tracks now buffering → re-fetch
        return toks

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

    def ready_lead(self, from_index: int) -> tuple[list[str], float, int]:
        """The contiguous run of already-fetched tracks from `from_index`: the
        playable tokens (failed fetches skipped), their total audio-seconds, and
        the next index to resume at (the first not-yet-fetched track — the buffer
        boundary). This is what is safe to hand HQPlayer right now."""
        with self._lock:
            playable: list[str] = []
            secs = 0.0
            idx = from_index
            for j in range(from_index, len(self._session)):
                e = self._entries.get(self._session[j])
                if e is None or not e.ready.is_set():
                    break                       # first un-fetched track = boundary
                idx = j + 1
                if e.audio is None:
                    continue                    # fetched-but-failed → skip, scan on
                playable.append(self._session[j])
                secs += e.query.duration or 0.0
            return playable, secs, idx

    def is_buffering(self, track_id: str) -> bool:
        """True if a track with this id is still in-flight in the served pool (an
        entry exists that hasn't finished fetching) — the UI buffering flag. Read
        by /api/albums/{id} so one re-fetch carries buffering with the features."""
        if not track_id:
            return False
        with self._lock:
            return any(e.query.track_id == track_id and not e.ready.is_set()
                       for e in self._entries.values())

    def url_for(self, token: str) -> str:
        return f"http://{self._advertised_host}:{self.port}/preview/{token}"

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
                "album": e.query.album, "provider": e.provider.manifest.id,
                "track_id": e.query.track_id, "cover_url": e.query.cover_url,
                "duration": e.query.duration}

    def wait_ready(self, token: str, timeout: Optional[float] = None) -> _Entry:
        """Block until a track is fetched (used by the endpoint to absorb the
        first track's startup latency before telling HQPlayer to play)."""
        return self._ensure_ready(token, timeout if timeout is not None
                                  else self._prepare_timeout)

    # ---- preparation -----------------------------------------------------
    def _prefetch(self, token: str) -> None:
        """Enqueue a token for the single sequential fetch worker (FIFO = playlist
        order). Idempotent — a claimed or already-queued token is a no-op."""
        with self._fetch_cv:
            e = self._entries.get(token)
            if e is None or e._claimed or token in self._fetch_q:
                return
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

    def _ensure_ready(self, token: str, timeout: float) -> _Entry:
        with self._lock:
            e = self._entries.get(token)
        if e is None:
            raise KeyError(token)
        if not e._claimed:
            self._prefetch(token)           # enqueue (keeps fetching strictly sequential)
        if not e.ready.wait(timeout):
            raise TimeoutError(f"preview not ready: {e.query.title}")
        return e

    def _advance(self, index: int) -> None:
        """Ensure the next track is fetched. Buffers are kept for the whole
        session: HQPlayer GETs every URI at ADD time (not in playback order),
        so evicting on GET dropped tracks before they were ever played. Memory-
        bounded eviction keyed on the real playback position is a follow-up."""
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

        def do_HEAD(self):
            # HQPlayer HEAD-probes each URI synchronously when it is ADDED and
            # needs Content-Length to accept it (the FLAC size is only known
            # after the fetch). The session prefetches the whole album and the
            # endpoint waits before adding, so these HEADs return immediately.
            tok = self._token()
            if tok is None:
                return
            try:
                e = proxy._ensure_ready(tok, proxy._prepare_timeout)
            except KeyError:
                self.send_error(404, "unknown token")
                return
            except TimeoutError:
                self.send_error(504, "preview preparation timed out")
                return
            if e.error or e.audio is None:
                self.send_error(502, "provider could not fetch this track")
                return
            self.send_response(200)
            self.send_header("Content-Type", e.audio.mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(e.audio.data)))
            self.end_headers()

        def do_GET(self):
            tok = self._token()
            if tok is None:
                return
            try:
                e = proxy._ensure_ready(tok, proxy._prepare_timeout)
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
            self.end_headers()

        def _write(self, chunk: bytes):
            # HQPlayer probes then resets connections — that's normal, not an error.
            try:
                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler
