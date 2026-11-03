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
again from the Host header. And what a request may weigh (BodyBudget): a
body read whole, within bounds, by a gate that must read it before it can
judge it (auth_hmac's door).

Light at import: the entrypoint imports it before the database is up.
"""

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import ssl
import sys
import threading
import weakref
from typing import Optional

import anyio
import uvicorn
from starlette.datastructures import Headers
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


class BodyBudget:
    """An HTTP request's body read whole before the app runs — at most `cap`
    bytes, within `deadline` seconds — while the bodies read through this
    budget and still held share `budget` bytes. Uvicorn reads a body only as
    the app asks for it, and Starlette joins what it is given with no
    ceiling: this is the ceiling, for a gate that must read a body before it
    can judge it (auth_hmac.HMACAuthMiddleware)."""

    def __init__(self, *, cap: int, budget: int, deadline: float) -> None:
        self.cap, self.budget, self.deadline = cap, budget, deadline
        self.held = 0            # bytes read or held through this budget now; event-loop only

    @contextlib.asynccontextmanager
    async def reading(self, scope, receive, send):
        """`async with budget.reading(scope, receive, send) as (body, refused):`
        — the whole body, counted in the budget until the block ends, and
        None; or None and the status of the refusal answered: a declared
        length over the cap before a byte is read (413), a body that grows
        past it (413), one still arriving at the deadline (408), one the
        budget cannot hold (503), a client that leaves mid-body (400, which
        nobody reads)."""
        taken = [0]
        try:
            yield await self._read(scope, receive, send, taken)
        finally:
            self.held -= taken[0]

    async def dropping(self, scope, receive) -> None:
        """A body declared within the cap read and dropped, chunk by chunk,
        until the deadline, and counted nowhere — it holds nothing: for an
        answer decided without it. A connection closed over an unread body
        can reset under the answer; Windows drops what it had received."""
        length = Headers(scope=scope).get("content-length", "")
        if not (length.isdigit() and 0 < int(length) <= self.cap):
            return
        with contextlib.suppress(TimeoutError):           # the answer goes as it stands
            async with asyncio.timeout(self.deadline):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect" or not message.get("more_body", False):
                        return

    async def _read(self, scope, receive, send, taken: list) -> tuple:
        length = Headers(scope=scope).get("content-length", "")
        if length.isdigit() and int(length) > self.cap:
            await refuse(send, 413, "request body too large")
            return None, 413
        chunks, refusal = [], None
        try:
            async with asyncio.timeout(self.deadline):
                while refusal is None:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        refusal = (400, "request body not received")
                        break
                    chunk = message.get("body", b"")
                    if taken[0] + len(chunk) > self.cap:
                        refusal = (413, "request body too large")
                    elif self.held + len(chunk) > self.budget:
                        refusal = (503, "busy: too many bodies in flight")
                    else:
                        self.held += len(chunk)
                        taken[0] += len(chunk)
                        chunks.append(chunk)
                        if not message.get("more_body", False):
                            return b"".join(chunks), None
        except TimeoutError:
            refusal = (408, "request body not received in time")
        await refuse(send, *refusal)
        return None, refusal[0]


def replaying(body: bytes, receive):
    """A `receive` that hands the app `body` whole, then the client's own
    messages (a disconnect among them)."""
    handed = False

    async def replay():
        nonlocal handed
        if handed:
            return await receive()
        handed = True
        return {"type": "http.request", "body": body, "more_body": False}
    return replay


async def refuse(send, status: int, detail: str, headers: tuple = ()) -> None:
    """A JSON refusal that closes the connection: the request's body may
    still be on its way, unread."""
    payload = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(payload)).encode()),
                            (b"connection", b"close"), *headers]})
    await send({"type": "http.response.body", "body": payload})


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
    """The peer surface's server settings (main._serve_p2p), served by the
    peer protocol (`http=peer_protocol(...)`, which writes no access line
    and takes no WebSocket): no logging setup of its own.

    The access logger is one per process, shared with the Web UI's server,
    and uvicorn's protocol writes to it whenever it has a handler; its own
    `access_log=False` works by emptying that logger, which would silence the
    Web UI too. So the peer protocol is told by itself, and the process's
    logging stays as its start made it (config.LOGGING_CONFIG)."""

    def configure_logging(self) -> None:
        pass


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


class _Census:
    """The connections a surface holds, in all and from each address — on
    the loop, so no lock. A `shared` address (the Docker gateway, which an
    address-hiding NAT gives every peer) has no share of its own: the total
    is its bound."""

    def __init__(self, total: int, per_address: Optional[int], shared: frozenset = frozenset()):
        self.total, self.per_address, self.shared = total, per_address, shared
        self.open = 0
        self.by: dict[str, int] = {}

    def admit(self, addr: str) -> bool:
        if self.open >= self.total or (
                self.per_address is not None and addr not in self.shared
                and self.by.get(addr, 0) >= self.per_address):
            return False
        self.open += 1
        self.by[addr] = self.by.get(addr, 0) + 1
        return True

    def leave(self, addr: str) -> None:
        self.open -= 1
        if self.by[addr] == 1:
            del self.by[addr]
        else:
            self.by[addr] -= 1


def _host(transport) -> str:
    peer = transport.get_extra_info("peername")
    return str(peer[0]) if peer else ""


# The peer addresses that stand for many peers: the Docker gateway, which an
# address-hiding NAT (Docker Desktop's published port) gives every one of
# them. Per-address shares do not apply to one — the surface's totals do.
# Set by the peer server as it starts (main._serve_p2p).
SHARED_ADDRESSES: frozenset = frozenset()


def is_shared(addr) -> bool:
    return addr in SHARED_ADDRESSES


# What the kernel keeps unsent on a peer connection before it asks for more
# (TCP_NOTSENT_LOWAT, Linux's option 25): its send buffer would otherwise
# grow to megabytes and wake the writer a third of it at a time — the drain
# watch would see a steady slow reader take nothing for a window and cut it.
_NOTSENT_LOWAT = 64 * 1024
_TCP_NOTSENT_LOWAT = 25
_lowat_refused = False


def _small_send_buffer(transport) -> None:
    """The option on a connection just accepted. A kernel that refuses it
    (it has had it since 3.13) costs the connection nothing — said once:
    the drain watch then measures what the kernel's buffer takes, in its
    steps."""
    global _lowat_refused
    if sys.platform != "linux" or _lowat_refused:
        return
    try:
        transport.get_extra_info("socket").setsockopt(
            socket.IPPROTO_TCP, _TCP_NOTSENT_LOWAT, _NOTSENT_LOWAT)
    except OSError as e:
        _lowat_refused = True
        logger.warning("the kernel refuses TCP_NOTSENT_LOWAT (%s): a steady slow peer "
                       "may be let go as stalled", e)


# The peer connections that owe their peer something now — what owed_bytes
# sums (on the loop, so no lock).
_OWING: set = set()


def owed_bytes() -> int:
    """What this node's peer connections hold for their peers, written and
    not yet taken: a bulk answer is not made past a ceiling of it
    (routers/sync._answer)."""
    return sum(c._unsent() for c in _OWING)


# The least a peer must take of what is owed it, in bytes a second, measured
# over each stall_s: a link slower than this holds its answer, buffered here,
# for as long as the answer takes.
DRAIN_FLOOR = 8 * 1024


class _Transport:
    """The HTTP protocol's view of its transport: its writes and its close
    start the connection's drain watch (peer_protocol)."""

    def __init__(self, owner, transport):
        self._owner, self._t = owner, transport

    def write(self, data) -> None:
        self._t.write(data)
        self._owner._wrote(len(data))

    def writelines(self, lines) -> None:
        for data in lines:
            self.write(data)

    def close(self) -> None:
        self._t.close()
        self._owner._wrote(0)

    def __getattr__(self, name):
        return getattr(self._t, name)


def peer_protocol(base: type, *, head_s: float, head_max: int, stall_s: float,
                  max_connections: int, per_address: Optional[int],
                  shared: frozenset = frozenset(), tls: Optional[ssl.SSLContext] = None):
    """uvicorn's HTTP protocol class `base`, bounded for a surface strangers
    reach — what uvicorn takes as `http=`. uvicorn bounds none of this:

    - a request head not whole within `head_s` of the connection's opening
      (or of its first byte after the previous response), or over
      `head_max` bytes — a head sent while the previous request is answered
      too —, ends the connection; httptools has no head size of its own;
    - a peer that takes less than DRAIN_FLOOR a second of what is owed it,
      over any `stall_s`, has its connection aborted and the answer held for
      it freed; one reading slowly but steadily gets the whole of it — a
      close waits for it, under TLS too (no shutdown timeout of its own
      cuts it) —, and a close whose answer is out ends within `stall_s`;
      what all of them owe together is owed_bytes();
    - at most `max_connections` at once and `per_address` from any one
      address (None behind a trusted front, where every connection is the
      front's; none for a `shared` one); one past either is closed before a
      byte is read;
    - with `tls`, the handshake is made here (loop.start_tls) within
      `head_s`: uvicorn gives asyncio no handshake timeout, whose own is
      60 s, and a connection in its handshake was one no limit counted;
    - no access line — a peer's address and the text of a search it sent
      are not this node's log; the contact log keeps what the defences
      need, for 30 days (desktop/p2p/contact_log.py) — and no WebSocket:
      the surface has no WebSocket route, uvicorn's WebSocket protocol
      logs each handshake with the asker's address and the path it asked
      for, query and all, and an upgrade would hand the transport to it,
      away from the census. An upgrade is answered as the plain request it
      also is: a connection is this protocol's to its end.

    A request in progress, a long event stream included, is never cut by
    the head clock."""
    census = _Census(max_connections, per_address, shared)

    class Bounded(base):
        _addr: Optional[str] = None       # its census entry, once admitted
        # What a TLS connection delivers before start_tls returns it: the
        # request bytes that rode the handshake's last flight, or its loss.
        _early: Optional[list] = None
        _lost = False
        _raw = None                       # the TCP transport under TLS

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.access_log = False
            # An upgraded connection's connection_lost is another protocol's:
            # this one's census entry would stay taken for good
            self.ws_protocol_class = None

        def _unsupported_upgrade_warning(self) -> None:
            # Anyone may ask this surface for an upgrade; answered as a
            # plain request, it warns of nothing here
            self.logger.debug("Unsupported upgrade request.")

        def connection_made(self, transport):
            self._head_timer = self._watch_timer = None
            self._head_bytes = self._written = self._owed = 0
            self._out = self._gone = False
            self._app = transport
            if self._raw is None:
                self._raw = transport
            super().connection_made(_Transport(self, transport))
            if self._addr is None:
                addr = _host(transport)
                if not census.admit(addr):
                    transport.abort()
                    return
                self._addr = addr
                _small_send_buffer(transport)
            self._arm_head()
            early, self._early = self._early, None
            for data in early or ():
                self.data_received(data)

        def _between_requests(self) -> bool:
            return self.cycle is None or self.cycle.response_complete

        def _arm_head(self) -> None:
            if self._head_timer is None:
                self._head_timer = self.loop.call_later(head_s, self._head_expired)

        def _disarm_head(self) -> None:
            if self._head_timer is not None:
                self._head_timer.cancel()
                self._head_timer = None

        def _head_expired(self) -> None:
            self._head_timer = None
            if self._between_requests() and not self._app.is_closing():
                logger.info("connection closed: no request head within %s s", head_s)
                self._app.abort()

        def data_received(self, data: bytes) -> None:
            if self.transport is None:
                self._early = (self._early or []) + [data]
                return
            cycle = self.cycle
            if self._between_requests():
                self._arm_head()
                self._head_bytes += len(data)
            elif not cycle.more_body:
                # Past a request's body, before its answer: the next head.
                self._head_bytes += len(data)
            super().data_received(data)
            if self.cycle is not cycle:
                self._disarm_head()
                self._head_bytes = 0
            if self._head_bytes > head_max and not self._app.is_closing():
                logger.info("connection closed: request head over %d bytes", head_max)
                self._app.abort()

        # -- what is owed the peer -------------------------------------------

        def _unsent(self) -> int:
            n = self._app.get_write_buffer_size()
            return n + (self._raw.get_write_buffer_size() if self._raw is not self._app else 0)

        def _wrote(self, n: int) -> None:
            self._written += n
            if self._gone:
                return
            if self._watch_timer is None and (self._unsent() or self._app.is_closing()):
                self._owed, self._written, self._out = self._unsent(), 0, False
                self._watch_timer = self.loop.call_later(stall_s, self._watch)
                _OWING.add(self)

        def _watch(self) -> None:
            """Every stall_s while something is owed or a close is under way:
            under the floor, aborted; all out and still closing — the peer
            does not answer TLS's close — aborted one stall_s on."""
            self._watch_timer = None
            left = self._unsent()
            owed = self._owed + self._written
            if left and owed - left < min(DRAIN_FLOOR * stall_s, owed):
                logger.info("connection aborted: its peer took under %d bytes a second for %s s",
                            DRAIN_FLOOR, stall_s)
                _OWING.discard(self)
                self._app.abort()
                return
            if not left and self._app.is_closing():
                if self._out:
                    _OWING.discard(self)
                    self._app.abort()
                    return
                self._out = True
            if left or self._app.is_closing():
                self._owed, self._written = left, 0
                self._watch_timer = self.loop.call_later(stall_s, self._watch)
            else:
                _OWING.discard(self)

        def connection_lost(self, exc) -> None:
            if self.transport is None:
                self._lost = True
                return
            self._gone = True
            _OWING.discard(self)
            self._disarm_head()
            if self._watch_timer is not None:
                self._watch_timer.cancel()
                self._watch_timer = None
            if self._addr is not None:
                census.leave(self._addr)
                self._addr = None
            super().connection_lost(exc)

    if tls is None:
        Bounded.census = census
        return Bounded

    def factory(**kwargs) -> asyncio.Protocol:
        return _TLSAcceptor(lambda: Bounded(**kwargs), tls, head_s, census)

    factory.census = census
    return factory


# The shutdown timeout start_tls takes, never the bound that holds: the
# drain watch ends a close long before.
_TLS_SHUTDOWN_BACKSTOP_S = 3600


def _feed(protocol, data: bytes) -> None:
    """`data` into a buffered protocol — the TLS one is — as its transport
    would hand it over."""
    view = memoryview(data)
    while view:
        buf = protocol.get_buffer(len(view))
        n = min(len(buf), len(view))
        buf[:n] = view[:n]
        protocol.buffer_updated(n)
        view = view[n:]


class _TLSAcceptor(asyncio.Protocol):
    """A connection until its TLS handshake is made: admitted by the census
    at accept, handed to the HTTP protocol once the handshake is through,
    closed when it is not within `handshake_s`."""

    def __init__(self, make_protocol, ctx: ssl.SSLContext, handshake_s: float,
                 census: _Census):
        self._make, self._ctx, self._handshake_s, self._census = (
            make_protocol, ctx, handshake_s, census)
        self._task = None
        self._early: list = []

    def connection_made(self, transport) -> None:
        addr = _host(transport)
        if not self._census.admit(addr):
            transport.abort()
            return
        _small_send_buffer(transport)
        transport.pause_reading()            # the first bytes are the handshake's
        self._task = asyncio.get_running_loop().create_task(self._handshake(transport, addr))

    async def _handshake(self, transport, addr: str) -> None:
        loop = asyncio.get_running_loop()
        protocol = self._make()
        if self._early:
            # uvloop reads a connection before the pause above can hold it:
            # what reached this protocol — the first bytes of the ClientHello
            # — goes to the TLS protocol start_tls puts in its place, ahead
            # of anything read later. Scheduled before start_tls schedules
            # that protocol's start, it reaches it first, which only buffers
            # bytes until its handshake begins.
            early = b"".join(self._early)
            loop.call_soon(lambda: _feed(transport.get_protocol(), early))
        try:
            # No shutdown timeout of TLS's own: it would cut an answer a slow
            # peer is still reading — the protocol's drain watch bounds a
            # close instead (peer_protocol).
            tls = await loop.start_tls(
                transport, protocol, self._ctx, server_side=True,
                ssl_handshake_timeout=self._handshake_s,
                ssl_shutdown_timeout=_TLS_SHUTDOWN_BACKSTOP_S)
        except (OSError, RuntimeError) as e:
            # A peer that speaks no TLS, never finishes, or left: nothing
            # was served, so nothing is lost but the connection.
            logger.debug("TLS handshake from %s failed: %r", addr, e)
            transport.abort()
            self._census.leave(addr)
            return
        if protocol._lost or tls.is_closing():
            self._census.leave(addr)
            return
        protocol._addr = addr                 # its census entry is the protocol's now
        protocol._raw = transport
        protocol.connection_made(tls)

    def data_received(self, data: bytes) -> None:
        self._early.append(data)

    def connection_lost(self, exc) -> None:
        pass                                 # the handshake's failure path counts it out


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
