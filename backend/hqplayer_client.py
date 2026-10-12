#!/usr/bin/env python3
"""
HQPlayer Control API Client — Desktop 5/6 and Embedded 6, one wire protocol
Based on HQPlayer SDK (engine version 5.29.2); wire-compatible with HQPlayer 6
Desktop and with HQPlayer Embedded 6 (verified against engine 6.2.3 on
HQPlayer OS, 2026-09-27).

Protocol: XML over TCP
Default port: 4321
"""

import logging
import re
import socket
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from typing import Any, Callable, Dict, Iterator, List, Optional, Protocol, Tuple
from dataclasses import asdict, dataclass
from enum import IntEnum
from urllib.parse import unquote

logger = logging.getLogger(__name__)

# A volume HQPlayer holds fixed, in words — the HQPlayer screen, the volume
# route and the assistant all say it so: its own answer to a Volume command is
# a bare result="Error". Direct SDM fixes it (Desktop 6.2.3, seen live).
VOLUME_FIXED = ("HQPlayer holds its volume fixed — Direct SDM or a fixed volume "
                "is on in its settings. Turn your amplifier instead.")


def connect_address(host: str) -> str:
    """The address a connection to HQPlayer dials: "localhost" is this
    machine's IPv4 loopback. Windows resolves the name to ::1 first, and
    HQPlayer listens on IPv4 only — a refused attempt on every connect. A
    saved "localhost" stays as it is (hqp_library reads it as this machine)."""
    return "127.0.0.1" if host.strip().lower() == "localhost" else host


# -- Redaction -------------------------------------------------------------------
# A path names what the owner listens to, so anything that leaves this process
# (the diagnostic bundle, a log line about a refused file) carries only its last
# two components — the album folder and the file — never where the library lives.

_TOKEN_URL = re.compile(r"^(https?://[^/]+/(?:file|preview)/)([^?/#]+)")


def redact_path(path: str) -> str:
    parts = [p for p in re.split(r"[\\/]+", path) if p]
    if len(parts) <= 2:
        return path
    return "…/" + "/".join(parts[-2:])


def redact_uri(uri: str) -> str:
    """A file:// URI or a bare path down to its last two components; a media
    proxy URL down to the first characters of its token (the token is the
    capability that serves the file)."""
    m = _TOKEN_URL.match(uri)
    if m:
        return f"{m.group(1)}{m.group(2)[:6]}…"
    if uri.startswith("file://"):
        return "file://" + redact_path(uri[len("file://"):])
    if re.match(r"^(?:[A-Za-z]:[\\/]|/|\\\\)", uri):      # a drive, POSIX or UNC path
        return redact_path(uri)
    return uri


@dataclass(frozen=True)
class CommandOutcome:
    """What HQPlayer answered to one command. `result` is the element's own
    `result` attribute — "OK", "Error" (`message` is HQPlayer's reason, the
    element's text) or None (queries carry none, some setters answer bare) —
    or what went wrong below the protocol: "refused" (no connection), "lost"
    (no answer: the socket dropped, timed out, or returned no XML; `during`
    names the command it was carrying) or "unparsed" (a Status/State answer
    that did not parse)."""
    ts: float
    host: str
    port: int
    command: str
    attributes: dict
    result: Optional[str]
    message: str = ""
    during: Optional[str] = None

    @property
    def failed(self) -> bool:
        return self.result not in ("OK", None)

    def public(self) -> dict:
        d = asdict(self)
        d["attributes"] = {k: redact_uri(str(v)) for k, v in self.attributes.items()}
        return d


class PlaybackState(IntEnum):
    """HQPlayer playback states as its control SDK (6.0.1) names them.
    HQPlayer reports others it does not name — Embedded 6.2.3 said 5 while
    it started a play after it had reset itself (2026-10-03) — and those
    stay their number: one unnamed state is no reason to lose the status."""
    STOPPED = 0
    PAUSED = 1
    PLAYING = 2
    STOPREQ = 3

    @classmethod
    def _missing_(cls, value):
        if not isinstance(value, int):
            return None
        state = int.__new__(cls, value)
        state._name_, state._value_ = f"STATE_{value}", value
        return state

    @property
    def known(self) -> bool:
        """Named by the SDK. An unnamed state is HQPlayer between two of
        them: no verdict on it — not stopped, not idle."""
        return self._name_ in type(self).__members__


class RepeatMode(IntEnum):
    """HQPlayer repeat modes"""
    NONE = 0
    SINGLE = 1
    ALL = 2


@dataclass
class TrackStatus:
    """Current track status"""
    state: PlaybackState
    track_index: int
    track_id: str
    position: float  # seconds
    length: float  # seconds
    volume: float
    artist: str = ""
    album: str = ""
    song: str = ""
    genre: str = ""
    convolution: bool = False
    matrix_profile: str = ""
    # Processing speed over playback speed (HQPlayer's running average of its
    # last processing units); None when HQPlayer does not report it — before
    # 5.17.0.
    process_speed: Optional[float] = None
    # The engine's input and output buffer fill (the SDK's statusIO); None
    # when HQPlayer does not report them.
    input_fill: Optional[float] = None
    output_fill: Optional[float] = None
    # HQPlayer's "Limited" counter (<Status clips>): how often its soft-knee
    # limiter acted because the OUTPUT would have passed 0 dB. None when
    # HQPlayer does not report it.
    limited: Optional[int] = None
    # The adaptive gain HQPlayer applies to this track (<metadata gain>, dB;
    # 0 with adaptive gain off or unreported) — ahead of its limiter, like
    # the volume. None while it names no track (stopped).
    track_gain: Optional[float] = None
    tracks_total: int = 0
    # What the engine is running right now, by name — the DSP line of a
    # playback trace without three more round-trips.
    active_mode: str = ""
    active_filter: str = ""
    active_shaper: str = ""
    active_rate: int = 0
    # The source HQPlayer decodes (<metadata samplerate bits channels sdm>):
    # active_rate and its siblings are the OUTPUT. None while nothing plays.
    src_rate: Optional[int] = None
    src_bits: Optional[int] = None
    src_channels: Optional[int] = None
    src_sdm: Optional[bool] = None
    # The entry HQPlayer is reading, in the form its playlist names it
    # (<metadata uri>, while a track is loaded; Desktop 6.2.3 sends it to an
    # unauthenticated client). The slot index names an entry only through
    # the playlist; this names it outright.
    uri: str = ""
    # HQPlayer 6's further Status fields; None from a build that does not
    # report them (HQPlayer 5). `apod` is its Apod counter for the track
    # playing — it starts at 0 with every track; `active_bits` and
    # `active_channels` describe the OUTPUT, like active_rate; `output_delay`
    # is the time from a sample's position to the output, in seconds
    # (HQPlayer reports microseconds); `display_position` is the position it
    # shows; `correction` and `filter_20k` are its switches, `filter_junk`
    # the junk filter it runs (an index into its own list of them).
    apod: Optional[int] = None
    active_bits: Optional[int] = None
    active_channels: Optional[int] = None
    output_delay: Optional[float] = None
    correction: Optional[bool] = None
    filter_20k: Optional[bool] = None
    filter_junk: Optional[int] = None
    display_position: Optional[float] = None

    @property
    def is_playing(self) -> bool:
        return self.state == PlaybackState.PLAYING

    @property
    def progress_percent(self) -> float:
        if self.length > 0:
            return (self.position / self.length) * 100
        return 0.0


