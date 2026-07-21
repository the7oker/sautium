"""
Local audio device enumeration.

sounddevice is an optional dependency at runtime: inside the Docker image
(Linux, no PortAudio shared library / no audio stack) the import fails and
`list_devices()` returns [] — the "local" output type then simply doesn't
appear in /api/player/outputs. No special-casing anywhere else.

ASIO note: the Windows wheels ship two PortAudio DLLs; the ASIO-enabled
one loads only when SD_ENABLE_ASIO is set BEFORE the first import — the
launcher writes it into the backend .env (config_manager.generate_env_file).
"""

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import sounddevice as sd
except Exception as e:  # missing wheel or missing PortAudio/audio stack
    sd = None
    logger.info("sounddevice unavailable — local output disabled (%s)", e)

# Host APIs we surface. MME / DirectSound / WDM-KS expose the same physical
# endpoints WASAPI already covers, only through older, higher-latency APIs —
# listing them would triple every device with worse duplicates.
_HOSTAPIS = ("Windows WASAPI", "ASIO", "Core Audio")

# PortAudio is not thread-safe across Pa_Terminate/Pa_Initialize: a rescan
# racing a concurrent enumeration or stream open/close from another uvicorn
# or player-loop thread dies with an access violation inside the DLL (the
# ASIO build especially — observed as a silent backend APPCRASH). EVERY
# PortAudio call in this process goes through pa_lock; engine.py takes it
# around stream open/start/stop/abort/close too.
pa_lock = threading.RLock()

# Open-stream census: reinit with a live stream invalidates it natively (no
# Python exception, just a dangling handle) — rescan() refuses while any
# stream exists, whatever backend owns it.
_open_streams = 0


def stream_opened() -> None:
    global _open_streams
    with pa_lock:
        _open_streams += 1


def stream_closed() -> None:
    global _open_streams
    with pa_lock:
        _open_streams -= 1


def available() -> bool:
    return sd is not None


def list_devices() -> list[dict]:
    """Output devices across the supported host APIs. `device_id` is
    "{hostapi}::{name}" — PortAudio indices shift between restarts and
    hotplugs, names within a host API don't."""
    if sd is None:
        return []
    devices = []
    try:
        with pa_lock:
            hostapis = sd.query_hostapis()
            for dev_index, dev in enumerate(sd.query_devices()):
                if dev["max_output_channels"] < 1:
                    continue
                api = hostapis[dev["hostapi"]]
                if api["name"] not in _HOSTAPIS:
                    continue
                devices.append({
                    "device_id": f"{api['name']}::{dev['name']}",
                    "name": dev["name"],
                    "hostapi": api["name"],
                    "channels": dev["max_output_channels"],
                    "default_samplerate": dev["default_samplerate"],
                    "is_default": dev_index == api["default_output_device"],
                })
    except Exception as e:
        logger.error("device enumeration failed: %s", e)
        return []
    return devices


def rescan() -> None:
    """Re-enumerate hardware. PortAudio snapshots the device list at library
    init — a DAC plugged in AFTER the backend started is invisible until a
    terminate+initialize cycle (hotplug is not tracked). Refuses while any
    stream is open: reinit would invalidate it natively."""
    if sd is None:
        return
    with pa_lock:
        if _open_streams > 0:
            logger.warning("device rescan skipped: %d stream(s) open",
                           _open_streams)
            return
        try:
            sd._terminate()
            sd._initialize()
            logger.info("PortAudio reinitialized (device rescan)")
        except Exception as e:
            logger.error("PortAudio reinit failed: %s", e)


def resolve_device(device_id: Optional[str]) -> int:
    """PortAudio device index for a stored device_id, resolved at stream-open
    time. None picks the default WASAPI/CoreAudio output. Raises LookupError
    when the device is gone (unplugged) — surfaces as a loud playback error,
    not a silent fallback."""
    if sd is None:
        raise LookupError("sounddevice unavailable")
    with pa_lock:
        if device_id is None:
            for d in list_devices():
                if d["is_default"] and d["hostapi"] != "ASIO":
                    idx = _index_of(d["hostapi"], d["name"])
                    if idx is not None:
                        return idx
            raise LookupError("no default output device")
        hostapi, _, name = device_id.partition("::")
        idx = _index_of(hostapi, name)
        if idx is None:
            raise LookupError(f"output device not found: {device_id}")
        return idx


def _index_of(hostapi: str, name: str) -> Optional[int]:
    hostapis = sd.query_hostapis()
    for dev_index, dev in enumerate(sd.query_devices()):
        if (dev["max_output_channels"] >= 1
                and hostapis[dev["hostapi"]]["name"] == hostapi
                and dev["name"] == name):
            return dev_index
    return None
