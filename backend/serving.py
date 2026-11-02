"""The backend's uvicorn server, as a node runs it — the Docker entrypoint
(not its development reload mode), the launcher's (desktop/backend_serve.py)
and the peer surface inside the app (main._serve_p2p): one that says the
signal stopping it. uvicorn stops on SIGINT and SIGTERM without a word of
which; a hangup, which it does not handle, ended the process at Python's
default — no word in the log, no lifespan shutdown (2026-10-18).

And the app's event streams (EventStream), which end as a server's stop
begins. They never end by themselves: uvicorn waited out its whole graceful
window on them, then cancelled them — a traceback for each in the log, and
five of the ten seconds the launcher gives a stop (2026-10-25).

And what a request names (request_target): every check that admits a
request reads the scope uvicorn routes on, never a URL Starlette builds
again from the Host header.

Light at import: the entrypoint imports it before the database is up.
"""

import asyncio
import logging
import os
import signal
import sys
import threading
import weakref

import anyio
import uvicorn
from starlette.responses import StreamingResponse

logger = logging.getLogger(__name__)

# Every open event stream as (its cancel scope, its loop), and the loops whose
# server began to stop: a stream that starts on one of them ends at once
_streams: set = set()
_stopping = weakref.WeakSet()
_streams_lock = threading.Lock()


def request_target(scope) -> str:
    """The request-target as the client sent it: the path's own bytes and the
    query string, both as received. A signature over a request covers this;
    the route is the decoded `scope["path"]`, a function of these bytes, so
    the signature binds it. `request.url` is no stand-in: Starlette builds it
    again from the Host header, and on some releases its path is not the one
    routed."""
    target = (scope.get("raw_path") or scope["path"].encode()).decode("latin-1")
    query = scope.get("query_string") or b""
    return target + "?" + query.decode("latin-1") if query else target


class EventStream(StreamingResponse):
    """A text/event-stream response from an async generator, ended — as a
    completed response, one the client reconnects to — when the server's
    stop begins: the stop cancels what the generator waits on, as a
    client's leaving does, and the response is closed in order. A stop
    that meets it mid-send leaves the generator at its yield, closed here.
    A client that knows a stream's end by its own words reads a close
    before them as a drop: the chat's reply ends at its `done`
    (app-shell.js `_pumpStream`)."""

    def __init__(self, content, headers=None) -> None:
        super().__init__(content, media_type="text/event-stream",
                         headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                                  "X-Accel-Buffering": "no", **(headers or {})})

    async def stream_response(self, send) -> None:
        await send({"type": "http.response.start", "status": self.status_code,
                    "headers": self.raw_headers})
        loop = asyncio.get_running_loop()
        with anyio.CancelScope() as scope:
            with _streams_lock:
                _streams.add((scope, loop))
                stopping = loop in _stopping
            if stopping:
                scope.cancel()
            try:
                async for chunk in self.body_iterator:
                    if not isinstance(chunk, bytes | memoryview):
                        chunk = chunk.encode(self.charset)
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
            finally:
                with _streams_lock:
                    _streams.discard((scope, loop))
        if scope.cancel_called:
            await self.body_iterator.aclose()
        await send({"type": "http.response.body", "body": b"", "more_body": False})


def _end_streams() -> None:
    """The server on this loop begins to stop: its event streams end now,
    and one that starts from here on ends at once — every stream on the
    loop, a second server's beside it too: the process is going."""
    loop = asyncio.get_running_loop()
    with _streams_lock:
        _stopping.add(loop)
        ending = [scope for scope, its in _streams if its is loop]
    for scope in ending:
        scope.cancel()


