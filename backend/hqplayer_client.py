#!/usr/bin/env python3
"""
HQPlayer Desktop 5 Control API Client
Based on HQPlayer SDK (engine version 5.29.2)

Protocol: XML over TCP
Default port: 4321
"""

import logging
import socket
import xml.etree.ElementTree as ET
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from enum import IntEnum

logger = logging.getLogger(__name__)


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
    HQPlayer Desktop 5 Control API Client

    Implements basic control functions without authentication.
    For full feature set including encrypted commands, authentication would be needed.
    """

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

    def connect(self) -> bool:
        """
        Connect to HQPlayer

        Returns:
            True if connected successfully
        """
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(self.timeout)
            self.socket.connect((self.host, self.port))
            logger.info(f"Connected to HQPlayer at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to HQPlayer: {e}")
            self.socket = None
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
            return False

        try:
            self.socket.sendall(xml_command.encode('utf-8'))
            return True
        except Exception as e:
            logger.error(f"Failed to send command: {e}")
            return False

    def _read_response(self) -> Optional[ET.Element]:
        """
        Read XML response from HQPlayer

        Returns:
            Parsed XML element or None
        """
        if not self.socket:
            return None

        try:
            # Read until we get a complete XML document (ends with newline)
            while b'\n' not in self.buffer:
                chunk = self.socket.recv(4096)
                if not chunk:
                    break
                self.buffer += chunk

            # Extract first complete line
            if b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                xml_str = line.decode('utf-8').strip()

                if xml_str:
                    return ET.fromstring(xml_str)

            return None
        except Exception as e:
            logger.error(f"Failed to read response: {e}")
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
        """
        # Build XML command
        root = ET.Element(command)
        if attributes:
            for key, value in attributes.items():
                root.set(key, str(value))

        xml_str = ET.tostring(root, encoding='unicode')

        # Send command
        if not self._send_command(xml_str):
            return None

        # Read response if expected
        if expect_response:
            return self._read_response()

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

        if response is None or response.tag != "Status":
            return None

        try:
            # Parse status
            status = TrackStatus(
                state=PlaybackState(int(response.get("state", 0))),
                track_index=int(response.get("track", 0)),
                track_id=response.get("track_id", ""),
                position=float(response.get("position", 0.0)),
                length=float(response.get("length", 0.0)),
                volume=float(response.get("volume", 0.0)),
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
        return response is not None

    def get_filters(self) -> List[Dict[str, Any]]:
        """
        Get available filters (PCM and SDM/DSD)

        Returns:
            List of dicts with: index, name, value, arg
            Example: [{"index": 0, "name": "poly-sinc-ext2", "value": 0, "arg": 1}, ...]
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
        return response is not None

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
        return response is not None

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
        return response is not None

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
