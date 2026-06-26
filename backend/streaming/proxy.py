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
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .base import FetchedAudio, ProviderError, StreamProvider, TrackQuery

logger = logging.getLogger(__name__)

@dataclass
class _Entry:
    token: str
    provider: StreamProvider
    query: TrackQuery
    index: int                      # position in the session, for prefetch/eviction
    ready: threading.Event = field(default_factory=threading.Event)
    audio: Optional[FetchedAudio] = None
    error: Optional[str] = None
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
        self._fetch_sem = threading.Semaphore(3)   # bound concurrent yt-dlp+ffmpeg
        self._entries: dict[str, _Entry] = {}
        self._session: list[str] = []   # ordered tokens of the current preview
        self._httpd: Optional[ThreadingHTTPServer] = None

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self._bind_host, self.port), handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True,
                         name="media-proxy").start()
        logger.info("media proxy on %s:%d (advertised host %s)",
                    self._bind_host, self.port, self._advertised_host)

    # ---- session API (called by the player endpoint) --------------------
    def start_session(self, provider: StreamProvider,
                      queries: list[TrackQuery]) -> list[str]:
        """Replace the current preview with an ordered track list. Returns the
        per-track tokens; build URLs with ``url_for`` and hand them to HQPlayer.
        Track 0 (and 1) are prefetched immediately."""
        with self._lock:
            self._entries.clear()
            self._session = []
            for i, q in enumerate(queries):
                tok = secrets.token_urlsafe(12)
                self._entries[tok] = _Entry(tok, provider, q, i)
                self._session.append(tok)
        # Prefetch the whole album (bounded by _fetch_sem). HQPlayer HEAD-probes
        # every URI synchronously at add time and needs Content-Length, so the
        # buffers must exist before we hand the URLs over.
        for tok in self._session:
            self._prefetch(tok)
        logger.info("preview session: %d tracks (%s)", len(queries), provider.manifest.id)
        return list(self._session)

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
                "album": e.query.album, "provider": e.provider.manifest.id}

    def wait_ready(self, token: str, timeout: Optional[float] = None) -> _Entry:
        """Block until a track is fetched (used by the endpoint to absorb the
        first track's startup latency before telling HQPlayer to play)."""
        return self._ensure_ready(token, timeout if timeout is not None
                                  else self._prepare_timeout)

    # ---- preparation -----------------------------------------------------
    def _prefetch(self, token: str) -> None:
        threading.Thread(target=self._prepare, args=(token,), daemon=True,
                         name=f"prefetch-{token[:6]}").start()

    def _prepare(self, token: str) -> None:
        with self._lock:
            e = self._entries.get(token)
            if e is None or e._claimed:
                return
            e._claimed = True
        try:
            with self._fetch_sem:
                audio = e.provider.fetch(e.query)
            e.audio = audio
        except ProviderError as ex:
            e.error = str(ex)
            logger.warning("preview fetch failed [%d] %s — %s: %s",
                           e.index, e.query.artist, e.query.title, ex)
        except Exception as ex:  # provider bug — surface, don't hang the GET
            e.error = f"unexpected: {ex}"
            logger.error("preview fetch crashed [%d]: %s", e.index, ex, exc_info=True)
        finally:
            e.ready.set()

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
            self._prepare(token)            # synchronous fallback (GET before prefetch)
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