class PeerSurfaceConfig(uvicorn.Config):
    """The peer surface's server settings (main._serve_p2p): no access line
    — a peer's address and the text of a search it sent are not this node's
    log, and the contact log keeps what the defences need, for 30 days
    (desktop/p2p/contact_log.py) — and no logging setup of its own.

    The access logger is one per process, shared with the Web UI's server,
    and uvicorn's protocol writes to it whenever it has a handler; its own
    `access_log=False` works by emptying that logger, which would silence the
    Web UI too. So this server's protocol is told by itself, and the
    process's logging stays as its start made it (config.LOGGING_CONFIG).

    No WebSocket either: the surface has no WebSocket route, and uvicorn's
    WebSocket protocol logs each handshake with the asker's address and the
    path it asked for, query and all — an upgrade is answered as the plain
    request it also is."""

    def configure_logging(self) -> None:
        pass

    def load(self) -> None:
        self.ws = "none"
        super().load()
        protocol = self.http_protocol_class

        class WithoutAccessLine(protocol):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.access_log = False

            def _unsupported_upgrade_warning(self) -> None:
                # Anyone may ask this surface for an upgrade; answered as a
                # plain request, it warns of nothing here
                self.logger.debug("Unsupported upgrade request.")

        self.http_protocol_class = WithoutAccessLine


class Server(uvicorn.Server):
    """uvicorn's server, saying the first signal that stops it and what it
    stops (`what`). A signal while it stops changes nothing: uvicorn would
    cut the lifespan's shutdown short at a SIGINT after the first signal
    (its force exit), and one arrives whenever a session's end signals each
    of its processes and the launcher, still quitting, stops the backend
    with SIGINT. So uvicorn's "(CTRL+C to force quit)" does not hold here: a
    stop that hangs is the launcher's or Docker's to kill. Its stop ends
    the event streams before uvicorn waits for the open connections, so the
    wait is for requests that end."""

    def __init__(self, config: uvicorn.Config, what: str = "the backend") -> None:
        super().__init__(config)
        self.what = what

    def handle_exit(self, sig, frame) -> None:
        if self.should_exit:
            return
        logger.info("%s received: %s shuts down", signal.Signals(sig).name, self.what)
        super().handle_exit(sig, frame)

    async def shutdown(self, sockets=None) -> None:
        _end_streams()
        await super().shutdown(sockets=sockets)


def undumpable() -> None:
    """Linux: this process becomes undumpable — its memory and its
    /proc/<pid>/environ readable by root alone, not by the agent CLIs, which
    run as the same user. A process exec'd as that user is dumpable again, so
    each says it for itself: the Docker entrypoint's (a reload supervisor's
    environment keeps the credentials its server is spawned with), and every
    process that starts agent CLIs (hold_secrets). Elsewhere nothing to do."""
    if sys.platform != "linux":
        return
    import ctypes
    PR_SET_DUMPABLE = 4
    if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_DUMPABLE) failed")


def hold_secrets() -> None:
    """What a process that starts agent CLIs does first — the app's lifespan,
    the gear research worker's CLI: become undumpable, and drop
    config.SECRET_ENV from its environment, in any case (the settings read
    names case-insensitively); the settings, built at import, keep the
    values. A child it starts — an agent CLI, ffmpeg, pg_restore — inherits
    none of them, so none shows in a /proc/<pid>/environ the CLIs' user can
    read. The backup CLI starts no agent: it reads its password by name
    (`--password-env P2P_PASSWORD`), drops that variable alone and is
    undumpable from its main()."""
    undumpable()
    from config import SECRET_ENV
    for name in [n for n in os.environ if n.upper() in SECRET_ENV]:
        del os.environ[name]


def heard_until_served() -> None:
    """Until a server holds them: SIGTERM, SIGINT and a hangup end the
    process at once, said first. PID 1 in a container has no default action
    for SIGTERM, so a stop during the wait for the database was not heard
    at all and waited out the grace's SIGKILL."""
    for sig in _STOPS:
        signal.signal(sig, _before_served)


def served() -> None:
    """The signals handed to the server about to start: SIGTERM and SIGINT
    back at their defaults — the server takes them, and restores and raises
    again what it found when it stops — and a hangup stopping it as SIGTERM
    does, which uvicorn does not handle. From the main thread, as every
    signal handler."""
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _hangup)


# The stops a node hears before it serves; Windows has no SIGHUP
_STOPS = tuple(getattr(signal, name) for name in ("SIGTERM", "SIGINT", "SIGHUP")
               if hasattr(signal, name))


def _before_served(sig, frame) -> None:
    logger.info("%s received before the server started: the backend stops",
                signal.Signals(sig).name)
    raise SystemExit(0)


def _hangup(sig, frame) -> None:
    logger.info("SIGHUP received: taken as SIGTERM")
    signal.raise_signal(signal.SIGTERM)
