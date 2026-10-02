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
from typing import Callable, Optional, Dict, Any, List
from dataclasses import asdict, dataclass
from enum import IntEnum
from urllib.parse import unquote

logger = logging.getLogger(__name__)


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
    """HQPlayer playback states"""
    STOPPED = 0
    PAUSED = 1
    PLAYING = 2
    STOPREQ = 3


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
    process_speed: float = 0.0  # HQP6 realtime-processing factor; 0.0 on HQP5
    # The engine's input and output buffer fill (the SDK's statusIO); None
    # when HQPlayer does not report them.
    input_fill: Optional[float] = None
    output_fill: Optional[float] = None
    tracks_total: int = 0
    # What the engine is running right now, by name — the DSP line of a
    # playback trace without three more round-trips.
    active_mode: str = ""
    active_filter: str = ""
    active_shaper: str = ""
    active_rate: int = 0

    @property
    def is_playing(self) -> bool:
        return self.state == PlaybackState.PLAYING

    @property
    def progress_percent(self) -> float:
        if self.length > 0:
            return (self.position / self.length) * 100
        return 0.0


class HQPlayerClient:
    """
    HQPlayer Desktop 5/6 Control API Client

    Implements basic control functions without authentication.
    For full feature set including encrypted commands, authentication would be needed.

    Every failed command lands in one ring shared by all instances
    (`last_errors()`): the playback backend replaces its client on every
    reconnect, and the drop that killed one instance must outlive it.

    A DSP setter is refused only by an explicit `result="Error"`: some
    answer with a bare element, which is HQPlayer accepting it.
    """

    _error_ring: deque = deque(maxlen=50)
    _error_lock = threading.Lock()

    def __init__(self, host: str = "localhost", port: int = 4321, timeout: float = 5.0):
        """
        Initialize HQPlayer client

        Args:
            host: HQPlayer host (use host.docker.internal for Docker, or Windows IP)
            port: Control port (default 4321)
            timeout: Socket timeout in seconds
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.socket: Optional[socket.socket] = None
        self.buffer = b""
        # The failure of this instance's latest command (None when HQPlayer
        # accepted it) — what a caller quotes when a setter returns False.
        self.last_error: Optional[CommandOutcome] = None
        # Called with every outcome, failures and successes alike — the
        # playback backend's trace listens on its command client.
        self.on_outcome: Optional[Callable[[CommandOutcome], None]] = None
        self._io_error: Optional[str] = None

    @classmethod
    def last_errors(cls, since: Optional[float] = None) -> List[Dict[str, Any]]:
        """The ring of failed commands, newest first, attributes redacted."""
        with cls._error_lock:
            entries = list(cls._error_ring)
        return [e.public() for e in reversed(entries) if since is None or e.ts >= since]

    def refusal(self) -> str:
        """Why the latest command was not accepted, in HQPlayer's words when
        it gave any — what a caller reports when a command returns False."""
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
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Connect must be quick on LAN/localhost. A stalled HQPlayer that
            # accepts SYN slowly would otherwise burn the full read timeout on
            # every reconnect attempt. Cap connect at 2s, then restore the
            # full timeout for reads (commands can legitimately take seconds).
            self.socket.settimeout(min(self.timeout, 2.0))
            self.socket.connect((self.host, self.port))
            self.socket.settimeout(self.timeout)
            # Tight TCP keepalive: the WSL2→Windows NAT silently drops idle
            # TCP mappings (no RST) — an idle command socket then surfaces
            # only as the NEXT command's full read-timeout + reconnect cycle.
            # Keepalive probes hold the mapping open and detect a dead peer
            # in ~35 s instead. Timer constants are per-platform (Linux:
            # KEEPIDLE/KEEPINTVL/KEEPCNT; macOS: TCP_KEEPALIVE; availability
            # probed with hasattr — plain SO_KEEPALIVE everywhere else).
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt, val in (("TCP_KEEPIDLE", 20), ("TCP_KEEPINTVL", 5),
                             ("TCP_KEEPCNT", 3), ("TCP_KEEPALIVE", 20)):
                if hasattr(socket, opt):
                    try:
                        self.socket.setsockopt(socket.IPPROTO_TCP,
                                               getattr(socket, opt), val)
                    except OSError:
                        pass  # platform exposes the constant but rejects it
            logger.info(f"Connected to HQPlayer at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to HQPlayer: {e}")
            self.socket = None
            self._outcome("<connection>", None, "refused",
                          f"connect to {self.host}:{self.port}: {e}")
            return False

    def disconnect(self):
        """Disconnect from HQPlayer"""
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None
            self.buffer = b""
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
            self.socket.sendall(xml_command.encode('utf-8'))
            return True
        except Exception as e:
            logger.error(f"Failed to send command: {e}")
            self._io_error = f"send failed: {e}"
            self.disconnect()
            return False

    def _read_response(self) -> Optional[ET.Element]:
        """
        Read XML response from HQPlayer

        Returns:
            Parsed XML element or None
        """
        if not self.socket:
            self._io_error = "not connected"
            return None

        try:
            # Read until we get a complete XML document (ends with newline)
            while b'\n' not in self.buffer:
                chunk = self.socket.recv(4096)
                if not chunk:
                    # EOF: HQPlayer closed its side. Close ours immediately so
                    # the socket doesn't linger in CLOSE_WAIT until the next
                    # _ensure_connected peek happens to notice it (which, with
                    # backoff, can be up to 30s away — and on the command
                    # socket, until the user's next action).
                    self._io_error = "closed by HQPlayer"
                    self.disconnect()
                    return None
                self.buffer += chunk

            # Extract first complete line
            if b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
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
        except Exception as e:
            logger.error(f"Failed to read response: {e}")
            self._io_error = f"no answer: {e}"
            self.disconnect()
            return None

    def _execute_command(self, command: str, attributes: Optional[Dict[str, str]] = None,
                        expect_response: bool = True) -> Optional[ET.Element]:
        """
        Execute command and optionally wait for response

        Args:
            command: Command name (e.g., "Play", "Stop")
            attributes: Command attributes dict
            expect_response: Whether to wait for response

        Returns:
            Response element or None

        Every command leaves a CommandOutcome (`_outcome`): HQPlayer's own
        `result`, and on "Error" its reason — the element's text, which is
        the only place HQPlayer says why (the Signalyst SDK's client reads it
        at the start tag, where Qt's stream reader has no text yet).
        """
        self.last_error = None
        self._io_error = None
        # Build XML command
        root = ET.Element(command)
        if attributes:
            for key, value in attributes.items():
                root.set(key, str(value))

        xml_str = ET.tostring(root, encoding='unicode')

        # Send command
        if not self._send_command(xml_str):
            self._outcome("<connection>", attributes, "lost",
                          f"{command}: {self._io_error}", during=command)
            return None

        # Read response if expected
        if expect_response:
            response = self._read_response()
            if response is None:
                self._outcome("<connection>", attributes, "lost",
                              f"{command}: {self._io_error}", during=command)
                return None
            result = response.get("result")
            self._outcome(command, attributes, result,
                          (response.text or "").strip() if result == "Error" else "")
            return response

        return None

    # ========== Playback Control ==========

    def play(self) -> bool:
        """Start playback"""
        response = self._execute_command("Play")
        return response is not None and response.get("result") == "OK"

    def pause(self) -> bool:
        """Pause playback"""
        response = self._execute_command("Pause")
        return response is not None

    def stop(self) -> bool:
        """Stop playback"""
        response = self._execute_command("Stop")
        return response is not None

    def next(self) -> bool:
        """Skip to next track"""
        response = self._execute_command("Next")
        return response is not None

    def previous(self) -> bool:
        """Go to previous track"""
        response = self._execute_command("Previous")
        return response is not None

    def forward(self) -> bool:
        """Fast forward"""
        response = self._execute_command("Forward")
        return response is not None

    def backward(self) -> bool:
        """Rewind"""
        response = self._execute_command("Backward")
        return response is not None

    def seek(self, position: int) -> bool:
        """
        Seek to position

        Args:
            position: Position in seconds
        """
        response = self._execute_command("Seek", {"position": str(position)})
        return response is not None

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
        response = self._execute_command("VolumeUp")
        return response is not None

    def volume_down(self) -> bool:
        """Decrease volume"""
        response = self._execute_command("VolumeDown")
        return response is not None

    def volume_mute(self) -> bool:
        """Toggle mute"""
        response = self._execute_command("VolumeMute")
        return response is not None

    def set_volume(self, value: float) -> bool:
        """
        Set volume level

        Args:
            value: Volume level (range depends on HQPlayer configuration)
        """
        response = self._execute_command("Volume", {"value": str(value)})
        return response is not None

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
        response = self._execute_command("PlaylistClear")
        return response is not None

    def playlist_remove(self, index: int) -> bool:
        """Remove track from playlist by index"""
        response = self._execute_command("PlaylistRemove", {"index": str(index)})
        return response is not None

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

    def get_status(self) -> Optional[TrackStatus]:
        """
        Get current playback status

        Returns:
            TrackStatus object or None
        """
        response = self._execute_command("Status", {"subscribe": "0"})

        if response is None:
            return None
        if response.tag != "Status":
            self._outcome("Status", None, "unparsed", f"answered <{response.tag}>")
            return None

        try:
            # Parse status
            in_fill, out_fill = response.get("input_fill"), response.get("output_fill")
            status = TrackStatus(
                state=PlaybackState(int(response.get("state", 0))),
                track_index=int(response.get("track", 0)),
                track_id=response.get("track_id", ""),
                position=float(response.get("position", 0.0)),
                length=float(response.get("length", 0.0)),
                volume=float(response.get("volume", 0.0)),
                process_speed=float(response.get("process_speed", 0.0)),
                input_fill=float(in_fill) if in_fill is not None else None,
                output_fill=float(out_fill) if out_fill is not None else None,
                tracks_total=int(response.get("tracks_total", 0)),
                active_mode=response.get("active_mode", ""),
                active_filter=response.get("active_filter", ""),
                active_shaper=response.get("active_shaper", ""),
                active_rate=int(response.get("active_rate") or 0),
            )

            # Parse metadata if present
            metadata = response.find("metadata")
            if metadata is not None:
                status.artist = metadata.get("artist", "")
                status.album = metadata.get("album", "")
                status.song = metadata.get("song", "")
                status.genre = metadata.get("genre", "")

            return status
        except Exception as e:
            logger.error(f"Failed to parse status: {e}")
            self._outcome("Status", None, "unparsed", str(e))
            return None

    def get_state(self) -> Optional[Dict[str, Any]]:
        """
        Get current HQPlayer DSP state (filter, shaper, mode, convolution, matrix profile).

        Returns:
            Dict with: mode, filter, shaper, rate, convolution, matrix_profile, etc.
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
                "shaper": int(response.get("shaper", 0)),
                "rate": int(response.get("rate", 0)),
                "active_mode": int(response.get("active_mode", 0)),
                "active_rate": int(response.get("active_rate", 0)),
                "convolution": bool(int(response.get("convolution", 0))),
                "matrix_profile": response.get("matrix_profile", ""),
                "invert": bool(int(response.get("invert", 0))),
                "volume": float(response.get("volume", 0.0)),
            }
        except Exception as e:
            logger.error(f"Failed to parse state: {e}")
            self._outcome("State", None, "unparsed", str(e))
            return None

    def get_info(self) -> Optional[Dict[str, str]]:
        """
        Get HQPlayer info

        Returns:
            Dict with name, product, version, platform, engine
        """
        response = self._execute_command("GetInfo")

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
        response = self._execute_command("SetRepeat", {"value": str(int(mode))})
        return response is not None

    def set_random(self, enabled: bool) -> bool:
        """Enable/disable random playback"""
        response = self._execute_command("SetRandom", {"value": "1" if enabled else "0"})
        return response is not None

    # ========== DSP Settings ==========

    def get_modes(self) -> List[Dict[str, Any]]:
        """
        Get available output modes (PCM/DSD)

        Returns:
            List of dicts with: index, name, value
            Example: [{"index": 0, "name": "[source]", "value": -1}, {"index": 1, "name": "PCM", "value": 0}, ...]
        """
        response = self._execute_command("GetModes")
        if response is None or response.tag != "GetModes":
            return []

        modes = []
        # Parse ModesItem children
        for item in response.findall("ModesItem"):
            modes.append({
                "index": int(item.get("index", 0)),
                "name": item.get("name", ""),
                "value": int(item.get("value", 0)),
            })

        return modes

    def set_mode(self, index: int) -> bool:
        """
        Set output mode (PCM/DSD)

        Args:
            index: Mode index from get_modes()
        """
        response = self._execute_command("SetMode", {"value": str(index)})
        return response is not None and response.get("result") != "Error"

    def get_filters(self) -> List[Dict[str, Any]]:
        """
        Get available filters (PCM and SDM/DSD)

        Returns:
            List of dicts with: index, name, value, arg, description
            `description` is the HQP6 per-filter blurb; empty string on HQP5.
            Example: [{"index": 0, "name": "poly-sinc-ext2", "value": 0, "arg": 1,
                       "description": "Closed form interpolation with 16 million taps"}, ...]
        """
        response = self._execute_command("GetFilters")
        if response is None or response.tag != "GetFilters":
            return []

        filters = []
        # Parse FiltersItem children
        for item in response.findall("FiltersItem"):
            filters.append({
                "index": int(item.get("index", 0)),
                "name": item.get("name", ""),
                "value": int(item.get("value", 0)),
                "arg": int(item.get("arg", 0)),
                "description": item.get("description", ""),
            })

        return filters

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

        response = self._execute_command("SetFilter", attrs)
        return response is not None and response.get("result") != "Error"

    def get_shapers(self) -> List[Dict[str, Any]]:
        """
        Get available dither/noise shapers

        Returns:
            List of dicts with: index, name, value
        """
        response = self._execute_command("GetShapers")
        if response is None or response.tag != "GetShapers":
            return []

        shapers = []
        # Parse ShapersItem children
        for item in response.findall("ShapersItem"):
            shapers.append({
                "index": int(item.get("index", 0)),
                "name": item.get("name", ""),
                "value": int(item.get("value", 0)),
            })

        return shapers

    def set_shaping(self, index: int) -> bool:
        """
        Set dither/noise shaper

        Args:
            index: Shaper index from get_shapers()
        """
        response = self._execute_command("SetShaping", {"value": str(index)})
        return response is not None and response.get("result") != "Error"

    def get_rates(self) -> List[Dict[str, Any]]:
        """
        Get available output sample rates

        Returns:
            List of dicts with: index, rate (Hz)
            Example: [{"index": 0, "rate": 44100}, {"index": 1, "rate": 88200}, ...]
        """
        response = self._execute_command("GetRates")
        if response is None or response.tag != "GetRates":
            return []

        rates = []
        # Parse RatesItem children
        for item in response.findall("RatesItem"):
            rates.append({
                "index": int(item.get("index", 0)),
                "rate": int(item.get("rate", 0)),
            })

        return rates

    def set_rate(self, index: int) -> bool:
        """
        Set output sample rate

        Args:
            index: Rate index from get_rates()
        """
        response = self._execute_command("SetRate", {"value": str(index)})
        return response is not None and response.get("result") != "Error"

    def get_inputs(self) -> List[str]:
        """
        Get available input devices

        Returns:
            List of input device names
        """
        response = self._execute_command("GetInputs")
        if response is None or response.tag != "GetInputs":
            return []

        inputs = []
        # Parse InputsItem children
        for item in response.findall("InputsItem"):
            inputs.append(item.get("name", ""))

        return inputs

    # ========== Convolution & Matrix ==========

    def set_convolution(self, enabled: bool) -> bool:
        """
        Enable or disable convolution engine.

        Args:
            enabled: True to enable, False to disable
        """
        response = self._execute_command("SetConvolution", {"value": "1" if enabled else "0"})
        return response is not None and response.get("result") != "Error"

    def matrix_list_profiles(self) -> List[str]:
        """
        Get list of saved matrix profiles.

        Returns:
            List of profile names
        """
        response = self._execute_command("MatrixListProfiles")
        if response is None or response.tag != "MatrixListProfiles":
            return []

        profiles = []
        for item in response.findall("MatrixProfile"):
            name = item.get("name", "")
            if name:
                profiles.append(name)
        return profiles

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
        response = self._execute_command("MatrixSetProfile", {"value": profile})
        return response is not None and response.get("result") != "Error"


# ========== Context Manager Support ==========

class HQPlayerConnection:
    """Context manager for HQPlayer connection"""

    def __init__(self, host: str = "localhost", port: int = 4321):
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
