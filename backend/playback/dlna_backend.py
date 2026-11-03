"""
DLNA output backend (HARDWARE-TIERS §2.6): the renderer plays one URI at a
time, so this backend walks the canonical queue — SetAVTransportURI + Play
per track, SetNextAVTransportURI where the renderer supports it.

Status acquisition per §2.6: GENA event subscription for transport-state
changes, plus a 1 Hz GetPositionInfo poll ONLY while playing — RelTime is
not evented, a documented boundary exception to the no-polling rule.

Runs its own asyncio loop on a daemon thread ("dlna-loop", the p2p_manager
pattern); the synchronous PlayerBackend methods bridge in with
run_coroutine_threadsafe. Media is served by the plain-http MediaProxy:
owned files via disk-backed /file/{token}, phantom streams via their
existing /preview/{token} buffers — both on a LAN-reachable advertised
host (renderers can't fetch from 127.0.0.1).
"""

import asyncio
import contextvars
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, nullcontext
from datetime import timedelta
from functools import partial
from typing import Callable, ContextManager, Optional
from urllib.parse import parse_qs, unquote, urlparse

from config import settings

from playback.base import Capabilities, PlaybackStatus, PlayerBackend
from playback.queue import CanonicalQueue, QueueItem
from streaming import transcode
from streaming.media_host import media_host, through_tunnel
from streaming.proxy import MIME_BY_FORMAT

logger = logging.getLogger(__name__)

try:
    from aiohttp import ClientConnectionError
    from async_upnp_client.aiohttp import AiohttpNotifyServer, AiohttpRequester
    from async_upnp_client.client_factory import UpnpFactory
    from async_upnp_client.exceptions import UpnpError
    from async_upnp_client.profiles.dlna import DmrDevice, TransportState
    HAS_UPNP = True
except ImportError:
    HAS_UPNP = False
    logger.warning("async-upnp-client not installed — DLNA output disabled")

_CMD_TIMEOUT = 10.0
# The liveness probe's TCP-connect window. A phone or DAP in Wi-Fi power-save
# answers the first packet after minutes of idle only once its radio wakes —
# seen live on KANN: 2 s ran out with the device on and healthy, "offline"
# was reported, and the retry a few seconds later attached in half a second.
# The kernel resends the SYN at 1 s and 3 s, so 5 s gives a sleeping radio
# three chances; a device that is really off costs those 5 s to diagnose.
_PROBE_TIMEOUT = 5.0
_TRACK_END_SLACK = 3.0      # STOPPED within this many seconds of the length = track finished
_RELTIME_TOLERANCE = 3.0    # a RelTime this close to our clock is the renderer's word on it
_PENDING_PLAY_DEADLINE = 20.0   # a Play unconfirmed this long stops claiming `loading`
_COVER_DEADLINE_S = 5.0     # a phantom's cover fetch, which the track's start waits for (CAA's ~2 s)

# One audio format's spellings across sink lists. The node names each format
# as MIME_BY_FORMAT does; a renderer may list it otherwise — a DAP's list
# names DSF audio/dsf and WAV audio/x-wav — and a match by the exact string
# read those as formats it cannot play. Ogg holds the Opus tiers.
_FORMATS = (
    ("audio/flac", "audio/x-flac"),
    ("audio/mpeg", "audio/mp3", "audio/x-mpeg"),
    ("audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"),
    ("audio/aiff", "audio/x-aiff"),
    ("audio/mp4", "audio/x-m4a", "audio/m4a"),
    ("audio/ogg", "application/ogg", "audio/x-ogg", "audio/opus"),
    ("audio/x-dsf", "audio/dsf"),
    ("audio/x-dff", "audio/dff"),
    ("audio/x-wavpack", "audio/wavpack"),
    ("audio/x-ape", "audio/ape", "audio/x-monkeys-audio"),
)


def _spellings(mime: str) -> tuple:
    """`mime`'s format under each of its spellings, `mime` itself first."""
    mime = mime.lower()
    group = next((g for g in _FORMATS if mime in g), ())
    return (mime, *(m for m in group if m != mime))


def sink_formats(entries: list) -> Optional[dict]:
    """What a renderer's sink ProtocolInfo says it plays over HTTP: each MIME
    type it names, lower-cased, to the spelling it uses. None when it says
    nothing — no list, an empty one, or a `*` that takes anything."""
    listed = {}
    for entry in entries:
        parts = entry.split(":")
        if len(parts) < 3 or parts[0].strip().lower() not in ("http-get", "*"):
            continue
        mime = parts[2].split(";")[0].strip()
        if mime == "*" or mime.endswith("/*"):
            return None
        listed.setdefault(mime.lower(), mime)
    return listed or None


def sink_spelling(mime: str, formats: dict) -> Optional[str]:
    """How a sink list (sink_formats) spells `mime`'s format — `mime` itself
    when it is listed — None when it names it under no spelling."""
    return next((formats[m] for m in _spellings(mime) if m in formats), None)


class DlnaAttachError(RuntimeError):
    """Attach failure whose message is already user-ready — start() re-raises
    it verbatim instead of wrapping it in the generic dozing-renderer hint."""


class Detached(RuntimeError):
    """A request of an instance the manager has let go (DlnaBackend.release,
    shutdown): it never reaches the renderer, and the intent it belonged to
    ends with the instance — nothing failed."""


class _Requester:
    """One instance's way to its renderer. Every SOAP request the instance
    makes goes through it — its own commands, the library's reads, the
    volume, the description of an attach — and none once the instance is let
    go: a load still on its way ends at its next request. The phone started
    the same track twice on 2026-11-01, once from an instance that had
    already unsubscribed: a play press's load went on there after a re-pick
    had let it go. The one request that still goes is the switch away's
    Stop, the instance's last word (last_word), sent once every request
    already on its way has its answer (drained): a Play answered after that
    Stop would start the renderer switched away from.

    GENA requests ride the shared notify server's own requester — every
    instance's events come through that server — so an instance's
    subscriptions end at its detach instead (DlnaBackend._subscribe,
    _async_shutdown)."""

    # Set for the last word's own task only: a load on its way in another
    # task stays refused while that Stop goes
    _last_word = contextvars.ContextVar("dlna_last_word", default=False)

    def __init__(self, inner, backend: "DlnaBackend"):
        self.inner = inner
        self._backend = backend
        self._on_its_way = 0
        self._answered = asyncio.Event()    # every request on its way has its answer
        self._answered.set()

    async def async_http_request(self, http_request):
        if self._backend._closed and not self._last_word.get():
            action = (http_request.headers or {}).get("SOAPAction", "").strip('"').rpartition("#")[2]
            raise Detached(f"'{self._backend.label}' was let go — "
                           f"{action or http_request.method + ' ' + urlparse(http_request.url).path} not sent")
        self._on_its_way += 1
        self._answered.clear()
        try:
            return await self.inner.async_http_request(http_request)
        finally:
            self._on_its_way -= 1
            if not self._on_its_way:
                self._answered.set()

    async def drained(self) -> None:
        """Until every request on its way has its answer — or its failure:
        the requester's own timeout bounds each."""
        await self._answered.wait()

    @contextmanager
    def last_word(self):
        token = self._last_word.set(True)
        try:
            yield
        finally:
            self._last_word.reset(token)

# The dlna-loop and the GENA notify server are PROCESS singletons: the
# notify port is a one-per-process resource, and rebinding it on every
# renderer switch proved fragile — any shutdown that failed to release it
# (a dozing phone holding half-open NOTIFY connections past the cleanup
# budget) bricked every later DLNA attach until a backend restart.
# Backends come and go; the loop and the server stay.
_shared_lock = threading.Lock()
_shared_loop: Optional[asyncio.AbstractEventLoop] = None
_shared_notify = None
_shared_notify_host: Optional[str] = None   # address its callback URL names
# The UNSUBSCRIBEs of instances let go while lost, which nobody waits for
# (_async_shutdown): held until they end — the loop keeps only weak
# references to its tasks
_detaching: set = set()


def _unsubscribed(task: asyncio.Task) -> None:
    _detaching.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.debug("GENA unsubscribe: %s", task.exception())


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _shared_loop
    with _shared_lock:
        if _shared_loop is None or _shared_loop.is_closed():
            loop = asyncio.new_event_loop()

            def _run():
                asyncio.set_event_loop(loop)
                loop.run_forever()

            threading.Thread(target=_run, daemon=True, name="dlna-loop").start()
            _shared_loop = loop
        return _shared_loop


async def _ensure_notify(requester, peer: Optional[str] = None):
    """The GENA callback server, rebuilt when the address it advertises stops
    being right. It is shared per process but its callback URL is not
    universal: a renderer reached over a tunnel must be told our tunnel
    address, and one on the LAN our LAN address, so switching between them
    means the server has to be re-announced."""
    global _shared_notify, _shared_notify_host
    host = media_host(peer)
    if _shared_notify is not None and _shared_notify_host != host:
        try:
            await _shared_notify.async_stop_server()
        except Exception as e:
            logger.debug("notify server stop failed: %s", e)
        _shared_notify = None
    if _shared_notify is None:
        port = settings.dlna_gena_port
        server = AiohttpNotifyServer(
            requester, source=("0.0.0.0", port),
            callback_url=f"http://{host}:{port}/notify")
        try:
            await server.async_start_server()
        except OSError as e:
            # UpnpServerOSError str()s to "None" (library bug) — translate
            # to the real story: the port is taken, most plausibly by another
            # Sautium node whose media ports weren't kept distinct.
            raise DlnaAttachError(
                f"DLNA event port {port} is already in use by another "
                "process on this host (another Sautium node?) — free it "
                "or change the port, then retry") from e
        _shared_notify = server
        _shared_notify_host = host
    return _shared_notify

def _rel_time(seconds: float) -> str:
    """Whole seconds as a REL_TIME target, H:MM:SS."""
    total = int(seconds)
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _parse_reltime(value) -> Optional[float]:
    """H:MM:SS[.f] → seconds; None for absent/NOT_IMPLEMENTED/garbage.
    str() first so a timedelta normalizes to the same shape."""
    try:
        h, m, s = str(value).split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (TypeError, ValueError):
        return None


def _transient_501(e: Exception) -> bool:
    """KANN's wedged-stack refusal: UPnP error 501 'Current state of service
    prevents invoking that action' — thrown as an NPE right after the output
    is selected, or as its internal transport lock timing out. The state is
    transient (the same call succeeds seconds later), unlike a real fault."""
    if getattr(e, "error_code", None) == 501:
        return True
    return "upnp error: 501" in str(e)


def _connection_failure(e: Exception) -> bool:
    """A failure below SOAP — the request got no answer at all: refused (the
    renderer app is closed, its port has moved), timed out, unreachable.
    That is the doze signature; a SOAP fault is the renderer answering.
    Every request to the renderer rides aiohttp, whose transport failures
    async_upnp_client re-raises as ClientConnectionError subclasses (a
    refused connect is NOT an OSError); TimeoutError is our own command
    window running out. A bare OSError is our own disk, never the renderer:
    counting it detached an awake KANN on every play while the library's
    drive was unmounted, and told the user to wake the device."""
    return isinstance(e, (TimeoutError, ClientConnectionError))


def _describe(e: Exception) -> str:
    """One readable line for a failure. async_upnp_client wraps a transport
    error as `UpnpError(repr(cause), None)`, whose str() is that tuple — the
    whole aiohttp ConnectionKey repr — where the cause itself says "Cannot
    connect to host …" in one line."""
    cause = e.__cause__
    if (isinstance(e, UpnpError) and cause is not None and e.args
            and e.args[0] == repr(cause)):
        e = cause
    text = str(e).strip() or type(e).__name__
    return text if len(text) <= 80 else text[:77] + "…"


def renderer_reachable(location: str, timeout: float = _PROBE_TIMEOUT) -> bool:
    """Cheap TCP-connect liveness check on a renderer's description host:port.
    A powered-off renderer fails this in ~timeout s, so the play-intent gate
    can report it offline fast instead of eating stacked SOAP/attach
    timeouts. A dozing-but-networked renderer still accepts the connect and
    wakes on the SOAP that follows."""
    import socket
    try:
        u = urlparse(location)
        host, port = u.hostname, u.port or 80
        if not host:
            return False
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _tunnel_address(location: Optional[str]) -> bool:
    """Is a renderer's address a tunnel's rather than its LAN's
    (media_host.through_tunnel)?"""
    return through_tunnel(urlparse(location or "").hostname)