# The DSP knobs in the order HQPlayer needs them set: the mode decides which
# rate, filter and shaper lists the indices after it point into; the
# convolution switch comes last, over whatever a matrix profile brought.
DSP_KNOBS = ("mode", "rate", "filter", "shaper", "matrix_profile", "convolution")

# The TrackStatus fields the playback status carries as HQPlayer gives them
# (playback.hqp_backend._status_of → PlaybackManager), each only while it
# reports one.
STATUS_READINGS = ("apod", "active_bits", "active_channels", "output_delay",
                   "correction", "filter_20k", "filter_junk", "display_position")


def _optional(el: ET.Element, key: str, cast: Callable[[str], Any]) -> Any:
    """An attribute a build may not report: None when it does not."""
    value = el.get(key)
    return None if value is None or value == "" else cast(value)


def _flag(value: str) -> bool:
    return bool(int(value))


# -- Filter and modulator descriptions ---------------------------------------------
# HQPlayer 6 describes each filter in its own words: Signalyst's rating out of
# five, the focus words, the mode's arrow and the ratio class —
# "4/5 transients, timbre, space ⥮ 2^x up". PCM mode writes ⥮, SDM mode ⥣;
# only PCM adds "up" (a filter that converts upwards only). A focus list may
# keep a trailing comma ("4/5 space, ⥮ Any") or be absent ("4/5 ⥮ Any"), and the
# two modes may list one filter's words in another order, so the focus is a
# set: two descriptions of one filter agree when their rating, focus and ratio
# class do. A modulator's description is its generation ("Gen7"). A shape not
# read here is left unparsed — never guessed — and said once per string.

_FILTER_DESCRIPTION = re.compile(r"^([1-5])/5 (?:(.+?),? )?([⥮⥣]) (1:1|Int|Any|2\^x)( up)?$")
_FOCUS_WORDS = frozenset({"space", "transients", "timbre"})
_ARROW_MODE = {"⥮": "pcm", "⥣": "sdm"}
_GENERATION = re.compile(r"^Gen([1-9][0-9]?)$")
_unread_descriptions: set = set()
_unread_lock = threading.Lock()


def _unread(kind: str, text: str) -> None:
    with _unread_lock:
        if text in _unread_descriptions:
            return
        _unread_descriptions.add(text)
    logger.warning("HQPlayer describes a %s as %r — a shape this client does not read", kind, text)


def parse_filter_description(text: str) -> Optional[Dict[str, Any]]:
    """{rating, focus (sorted), ratio, up, mode} from a filter's description;
    None for none (HQPlayer 5) or a shape not read here."""
    if not text:
        return None
    m = _FILTER_DESCRIPTION.match(text)
    focus = frozenset(w.strip() for w in (m.group(2) or "").split(",") if w.strip()) if m else None
    if m is None or not focus <= _FOCUS_WORDS:
        _unread("filter", text)
        return None
    return {"rating": int(m.group(1)), "focus": sorted(focus), "ratio": m.group(4),
            "up": m.group(5) is not None, "mode": _ARROW_MODE[m.group(3)]}


def parse_shaper_description(text: str) -> Optional[Dict[str, Any]]:
    """{generation} from a modulator's description; None for none (PCM
    dithers, HQPlayer 5) or a shape not read here."""
    if not text:
        return None
    m = _GENERATION.match(text)
    if m is None:
        _unread("modulator", text)
        return None
    return {"generation": int(m.group(1))}


class LateAnswers(Protocol):
    """Who hears of a late answer (HQPlayerClient `late`). HQPlayer answers
    nothing on its control port while it builds a setting, so an answer to
    one of `commands` that has not come after `after` seconds is that.
    `begin` is told on the thread that waits for it, and returns what that
    thread calls once the wait ends, with how it ended: "answered", "timeout"
    (the wait ran out, the connection open) or "lost" (the connection broke
    — HQPlayer closed it, restarted, or its machine stopped answering the
    keepalive probes)."""
    after: float
    commands: frozenset

    def begin(self, command: str, attributes: Dict[str, str]) -> Callable[[str], None]: ...


class _PerThread:
    """What the latest request a thread sent came to — its failure, whether
    HQPlayer kept the connection silent, whether it answered — is that
    thread's: a link's one client is shared, and its requests interleave
    (playback.hqp_link), so a caller reads its own outcome in the same call
    as its request, never another thread's."""

    def __init__(self, default: Any) -> None:
        self.default = default

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def __get__(self, obj: Any, owner: Optional[type] = None) -> Any:
        if obj is None:
            return self
        return getattr(obj._outcomes, self.name, self.default)

    def __set__(self, obj: Any, value: Any) -> None:
        setattr(obj._outcomes, self.name, value)