def known_locations(renderer: dict) -> list[str]:
    """Every address the renderer was reached at, the current one first."""
    known = []
    for loc in [renderer.get("location")] + list(renderer.get("locations") or []):
        if loc and loc not in known:
            known.append(loc)
    return known


def renderer_online(renderer: dict, timeout: float = _PROBE_TIMEOUT) -> bool:
    """Does the renderer answer at ANY address it was ever reached at? The
    play-intent gate must not call a device offline because it moved
    networks since we last saw it. The addresses are probed at once and the
    first answer decides — a stale entry must not spend the whole window
    before the live one is even tried."""
    known = known_locations(renderer)
    if not known:
        return False
    pool = ThreadPoolExecutor(max_workers=len(known))
    try:
        return any(f.result() for f in as_completed(
            [pool.submit(renderer_reachable, loc, timeout) for loc in known]))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


class DlnaBackend(PlayerBackend):
    id = "dlna"

    def __init__(self, emit, queue: CanonicalQueue, renderer: dict):
        super().__init__(emit)
        if not HAS_UPNP:
            raise RuntimeError("async-upnp-client not installed")
        self._queue = queue
        self._renderer = renderer                # {udn, location, name}
        # The renderer's own address decides which of our addresses it can
        # reach us at — see media_host().
        self._peer_host = urlparse(renderer.get("location") or "").hostname
        self.label = renderer.get("name") or "DLNA renderer"
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # dlna-loop-owned state
        self._dmr: Optional["DmrDevice"] = None
        self._gone = False       # device believed off the network (see healthy)
        self._index = 0                          # 1-based canonical slot
        self._current_url: Optional[str] = None
        self._next_url: Optional[str] = None     # set via SetNextAVTransportURI
        # Every URI this attachment handed the renderer, each CurrentURI and
        # NextURI: one it names is ours however stale — a renderer names the
        # URI before ours for a tick after our own Stop, SetAVTransportURI
        # and Play, and can sit PAUSED on it; any other is another
        # controller's (_taken_by_another)
        self._handed: set = set()
        # Another controller holds the renderer: nothing of ours is loaded,
        # what it plays is said at slot 0, and nothing it does moves the
        # queue — until a load of ours (_taken_by_another)
        self._external = False
        self._position = 0.0
        # Wall-clock position estimate for renderers that don't report RelTime
        # (KANN on VBR Opus): position at the last baseline + elapsed since.
        self._wall_base = 0.0
        self._wall_t0 = 0.0
        # Server-side seek offset: the current stream may be a track
        # re-encoded from N seconds (Opus seek), so a renderer's RelTime is
        # 0-based on that stream — the real track position is offset + rel.
        self._pos_offset = 0.0
        self._length = 0.0
        # The second a play press starts the slot at when nothing of it is
        # loaded: the spot an output switch carried over (resume_at), the one
        # a renderer that stopped answering was left at (_lose), or where
        # another controller cut the node's own listen (_taken_by_another).
        self._resume: Optional[float] = None
        # The queue item the slot names while another controller holds the
        # renderer (_taken_by_another): a queue edit moves the slot with it,
        # and its spot goes with it (queue_changed)
        self._held_item: Optional[QueueItem] = None
        # A start mid-track the renderer seeks itself: sent once it is seen
        # playing our load (_confirmed) — a Seek in STOPPED is refused.
        self._seek_on_start: Optional[float] = None
        # A Seek of the node's — a start's, or the person's sent through the
        # node — not yet seen landing, as (its second, monotonic when it was
        # asked for, its distance from where the renderer was): till then
        # the renderer reads where it was before it (_adopt_reltime). The
        # distance is set for a Seek in an Opus stream judged as the
        # renderer's fact — a landing, or none by the pending-play deadline
        # (_remember_opus_seek), unlanded the stream loads again from the
        # clock's second (_seek_unlanded) — and None for any other
        self._seek_due: Optional[tuple[float, float, Optional[float]]] = None
        # RelTime readings taken as the renderer's word on this load — with
        # the clock, a move of its own, a Seek's landing (_heard)
        self._heard_on_load = 0
        # The poll's updates failed in a row since one last answered: three
        # lose the renderer; one already puts its address in doubt (reachable)
        self._misses = 0
        # A play press waiting on a slot whose stream is on its way
        # (playback.substitute): (its item — the slot follows it through
        # queue edits — and the second it starts at); slot_ready starts it.
        self._parked: Optional[tuple[QueueItem, float]] = None
        # What the renderer named as that wait began — its track's URI and
        # its transport's, as it last said them: only another URI, none this
        # attachment handed, is another controller taking it meanwhile
        # (_moved_on) — what it held before was there before the person's word
        self._park_uris: frozenset = frozenset()
        # That wait was asked while another controller played on the renderer
        # (a URI none of ours, playing or paused): the person's take-back — the
        # newer word, which that controller's own next track never ends
        self._park_under_another = False
        # The state this instance last told the manager, or a Stop it has
        # decided: what a renderer that stops answering was doing, as far as
        # the node knows it — its own transport state is stale by then
        # (_lose).
        self._said: Optional[str] = None
        # A RelTime reading that disagreed with the clock, kept until the
        # next one confirms it (a device-side seek) or the clock wins.
        self._rel_stray: Optional[tuple[float, float]] = None
        # A Play we issued that the renderer has not been seen acting on.
        # Until it reports PLAYING/PAUSED for this load, everything it says
        # is still about the PREVIOUS track: KANN sits in STOPPED for
        # seconds while it opens the new stream, and its last position
        # reading (246 of 248) read as the new track finishing at 99% —
        # the poll died, the backend went deaf, the renderer played on.
        self._awaiting_play = False
        self._pending_since = 0.0     # monotonic mark of the awaited Play
        # Last direct GetPositionInfo reading (RelTime, monotonic at) — the
        # renderer's word via position when its transport state is idle or
        # lying (see _probe_advancing).
        self._probe_last: Optional[tuple[float, float]] = None
        # Bumped by every load as it adopts its slot. Poll evidence gathered
        # across an await is stamped with the generation it started under; a
        # verdict whose generation moved mixes the old device reading with
        # the new load's fields and must dissolve (observed live: a stale
        # track-end verdict advancing into a freshly replaced queue).
        self._load_gen = 0
        self._poll_task: Optional[asyncio.Task] = None
        self._preload_task: Optional[asyncio.Task] = None   # SetNext, once the next buffer is in hand
        self._resubscribe_task: Optional[asyncio.Task] = None
        self._art_misses: set = set()   # cover URLs that answered "no art" — not re-asked per track
        self._error: Optional[str] = None
        # Track-change SOAP sequence in flight → status reports `loading`
        # (the UI shows a transition spinner on the tapped track).
        self._loading = False
        self._load_lock = asyncio.Lock()
        # Let go by the manager (release, shutdown): from then on nothing of
        # this instance reaches the renderer (_Requester), and nothing it
        # still hears is said or acted on
        self._closed = False
        self._requester: Optional[_Requester] = None
        # Why the last word this thread asked for was refused here, before
        # anything was sent (refusal)
        self._local = threading.local()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._loop = _ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(self._async_start(), self._loop)
        try:
            fut.result(timeout=25)
        except DlnaAttachError as e:
            logger.error("DLNA attach failed: %s", e)
            raise
        except Exception as e:
            msg = (f"'{self.label}' did not respond ({_describe(e)}) — "
                   "wake the device (phone renderers doze) and try again")
            logger.error("DLNA attach failed: %s", msg)
            raise RuntimeError(msg)
        logger.info("DLNA backend attached to %s", self.label)

    def shutdown(self) -> None:
        # Let go before cleanup runs: a load, a seek or an advance still on
        # its way sends nothing more, and nothing is said or polled
        self._closed = True
        if self._loop is not None:
            # Unsubscribe only — the shared loop and notify server outlive
            # this backend by design (see the singleton note above).
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self._async_shutdown(), self._loop)
                fut.result(timeout=8)
            except Exception as e:
                logger.warning("DLNA shutdown cleanup incomplete: %s",
                               str(e).strip() or type(e).__name__)
        logger.info("DLNA backend detached from %s", self.label)

    def release(self) -> Optional[str]:
        """A switch away. The instance is let go first — a load, a seek or an
        advance still on its way sends nothing more — and its last word is
        the renderer's Stop, once what it had already sent has its answer: a
        load in flight used to go on past that Stop, and a Play answered after
        it would start the renderer the person had just switched away from. A
        renderer believed off the network gets no Stop
        (PlayerBackend.release)."""
        healthy = self.healthy()
        self._closed = True
        if not healthy:
            logger.info("%s believed off the network — switched away without a stop", self.id)
            return None

        async def _s():
            await self._requester.drained()
            with self._requester.last_word():
                await self._avt("Stop")
        self._call(_s())
        return None

    def drives(self, renderer: Optional[dict]) -> bool:
        """Is `renderer` (a record: udn, location…) the device this instance
        drives — by its UDN, whatever address it was picked at?"""
        from routers.player import udn_key
        return bool(renderer) and udn_key(renderer.get("udn")) == udn_key(self._renderer.get("udn"))

    @property
    def location(self) -> Optional[str]:
        """The address this instance talks to its renderer at."""
        return self._renderer.get("location")

    def answers_here(self) -> bool:
        """Does the renderer answer at the address this instance talks to it
        at (a TCP connect, the probe window at most)? One that does not is
        lost there, as one that stopped answering: its spot is what this
        instance last said — the clock's second if it played, not a reading
        the failing poll never refreshed — and the instance let go with it
        waits for nothing from that address (_lose, _async_shutdown)."""
        if renderer_reachable(self.location):
            return True
        self._lose()
        self._emit_now()
        return False

    def capabilities(self) -> Capabilities:
        return Capabilities(volume=True, volume_kind="percent", seek=True,
                            gapless=False)

    def refusal(self) -> str:
        """Why this thread's last word was refused before anything was sent
        (a Seek while another controller holds the renderer), said once."""
        words, self._local.refusal = getattr(self._local, "refusal", None), None
        return words or ""

    def healthy(self) -> bool:
        """Battery DAPs and phones drop off the network in deep sleep —
        once that's detected (poll misses, connection-level command
        failures) the next play intent must re-attach, not trust this
        instance."""
        return self._dmr is not None and not self._gone

    def reachable(self, waiting: Callable[[], ContextManager] = nullcontext) -> bool:
        """The play gate's probe: does the renderer answer where this
        instance talks to it — or, moved since, at another address it is
        known at now (a scan or a pin added it: _record)? Then this instance
        is let go and the gate attaches the renderer again where it answers:
        a phone back on the LAN from a tunnel failed its first Play on the
        tunnel address it had been chosen at. False when it answers nowhere.

        Every address is asked at once, and where it answers first decides
        — unless the attached address is to be waited for: the node has no
        word against it (no request of its poll has failed there, the
        renderer is not lost) and the address that answered first is no
        better (a LAN address is better than a tunnel's). A renderer that
        answers at two addresses of a kind about as fast is never moved
        between them press after press; one attached through a tunnel that
        answers on the LAN is moved there, once: the phone back home, the
        tunnel down or up. The wait is said (`waiting`: the manager's
        connecting), up to the probe window if the address went dark before
        any request there failed. Waiting on the attached address first
        whatever answered cost a Play pressed back on the LAN the whole probe
        window on the tunnel address (2026-11-01)."""
        attached = self._renderer.get("location")
        known = [attached, *(loc for loc in known_locations(self._record()) if loc != attached)]
        trusted = not (self._misses or self._gone)
        pool = ThreadPoolExecutor(max_workers=len(known))
        try:
            probes = {pool.submit(renderer_reachable, loc): loc for loc in known}
            here = next(f for f, loc in probes.items() if loc == attached)
            answered = next((probes[f] for f in as_completed(probes) if f.result()), None)
            better = answered is not None and _tunnel_address(attached) and not _tunnel_address(answered)
            if answered not in (None, attached) and trusted and not better:
                # Its own address decides first — its answer waited for, and
                # said while it is still to come
                with nullcontext() if here.done() else waiting():
                    if here.result():
                        answered = attached
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if answered is None:
            return False
        if answered != attached:
            logger.info("renderer %s answers at another address now — attaching it there", self.label)
            self._lose()
            self._emit_now()
        return True

    def _record(self) -> dict:
        """The renderer as the node remembers it now: the chosen output's
        record, which every address a scan reaches it at joins
        (routers.player.note_renderer_location), with the addresses this
        instance was attached with behind it."""
        from routers.player import udn_key
        from routers.settings import _read
        chosen = _read("output.dlna_renderer") or {}
        if udn_key(chosen.get("udn")) != udn_key(self._renderer.get("udn")):
            return self._renderer
        locations = known_locations(chosen)
        return {**chosen, "locations": [*locations, *(
            loc for loc in known_locations(self._renderer) if loc not in locations)]}

    def slot_ready(self, index: int) -> None:
        """A slot's stream landed, or it was found unplayable here
        (playback.substitute): a play parked on it goes now — read on the
        loop, where the park is made."""
        async def _parked_start():
            parked = self._parked
            if parked is None or self._queue.index_of(parked[0]) != index:
                return
            item = parked[0]
            if not self._external:
                # The decision point: a renderer whose events do not reach
                # the node may have been taken during the wait — read once
                cur = await self._renderer_uri()
                if self._parked is not None and self._parked[0] is item and self._moved_on(cur):
                    self._taken_by_another()
            # The wait as it stands after the read: a Seek meanwhile moved its
            # second, a queue edit its slot; another word ended it
            parked = self._parked
            slot = self._queue.index_of(item) if parked is not None and parked[0] is item else None
            if slot is None:
                return
            self._parked = None
            await self._load_and_play(slot, start_at=parked[1])
        if self._loop is None:
            return
        # Not through _call_async: a slot nobody waits on must not clear
        # the renderer's error (a load that does go clears its own)
        fut = asyncio.run_coroutine_threadsafe(_parked_start(), self._loop)

        def _done(f):
            if not f.cancelled() and f.exception() is not None:
                self._cmd_failed(f.exception(), "load and play")
        fut.add_done_callback(_done)

    def resume_at(self, index: int, position: float = 0.0) -> None:
        item = self._queue.item_at(index)
        if item is None:
            return
        self._index = index
        self._current_url = None     # play() loads this slot fresh
        self._resume = position
        self._position = position
        self._length = item.duration_seconds or 0.0
        # Nothing of ours is loaded: whatever the renderer plays is not this
        # slot's. The spot is held paused, at 0 s as at any second: said
        # stopped, it ended the listen the switch carried
        self._emit_now("paused")

    async def _create_device(self, factory):
        """Build the device from whichever known address answers first.

        A renderer moves between networks — the same phone is on the LAN at
        home and on a tunnel from a train — so a stored address is a guess,
        not a fact: every known one is asked at once.

        A device that has left a network does not refuse the connection, it
        swallows it, so walking the list with full description fetches let one
        dead address spend the entire attach budget before a live one was
        ever tried. Probed together, they still waited for the slowest: the
        dead address's whole probe window, 5 s of every attach of a phone
        moved to a tunnel (2026-11-01). The first address that answers is
        described now, and taken when what answers there is this renderer —
        its UDN, the device's own or one it embeds: another device holding an
        address this one has left answers first as well; the others are let
        go."""
        from routers.player import forget_renderer_location, udn_key
        known = known_locations(self._renderer)
        udn = udn_key(self._renderer.get("udn"))
        others = []

        async def probe(loc: str) -> tuple:
            return loc, await asyncio.to_thread(renderer_reachable, loc)
        probes = [asyncio.ensure_future(probe(loc)) for loc in known]
        try:
            for answer in asyncio.as_completed(probes):
                loc, ok = await answer
                if not ok:
                    continue
                try:
                    device = await factory.async_create_device(loc)
                except Detached:
                    raise
                except Exception as e:
                    logger.debug("renderer answered at %s but described badly: %s", loc, e)
                    continue
                if udn in {udn_key(d.udn) for d in (device, *device.embedded_devices.values())}:
                    return device, loc
                # Another device holds an address this renderer has left: no
                # address of its from now on — probed at every press, a LAN
                # address answering there drew the renderer off its tunnel
                # each time (reachable)
                logger.info("another device answers at %s now, not %s — forgotten for it", loc, self.label)
                others.append(loc)
                forget_renderer_location(self._renderer["udn"], loc)
                self._renderer = {**self._renderer, "locations": [
                    known_loc for known_loc in self._renderer.get("locations") or [] if known_loc != loc]}
        finally:
            for p in probes:
                p.cancel()
        if others:
            raise DlnaAttachError(
                f"'{self.label}' is not where it was known: another device answers at "
                f"{', '.join(urlparse(t).hostname or t for t in others)} now — if it was set up "
                "again, add it again by its current address")
        raise DlnaAttachError(
            f"'{self.label}' did not answer at any known address "
            f"({', '.join(urlparse(t).hostname or t for t in known) or 'none'})"
            " — if it moved networks, add it again by its current address")

    async def _async_start(self) -> None:
        # 10s over the default 5s: phone renderers in Wi-Fi power-save can
        # stall the first request for seconds while the radio wakes up.
        requester = AiohttpRequester(timeout=10)
        self._requester = _Requester(requester, self)
        factory = UpnpFactory(self._requester, non_strict=True)
        device, location = await self._create_device(factory)
        if location != self._renderer.get("location"):
            self._renderer = {**self._renderer, "location": location}
            self._peer_host = urlparse(location).hostname
            from routers.player import note_renderer_location
            note_renderer_location(self._renderer["udn"], location)
            logger.info("renderer %s answered at %s instead", self.label, location)
        # The shared server is every instance's: never handed this one's way
        # in, which refuses all once this instance is let go
        notify_server = await _ensure_notify(requester, self._peer_host)

        self._dmr = DmrDevice(device, notify_server.event_handler)
        self._dmr.on_event = self._on_gena_event
        try:
            await self._subscribe()
        except Detached:
            raise
        except Exception as e:
            # Some renderers/firewalls reject eventing — degrade to the
            # position poll (it already carries transport state) and try
            # again on the next track load (_resubscribe).
            logger.warning("GENA subscribe failed (%s) — poll-only mode", e)
        await self._dmr.async_update()
        self._emit_now("stopped")

    async def _subscribe(self) -> None:
        """GENA subscription with its renewals. Its requests go through the
        shared notify server, not this instance's way in, so a let-go that
        lands while it is under way is met after it: what it subscribed then
        is let go too — an attach the manager gave up on (its start timed
        out) left renewals running for nobody."""
        if self._closed:
            raise Detached(f"'{self.label}' was let go — SUBSCRIBE not sent")
        await self._dmr.async_subscribe_services(auto_resubscribe=True)
        if self._closed:
            await self._dmr.async_unsubscribe_services()
            raise Detached(f"'{self.label}' was let go while it subscribed")

    async def _resubscribe(self) -> None:
        """A rejected GENA subscription is not forever: KANN's UPnP stack
        answered SUBSCRIBE with 500 while it was throwing NPEs at every
        action, and worked again minutes later — but the backend stayed
        poll-only for the whole session, blind to play/stop from the
        device's own screen. Retried off the load path, at the user's next
        track load (the renderer is awake and answering then)."""
        try:
            await self._subscribe()
            logger.info("GENA subscription established on retry")
        except Detached as e:
            logger.info("GENA subscribe retry ends — %s", e)
            return
        except Exception as e:
            logger.info("GENA subscribe retry failed (%s) — still poll-only", e)

    async def _async_shutdown(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        if self._preload_task:
            self._preload_task.cancel()
        if self._resubscribe_task:
            self._resubscribe_task.cancel()
        if self._dmr is None:
            return
        unsubscribe = asyncio.ensure_future(self._dmr.async_unsubscribe_services())
        if self._gone:
            # Lost where it was attached: the subscriptions are let go at
            # the call's first step, before any request — no renewal after
            # this — and the renderer's answer at that address is not waited
            # for. Every attach of a phone moved to a tunnel waited 3 s on its
            # LAN address here first (2026-11-01).
            _detaching.add(unsubscribe)
            unsubscribe.add_done_callback(_unsubscribed)
            return
        try:
            # Unsubscribe is HTTP to the renderer — a powered-off or
            # dozing device would hang it far past the shutdown budget.
            await asyncio.wait_for(unsubscribe, 3)
        except Exception as e:
            logger.debug("GENA unsubscribe: %s", e)

    # -- status ---------------------------------------------------------------

    def _state_name(self) -> str:
        st = self._dmr.transport_state if self._dmr else None
        if st in (TransportState.PLAYING, TransportState.TRANSITIONING):
            return "playing"
        if st == TransportState.PAUSED_PLAYBACK:
            return "paused"
        return "stopped"

    def _emit_now(self, state: Optional[str] = None) -> None:
        if self._closed:
            # The manager no longer hears a detached backend (every tick
            # names its sender); this only spares building the status for
            # the GENA event the SHARED notify server can still deliver
            # after unsubscribe.
            return
        if state is None and (self._loading or self._awaiting_play):
            # Poll/GENA ticks that land mid-track-change would report the
            # OLD track's transport state against the NEW index — and so
            # would ticks before the renderer has started on our Play.
            state = "loading"
        extra = {}
        if self._error:
            extra["error"] = self._error
        position, length, index = self._position, self._length, self._index
        if self._gone:
            # Lost: stopped — its own transport state is stale — and, with a
            # spot, stopped there: the play press attaches it again and
            # resumes there
            state = "stopped"
            if self._resume is not None:
                position = self._resume
                extra["resume"] = {"index": self._index, "position": self._resume}
        elif state is None and self._parked is not None:
            state = "loading"         # a play waiting on its slot's stream
        elif self._external and not self._loading:
            # Another controller holds the renderer: what it plays is none of
            # the queue's — slot 0, in the renderer's own state; or, where it
            # cut the node's own listen, that spot: its slot, stopped at its
            # second, Play loads it there. A load of ours under way says its
            # own slot
            extra["source"] = "external"
            if self._resume is not None:
                state, position = "stopped", self._resume
                extra["resume"] = {"index": self._index, "position": self._resume}
            else:
                position, length, index = 0.0, 0.0, 0
        elif state is None and self._current_url is None and self._resume is not None:
            state = "paused"          # a spot carried over, nothing of it loaded yet
        state = state or self._state_name()
        self._said = state
        vol = None
        if self._dmr is not None and self._dmr.volume_level is not None:
            vol = round(self._dmr.volume_level * 100.0, 1)
        self._emit(PlaybackStatus(
            state=state,
            position=position,
            length=length,
            queue_index=index,
            volume=vol,
            extra=extra,
        ))

    def _on_gena_event(self, service, state_variables) -> None:
        """Transport-state changes arrive here (renderer's own remote
        included). Runs on the dlna-loop."""
        asyncio.ensure_future(self._handle_state_change(), loop=self._loop)

    async def _handle_state_change(self) -> None:
        if self._closed or self._gone:
            # Lost: what it still says moves nothing (_lose). Nothing of
            # ours loaded: its state is about something else.
            return
        if self._external:
            # Another controller holds it: its state is said, and moves nothing
            self._emit_now()
            return
        if self._parked is not None:
            # A play waiting for its slot's stream, nothing of ours loaded:
            # what the renderer held as the wait began moves nothing — its
            # load sequence's own events neither — and another controller
            # taking it meanwhile is the newer word (_taken_by_another)
            if not self._loading and self._moved_on(getattr(self._dmr, "current_track_uri", None)):
                self._taken_by_another()
            return
        if self._current_url is None:
            return
        if self._loading:
            # A manual track change owns the transport: its own Stop and
            # URI flapping must not be read as track-end or auto-advance,
            # and its `loading` status must not be overwritten.
            return
        state = self._state_name()
        if self._settle_pending_play(state):
            return
        cur = getattr(self._dmr, "current_track_uri", None)
        if cur and self._next_url and cur == self._next_url:
            # The renderer moved to the armed NextURI on its own — a gapless
            # boundary, or the device's own "next". Whatever transport state
            # rides on this event belongs to the new track.
            self._advanced_to_next(restart_poll=True)
            return
        if cur and cur != self._current_url:
            if cur not in self._handed:
                # A URI we never handed over: another controller took the
                # renderer — one past UPnP, or media picked on the device
                self._stop_poll()
                self._taken_by_another()
            # One of ours, stale: nothing to attribute
            return
        if state == "playing":
            self._ensure_poll()
        elif state == "stopped":
            self._stop_poll()
            finished = (self._length > 0
                        and self._position >= self._length - _TRACK_END_SLACK)
            if finished:
                await self._advance()
                return
        self._emit_now(state)

    def _seek_pending(self, real: float, *, paused: bool = False) -> bool:
        """Is `real`, a reading of the renderer's, from before a Seek of the
        node's (_seek_due)? A renderer that plays on from where it was until
        its seek takes effect reads that place meanwhile — taken for its
        word, the listen an output switch carried restarted there and was
        scrobbled twice. A reading at the Seek's second, or as far past it
        as the time since it was asked, is the Seek landed — however late:
        the clock takes it at once. Till then the clock runs on from what the
        Seek set, and holds still while the renderer is paused; past the
        pending-play deadline after it was asked, the renderer is believed
        again. A Seek in an Opus stream judged as the renderer's fact lands
        only on a reading its staying where it was cannot explain — a
        renderer that ignores the Seek and plays on enters the window above
        once the distance is played, and was taken for one that seeks — and
        unlanded it loads again from the second (_seek_unlanded, at a playing
        tick). True when the reading is spent here — the Seek's own, or one
        from before it; False when it is the caller's to judge."""
        due = self._seek_due
        if due is None:
            return False
        second, asked, distance = due
        age = time.monotonic() - asked
        # Where the renderer would read had it stayed: the clock, which runs
        # from the Seek's second while it plays and holds while it is paused,
        # less the distance asked — uncapped: capped at the track's length,
        # it stopped where a renderer playing on near the end went on
        stayed = (distance is not None
                  and abs(real - (self._wall_base + (self._clock() - self._wall_t0) - distance))
                  <= _RELTIME_TOLERANCE)
        if second - _RELTIME_TOLERANCE <= real <= second + age + _RELTIME_TOLERANCE and not stayed:
            self._seek_due = None
            self._heard(real)
            if distance is not None:
                self._remember_opus_seek(True)
            return True
        if age > _PENDING_PLAY_DEADLINE and distance is None:
            self._seek_due = None
            return False
        self._rel_stray = None
        if paused:
            self._rebaseline(self._position)
        else:
            self._position = self._wall_estimate()
        return True

    def _adopt_reltime(self, real: float, now: float, *, paused: bool = False) -> None:
        """A renderer's RelTime is its word on the position — when it moves
        with the clock. KANN on Opus reports 00:00:00 for the whole track and
        one stray non-zero reading right after SetNextAVTransportURI (the
        probed NEXT stream's position, live: ~1 s at 11 s in); anchoring on
        it lost everything played before, the clock then ran that much
        short, and every natural end read as a manual stop 6–16 s early.
        A reading within tolerance of the clock is adopted; one far from it
        is held, and adopted only if the next reading advanced from it —
        that is a seek made on the device, which the clock cannot know.
        Said in the log with what the renderer reported: a renderer started a
        track over twice on its own (live, 2026-10-31), and only its debug
        readings could have told that from a load of the node's. A reading
        from before a Seek of the node's is no such move (_seek_pending).
        `paused`: the renderer says it is paused."""
        if self._seek_pending(real, paused=paused):
            return
        if abs(real - self._wall_estimate()) <= _RELTIME_TOLERANCE:
            self._heard(real)
            return
        if self._rel_stray is not None:
            held, at = self._rel_stray
            if abs(real - (held + (now - at))) <= _RELTIME_TOLERANCE:
                logger.info("renderer '%s' moved slot %d from %.0f s to %.0f s by itself "
                            "(%s, its track …%s)", self.label, self._index, self._wall_estimate(),
                            real, self._state_name(),
                            (getattr(self._dmr, "current_track_uri", None) or "")[-14:])
                self._heard(real)
                return
        self._rel_stray = (real, now)
        self._position = self._wall_estimate()

    def _heard(self, real: float) -> None:
        """A RelTime reading taken as the renderer's word: the clock runs on
        from it. Two on a load are a renderer reporting its position there
        (a Seek's landing can be seen on it); one may be the stray reading a
        DAP gives on VBR Opus, 0:00:00 otherwise."""
        self._heard_on_load += 1
        self._rebaseline(real)

    def _adopt_length(self, reported: float) -> None:
        """The renderer's TrackDuration is the length of the STREAM it plays:
        the whole track on a fresh one — truer than a catalog length for a
        streamed track — but only the rest of it after an Opus seek's `ss=`.
        There the track keeps the length the node knows; one it does not know
        is read 0-based, like RelTime. A phone reports what its decoder
        measured (4:45 of a 6:32 track after ss=107): taken for the track's,
        it shrank the slider's scale and every later seek landed off."""
        if self._pos_offset <= 0:
            self._length = reported
            return
        item = self._queue.item_at(self._index)
        self._length = ((item.duration_seconds if item is not None else None)
                        or self._pos_offset + reported)

    @staticmethod
    def _clock() -> float:
        """The clock the position estimate runs on: REALTIME, not monotonic.
        A WSL2 VM's CLOCK_MONOTONIC runs slow after the host slept (measured
        4.9 %: systemd-timesyncd steps realtime +1.47 s every 30 s while
        monotonic is never corrected) — a monotonic estimate came up 7 s
        short on every 152-s track and each natural end read as a manual
        stop, halting the queue mid-album. Realtime is what the OS keeps
        honest against the world; its corrections are small forward steps
        the progress bar absorbs."""
        return time.time()

    def _wall_estimate(self) -> float:
        """Position by the clock since the last anchor (a real RelTime, a
        Play/seek, or the paused position), capped at the track length."""
        est = self._wall_base + (self._clock() - self._wall_t0)
        return min(est, self._length) if self._length else est

    def _rebaseline(self, pos: float) -> None:
        """Anchor the wall-clock position estimate at `pos` now — called
        whenever the true position is known (track start, resume, seek, or a
        real RelTime reading)."""
        self._position = pos
        self._wall_base = pos
        self._wall_t0 = self._clock()
        self._rel_stray = None

    def _settle_pending_play(self, state: str) -> Optional[str]:
        """Reconcile one transport observation with a Play still awaiting
        the renderer. None: nothing pending, or the renderer just confirmed
        it — the observation is about the current track. 'starting': the
        renderer is still idle on its way to playing; status stays
        `loading` and nothing it reports may be attributed to this track.
        'failed': it reported ERROR_OCCURRED — a stream it could not open."""
        if not self._awaiting_play:
            return None
        if state in ("playing", "paused"):
            cur = getattr(self._dmr, "current_track_uri", None)
            if cur and cur != self._current_url:
                # Still on the previous URI: the renderer works through our
                # Stop/SetAVTransportURI/Play asynchronously, and KANN sat
                # PAUSED on the old track for a tick after all three — read
                # as confirmation, that adopted the old position and opened
                # a play session the real start then closed as a 0% skip.
                self._emit_now("loading")
                return "starting"
            self._confirmed()
            return None
        if self._transport_error():
            self._awaiting_play = False
            self._error = (f"'{self.label}' could not play this track — "
                           "the renderer reported an error")
            logger.warning("DLNA renderer reported ERROR_OCCURRED starting "
                           "slot %d", self._index)
            self._emit_now("stopped")
            return "failed"
        self._emit_now("loading")
        return "starting"

    def _lose(self) -> None:
        """The renderer stopped answering. The queue and the spot stay as
        they were: the slot, and the second it was at — by the clock when it
        played — become what the next play intent resumes (the manager
        attaches the renderer again, resume_at), and nothing it still says
        moves them (_handle_state_change, _ensure_poll). A phone that lost
        its listener played on through its armed NextURI and its events
        moved this instance on; the play press then started the slot it was
        left at from 0:00, after the phone had played the next one."""
        self._gone = True
        if self._resume is not None or self._external:
            # A spot already: carried over, or lost before; or another
            # controller held it — what was said is none of our listen
            return
        # By what this instance last said, not the renderer's stale state: a
        # track change under way is at its own start (0, or the second it
        # was asked at), a Stop the renderer never answered leaves no spot
        if self._said == "playing":
            self._resume = self._wall_estimate()
        elif self._said in ("paused", "loading"):
            self._resume = self._position

    def _moved_on(self, cur: Optional[str]) -> bool:
        """Has the renderer, while a play of ours waits for its slot's
        stream, moved to a URI (`cur`) it did not name as the wait began and
        this attachment never handed — another controller taking it
        meanwhile? Whatever it held before the person's word (another app
        playing on, a stale URI of an idle device, one of ours from an
        earlier attachment) is no takeover."""
        return (not self._park_under_another and bool(cur) and cur not in self._park_uris
                and cur not in self._handed)

    async def _renderer_uri(self) -> Optional[str]:
        """The URI the renderer names now, asked (GetPositionInfo): its
        evented state may be unset or stale while it is stopped — what it
        last said when it does not answer the question."""
        action = self._dmr._action("AVT", "GetPositionInfo")
        if action is not None:
            try:
                return (await action.async_call(InstanceID=0)).get("TrackURI") or None
            except Exception as e:
                logger.debug("the renderer's URI not read (%s) — taken as last said", e)
        return getattr(self._dmr, "current_track_uri", None)

    def _taken_by_another(self) -> None:
        """Another controller took the renderer: it names a URI that is
        neither ours nor the armed next — none this attachment handed it
        (_handed). Its playback is external, at slot 0: the listen ends at
        its own seconds, the clock running on no further; the load is let
        go — nothing of ours current or armed, no start or Seek owed; and
        nothing the renderer does from here is a track end or an advance
        (_handle_state_change). Nothing is sent to it: the next play intent
        loads our slot afresh (play). Taken from the node's own listen —
        playing or paused, as this instance last said — it cut that listen:
        the spot stays at the slot and its last own second, Now Playing names
        its track saying another app took the renderer (_emit_now), and Play
        loads it there; erased as any spot is, with its item or once a start
        from it is taken. Taken from a stop, no listen was cut: no spot. A
        play that waited for its slot's stream (_parked) is over: the other
        controller's word is newer — its track and second are the spot, as a
        Stop leaves them. Its callers end the position poll. Runs on the
        dlna-loop."""
        logger.info("another controller took %s — slot %d let go at %.0f s",
                    self.label, self._index, self._position)
        parked, self._parked = self._parked, None
        slot = self._queue.index_of(parked[0]) if parked is not None else None
        if slot is not None:
            self._index, self._resume = slot, parked[1]
        elif self._said in ("playing", "paused"):
            self._resume = self._position
        self._held_item = self._queue.item_at(self._index)
        self._external = True
        self._current_url = self._next_url = None
        self._awaiting_play = False
        self._seek_on_start = self._seek_due = None
        if self._preload_task is not None:
            self._preload_task.cancel()
        self._emit_now()

    def _confirmed(self) -> None:
        """The renderer was seen acting on our Play: the pending play is
        settled, and a start mid-track it seeks itself goes now."""
        self._awaiting_play = False
        target, self._seek_on_start = self._seek_on_start, None
        if target:
            asyncio.ensure_future(self._seek_started(target), loop=self._loop)

    async def _seek_started(self, target: float) -> None:
        cur = self._current_url or ""
        try:
            await self._avt("Seek", Unit="REL_TIME", Target=_rel_time(target))
        except Detached as e:
            logger.info("DLNA: the start's Seek ends — %s", e)
            return
        except Exception as e:
            if self._opus_refused(e, cur, target):
                return
            logger.warning("DLNA: the start at %d s was refused (%s) — playing "
                           "from the top", target, _describe(e))
            self._seek_due = None
            self._rebaseline(0.0)
            return
        # The renderer plays from its top until the Seek lands: the distance
        # is the second itself
        self._seek_due = (float(int(target)), time.monotonic(), self._judged(cur, float(int(target))))
        self._rebaseline(float(int(target)))
        self._emit_now()

    def _opus_seeks(self) -> Optional[bool]:
        """Whether this renderer seeks an Opus stream itself, as it was seen
        to (output.dlna_opus_seek, by UDN) — None while no Seek of the
        node's in one has landed or failed."""
        from routers.player import udn_key
        from routers.settings import _read
        return (_read("output.dlna_opus_seek") or {}).get(udn_key(self._renderer.get("udn")))

    def _remember_opus_seek(self, seeks: bool) -> None:
        """What a Seek in an Opus stream showed of this renderer, kept per
        UDN: a stored fact, never a guess from its name. The phone seeks
        one in 57–74 ms; a DAP that maps no time to VBR Opus bytes plays on
        where it was and reports no position there."""
        from routers.player import udn_key
        from routers.settings import _read, _write
        facts = dict(_read("output.dlna_opus_seek") or {})
        key = udn_key(self._renderer.get("udn"))
        if facts.get(key) is seeks:
            return
        facts[key] = seeks
        _write("output.dlna_opus_seek", facts)
        logger.info("renderer '%s' %s an Opus stream itself — remembered", self.label,
                    "seeks" if seeks else "does not seek")

    def _seeks_opus_to(self, seconds: float) -> bool:
        """Is the Opus stream playing sought to `seconds` by the renderer
        itself? The stream must hold the second (an offset stream starts at
        its ?ss=), and the renderer must be known to seek one — or, never
        seen to either way, be reporting its position on this load, where
        the Seek's landing can be seen. One that reports none (VBR Opus on a
        DAP: 0:00:00 throughout) keeps the reload, with nothing to remember."""
        if seconds < self._pos_offset:
            return False
        seeks = self._opus_seeks()
        return self._heard_on_load >= 2 if seeks is None else seeks

    def _load_again_from(self, cur: str, seconds: float):
        """The stream `cur` loaded again from `seconds` (?ss=): the Opus tier
        it was dispatched in kept, whatever the quality setting says now —
        a coroutine for _call_async."""
        base = cur
        for sep in ("&ss=", "?ss="):
            if sep in base:
                base = base.split(sep)[0]
        url = base + ("&" if "?" in base else "?") + f"ss={int(seconds)}"
        return self._load_and_play(self._index, ss=float(int(seconds)), url_override=url)

    @staticmethod
    def _judged(cur: str, distance: float) -> Optional[float]:
        """A Seek of `distance` in the stream `cur` is judged as the renderer's
        fact (its distance, else None) in an Opus stream, and only when its
        landing can be told from staying where it was: farther than both
        readings' tolerance."""
        return distance if "q=opus" in cur and abs(distance) > 2 * _RELTIME_TOLERANCE else None

    def _opus_refused(self, e: Exception, cur: str, second: float) -> bool:
        """A Seek in the Opus stream `cur` refused as one the renderer does
        not take — 710, no such seek mode; 711, no such target — loads the
        stream again from `second` instead (?ss=). 710 is the renderer's
        fact and is remembered; 711 is the target's. True when it was such a
        refusal — spent, with nothing loaded for a stream since replaced;
        False for any other refusal or format."""
        code = getattr(e, "error_code", None)
        if "q=opus" not in cur or code not in (710, 711):
            return False
        if self._current_url == cur:
            if code == 710:
                self._remember_opus_seek(False)
            logger.info("renderer '%s' refused a Seek in its Opus stream (%s) — loading it again "
                        "from %.0f s", self.label, code, second)
            self._call_async(self._load_again_from(cur, second))
        return True

    def _seek_unlanded(self) -> None:
        """A Seek in an Opus stream not seen landing by the pending-play
        deadline: the renderer played on where it was, or says nothing of its
        position — it does not seek such a stream itself. Remembered, and
        the stream loads again from where the clock stands: the Seek's
        second, run on since."""
        self._seek_due = None
        self._remember_opus_seek(False)
        second = self._wall_estimate()
        logger.info("renderer '%s' did not land a Seek in its Opus stream — loading it again "
                    "from %.0f s", self.label, second)
        self._call_async(self._load_again_from(self._current_url, second))

    def _transport_error(self) -> bool:
        """CurrentTransportStatus from GetTransportInfo: ERROR_OCCURRED is
        the renderer's word for "the last transport action failed" — a
        stream it could not open, a format it cannot decode."""
        var = self._dmr._state_variable("AVT", "TransportStatus")
        return bool(var is not None and var.value
                    and str(var.value).upper() == "ERROR_OCCURRED")

    def _ensure_poll(self) -> None:
        if self._closed or self._gone:
            return
        # The track-end advance runs INSIDE the exiting poll task: a naive
        # aliveness check sees "the poll" still running (it is the caller,
        # one `return` from death) and skips the restart — every track that
        # started through that path played deaf (position frozen at 0, no
        # end-of-track backstop; radio died on the next boundary).
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if (self._poll_task is None or self._poll_task.done()
                or self._poll_task is current):
            self._poll_task = asyncio.ensure_future(self._poll_position(),
                                                    loop=self._loop)

    def _stop_poll(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()

    async def _poll_position(self) -> None:
        """1 Hz GetPositionInfo while playing — RelTime is not evented
        (documented boundary exception, §2.6). Also our track-end detector:
        some renderers under-report the final GENA STOPPED."""
        self._misses = 0
        logger.info("position poll started (slot %d)", self._index)
        try:
            while True:
                await asyncio.sleep(1.0)
                if self._dmr is None or self._closed:
                    return
                if self._loading:
                    continue   # the load sequence owns the status right now
                # Everything read across the awaits below is evidence about
                # THIS load generation — a load landing mid-await (queue
                # replace, manual jump) rewrites index/position/length, and
                # a verdict mixing the old device reading with the new
                # fields fired _advance() into the freshly replaced queue
                # (observed live: [Play all] starting on track 2).
                gen = self._load_gen
                t_tick = time.monotonic()
                try:
                    await self._dmr.async_update()
                    self._misses = 0
                except Exception as e:
                    logger.debug("position poll update failed: %s", e)
                    self._misses += 1
                    if self._misses >= 3:
                        # The renderer left the network (battery DAPs drop
                        # Wi-Fi in deep sleep while playing from buffer) —
                        # a frozen "playing" status would just gaslight the
                        # user. Report the loss and stop pretending.
                        self._lose()
                        self._error = (f"'{self.label}' stopped responding — "
                                       "it may have left the network "
                                       "(deep sleep / Wi-Fi off)")
                        logger.warning("DLNA renderer unreachable after "
                                       "%d poll misses — stopped at slot %d, %.0f s",
                                       self._misses, self._index, self._resume or 0.0)
                        self._emit_now("stopped")
                        return
                    continue
                if self._load_gen != gen:
                    continue
                state = self._state_name()
                pending = self._settle_pending_play(state)
                if pending == "failed":
                    return
                if pending:
                    probed = await self._probe_advancing()
                    if self._load_gen != gen:
                        continue
                    if probed is not None:
                        # The position moves on our URI: the renderer IS
                        # playing this load, whatever its transport state
                        # claims — KANN's wedged stack reported STOPPED
                        # through an entire playing track (2026-09-01).
                        self._confirmed()
                        if not self._seek_pending(self._pos_offset + probed):
                            self._heard(self._pos_offset + probed)
                        logger.info("pending play confirmed by position "
                                    "probe (rel=%.1f, transport says %s)",
                                    probed, state)
                        state = "playing"
                    elif (time.monotonic() - self._pending_since
                            > _PENDING_PLAY_DEADLINE):
                        # Never confirmed, never denied, no error reported —
                        # a pending play must not hold `loading` forever (it
                        # kept the backend deaf for 13 minutes: no track-end,
                        # no advance, no tracking). Let the renderer's own
                        # state through; the stopped verdict below closes
                        # the session honestly.
                        self._awaiting_play = False
                        logger.warning("pending play unresolved after %.0fs "
                                       "(transport says %s) — reporting the "
                                       "renderer's own state",
                                       _PENDING_PLAY_DEADLINE, state)
                    else:
                        continue
                cur = getattr(self._dmr, "current_track_uri", None)
                if cur and self._next_url and cur == self._next_url:
                    # Gapless boundary: this very reading (URI, duration,
                    # RelTime 0) is the next track's — adopt the slot first.
                    self._advanced_to_next(restart_poll=False)
                elif cur and cur not in self._handed:
                    # A URI we never handed over: another controller took
                    # the renderer — never our slot on the clock, never its
                    # end (_taken_by_another)
                    self._taken_by_another()
                    return
                # async_update re-polls GetPositionInfo only while PLAYING or
                # PAUSED; in any other state CurrentTrackDuration and
                # RelativeTimePosition still hold the last reading taken —
                # across a track change, the previous track's. And a reading
                # that names another URI is another track's however fresh.
                fresh = (self._dmr.transport_state in (
                    TransportState.PLAYING, TransportState.PAUSED_PLAYBACK)
                    and (not cur or cur == self._current_url))
                dur = self._dmr.media_duration if fresh else None
                if dur:
                    self._adopt_length(float(dur))
                # Position: trust the renderer's RelTime when it advances, but
                # some renderers (KANN on VBR Opus, live) play fine yet report
                # RelTime 00:00:00 forever. So keep a wall-clock estimate and
                # snap to RelTime whenever it gives a real value — FLAC and
                # software renderers self-correct every tick; the frozen ones
                # ride the clock instead of a stuck slider.
                rel = self._dmr.media_position if fresh else None
                t_update = time.monotonic()
                if rel is not None and rel > 0.5:
                    # RelTime is 0-based on the (possibly offset) stream.
                    self._adopt_reltime(self._pos_offset + float(rel), self._clock(),
                                        paused=state == "paused")
                elif state == "playing":
                    self._position = self._wall_estimate()
                elif state == "paused":
                    # Freeze the clock: paused seconds are not played seconds,
                    # and the wall estimate is what the track-end verdict
                    # falls back on.
                    self._rebaseline(self._position)
                logger.debug("poll tick %s rel=%s dur=%s cur=…%s pos=%.1f/%.1f",
                             state, rel, dur, (cur or "")[-14:],
                             self._position, self._length)
                if state == "stopped":
                    probed = await self._probe_advancing()
                    if self._load_gen != gen:
                        continue
                    if probed is not None:
                        # Transport says STOPPED while the position advances
                        # on our URI (the wedged-KANN signature): the
                        # position is the renderer's word — keep the session
                        # alive on it instead of closing a playing track.
                        if not self._seek_pending(self._pos_offset + probed):
                            self._heard(self._pos_offset + probed)
                        self._emit_now("playing")
                        continue
                    # The verdict is about where the audio WAS when it stopped.
                    # _position is the previous tick's value; if the tick before
                    # the stop stalled (the renderer's control channel, the
                    # status pipeline) it is seconds stale and a natural end
                    # read as a manual stop — the queue halted mid-album on
                    # KANN, 7 s short of 152 s. The clock only runs while
                    # playing (paused re-anchors it), so it is the honest
                    # bound.
                    pos = max(self._position, self._wall_estimate())
                    finished = (self._length > 0 and
                                pos >= self._length - _TRACK_END_SLACK)
                    logger.info("position poll exit: device stopped (pos=%.1f/%.1f finished=%s)",
                                pos, self._length, finished)
                    if finished:
                        await self._advance()
                    else:
                        self._emit_now("stopped")
                    return
                due = self._seek_due
                if due is not None and due[2] is not None:
                    if state == "paused":
                        # A judged Seek's wait counts while the renderer
                        # plays: paused, it neither lands visibly nor plays on,
                        # and its reload would start it
                        self._seek_due = (due[0], time.monotonic(), due[2])
                    elif time.monotonic() - due[1] > _PENDING_PLAY_DEADLINE:
                        # Not seen landing, with or without readings meanwhile
                        self._seek_unlanded()
                        continue
                self._emit_now(state)
                t_emit = time.monotonic()
                if t_emit - t_tick > 2.0:
                    logger.warning("poll tick took %.1fs (renderer update %.1fs, "
                                   "status emit %.1fs) — position estimate stalls",
                                   t_emit - t_tick, t_update - t_tick, t_emit - t_update)
        except asyncio.CancelledError:
            logger.info("position poll exit: cancelled")

    async def _probe_advancing(self) -> Optional[float]:
        """RelTime from a direct GetPositionInfo when it names the current
        URL and has advanced since the previous probe — the renderer's word
        via position that it is really playing this load. None: no answer,
        another URI, or no movement. DmrDevice.async_update refreshes the
        position variables only in PLAYING/PAUSED, so when the transport
        state is idle (or lying — KANN's wedged stack) this direct action
        is the only live reading there is."""
        action = self._dmr._action("AVT", "GetPositionInfo")
        if action is None or not self._current_url:
            return None
        try:
            res = await action.async_call(InstanceID=0)
        except Exception as e:
            logger.debug("position probe failed: %s", e)
            self._probe_last = None
            return None
        rel = _parse_reltime(res.get("RelTime"))
        if rel is None or res.get("TrackURI") != self._current_url:
            self._probe_last = None
            return None
        now = time.monotonic()
        prev, self._probe_last = self._probe_last, (rel, now)
        # A baseline older than a few ticks proves nothing about NOW — at
        # the first stopped tick after real play it would read a track's
        # worth of natural progress as "advancing".
        if prev and now - prev[1] <= 3.0 and rel > prev[0] + 0.3:
            return rel
        return None

    # -- queue walking -----------------------------------------------------------

    def _url_for(self, item: QueueItem, *, quality: Optional[str] = None) -> Optional[str]:
        from streaming import service as streaming_service
        # What THIS output opens (QueueItem.play, playback.substitute): a rip
        # here, or the stream standing in for a copy held elsewhere.
        src = item.opener()
        host = media_host(self._peer_host)
        # Snapshot the global quality setting into the URL — the proxy
        # transcodes to Opus for the remote/bandwidth tiers. HQPlayer's own
        # _url_for equivalent never appends this, so its previews (same
        # /preview route) stay lossless.
        if quality is None:
            quality = self._quality()
        if src["kind"] == "file":
            proxy = streaming_service.ensure_proxy()
            path = settings.translate_to_local_path(src["path"])
            if src.get("cue_start") is not None:
                # CUE slice: the proxy serves a cached FLAC cut (produced
                # lazily on the renderer's first GET); distinct (path, span)
                # keys keep slice URLs distinct for URI-based auto-advance.
                tok = proxy.register_file(
                    path, transcode.FLAC_MIME,
                    start=src["cue_start"], end=src.get("cue_end"),
                    tags={"title": item.title, "artist": item.artist,
                          "album": item.album, "track": item.track_number})
                return proxy.file_url(tok, host=host) + self._tier(
                    transcode.FLAC_MIME, quality, recode=False)
            mime = MIME_BY_FORMAT.get((src.get("format") or "").upper(),
                                      "application/octet-stream")
            tok = proxy.register_file(path, mime)
            return proxy.file_url(tok, host=host) + self._tier(mime, quality, recode=True)
        if src["kind"] == "proxy":
            proxy = streaming_service.get_proxy()
            if proxy is None:
                return None
            # A stream's buffer is FLAC or MP3, which every renderer seen
            # lists: only its Opus tier is the sink list's to decide.
            return proxy.url_for(src["token"], host=host) + self._tier(
                None, quality, recode=False)
        return None   # foreign URIs (file:// passthrough) aren't renderer-reachable

    @staticmethod
    def _quality() -> str:
        from routers.settings import _read
        return _read("output.stream_quality") or "lossless"

    def _sink(self) -> Optional[dict]:
        """The renderer's sink list as sink_formats reads it. The library
        asks GetProtocolInfo on the first update, and ConnectionManager
        events keep it current."""
        return sink_formats(self._dmr.sink_protocol_info if self._dmr is not None else [])

    def _tier(self, mime: Optional[str], quality: str, *, recode: bool) -> str:
        """The `?q=` a stream of `mime` (None: the stream's own, FLAC or MP3)
        goes to this renderer with — the quality chosen, as far as its sink
        list allows. Opus only where the list names Ogg; the format as it is
        where the list names it; else, for a whole owned file (`recode`), a
        lossless FLAC of it, and failing that the Opus tier. A list that says
        nothing keeps the quality chosen; one that names none of these sends
        the format as it is, and the renderer's own refusal says why.

        A list was there to read before: the node handed every renderer the
        file as it is, and a phone that decodes no DSD refused the first DSD
        file of a queue (714), which stopped the queue there."""
        opus = transcode.wants_opus(quality)
        formats = self._sink()
        if formats is None:
            return f"?q={quality}" if opus else ""
        if opus and sink_spelling(transcode.MIME, formats):
            return f"?q={quality}"
        if mime is None or sink_spelling(mime, formats):
            return ""
        if recode and sink_spelling(transcode.FLAC_MIME, formats):
            return f"?q={transcode.FLAC_TIER}"
        if sink_spelling(transcode.MIME, formats):
            return "?q=opus_192"
        return ""

    @staticmethod
    def _didl_meta(item: QueueItem) -> dict:
        """Structured DIDL-Lite fields — renderers show artist/album in their
        own UI (a bare title left them as "unknown artist/album")."""
        meta = {}
        if item.artist:
            # Both spellings: renderers disagree on which one feeds the
            # "artist" line (upnp:artist per spec, dc:creator in practice).
            meta["artist"] = item.artist
            meta["creator"] = item.artist
        if item.album:
            meta["album"] = item.album
        if item.track_number:
            meta["original_track_number"] = item.track_number
        return meta

    @staticmethod
    def _to_jpeg(data: bytes, label: str) -> Optional[bytes]:
        """Any image → JPEG bytes: hi-fi renderers rarely decode webp."""
        try:
            import io
            from PIL import Image
            img = Image.open(io.BytesIO(data)).convert("RGB")
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=85)
            return out.getvalue()
        except Exception as e:
            logger.debug("cover convert failed for %s: %s", label, e)
            return None

    @staticmethod
    def _load_cover_jpeg(cover_id: str) -> Optional[bytes]:
        """Cover bytes from the covers table as JPEG. Runs in a worker
        thread (blocking DB + PIL)."""
        from db_pool import db_query_one
        row = db_query_one("SELECT data FROM covers WHERE id = %(id)s::uuid",
                           {"id": cover_id})
        if not row or not row["data"]:
            return None
        return DlnaBackend._to_jpeg(bytes(row["data"]), cover_id)

    @staticmethod
    async def _fetch_cover_jpeg(url: str) -> Optional[bytes]:
        """A phantom's provider/CAA cover as JPEG, fetched by the backend:
        renderers were handed the external URL and showed nothing — it is
        HTTPS, and Cover Art Archive answers with a 307 to archive.org on
        top, neither of which a DAP's UPnP stack follows. Fetched by the rule
        of every outside cover the node shows (covers.fetch_art: the URL
        shapes and hosts of ART_SOURCES, the size cap); a URL of no source's
        shape is no cover, and nothing is fetched for it. The renderer is
        handed its track only once this answers, so the wait is short."""
        from covers import art_source, fetch_art
        source = art_source(url)
        if source is None:
            return None
        status, _mime, body = await fetch_art(source, url, deadline=_COVER_DEADLINE_S)
        if status != 200:
            return None
        return await asyncio.to_thread(DlnaBackend._to_jpeg, body, url)

    async def _art_url(self, item: QueueItem) -> Optional[str]:
        """Renderer-fetchable cover URL: art is re-served as JPEG on the
        plain-http proxy (/art/{token}) — owned covers from the covers table,
        phantom covers fetched once from their provider/CAA URL. A cover the
        node could not fetch itself is none at all: its provider's address
        would send the renderer to the internet, which a phone renderer must
        never reach and a DAP's stack cannot (HTTPS, a redirect)."""
        from streaming import service as streaming_service
        proxy = streaming_service.ensure_proxy()
        if item.cover_id:
            sources = [(item.cover_id, partial(asyncio.to_thread, self._load_cover_jpeg))]
        elif item.preview:
            from playback.queue import resolved_artwork
            # Same order as the web UI's coverUrls(): the queued album's own
            # art first, the provider's matched release only as a fallback.
            # A provider resolves a track to whichever release carries it — a
            # compilation, another edition — so its cover can be the same
            # artist's OTHER album (seen live: renderer and Now Playing
            # disagreeing on the first two tracks of a streamed queue).
            urls = [u for u in (item.cover_url, resolved_artwork.get(item.track_id))
                    if u and u not in self._art_misses]
            sources = [(u, self._fetch_cover_jpeg) for u in urls]
        else:
            return None
        for key, loader in sources:
            tok = proxy.blob_token_for(key)
            if tok is None:
                data = await loader(key)
                if data is None:
                    # A cover not fetched is none for this renderer from now
                    # on, the host's trouble included: every track start
                    # would wait for it again, and the art is the lesser part.
                    if loader is self._fetch_cover_jpeg:
                        self._art_misses.add(key)
                    continue
                tok = proxy.register_blob(key, data, "image/jpeg")
            return proxy.art_url(tok, host=media_host(self._peer_host))
        return None

    async def _transport_meta(self, url: str, item: QueueItem, mime: str) -> str:
        """DIDL with the class FORCED to musicTrack: the library derives the
        class from the mime type and lands on plain audioItem, whose didl
        type silently drops artist/album/track-number on serialization
        (verified by reading CurrentURIMetaData back off the renderer).
        albumArtURI is injected manually — this didl_lite version has no such
        property on MusicTrack at all. The format (`mime`, what the proxy
        serves) is named as the renderer's sink list spells it: a renderer
        holds the DIDL's protocolInfo against its own list."""
        formats = self._sink()
        xml = await self._dmr.construct_play_media_metadata(
            url, item.title or "Sautium",
            override_mime_type=(formats and sink_spelling(mime, formats)) or mime,
            override_upnp_class="object.item.audioItem.musicTrack",
            meta_data=self._didl_meta(item))
        # Declare the track duration on <res>. Lossless (FLAC/PCM) renderers
        # infer elapsed time from byte offset × a fixed rate, so they track
        # RelTime without it — but VBR Opus has no such mapping, and a
        # renderer with no res@duration then plays fine yet reports RelTime
        # 00:00:00 forever (KANN, live). Standard DLNA metadata anyway.
        if item.duration_seconds:
            total = int(item.duration_seconds)
            dur = f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}.000"
            xml = xml.replace("<res ", f'<res duration="{dur}" ', 1)
        art = await self._art_url(item)
        if art:
            from xml.sax.saxutils import escape
            xml = xml.replace(
                "</item>",
                f"<upnp:albumArtURI>{escape(art)}</upnp:albumArtURI></item>", 1)
        return xml

    async def _load_and_play(self, index: int, *, play: bool = True,
                             ss: float = 0.0,
                             url_override: Optional[str] = None,
                             start_at: float = 0.0) -> bool:
        # Serialized: fire-and-forget track changes must not interleave
        # their SOAP sequences. The lock lives OUTSIDE the recursion in
        # _load_seq (asyncio.Lock is not reentrant). `ss` (seconds) is a
        # server-side seek — the track re-encodes from the offset;
        # `url_override` reuses the currently-playing stream's exact URL
        # (a seek must keep the format the track was dispatched in).
        # `start_at` (seconds) starts the slot mid-track: an Opus stream
        # reloads from there, any other is sought once it plays. While
        # another controller holds the renderer the slot is adopted for the
        # load's own "loading" only: a hand-over the renderer refuses leaves
        # the spot its takeover left where it was.
        async with self._load_lock:
            self._loading = True
            held = (self._index, self._position, self._length) if self._external else None
            try:
                return await self._load_seq(index, play=play, ss=ss,
                                            url_override=url_override,
                                            start_at=start_at)
            except Exception:
                if held is not None and self._external:
                    self._index, self._position, self._length = held
                raise
            finally:
                self._loading = False

    def _unplayable_reason(self, first: int, skipped: int) -> str:
        """Why slots ``first`` .. ``first + skipped - 1`` had nothing this
        renderer could play.

        The two cases are not the same problem and must not read the same. A
        file UNDER the library root with no media_files row is an indexing gap
        — scanning fixes it. A file outside the root is not a gap at all:
        HQPlayer can open anything on the machine, while the library is defined
        by its root, so importing whatever HQPlayer happened to be playing
        would catalogue files the owner never chose — and in a node that syncs,
        mint rows that travel to peers as a side effect of pressing play.

        A catalogued file the disk refused is neither: it was scanned, and no
        rescan brings it back while the library's drive is unmounted."""
        root = settings.library_db_root()
        norm = lambda p: p.replace("\\", "/").rstrip("/").lower()
        inside = outside = streamed = unreadable = held = 0
        for i in range(skipped):
            item = self._queue.item_at(first + i)
            src = (item.opener() or {}) if item else {}
            if src.get("kind") == "proxy":
                streamed += 1
                continue
            if src.get("kind") in ("hqp", "pending", "unplayable"):
                # A file in an HQPlayer's own library (hqp_library.sync):
                # only that HQPlayer can open it; every other output plays
                # the album afresh as a stream.
                held += 1
                continue
            if src.get("kind") == "file":
                unreadable += 1
                continue
            uri = src.get("uri", "")
            path = unquote(uri[8:] if uri.startswith("file:///") else uri)
            if root and norm(path).startswith(norm(root) + "/"):
                inside += 1
            else:
                outside += 1
        if held and not (inside or outside or unreadable or streamed):
            return (f"{held} queued track(s) live in the HQPlayer's own library "
                    "— only that HQPlayer can play them. Pick it as the output, "
                    "or play the album again to stream it here.")
        if streamed and not (inside or outside or unreadable or held):
            return (f"{streamed} streamed track(s) could not be fetched from "
                    "their provider, and nothing else is left in the queue.")
        if unreadable and not (inside or outside or held):
            return (f"{unreadable} queued track(s) could not be read from the "
                    f"library ({root or 'unset'}) — is its drive connected? "
                    "Reconnect it, then try again.")
        if inside and not (outside or unreadable or held):
            return (f"{inside} queued item(s) are in your library folder but "
                    "have not been scanned yet, so they cannot be streamed to a "
                    "renderer. Run Scan Library, then try again.")
        if outside and not (inside or unreadable or held):
            return (f"{outside} queued item(s) live outside this node's library "
                    f"({root or 'unset'}) — HQPlayer can open them directly, but "
                    "they cannot be streamed to another renderer. Play something "
                    "from the library to replace the queue.")
        return (f"None of the {skipped} queued item(s) can be sent to this "
                f"renderer: {unreadable} unreadable, {inside} not scanned yet, "
                f"{outside} outside the library folder, {held} in the "
                f"HQPlayer's own library, {streamed} not fetchable from their "
                "provider. Play something from the library to replace the queue.")

    async def _load_seq(self, index: int, *, play: bool = True, ss: float = 0.0,
                        url_override: Optional[str] = None,
                        start_at: float = 0.0) -> bool:
        # Slots with nothing to serve are walked past in a loop, not by
        # recursion: with the library's drive unmounted every owned slot is
        # one, and a queue past ~1000 of them overran the recursion limit.
        # A seek (ss, url_override) belongs to the first slot only.
        first = index
        self._parked = None
        while True:
            item = self._queue.item_at(index)
            if item is None:
                # Running off the end having skipped everything is not the
                # same as reaching the end of a queue, and it used to look
                # identical: a silent stop, with the reason only in the log.
                # It happens for real — a queue adopted from HQPlayer's own
                # playlist holds entries this library has no media_files row
                # for, so no token can be minted and no renderer but
                # HQPlayer itself can be handed them.
                if index > first:
                    self._error = self._unplayable_reason(first, index - first)
                self._emit_now("stopped")
                return False
            # url_override (a seek) carries the currently-playing stream's
            # URL, already including ?ss — keeping the format the track was
            # dispatched in even if the global quality has since changed.
            url = url_override or self._url_for(item)
            if url is None and play and (item.opener() or {}).get("kind") == "pending":
                # Its stream is on its way (playback.substitute): the play
                # waits on the slot and starts when it lands (slot_ready), as
                # the browser output's does. Walked past, every slot only a
                # stream could serve was skipped while its fetch ran — an
                # output switch's play press met that at once.
                self._stop_poll()
                if self._preload_task is not None:
                    self._preload_task.cancel()   # its "next" is not this wait's
                self._index = index
                self._load_gen += 1
                self._current_url = self._next_url = None
                self._awaiting_play = False
                # The second it starts at stays the slot's: a Play or a seek
                # meanwhile keeps or moves it
                self._resume = start_at if start_at > 0 else None
                self._parked = (item, start_at)
                self._park_uris = frozenset(u for u in (getattr(self._dmr, "current_track_uri", None),
                                                        getattr(self._dmr, "av_transport_uri", None)) if u)
                self._park_under_another = (self._state_name() in ("playing", "paused")
                                            and any(u not in self._handed for u in self._park_uris))
                self._seek_due = None
                self._position, self._length = start_at, item.duration_seconds or 0.0
                self._error = None
                logger.info("DLNA: slot %d waits for its stream", index)
                # Said before the disarm's round trip: a Stop or a Pause made
                # meanwhile says its own state last
                self._emit_now("loading")
                try:
                    # What plays now plays out; the next it had armed is no
                    # longer what follows it
                    await self._avt("SetNextAVTransportURI", NextURI="", NextURIMetaData="")
                except Exception as e:
                    logger.debug("SetNext disarm unsupported/failed: %s", e)
                return True
            if url is None:
                logger.warning("DLNA: skipping unreachable slot %d", index)
                index, ss, url_override, start_at = index + 1, 0.0, None, 0.0
                continue
            if start_at > 0 and "q=opus" in url and self._opus_seeks() is not True:
                # The Opus stream starts there — but a renderer known to seek
                # one itself starts it from the top and is sought (N-7): its
                # own player keeps the track's scale
                ss, start_at = start_at, 0.0
            if ss > 0 and not url_override:
                # Server-side seek: re-encode the track from the offset (the
                # media route honors ?ss). Only Opus URLs (which carry ?q)
                # take this path, so the "&" join is always right.
                url += ("&" if "?" in url else "?") + f"ss={int(ss)}"
            # A track change is seconds of SOAP + renderer buffering: adopt
            # the new slot and report `loading` FIRST, so the UI shows the
            # tapped track with a transition spinner instead of either lying
            # ("playing") or looking dead while the old audio rides on.
            self._index = index
            self._load_gen += 1     # in-flight poll evidence is now about a dead world
            self._pos_offset = ss   # 0 for a fresh track; the seek offset otherwise
            self._position = ss or start_at   # so the loading state shows the target
            self._length = item.duration_seconds or 0.0
            self._error = None      # something in the queue is playable after all
            self._awaiting_play = False   # a new load supersedes an unconfirmed Play
            self._seek_due = None         # ...and the old load's Seek
            self._heard_on_load = 0       # a new stream: what it reports is to be heard
            if play:
                self._emit_now("loading")
            if not self._dmr.is_subscribed and (self._resubscribe_task is None
                                                or self._resubscribe_task.done()):
                self._resubscribe_task = asyncio.ensure_future(self._resubscribe(), loop=self._loop)
            if self._preload_task is not None:
                self._preload_task.cancel()   # its "next" is not this load's next
            mime = await self._serve_ready(item, url, front=True)
            if mime is not None:
                break
            index, ss, url_override, start_at = index + 1, 0.0, None, 0.0
        # The wait may have (re)fetched the stream — as an excerpt, or from
        # another provider — and the proxy's track-ready hook has brought the
        # item up to date; the DIDL below and the track-end detection read
        # this length, so re-read it now.
        self._length = item.duration_seconds or 0.0
        # Disarm the gapless auto-advance detector: a manual jump to the
        # very track that SetNext armed makes CurrentURI == _next_url and
        # looked exactly like an auto-advance — the index bumped one PAST
        # the tapped track (observed live on BubbleUPnP).
        self._next_url = None
        if play and self._state_name() in ("playing", "paused"):
            # Renderers with gapless (BubbleUPnP) do NOT interrupt current
            # playback on SetAVTransportURI while PLAYING: the display
            # adopts the new track, the audio keeps the old one, and Play
            # is a no-op. An explicit Stop makes the switch real.
            try:
                await self._avt("Stop")
            except Exception as e:
                logger.debug("pre-load stop: %s", e)
        # Direct AVT actions, never the DmrDevice helpers: they gate on the
        # renderer's CurrentTransportActions report and silently NO-OP when
        # an action is missing from it (the Phase-2 Pause lesson). A gated
        # SetAVTransportURI/Play here meant next/prev committed the new
        # index while the renderer kept playing the old track.
        self._handed.add(url)
        await self._avt_retry("SetAVTransportURI", CurrentURI=url,
                              CurrentURIMetaData=await self._transport_meta(url, item, mime))
        self._current_url = url
        self._resume = self._held_item = None
        self._external = False      # ours again; a hand-over refused raised above, theirs still
        if play:
            await self._avt_retry("Play", Speed="1")
            # The clock starts at the offset (0 for a fresh track), or at the
            # second the start's own Seek goes to once the renderer plays —
            # due from now: it plays from the top until that Seek lands
            self._seek_on_start = start_at or None
            self._seek_due = (start_at, time.monotonic(), None) if start_at else None
            self._rebaseline(ss or start_at)
            # Accepted is not started: the renderer confirms by reporting
            # PLAYING (poll/GENA clear this), and until then the track is
            # `loading` — not "playing at 0:00", which opened a play session
            # and a Last.fm now-playing for a track the renderer may yet
            # refuse, and made its first idle tick read as a stop.
            self._awaiting_play = True
            self._pending_since = time.monotonic()
            self._probe_last = None
            self._ensure_poll()
        self._arm_preload()
        # Explicit state: _loading is still set here, and a load that did
        # not ask to play must report the renderer's actual state.
        self._emit_now("loading" if play else self._state_name())
        return True

    async def _serve_ready(self, item: QueueItem, url: str, *,
                           front: bool) -> Optional[str]:
        """Wait (off the loop) until the proxy can serve `url` at once —
        the preview fetched, and the CUE cut / FLAC / Opus tier the URL's ?q
        and ?ss ask for produced on disk. Returns the MIME type it serves
        `url` as; None when it never will (provider failure, expired token, a
        file gone from disk, a cut or a transcode that cannot be made).

        KANN probes the URI INSIDE SetAVTransportURI, and its control channel
        answers nothing until the probe returns: a preview still being fetched
        (the proxy holds the GET for up to prepare_timeout), a dead token
        (KANN retries a 404 for 10+ s) or a first-GET Opus transcode (tens of
        seconds for a long mix) froze every SOAP call — read as "3 poll misses
        → renderer left the network", then a re-attach into a stack that was
        throwing NPEs (observed live). A renderer is only ever handed a URL
        the proxy can serve immediately; the wait shows as `loading` here,
        the previous track still playing meanwhile."""
        from streaming import service as streaming_service
        proxy = streaming_service.get_proxy()
        if proxy is None:
            return None
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        q = query.get("q", [None])[0]
        try:
            ss = float(query.get("ss", ["0"])[0])
        except ValueError:
            ss = 0.0
        route, _, tok = parsed.path.rpartition("/")

        def materialize() -> str:
            if route.endswith("/preview"):
                e = proxy.wait_ready(tok, front=front)
                if e.error or e.audio is None:
                    raise RuntimeError(e.error or "no audio")
                return transcode.MIME if proxy.materialize_preview(tok, q, ss) else e.audio.mime
            return proxy.materialize_file(tok, q, ss)[1]

        try:
            return await asyncio.to_thread(materialize)
        except (KeyError, TimeoutError, RuntimeError, OSError) as ex:
            logger.warning("DLNA: stream for slot %s unavailable: %s",
                           self._queue.index_of(item), ex)
            return None

    def _arm_preload(self) -> None:
        """SetNextAVTransportURI runs as its own task: it waits for the next
        track's buffer, which must hold neither the load lock nor the
        `loading` state of a track that is already playing."""
        if self._preload_task is not None:
            self._preload_task.cancel()
        self._preload_task = asyncio.ensure_future(self._preload_next(),
                                                   loop=self._loop)

    async def _preload_next(self) -> None:
        """SetNextAVTransportURI where supported — the renderer transitions
        gaplessly on its own; we detect the URI change and follow."""
        self._next_url = None
        nxt = self._queue.item_at(self._index + 1)
        url = self._url_for(nxt) if nxt is not None else None
        mime = await self._serve_ready(nxt, url, front=False) if url is not None else None
        if mime is None:
            url = None      # a next the proxy cannot serve is disarmed, not handed over
        if url is None:
            # DISARM, don't just skip: the renderer still holds the last
            # armed NextURI — e.g. a track the user just REMOVED from the
            # queue (its proxy buffer outlives the removal) — and would
            # gapless-transition into it at track end, playing deleted
            # content while the indication stays frozen (our detector no
            # longer knows that URL). Observed live: a de-duplicated
            # streamed album "restarting from the top".
            try:
                await self._avt("SetNextAVTransportURI",
                                NextURI="", NextURIMetaData="")
            except Exception as e:
                logger.debug("SetNext disarm unsupported/failed: %s", e)
            return
        try:
            self._handed.add(url)
            await self._avt("SetNextAVTransportURI", NextURI=url,
                            NextURIMetaData=await self._transport_meta(url, nxt, mime))
            self._next_url = url
        except Exception as e:
            logger.debug("SetNextAVTransportURI unsupported/failed: %s", e)

    def _slot_of(self, url: Optional[str]) -> Optional[int]:
        """Queue slot whose media URL is `url` (file tokens are idempotent
        per path, preview tokens stable — URLs survive queue edits)."""
        if not url:
            return None
        quality = self._quality()
        for i, it in enumerate(self._queue.snapshot(), start=1):
            if self._url_for(it, quality=quality) == url:
                return i
        return None

    def _advanced_to_next(self, *, restart_poll: bool) -> None:
        """Renderer moved to the preloaded next URI by itself. The slot is
        found by that URL, not assumed to be index+1: a queue edit between
        arming SetNext and the boundary (insert-next, reorder) leaves the
        renderer playing the track that WAS next, and the display must show
        what plays — assuming index+1 showed the wrong track, one behind."""
        played = self._next_url
        self._next_url = None
        self._current_url = played
        self._index = self._slot_of(played) or self._index + 1
        item = self._queue.item_at(self._index)
        self._length = (item.duration_seconds or 0.0) if item else 0.0
        self._pos_offset = 0.0    # a gapless auto-advance is always a fresh track
        self._seek_due = None
        self._heard_on_load = 0
        self._rebaseline(0.0)     # gapless auto-advance → fresh track clock
        # The poll exits on any STOPPED observation (including the brief
        # blip some renderers emit at a gapless boundary) — a track that
        # starts through THIS path must restart it, or the backend goes
        # deaf: position frozen at 0, and the poll-side track-end backstop
        # (which caught radio's queue walking when GENA went quiet) never
        # fires again. Observed live: radio died at the end of the first
        # auto-advanced track. From inside the poll itself there is nothing
        # to restart — calling it there spawned a second, duplicate poll.
        if restart_poll:
            self._ensure_poll()
        self._arm_preload()
        self._emit_now("playing")

    async def _advance(self) -> None:
        """Track finished (STOPPED near the end) → play the next queue slot.
        One the instance was let go in the middle of ends there."""
        if self._queue.item_at(self._index + 1) is not None:
            try:
                await self._load_and_play(self._index + 1)
            except Detached as e:
                logger.info("DLNA: the advance to slot %d ends — %s", self._index + 1, e)
        else:
            self._emit_now("stopped")

    # -- transport (uvicorn threads → dlna-loop) ----------------------------------

    async def _avt(self, name: str, **kwargs):
        """Invoke an AVTransport action DIRECTLY. DmrDevice's transport
        helpers gate on the renderer's CurrentTransportActions report and
        silently no-op when an action is missing from it — the AK Connect
        renderer omits "Pause" there while executing the action perfectly,
        so the report can't be trusted."""
        action = self._dmr._action("AVT", name)
        if action is None:
            raise RuntimeError(f"renderer lacks AVTransport/{name}")
        return await action.async_call(InstanceID=0, **kwargs)

    async def _avt_retry(self, name: str, **kwargs):
        """One bounded retry on a transient 501. KANN's freshly-selected
        stack refuses the very first SetAVTransportURI with an NPE and a
        Play with an internal lock timeout, then executes the same call
        seconds later — there is no event to wait on (its "ready" is
        unobservable; SUBSCRIBE 500s through the same window), so a single
        spaced retry at this external boundary is the whole remedy. A
        second 501 propagates: the device is refusing, not settling."""
        try:
            return await self._avt(name, **kwargs)
        except Exception as e:
            if not _transient_501(e):
                raise
            logger.warning("%s refused with a transient 501 — retrying once "
                           "(%s)", name, str(e).strip() or type(e).__name__)
            await asyncio.sleep(2.0)
            return await self._avt(name, **kwargs)

    def _cmd_failed(self, e: Exception, what: str) -> None:
        if isinstance(e, Detached):
            # Let go while it was on its way: the intent ends with the
            # instance it was made on — nothing failed, nothing to say
            logger.info("DLNA %s ends — %s", what, e)
            return
        reason = _describe(e)
        logger.warning("DLNA %s failed: %s", what, reason)
        # Surface it: a silently swallowed command is how "next" looked
        # like it worked while the renderer kept playing the old track.
        if _connection_failure(e):
            # Mark the instance so the next play intent re-attaches instead
            # of hammering a ghost. The transport detail is for the log;
            # the user gets what to do about it.
            self._lose()
            self._error = (f"'{self.label}' did not respond to {what} — "
                           "wake the device (phone renderers doze) and try again")
        else:
            # A SOAP fault is the renderer answering — KANN refuses a URI it
            # cannot fetch with a 500 at SetAVTransportURI.
            self._error = f"'{self.label}' {what} failed ({reason})"
        self._emit_now()

    @staticmethod
    def _what(coro) -> str:
        """The backend method a command coroutine belongs to, for the log
        and the status error ("pause", "seek", "load and play") —
        `DlnaBackend.pause.<locals>._p` says nothing at 2 a.m."""
        parts = getattr(coro, "__qualname__", "").split(".")
        name = parts[1] if len(parts) > 1 else (parts[0] or "command")
        return name.strip("_").replace("_", " ")

    def _call(self, coro) -> bool:
        if self._loop is None:
            return False
        what = self._what(coro)
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            fut.result(timeout=_CMD_TIMEOUT)
            if self._error:
                self._error = None   # a command went through — device is back
            return True
        except Exception as e:
            self._cmd_failed(e, what)
            return False

    def _call_async(self, coro) -> bool:
        """Track changes: accept immediately and run on the loop — a DLNA
        load is seconds of SOAP + renderer buffering, and blocking the API
        request on it made every tap feel dead. Progress reaches the UI as
        the `loading` state; failures land in the status error field."""
        if self._loop is None:
            return False
        what = self._what(coro)
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def _done(f):
            try:
                f.result()
                if self._error:
                    self._error = None
            except Exception as e:
                self._cmd_failed(e, what)
        fut.add_done_callback(_done)
        return True

    def play(self) -> bool:
        async def _p():
            if self._current_url is None:
                await self._load_and_play(self._index if self._index >= 1 else 1,
                                          start_at=self._resume or 0.0)
                return
            # Serialized behind any track load: a Play that ran alongside one
            # saw the OLD track's URL and resumed it — audible for the seconds
            # the new track took to buffer, then the load's Stop cut it off
            # (the "track 1 plays for a moment before track 2" report). Once
            # the load is through, its own Play is still pending and this
            # intent is already met.
            async with self._load_lock:
                if self._awaiting_play:
                    return
                # From PAUSED the renderer holds the media and plays at once;
                # from STOPPED (a finished or refused track, still loaded)
                # this is a fresh start it has yet to confirm.
                fresh_start = self._state_name() != "paused"
                await self._avt_retry("Play", Speed="1")
                # Paused → resumes where it was; stopped → UPnP Stop reset the
                # position and Play starts the track over (KANN, live: the
                # display sat at 151/151 while the track restarted).
                self._rebaseline(0.0 if fresh_start else self._position)
                self._awaiting_play = fresh_start
                if fresh_start:
                    self._pending_since = time.monotonic()
                    self._probe_last = None
                    # A Seek still waited for is the play-through's that
                    # ended: one into the last track's end, never seen
                    # landing, reloaded this start at its deadline and was
                    # remembered as one the renderer does not take
                    self._seek_due = None
                self._ensure_poll()
                self._emit_now("loading" if fresh_start else "playing")
        return self._call_async(_p())

    def pause(self) -> bool:
        async def _p():
            if self._parked is not None:
                # A wait for a slot's stream ends: paused at its second, the
                # play press starts it there — and only what the renderer
                # still plays (the track tapped away from) is paused
                self._parked = None
                if self._state_name() == "playing":
                    await self._avt("Pause")
                self._emit_now("paused")
                return
            await self._avt("Pause")
            self._emit_now("paused")
        return self._call(_p())

    def stop(self) -> bool:
        async def _s():
            self._stop_poll()
            self._awaiting_play = False
            # A Stop starts the slot over, as a renderer's own Stop does. It
            # is this instance's word from here: a lost renderer that never
            # answers it leaves no spot behind (_lose) — said once taken, so
            # one the renderer refuses never reads as a stop of the listen.
            # No retry: a slow refusal and its retry could outlast the
            # command's window, read as the renderer gone. While another
            # controller holds it the Stop is of its playback: the spot its
            # takeover left stays, as on HQPlayer
            self._seek_on_start = self._parked = self._seek_due = None
            if not self._external:
                self._resume = None
            self._said = "stopped"
            await self._avt("Stop")
            self._emit_now("stopped")
        return self._call(_s())

    def next(self) -> bool:
        return self._call_async(self._load_and_play(self._index + 1))

    def previous(self) -> bool:
        return self._call_async(self._load_and_play(max(1, self._index - 1)))

    def select(self, index: int, track_id: Optional[str] = None) -> bool:
        if self._queue.item_at(index) is None:
            return False
        return self._call_async(self._load_and_play(index))

    def seek(self, seconds: int) -> bool:
        if self._external and self._resume is None and self._parked is None:
            # Another controller holds the renderer and no listen of ours was
            # cut: nothing of ours to seek in, and its stream is not ours to move
            self._local.refusal = f"another app is playing through {self.label} — Play takes it back"
            return False
        if self._current_url is None and (self._resume is not None or self._parked is not None):
            # A carried-over spot, a slot waiting for its stream, the spot
            # another controller's takeover left — nothing of ours loaded:
            # the start goes to the new second (a Seek now finds no media)
            self._resume = self._position = float(seconds)
            if self._parked is not None:
                self._parked = (self._parked[0], float(seconds))
            self._emit_now()
            return True
        # Decide by the CURRENTLY-PLAYING stream's format, not the global
        # quality setting: a mid-track quality switch leaves the current
        # track playing its ORIGINAL format (the change applies from the
        # next track), so its URL — not the setting — is the source of truth.
        cur = self._current_url or ""
        if "q=opus" in cur and not self._seeks_opus_to(seconds):
            # A renderer that cannot time-seek VBR Opus (a DAP: no byte↔time
            # map) gets the SAME stream again from the second. The reload
            # restarts the renderer's own player at 0:00 (the phone's scale
            # read 2:13 of a 7:22 track after ss=309), so a renderer that
            # seeks the stream itself is asked to first (N-7). Fire-and-forget
            # like a track change; the UI holds the scrubbed position meanwhile.
            return self._call_async(self._load_again_from(cur, seconds))

        async def _s():
            # Where the renderer is as it is asked: the clock, held if paused
            was = self._position if self._said == "paused" else self._wall_estimate()
            try:
                # In the stream's own time: an offset stream starts at its ?ss=
                await self._avt("Seek", Unit="REL_TIME", Target=_rel_time(seconds - self._pos_offset))
            except Exception as e:
                if self._opus_refused(e, cur, seconds):
                    return
                raise
            self._rebaseline(float(int(seconds)))   # clock continues from the seek target
            self._seek_due = (float(int(seconds)), time.monotonic(), self._judged(cur, seconds - was))
            self._emit_now()
        return self._call(_s())

    def set_volume(self, level: float) -> bool:
        async def _v():
            await self._dmr.async_set_volume_level(
                max(0.0, min(1.0, float(level) / 100.0)))
            self._emit_now()
        return self._call(_v())

    def volume_up(self) -> bool:
        cur = (self._dmr.volume_level or 0.0) * 100.0 if self._dmr else 0.0
        return self.set_volume(min(100.0, cur + 5.0))

    def volume_down(self) -> bool:
        cur = (self._dmr.volume_level or 0.0) * 100.0 if self._dmr else 0.0
        return self.set_volume(max(0.0, cur - 5.0))

    # -- canonical-queue hooks -------------------------------------------------------

    def queue_changed(self, kind: str, *, play: bool = False) -> None:
        if kind == "replace":
            if play and len(self._queue):
                self._call_async(self._load_and_play(1))
            elif self._external:
                # Another controller holds the renderer: nothing of ours plays
                # there to stop, and the spot — or a play waiting for its
                # stream — went with the queue it was in
                async def _drop():
                    self._resume = self._held_item = self._parked = None
                    self._index = 0
                    self._emit_now()
                self._call(_drop())
            else:
                self.stop()
            return

        async def _resync():
            if self._parked is not None:
                # A play waiting on its stream: its slot follows its track,
                # and nothing is armed after it — the renderer may still play
                # the track tapped away from, and would roll into that
                slot = self._queue.index_of(self._parked[0])
                if slot is None:
                    self._parked = self._resume = None   # its track left the queue
                    self._emit_now("stopped")
                    return
                self._index = slot
                self._emit_now()
                return
            # The queue mutated around the playing slot: re-locate the
            # current index by media URL (file tokens are idempotent per
            # path, preview tokens stable — URLs survive mutations), then
            # refresh the preloaded next. A removed current item keeps
            # playing on the renderer until its natural end. Nothing of ours
            # loaded — another controller's playback among it — arms nothing:
            # the renderer would roll from what it plays into our next
            slot = self._slot_of(self._current_url)
            if self._external and self._held_item is not None:
                # Another controller holds it: the slot Play loads follows its
                # item, and the spot goes with it
                slot = self._queue.index_of(self._held_item)
                if slot is None:
                    self._resume = self._held_item = None
            if slot is not None:
                self._index = slot
            if self._current_url is not None:
                self._arm_preload()
            self._emit_now()
        self._call(_resync())