class HQPlayerClient:
    """
    HQPlayer Desktop 5/6 Control API Client

    Implements basic control functions without authentication.
    For full feature set including encrypted commands, authentication would be needed.

    Every failed command lands in one ring shared by all instances
    (`last_errors()`): the diagnostics read every HQPlayer's failures —
    each link's client, the benchmark's own — in one place.

    A command is taken when its outcome did not fail (`CommandOutcome.failed`,
    `_accepted`): `result="OK"`, or a bare element — some setters answer that
    way, and it is HQPlayer accepting. Play, SelectTrack and PlaylistAdd
    count only an explicit OK.
    """

    _error_ring: deque = deque(maxlen=50)
    _error_lock = threading.Lock()

    # The failure of the thread's latest command (None when HQPlayer accepted
    # it) — what a caller quotes when a setter returns False.
    last_error = _PerThread(None)
    # A command since the last connect found HQPlayer there and got no answer
    # within the timeout — as opposed to a connection that closed or refused.
    # Building a heavy setting blocks HQPlayer's control port for many
    # seconds (playback.hqp_benchmark waits it out). Kept until the next
    # connect: without a gate the commands after the silent one in a step
    # (apply_settings) fail as "not connected", and still mean it; a link's
    # gate clears it before each request it lets through.
    timed_out = _PerThread(False)
    # The latest read got its line: HQPlayer answered, whatever it said.
    answered = _PerThread(False)
    _io_error = _PerThread(None)

    def __init__(self, host: str = "127.0.0.1", port: int = 4321, timeout: float = 5.0,
                 *, ring: bool = True, waits: Optional[Dict[str, float]] = None,
                 late: Optional["LateAnswers"] = None, gate: Any = None):
        """
        Initialize HQPlayer client

        Args:
            host: HQPlayer host — "localhost" is dialled as 127.0.0.1
                (connect_address); the Docker node names host.docker.internal
                or the HQPlayer's IP
            port: Control port (default 4321)
            timeout: Socket timeout in seconds — how long an answer is waited
                for, unless `waits` names the command
            ring: whether this instance's failures join the shared ring — the
                benchmark's client says no: it asks for settings HQPlayer may
                refuse by design, and records each refusal in its own log
            waits: seconds to wait for the answer to a command, by command —
                what a Stop's output teardown or a build may take; a command
                given up on is carried out by HQPlayer all the same
            late: told when the answer to one of its commands is late — the
                playback backend's build watch
            gate: what every request passes, around its send and its answer
                (`gate.request(command)`, a context), and around its bytes
                (`gate.sending()`) — the HQPlayer link (playback.hqp_link):
                one request on its connection at a time, and none it may no
                longer send
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ring = ring
        self.waits: Dict[str, float] = dict(waits or {})
        self.late = late
        self.gate = gate
        self.socket: Optional[socket.socket] = None
        self.buffer = bytearray()
        # Connections made so far, and the ones HQPlayer ended — closed,
        # reset, refused: a status poller sharing the client tells an
        # HQPlayer that came back by the latter, never by a reconnect of
        # this node's own after a silence
        self.connections = 0
        self.losses = 0
        self._outcomes = threading.local()
        # Called with every outcome, failures and successes alike — the
        # playback backend's trace listens on its link's client.
        self.on_outcome: Optional[Callable[[CommandOutcome], None]] = None

    @classmethod
    def last_errors(cls, since: Optional[float] = None) -> List[Dict[str, Any]]:
        """The ring of failed commands, newest first, attributes redacted."""
        with cls._error_lock:
            entries = list(cls._error_ring)
        return [e.public() for e in reversed(entries) if since is None or e.ts >= since]

    def refusal(self) -> str:
        """Why this thread's latest command was not accepted, in HQPlayer's
        words when it gave any — what a caller reports when a command returns
        False."""
        e = self.last_error
        if e is None:
            return "HQPlayer did not confirm it"
        if e.message:
            return e.message
        return f"HQPlayer refused {e.command}" if e.result == "Error" else f"HQPlayer: {e.result}"

    def _outcome(self, command: str, attributes: Optional[Dict[str, str]],
                 result: Optional[str], message: str = "",
                 during: Optional[str] = None) -> None:
        outcome = CommandOutcome(ts=time.time(), host=self.host, port=self.port,
                                 command=command, attributes=dict(attributes or {}),
                                 result=result, message=message, during=during)
        if outcome.failed:
            self.last_error = outcome
            if self.ring:
                with self._error_lock:
                    self._error_ring.append(outcome)
        if self.on_outcome is not None:
            self.on_outcome(outcome)

    def connect(self) -> bool:
        """
        Connect to HQPlayer

        Returns:
            True if connected successfully
        """
        sock = self.dial()
        if sock is None:
            self.socket = None
            return False
        self.use(sock)
        return True

    def use(self, sock: socket.socket) -> None:
        """Talk to HQPlayer on `sock`, a connection `dial` made."""
        self.socket = sock
        self.buffer = bytearray()
        self.connections += 1
        logger.info(f"Connected to HQPlayer at {self.host}:{self.port}")

    def dial(self) -> Optional[socket.socket]:
        """A new connection to HQPlayer, set up for this client's reads — not
        yet the one it talks on (use) — or None, the outcome saying why."""
        self.timed_out = False      # a failed connect is HQPlayer away, never its silence
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Connect must be quick on LAN/localhost. A stalled HQPlayer that
            # accepts SYN slowly would otherwise burn the full read timeout on
            # every reconnect attempt. Cap connect at 2s, then restore the
            # full timeout for reads (commands can legitimately take seconds).
            sock.settimeout(min(self.timeout, 2.0))
            sock.connect((connect_address(self.host), self.port))
            sock.settimeout(self.timeout)
            # Tight TCP keepalive: the WSL2→Windows NAT silently drops idle
            # TCP mappings (no RST) — an idle command socket then surfaces
            # only as the NEXT command's full read-timeout + reconnect cycle.
            # Keepalive probes hold the mapping open and detect a dead peer
            # in ~35 s instead. Timer constants are per-platform (Linux:
            # KEEPIDLE/KEEPINTVL/KEEPCNT; macOS: TCP_KEEPALIVE; availability
            # probed with hasattr — plain SO_KEEPALIVE everywhere else).
            # Keepalive probes only an idle connection: TCP_USER_TIMEOUT
            # (Linux, ms) keeps the same bound while a command sent to a
            # vanished HQPlayer is still unacknowledged — a build's held
            # answer waits minutes, and a dead peer must not pass for one.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt, val in (("TCP_KEEPIDLE", 20), ("TCP_KEEPINTVL", 5),
                             ("TCP_KEEPCNT", 3), ("TCP_KEEPALIVE", 20),
                             ("TCP_USER_TIMEOUT", 35_000)):
                if hasattr(socket, opt):
                    try:
                        sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
                    except OSError:
                        pass  # platform exposes the constant but rejects it
            return sock
        except Exception as e:
            logger.error(f"Failed to connect to HQPlayer: {e}")
            self.losses += 1
            if sock is not None:
                sock.close()
            self._outcome("<connection>", None, "refused",
                          f"connect to {self.host}:{self.port}: {e}")
            return None

    def disconnect(self):
        """Disconnect from HQPlayer"""
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None
            self.buffer = bytearray()
            logger.info("Disconnected from HQPlayer")

    def is_connected(self) -> bool:
        """Check if connected to HQPlayer"""
        return self.socket is not None

    def _send_command(self, xml_command: str) -> bool:
        """
        Send XML command to HQPlayer

        Args:
            xml_command: XML command string

        Returns:
            True if sent successfully
        """
        if not self.socket:
            logger.error("Not connected to HQPlayer")
            self._io_error = "not connected"
            return False

        try:
            # A read leaves the socket at whatever its wait had left
            self.socket.settimeout(self.timeout)
            self.socket.sendall(xml_command.encode('utf-8'))
            return True
        except Exception as e:
            logger.error(f"Failed to send command: {e}")
            self._io_error = f"send failed: {e}"
            self.losses += 1
            self.disconnect()
            return False

    def _read_line(self, wait: Optional[float] = None,
                   on_late: Optional[Callable[[], Callable[[str], None]]] = None
                   ) -> Optional[bytes]:
        """
        Read one answer — a newline-framed line — waiting `wait` seconds for
        it (the client's timeout by default). With `on_late`, an answer that
        has not come after `late.after` seconds is told to it, and what it
        returns hears how the wait ended (LateAnswers). None when there is
        none: `_io_error` says why, `timed_out` whether HQPlayer kept the
        connection and said nothing; `answered` whether a line came.
        """
        self.answered = False
        if not self.socket:
            self._io_error = "not connected"
            return None

        wait = self.timeout if wait is None else wait
        deadline = time.monotonic() + wait
        ended: Optional[Callable[[str], None]] = None
        how = "lost"
        try:
            self.socket.settimeout(min(wait, self.late.after) if on_late else wait)
            scanned = 0
            while (nl := self.buffer.find(b"\n", scanned)) < 0:
                scanned = len(self.buffer)
                try:
                    chunk = self.socket.recv(1 << 16)
                except TimeoutError as e:
                    # errno set: the peer is gone (keepalive), not our wait run out
                    if on_late is None or ended is not None or e.errno is not None:
                        raise
                    ended = on_late()
                    self.socket.settimeout(max(deadline - time.monotonic(), 0.001))
                    continue
                if not chunk:
                    # EOF: HQPlayer closed its side. Close ours immediately so
                    # the socket doesn't linger in CLOSE_WAIT until the next
                    # request's peek happens to notice it.
                    self._io_error = "closed by HQPlayer"
                    self.losses += 1
                    self.disconnect()
                    return None
                self.buffer += chunk
            how = "answered"
            self.answered = True
            line = bytes(self.buffer[:nl])
            del self.buffer[:nl + 1]
            return line
        except TimeoutError as e:
            if e.errno is not None:
                logger.error(f"Failed to read response: {e}")
                self._io_error = f"no answer: {e}"
                self.losses += 1
                self.disconnect()
                return None
            # The connection goes all the same: the late answer would be
            # read as the next command's.
            logger.warning(f"HQPlayer did not answer within {wait:g} s")
            self._io_error = f"no answer within {wait:g} s"
            self.timed_out = True
            how = "timeout"
            self.disconnect()
            return None
        except Exception as e:
            logger.error(f"Failed to read response: {e}")
            self._io_error = f"no answer: {e}"
            self.losses += 1
            self.disconnect()
            return None
        finally:
            if ended is not None:
                ended(how)

    def _read_response(self, wait: Optional[float] = None,
                       on_late: Optional[Callable[[], Callable[[str], None]]] = None
                       ) -> Optional[ET.Element]:
        """The answer (_read_line) as an XML element, or None."""
        line = self._read_line(wait, on_late)
        if line is None:
            return None
        try:
            xml_str = line.decode('utf-8').strip()
            if xml_str:
                return ET.fromstring(xml_str)
            self._io_error = "empty answer"
            return None
        except (ET.ParseError, UnicodeDecodeError) as e:
            logger.error(f"Failed to read response: {e}")
            self._io_error = f"unreadable answer: {e}"
            self.disconnect()
            return None

    def _execute_command(self, command: str, attributes: Optional[Dict[str, str]] = None,
                        expect_response: bool = True, *, wait: Optional[float] = None,
                        watched: Optional[bool] = None) -> Optional[ET.Element]:
        """
        Execute command and optionally wait for response

        Args:
            command: Command name (e.g., "Play", "Stop")
            attributes: Command attributes dict
            expect_response: Whether to wait for response
            wait: seconds to wait for the answer — by default what `waits`
                says for the command, else the client's timeout
            watched: whether a late answer is told to `late` — by default
                whether `late` names the command

        Returns:
            Response element or None

        Every command leaves a CommandOutcome (`_outcome`): HQPlayer's own
        `result`, and on "Error" its reason — the element's text, which is
        the only place HQPlayer says why (the Signalyst SDK's client reads it
        at the start tag, where Qt's stream reader has no text yet).

        With a gate, the request passes it first: it may be refused there
        (the gate's own exceptions — HQPlayer building, a request no longer
        wanted, no connection to be had), and then nothing was sent.
        """
        if self.gate is None:
            return self._exchange(command, attributes, expect_response, wait, watched)
        with self.gate.request(command):
            return self._exchange(command, attributes, expect_response, wait, watched)

    def _exchange(self, command: str, attributes: Optional[Dict[str, str]],
                  expect_response: bool, wait: Optional[float],
                  watched: Optional[bool]) -> Optional[ET.Element]:
        self.last_error = None
        self._io_error = None
        # Build XML command
        root = ET.Element(command)
        if attributes:
            for key, value in attributes.items():
                root.set(key, str(value))

        xml_str = ET.tostring(root, encoding='unicode')

        # Send command
        if not self._send(xml_str):
            self._outcome("<connection>", attributes, "lost",
                          f"{command}: {self._io_error}", during=command)
            return None

        # Read response if expected
        if expect_response:
            if watched is None:
                watched = self.late is not None and command in self.late.commands
            on_late = ((lambda: self.late.begin(command, dict(attributes or {})))
                       if watched else None)
            response = self._read_response(self.waits.get(command, self.timeout)
                                           if wait is None else wait, on_late)
            if response is None:
                self._outcome("<connection>", attributes, "lost",
                              f"{command}: {self._io_error}", during=command)
                return None
            result = response.get("result")
            self._outcome(command, attributes, result,
                          (response.text or "").strip() if result == "Error" else "")
            return response

        return None

    def raw(self, xml: str, wait: float) -> bytes:
        """One request sent as it stands, answered as the line of bytes
        HQPlayer sent — a library listing is no XML a parser takes as it
        stands (hqp_library). Raises ConnectionError — TimeoutError when
        HQPlayer kept the connection and said nothing — when there is none."""
        if self.gate is None:
            return self._raw(xml, wait)
        with self.gate.request("raw"):
            return self._raw(xml, wait)

    def _send(self, xml_command: str) -> bool:
        """_send_command, the gate's last word on it first (`gate.sending()`,
        a context): it may still refuse, and then nothing was sent."""
        if self.gate is None:
            return self._send_command(xml_command)
        with self.gate.sending():
            return self._send_command(xml_command)

    def _raw(self, xml: str, wait: float) -> bytes:
        self._io_error = None
        if not self._send(xml):
            raise ConnectionError(f"HQPlayer at {self.host}:{self.port}: {self._io_error}")
        line = self._read_line(wait)
        if line is None:
            error = TimeoutError if self._io_error.startswith("no answer within") else ConnectionError
            raise error(f"HQPlayer at {self.host}:{self.port}: {self._io_error}")
        return line

    def _accepted(self, command: str, attributes: Optional[Dict[str, str]] = None) -> bool:
        """Sent, answered, and not refused — by the one rule the error ring,
        `last_error` and the playback trace read: the outcome did not fail.
        An answer alone is no acceptance: HQPlayer answers <Volume
        result="Error"/> while its volume is fixed, and HQPlayer 6 without
        internet access refuses every state change of an unauthenticated
        client ("not authenticated and no internet access")."""
        response = self._execute_command(command, attributes)
        return response is not None and self.last_error is None

    # ========== Playback Control ==========

    def play(self) -> bool:
        """Start playback"""
        response = self._execute_command("Play")
        return response is not None and response.get("result") == "OK"

    def pause(self) -> bool:
        """Pause playback"""
        return self._accepted("Pause")

    def stop(self) -> bool:
        """Stop playback"""
        return self._accepted("Stop")

    def next(self) -> bool:
        """Skip to next track"""
        return self._accepted("Next")

    def previous(self) -> bool:
        """Go to previous track"""
        return self._accepted("Previous")

    def forward(self) -> bool:
        """Fast forward"""
        return self._accepted("Forward")

    def backward(self) -> bool:
        """Rewind"""
        return self._accepted("Backward")

    def seek(self, position: int) -> bool:
        """
        Seek to position

        Args:
            position: Position in seconds
        """
        return self._accepted("Seek", {"position": str(position)})

    def select_track(self, index: int) -> bool:
        """
        Select track by index in playlist

        Args:
            index: Track index (0-based)
        """
        response = self._execute_command("SelectTrack", {"index": str(index)})
        return response is not None and response.get("result") == "OK"

    # ========== Volume Control ==========

    def volume_up(self) -> bool:
        """Increase volume"""
        return self._accepted("VolumeUp")

    def volume_down(self) -> bool:
        """Decrease volume"""
        return self._accepted("VolumeDown")

    def volume_mute(self) -> bool:
        """Toggle mute — a toggle whose state neither State nor Status reports"""
        return self._accepted("VolumeMute")

    def volume_range(self) -> Optional[Dict[str, Any]]:
        """The volume HQPlayer allows: {min, max} in dB, `enabled` False when
        its volume is fixed on this output, `adaptive` its own levelling.
        None unless HQPlayer gave the range: a refusal or an answer without
        `min`/`max` says nothing a caller may lower the volume by. A missing
        `enabled` is a fixed volume, as Signalyst's own client reads it."""
        response = self._execute_command("VolumeRange")
        if (response is None or response.tag != "VolumeRange"
                or response.get("result") == "Error"):
            return None
        try:
            low, high = float(response.get("min")), float(response.get("max"))
        except (TypeError, ValueError):
            return None
        return {
            "min": low,
            "max": high,
            "enabled": response.get("enabled") == "1",
            "adaptive": response.get("adaptive") == "1",
        }

    def volume_refusal(self) -> str:
        """Why the latest volume command was not taken. A bare Error is what
        a fixed volume answers, so only then is VolumeRange asked, and a
        fixed one said so; HQPlayer's own words, or a connection that went,
        stand as they are — a range asked on a dead socket would only add a
        made-up failure to the ring."""
        why, e = self.refusal(), self.last_error
        if e is None or e.result != "Error" or e.message:
            return why
        span = self.volume_range()
        return VOLUME_FIXED if span is not None and not span["enabled"] else why

    def set_volume(self, value: float) -> bool:
        """
        Set volume level

        Args:
            value: Volume level (range depends on HQPlayer configuration)
        """
        return self._accepted("Volume", {"value": str(value)})

    def set_adaptive_volume(self, enabled: bool) -> bool:
        """Turn HQPlayer's adaptive volume on or off — State `adaptive`."""
        return self._accepted("SetAdaptiveVolume", {"value": "1" if enabled else "0"})

    # ========== Playlist Control ==========

    def playlist_add(self, uri: str, clear: bool = False, queued: bool = False) -> bool:
        """
        Add track to playlist

        Args:
            uri: File path or URI (e.g., "file:///E:/Music/...")
            clear: Clear playlist before adding
            queued: Add to queue instead of playlist
        """
        attributes = {
            "uri": uri,
            "clear": "1" if clear else "0",
            "queued": "1" if queued else "0",
        }
        response = self._execute_command("PlaylistAdd", attributes)
        return response is not None and response.get("result") == "OK"

    def playlist_clear(self) -> bool:
        """Clear playlist"""
        return self._accepted("PlaylistClear")

    def playlist_remove(self, index: int) -> bool:
        """Remove track from playlist by index"""
        return self._accepted("PlaylistRemove", {"index": str(index)})

    def get_playlist(self) -> List[Dict[str, Any]]:
        """
        Get current playlist from HQPlayer.

        Returns:
            List of dicts with track info (uri, metadata)
        """
        response = self._execute_command("PlaylistGet", {"picture": "0"})
        if response is None:
            logger.debug("PlaylistGet returned None")
            return []

        logger.debug(f"PlaylistGet response tag: {response.tag}, attrib: {response.attrib}")

        if response.tag != "PlaylistGet":
            logger.warning(f"Unexpected response tag: {response.tag}")
            return []

        tracks = []
        items = list(response.findall("PlaylistItem"))
        logger.debug(f"Found {len(items)} PlaylistItem elements")

        for item in items:
            track = {
                "uri": item.get("uri", ""),
                "artist": "",
                "album": "",
                "song": "",
                "genre": "",
            }
            # Parse metadata if present
            metadata = item.find("metadata")
            if metadata is not None:
                track["artist"] = metadata.get("artist", "")
                track["album"] = metadata.get("album", "")
                track["song"] = metadata.get("song", "")
                track["genre"] = metadata.get("genre", "")
            tracks.append(track)
            logger.debug(f"Track {len(tracks)}: {track['song']} by {track['artist']}")

        return tracks

    # ========== Status & Info ==========

    def get_status(self, *, wait: Optional[float] = None,
                   watched: Optional[bool] = None) -> Optional[TrackStatus]:
        """
        Get current playback status — `wait` and `watched` as
        `_execute_command` takes them: the ask held open after a Set* until
        HQPlayer answers again (the link's build watch, playback.hqp_link)

        Returns:
            TrackStatus object or None
        """
        response = self._execute_command("Status", {"subscribe": "0"},
                                         wait=wait, watched=watched)

        if response is None:
            return None
        if response.tag != "Status":
            self._outcome("Status", None, "unparsed", f"answered <{response.tag}>")
            return None

        try:
            # Parse status
            in_fill, out_fill = response.get("input_fill"), response.get("output_fill")
            speed = response.get("process_speed")
            clips = response.get("clips")
            status = TrackStatus(
                state=PlaybackState(int(response.get("state", 0))),
                track_index=int(response.get("track", 0)),
                track_id=response.get("track_id", ""),
                position=float(response.get("position", 0.0)),
                length=float(response.get("length", 0.0)),
                volume=float(response.get("volume", 0.0)),
                process_speed=float(speed) if speed is not None else None,
                input_fill=float(in_fill) if in_fill is not None else None,
                output_fill=float(out_fill) if out_fill is not None else None,
                limited=int(clips) if clips is not None else None,
                tracks_total=int(response.get("tracks_total", 0)),
                active_mode=response.get("active_mode", ""),
                active_filter=response.get("active_filter", ""),
                active_shaper=response.get("active_shaper", ""),
                active_rate=int(response.get("active_rate") or 0),
                apod=_optional(response, "apod", int),
                active_bits=_optional(response, "active_bits", int),
                active_channels=_optional(response, "active_channels", int),
                output_delay=_optional(response, "output_delay", lambda v: int(v) / 1e6),
                correction=_optional(response, "correction", _flag),
                filter_20k=_optional(response, "filter_20k", _flag),
                filter_junk=_optional(response, "filter_junk", int),
                display_position=_optional(response, "display_position", float),
            )

            # Parse metadata if present
            metadata = response.find("metadata")
            if metadata is not None:
                status.artist = metadata.get("artist", "")
                status.album = metadata.get("album", "")
                status.song = metadata.get("song", "")
                status.genre = metadata.get("genre", "")
                status.uri = metadata.get("uri", "")
                status.track_gain = float(metadata.get("gain") or 0.0)
                if metadata.get("samplerate"):
                    status.src_rate = int(metadata.get("samplerate"))
                    status.src_bits = int(metadata.get("bits") or 0) or None
                    status.src_channels = int(metadata.get("channels") or 0) or None
                    status.src_sdm = metadata.get("sdm") == "1"

            return status
        except Exception as e:
            logger.error(f"Failed to parse status: {e}")
            self._outcome("Status", None, "unparsed", str(e))
            return None

    def get_state(self) -> Optional[Dict[str, Any]]:
        """
        Get current HQPlayer DSP state (filter, shaper, mode, convolution, matrix profile).

        Returns:
            Dict with: mode, filter, shaper, rate, convolution, matrix_profile, etc.;
            filter_20k, filter_junk, random and repeat None from a build that
            does not report them.
        """
        response = self._execute_command("State")

        if response is None:
            return None
        if response.tag != "State":
            self._outcome("State", None, "unparsed", f"answered <{response.tag}>")
            return None

        try:
            return {
                "mode": int(response.get("mode", 0)),
                "filter": int(response.get("filter", 0)),
                "filter1x": int(response.get("filter1x", -1)),
                # The Nx slot SetFilter's `value` sets; `filter` read as the
                # 1x slot while a 44.1 kHz source played (Desktop 6.2.3)
                "filterNx": int(response.get("filterNx", response.get("filter", 0))),
                "shaper": int(response.get("shaper", 0)),
                "rate": int(response.get("rate", 0)),
                "active_mode": int(response.get("active_mode", 0)),
                "active_rate": int(response.get("active_rate", 0)),
                "convolution": bool(int(response.get("convolution", 0))),
                "matrix_profile": response.get("matrix_profile", ""),
                "invert": bool(int(response.get("invert", 0))),
                "adaptive": bool(int(response.get("adaptive", 0))),
                "volume": float(response.get("volume", 0.0)),
                "filter_20k": _optional(response, "filter_20k", _flag),
                "filter_junk": _optional(response, "filter_junk", int),
                "random": _optional(response, "random", _flag),
                "repeat": _optional(response, "repeat", int),
            }
        except Exception as e:
            logger.error(f"Failed to parse state: {e}")
            self._outcome("State", None, "unparsed", str(e))
            return None

    def get_info(self, *, wait: Optional[float] = None) -> Optional[Dict[str, str]]:
        """
        Get HQPlayer info, waiting `wait` seconds (the client's timeout by
        default)

        Returns:
            Dict with name, product, version, platform, engine
        """
        response = self._execute_command("GetInfo", wait=wait)

        if response is None or response.tag != "GetInfo":
            return None

        return {
            "name": response.get("name", ""),
            "product": response.get("product", ""),
            "version": response.get("version", ""),
            "platform": response.get("platform", ""),
            "engine": response.get("engine", ""),
        }

    def set_repeat(self, mode: RepeatMode) -> bool:
        """Set repeat mode"""
        return self._accepted("SetRepeat", {"value": str(int(mode))})

    def set_random(self, enabled: bool) -> bool:
        """Enable/disable random playback"""
        return self._accepted("SetRandom", {"value": "1" if enabled else "0"})

    # ========== DSP Settings ==========

    def _listing(self, command: str, item: str) -> Optional[List[ET.Element]]:
        """The items of a list HQPlayer gave — [] only for a list it answered
        empty. None when it refused, answered another element or never
        answered: a list HQPlayer did not give is not an empty one, and a
        picker drawn from it would say HQPlayer has nothing to choose."""
        response = self._execute_command(command)
        if response is None or self.last_error is not None:
            return None
        if response.tag != command:
            self._outcome(command, None, "unparsed", f"answered <{response.tag}>")
            return None
        return response.findall(item)

    def get_modes(self) -> Optional[List[Dict[str, Any]]]:
        """
        Get available output modes (PCM/DSD)

        Returns:
            List of dicts with: index, name, value; None when HQPlayer did
            not give the list (`_listing`)
            Example: [{"index": 0, "name": "[source]", "value": -1}, {"index": 1, "name": "PCM", "value": 0}, ...]
        """
        items = self._listing("GetModes", "ModesItem")
        if items is None:
            return None
        return [{
            "index": int(item.get("index", 0)),
            "name": item.get("name", ""),
            "value": int(item.get("value", 0)),
        } for item in items]

    def set_mode(self, index: int) -> bool:
        """
        Set output mode (PCM/DSD)

        Args:
            index: Mode index from get_modes()
        """
        return self._accepted("SetMode", {"value": str(index)})

    def get_filters(self) -> Optional[List[Dict[str, Any]]]:
        """
        Get available filters (PCM and SDM/DSD)

        Returns:
            List of dicts with: index, name, value, arg, description, parsed;
            None when HQPlayer did not give the list (`_listing`)
            `description` is HQPlayer 6's own line for the filter (its rating,
            focus and ratio); empty string on HQP5. `parsed` is that line
            read (`parse_filter_description`), None when there is none or
            its shape is not read here.
            Example, SDM mode on engine 6.2.3: [{"index": 6, "name": "poly-sinc-lp",
                       "value": 0, "arg": 2, "description": "4/5 space ⥣ Any",
                       "parsed": {"rating": 4, "focus": ["space"], "ratio": "Any",
                                  "up": False, "mode": "sdm"}}, ...]
            PCM mode lists the same filter as index 7, value 6, "4/5 space ⥮ Any":
            the index, the value and the arrow belong to the mode.
        """
        items = self._listing("GetFilters", "FiltersItem")
        if items is None:
            return None
        return [{
            "index": int(item.get("index", 0)),
            "name": item.get("name", ""),
            "value": int(item.get("value", 0)),
            "arg": int(item.get("arg", 0)),
            "description": item.get("description", ""),
            "parsed": parse_filter_description(item.get("description", "")),
        } for item in items]

    def set_filter(self, index: int, index_1x: Optional[int] = None) -> bool:
        """
        Set filter (PCM or SDM/DSD)

        Args:
            index: Filter index from get_filters()
            index_1x: Optional 1x filter index (for PCM)
        """
        attrs = {"value": str(index)}
        if index_1x is not None:
            attrs["value1x"] = str(index_1x)

        return self._accepted("SetFilter", attrs)

    def get_shapers(self) -> Optional[List[Dict[str, Any]]]:
        """
        Get available dither/noise shapers

        Returns:
            List of dicts with: index, name, value, description, parsed;
            None when HQPlayer did not give the list (`_listing`).
            HQPlayer 6.1+ names a modulator's generation in its description
            ("Gen7"; `parsed` {"generation": 7}); PCM dithers and earlier
            builds leave it empty (`parsed` None).
        """
        items = self._listing("GetShapers", "ShapersItem")
        if items is None:
            return None
        return [{
            "index": int(item.get("index", 0)),
            "name": item.get("name", ""),
            "value": int(item.get("value", 0)),
            "description": item.get("description", ""),
            "parsed": parse_shaper_description(item.get("description", "")),
        } for item in items]

    def set_shaping(self, index: int) -> bool:
        """
        Set dither/noise shaper

        Args:
            index: Shaper index from get_shapers()
        """
        return self._accepted("SetShaping", {"value": str(index)})

    def get_rates(self) -> Optional[List[Dict[str, Any]]]:
        """
        Get available output sample rates

        Returns:
            List of dicts with: index, rate (Hz); None when HQPlayer did not
            give the list (`_listing`)
            Example: [{"index": 0, "rate": 44100}, {"index": 1, "rate": 88200}, ...]
        """
        items = self._listing("GetRates", "RatesItem")
        if items is None:
            return None
        return [{
            "index": int(item.get("index", 0)),
            "rate": int(item.get("rate", 0)),
        } for item in items]

    def set_rate(self, index: int) -> bool:
        """
        Set output sample rate

        Args:
            index: Rate index from get_rates()
        """
        return self._accepted("SetRate", {"value": str(index)})

    def get_inputs(self) -> Optional[List[str]]:
        """
        Get available input devices

        Returns:
            List of input device names; None when HQPlayer did not give the
            list (`_listing`)
        """
        items = self._listing("GetInputs", "InputsItem")
        if items is None:
            return None
        return [item.get("name", "") for item in items]

    # ========== Convolution & Matrix ==========

    def set_convolution(self, enabled: bool) -> bool:
        """
        Enable or disable convolution engine.

        Args:
            enabled: True to enable, False to disable
        """
        return self._accepted("SetConvolution", {"value": "1" if enabled else "0"})

    def matrix_list_profiles(self) -> Optional[List[str]]:
        """
        Get list of saved matrix profiles.

        Returns:
            List of profile names; None when HQPlayer did not give the list
            (`_listing`)
        """
        items = self._listing("MatrixListProfiles", "MatrixProfile")
        if items is None:
            return None
        return [item.get("name") for item in items if item.get("name")]

    def matrix_get_profile(self) -> Optional[str]:
        """
        Get currently active matrix profile name.

        Returns:
            Profile name or None
        """
        response = self._execute_command("MatrixGetProfile")
        if response is None or response.tag != "MatrixGetProfile":
            return None
        return response.get("value", "")

    def matrix_set_profile(self, profile: str) -> bool:
        """
        Set active matrix profile by name.

        Args:
            profile: Profile name (must exist in HQPlayer)
        """
        return self._accepted("MatrixSetProfile", {"value": profile})

    def set_knob(self, knob: str, value: Any, filter1x: Optional[int] = None) -> bool:
        """One of DSP_KNOBS by its Set* command — `filter1x` goes with the
        filter."""
        if knob == "mode":
            return self.set_mode(value)
        if knob == "rate":
            return self.set_rate(value)
        if knob == "filter":
            return self.set_filter(value, filter1x)
        if knob == "shaper":
            return self.set_shaping(value)
        if knob == "matrix_profile":
            return self.matrix_set_profile(value)
        return self.set_convolution(bool(value))

    def set_knobs(self, knobs: Dict[str, Any]) -> Iterator[Tuple[str, Optional[Dict[str, Any]], str]]:
        """Each DSP knob `knobs` names, one Set* command each in DSP_KNOBS'
        order (`filter1x` rides with the filter): (knob, what HQPlayer took —
        the filter with its 1x slot —, "") or (knob, None, its refusal). A
        caller acts between them, or stops."""
        filter1x = knobs.get("filter1x")
        for knob in DSP_KNOBS:
            if knobs.get(knob) is None:
                continue
            if self.set_knob(knob, knobs[knob], filter1x):
                yield knob, ({knob: knobs[knob], "filter1x": filter1x}
                             if knob == "filter" and filter1x is not None
                             else {knob: knobs[knob]}), ""
            else:
                yield knob, None, self.refusal()

    def apply_settings(self, *, mode: Optional[int] = None, rate: Optional[int] = None,
                       filter: Optional[int] = None, filter1x: Optional[int] = None,
                       shaper: Optional[int] = None, matrix_profile: Optional[str] = None,
                       convolution: Optional[bool] = None) -> tuple:
        """Apply any subset of the DSP knobs (set_knobs). Returns (applied,
        failed) — a refused knob carries HQPlayer's reason and the rest still
        go."""
        applied: Dict[str, Any] = {}
        failed: Dict[str, str] = {}
        for knob, taken, refusal in self.set_knobs({
                "mode": mode, "rate": rate, "filter": filter, "filter1x": filter1x,
                "shaper": shaper, "matrix_profile": matrix_profile, "convolution": convolution}):
            if taken is None:
                failed[knob] = refusal
            else:
                applied.update(taken)
        return applied, failed


# ========== Context Manager Support ==========

class HQPlayerConnection:
    """Context manager for HQPlayer connection"""

    def __init__(self, host: str = "127.0.0.1", port: int = 4321):
        self.client = HQPlayerClient(host, port)

    def __enter__(self) -> HQPlayerClient:
        if not self.client.connect():
            raise ConnectionError(f"Failed to connect to HQPlayer at {self.client.host}:{self.client.port}")
        return self.client

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.client.disconnect()
        return False


# ========== Helper Functions ==========

def format_time(seconds: float) -> str:
    """Format seconds to MM:SS"""
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins:02d}:{secs:02d}"


def file_path_to_uri(file_path: str) -> str:
    """
    Convert Windows file path to file:// URI

    Args:
        file_path: Windows path (e.g., E:\\Music\\file.flac)

    Returns:
        File URI (e.g., file:///E:/Music/file.flac)
    """
    # Convert backslashes to forward slashes
    path = file_path.replace("\\", "/")

    # Ensure it starts with file:///
    if not path.startswith("file:///"):
        if path.startswith("/"):
            path = "file://" + path
        else:
            path = "file:///" + path

    return path


def uri_to_file_path(uri: str) -> str:
    """
    Convert a file:// URI back to the stored file path (inverse of
    file_path_to_uri).

    media_files.file_path is stored with forward slashes (e.g.
    E:/Music/.../track.flac), so we only strip the scheme and
    percent-decode — no slash conversion. Handles both Windows drive
    paths (file:///E:/... → E:/...) and POSIX (file:///mnt/... → /mnt/...).
    """
    if uri.startswith("file://"):
        path = uri[len("file://"):]
        # file:///E:/... carries a leading slash before the drive letter;
        # file:///mnt/... keeps its leading slash as a real POSIX root.
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
    else:
        path = uri
    return unquote(path)
