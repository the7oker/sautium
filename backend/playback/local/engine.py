"""
The local playback engine: ffmpeg decode → ring buffer → sounddevice.

Threading model (strict ownership):

  * player-loop (one daemon thread) — the ONLY owner of playback state:
    ffmpeg lifecycles, track sequencing, stream open/close, position
    emits (1 Hz while playing, from our own frame counters — no external
    process is polled). Commands arrive on a queue.Queue.
  * decode-pump (one daemon thread per live ffmpeg) — reads stdout in
    64 KiB chunks into the ring buffer, blocks when the ring is full.
  * PortAudio callback (real-time, not ours) — consumes the ring through
    a pointer-arithmetic-only mutex, applies volume (100 = untouched
    samples, bit-perfect), counts frames; an underrun outputs silence and
    increments a counter instead of ever blocking.
  * uvicorn request threads — post Command objects; never touch state.

Gapless: when the current ffmpeg hits EOF the loop immediately probes and
spawns the next queue item; same rate/channels feed the same ring (truly
seamless). A rate/channel change parks a pending switch, lets the ring
drain while commands stay responsive, then reopens the stream at the new
rate (small audible gap at the boundary — this engine never resamples).

No DSD. No resampling. PCM in the file is PCM on the wire.
"""

import logging
import queue as queue_lib
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from config import settings

from playback.base import PlaybackStatus
from playback.queue import CanonicalQueue, QueueItem

logger = logging.getLogger(__name__)

try:
    import sounddevice as sd
except Exception:
    sd = None

_RING_SECONDS = 8.0        # ring capacity
_PREFILL_SECONDS = 1.0     # buffered audio required before the stream starts
_PREFILL_TIMEOUT = 10.0
_PUMP_CHUNK = 65536        # bytes per ffmpeg stdout read
_MAX_CONSECUTIVE_TRACK_FAILURES = 5   # then stop instead of spinning


def _probe(src: str) -> Optional[tuple[int, int, Optional[float]]]:
    """(sample_rate, channels, duration|None) of the first audio stream, via
    ffprobe. None when the source can't be probed (missing file, dead URL)."""
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate,channels:format=duration",
             "-of", "csv=p=0:nk=1", src],
            capture_output=True, text=True, timeout=10)
        lines = [l for l in out.stdout.strip().splitlines() if l]
        rate_str, ch_str = lines[0].split(",")[:2]
        duration = None
        if len(lines) > 1:
            try:
                duration = float(lines[1])
            except ValueError:
                duration = None
        return int(rate_str), int(ch_str), duration
    except Exception as e:
        logger.error("probe failed for %s: %s", src, e)
        return None


class _Ring:
    """Interleaved int32 ring buffer. One producer (decode-pump), one
    consumer (PortAudio callback). The mutex only guards pointer math —
    microseconds — so the RT callback never waits on a slow holder."""

    def __init__(self, capacity_frames: int, channels: int):
        self._buf = np.zeros((capacity_frames, channels), dtype=np.int32)
        self._capacity = capacity_frames
        self._lock = threading.Lock()
        self._read = 0          # absolute frame counters (monotonic)
        self._write = 0
        self.closed = False     # producer done — drain, then CallbackStop

    def available_read(self) -> int:
        with self._lock:
            return self._write - self._read

    def write(self, frames: np.ndarray) -> int:
        """Write up to len(frames); returns frames accepted (pump loops)."""
        with self._lock:
            free = self._capacity - (self._write - self._read)
            n = min(free, len(frames))
            if n == 0:
                return 0
            pos = self._write % self._capacity
            first = min(n, self._capacity - pos)
            self._buf[pos:pos + first] = frames[:first]
            if n > first:
                self._buf[:n - first] = frames[first:n]
            self._write += n
            return n

    def read_into(self, out: np.ndarray) -> int:
        """Fill `out` from the ring; returns frames delivered (the caller
        zero-fills the rest). Callback-side."""
        with self._lock:
            avail = self._write - self._read
            n = min(avail, len(out))
            if n:
                pos = self._read % self._capacity
                first = min(n, self._capacity - pos)
                out[:first] = self._buf[pos:pos + first]
                if n > first:
                    out[first:n] = self._buf[:n - first]
                self._read += n
            return n

    @property
    def frames_read(self) -> int:
        with self._lock:
            return self._read


@dataclass
class Command:
    kind: str                        # play|pause|stop|select|seek|volume|resync|quit
    index: Optional[int] = None      # select: 1-based queue slot
    seconds: Optional[float] = None  # seek target
    level: Optional[float] = None    # volume 0..100


class Engine:
    """See module docstring. Public methods are thread-safe command posts;
    everything else runs on the player-loop thread."""

    def __init__(self, queue: CanonicalQueue, emit: Callable[[PlaybackStatus], None],
                 device_id: Optional[str], exclusive: bool):
        self._queue = queue
        self._emit = emit
        self._device_id = device_id
        self._exclusive = exclusive
        self._commands: "queue_lib.Queue[Command]" = queue_lib.Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = False

        # player-loop state
        self._state = "stopped"          # stopped|paused|playing
        self._current: Optional[QueueItem] = None
        self._index = 0                  # 1-based; 0 = nothing selected
        self._length: float = 0.0
        self._position_offset = 0.0      # seek base of the active track
        self._volume: float = 100.0
        self._error: Optional[str] = None

        # audio pipeline (player-loop owned)
        self._stream = None
        self._stream_rate = 0
        self._stream_channels = 0
        self._ring: Optional[_Ring] = None
        self._ffmpeg: Optional[subprocess.Popen] = None
        self._pump: Optional[threading.Thread] = None
        self._pump_stop = threading.Event()
        self._written_lock = threading.Lock()
        self._written_frames = 0         # producer-side total on this stream
        self._pending_switch: Optional[int] = None   # queue index awaiting a
        #                                              rate-change stream reopen
        # Frame accounting on the CURRENT stream: (queue_index, start_frame,
        # length_s, item). The callback's read counter walks these to derive
        # the playing slot and in-track position.
        self._boundaries: list[tuple[int, int, float, QueueItem]] = []
        self._underruns = 0
        self._track_failures = 0

    # -- public (any thread) -------------------------------------------------

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="local-player-loop")
        self._thread.start()

    def shutdown(self) -> None:
        self._running = False
        self._commands.put(Command("quit"))
        if self._thread:
            self._thread.join(timeout=5)

    def post(self, kind: str, **kw) -> None:
        self._commands.put(Command(kind, **kw))

    @property
    def state(self) -> str:
        return self._state

    @property
    def volume(self) -> float:
        return self._volume

    # -- player loop -----------------------------------------------------------

    def _loop(self) -> None:
        if sys.platform == "win32":
            # WASAPI and ASIO are COM-based and PortAudio opens the device on
            # the CALLING thread — without COM initialized here, WASAPI fails
            # with CO_E_NOTINITIALIZED. Apartment-threaded (0x2) specifically:
            # ASIO drivers are legacy STA COM objects and refuse to load under
            # MTA ("Failed to load ASIO driver"); WASAPI works in either.
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)
        last_emit = 0.0
        self._emit_status()   # announce the idle backend as soon as it attaches
        while self._running:
            try:
                cmd = self._commands.get(timeout=0.25)
            except queue_lib.Empty:
                cmd = None
            try:
                if cmd:
                    self._handle(cmd)
                self._advance()
                now = time.monotonic()
                if self._state == "playing" and now - last_emit >= 0.95:
                    self._emit_status()
                    last_emit = now
            except Exception:
                logger.exception("player-loop error")
                self._teardown_pipeline()
                self._set_state("stopped", error="internal player error")
        self._teardown_pipeline()

    def _handle(self, cmd: Command) -> None:
        if cmd.kind == "quit":
            self._running = False
        elif cmd.kind == "play":
            if self._state == "paused" and self._stream is not None:
                self._stream.start()
                self._set_state("playing")
            elif self._state != "playing":
                self._start_at(self._index if self._index >= 1 else 1)
        elif cmd.kind == "pause":
            if self._state == "playing" and self._stream is not None:
                self._stream.stop()
                self._set_state("paused")
        elif cmd.kind == "stop":
            self._teardown_pipeline()
            self._set_state("stopped")
        elif cmd.kind == "select":
            self._start_at(cmd.index)
        elif cmd.kind == "seek":
            self._seek(cmd.seconds or 0.0)
        elif cmd.kind == "volume":
            self._volume = max(0.0, min(100.0, cmd.level))
            self._emit_status()
        elif cmd.kind == "resync":
            self._resync_index()

    # -- pipeline ------------------------------------------------------------------

    def _start_at(self, index: int, seek_to: float = 0.0) -> None:
        """(Re)start playback from queue slot `index` (1-based). Skips
        unplayable tracks forward, stopping loudly after the failure cap."""
        self._teardown_pipeline()
        self._track_failures = 0
        while True:
            item = self._queue.item_at(index)
            if item is None:
                self._set_state("stopped")
                return
            src = self._source_url(item)
            probed = _probe(src) if src else None
            if probed is not None:
                break
            logger.error("track unplayable, skipping: %s — %s", item.artist, item.title)
            self._track_failures += 1
            seek_to = 0.0
            if self._track_failures >= _MAX_CONSECUTIVE_TRACK_FAILURES:
                self._set_state("stopped", error="too many unplayable tracks")
                return
            index += 1
        rate, channels, duration = probed

        if not self._open_stream(rate, channels):
            return
        self._current = item
        self._index = index
        self._length = item.duration_seconds or duration or 0.0
        self._position_offset = seek_to
        self._boundaries = [(index, 0, self._length, item)]
        self._spawn_ffmpeg(src, rate, channels, seek_to)
        if not self._prefill_and_start():
            return
        self._track_failures = 0
        self._set_state("playing")

    def _prefill_and_start(self) -> bool:
        """Block (player-loop only) until ~1 s of audio is buffered — or the
        decoder already finished / timed out — then start the callback so it
        never starves at t=0. WASAPI surfaces format rejections at START, not
        open — fail loudly with the device error in the status."""
        target = int(_PREFILL_SECONDS * self._stream_rate)
        deadline = time.monotonic() + _PREFILL_TIMEOUT
        while (self._ring.available_read() < target
               and self._ffmpeg is not None and self._ffmpeg.poll() is None
               and time.monotonic() < deadline):
            time.sleep(0.02)
        try:
            self._stream.start()
        except Exception as e:
            logger.error("stream start failed (rate=%d exclusive=%s): %s",
                         self._stream_rate, self._exclusive, e)
            self._teardown_pipeline()
            self._set_state("stopped", error=f"device start failed: {e}")
            return False
        return True

    def _source_url(self, item: QueueItem) -> Optional[str]:
        src = item.source
        if src["kind"] == "file":
            return settings.translate_to_local_path(src["path"])
        if src["kind"] == "proxy":
            from streaming import service as streaming_service
            proxy = streaming_service.get_proxy()
            return proxy.url_for(src["token"]) if proxy else None
        return src.get("uri")

    def _spawn_ffmpeg(self, src: str, rate: int, channels: int, seek_to: float) -> None:
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        cmd = [ffmpeg, "-v", "error"]
        if seek_to > 0:
            cmd += ["-ss", f"{seek_to:.3f}"]
        cmd += ["-i", src, "-f", "s32le", "-acodec", "pcm_s32le",
                "-ac", str(channels), "-ar", str(rate), "pipe:1"]
        self._ffmpeg = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL)
        self._pump_stop.clear()
        self._pump = threading.Thread(
            target=self._pump_loop,
            args=(self._ffmpeg, self._ring, channels, self._pump_stop),
            daemon=True, name="local-decode-pump")
        self._pump.start()

    def _pump_loop(self, proc: subprocess.Popen, ring: _Ring, channels: int,
                   stop: threading.Event) -> None:
        frame_bytes = 4 * channels
        pending = b""
        while not stop.is_set():
            chunk = proc.stdout.read(_PUMP_CHUNK)
            if not chunk:
                break
            pending += chunk
            usable = len(pending) - (len(pending) % frame_bytes)
            if not usable:
                continue
            frames = np.frombuffer(pending[:usable], dtype=np.int32).reshape(-1, channels)
            pending = pending[usable:]
            offset = 0
            while offset < len(frames) and not stop.is_set():
                wrote = ring.write(frames[offset:])
                if wrote == 0:
                    time.sleep(0.05)   # ring full — playback will drain it
                    continue
                offset += wrote
                with self._written_lock:
                    self._written_frames += wrote

    def _open_stream(self, rate: int, channels: int) -> bool:
        """Create the ring + PortAudio stream at (rate, channels) — NOT yet
        started; _prefill_and_start does that once audio is buffered."""
        if sd is None:
            self._set_state("stopped", error="sounddevice unavailable")
            return False
        from playback.local import devices as devices_mod
        try:
            device_index = devices_mod.resolve_device(self._device_id)
        except LookupError as e:
            self._set_state("stopped", error=str(e))
            return False

        hostapi = sd.query_hostapis(
            sd.query_devices(device_index)["hostapi"])["name"]
        extra = None
        if hostapi == "Windows WASAPI":
            # Shared mode MUST auto-convert: the OS mixer accepts only the
            # device mix format (typically 48k float), and a native-rate
            # int32 stream fails at stream.start() with
            # AUDCLNT_E_UNSUPPORTED_FORMAT. Bit-perfect shared doesn't exist
            # anyway — exclusive is the bit-perfect path, native format only.
            extra = (sd.WasapiSettings(exclusive=True) if self._exclusive
                     else sd.WasapiSettings(auto_convert=True))
        ring = _Ring(int(_RING_SECONDS * rate), channels)

        def callback(outdata, frames, _time_info, status_flags):
            if status_flags.output_underflow:
                self._underruns += 1
            out = np.frombuffer(outdata, dtype=np.int32).reshape(-1, channels)
            got = ring.read_into(out)
            if got < frames:
                out[got:] = 0
                if ring.closed and got == 0:
                    raise sd.CallbackStop
            vol = self._volume
            if vol < 100.0:
                # Software volume — float scale, truncate back to int32.
                # At 100 this branch never runs: bit-perfect passthrough.
                np.multiply(out, vol / 100.0, out=out, casting="unsafe")

        try:
            stream = sd.RawOutputStream(
                samplerate=rate, channels=channels, dtype="int32",
                device=device_index, callback=callback,
                extra_settings=extra)
        except Exception as e:
            logger.error("stream open failed (%s ch=%d rate=%d exclusive=%s): %s",
                         hostapi, channels, rate, self._exclusive, e)
            self._set_state("stopped", error=f"device open failed: {e}")
            return False

        self._ring = ring
        self._stream = stream
        self._stream_rate = rate
        self._stream_channels = channels
        self._underruns = 0
        with self._written_lock:
            self._written_frames = 0
        return True

    # -- progression --------------------------------------------------------------

    def _advance(self) -> None:
        """Player-loop tick: line up the next decode on ffmpeg EOF (gapless
        or pending rate switch), walk track boundaries against the callback's
        read counter, finish naturally when the closed ring drains."""
        if self._ring is None or self._state == "stopped":
            return

        # Pending rate switch: wait (non-blocking — we re-check every tick)
        # for the ring to drain, then reopen at the new rate.
        if self._pending_switch is not None:
            if self._state == "playing" and self._ring.available_read() == 0:
                index = self._pending_switch
                self._start_at(index)
            return

        # Current decode finished → queue up the next track.
        if (self._state == "playing"
                and self._ffmpeg is not None and self._ffmpeg.poll() is not None):
            if self._pump is not None:
                self._pump.join(timeout=2)
            self._ffmpeg = None
            self._line_up_next()

        # Walk boundaries against consumed frames → current slot + position.
        consumed = self._ring.frames_read
        active = None
        for b in self._boundaries:
            if consumed >= b[1]:
                active = b
            else:
                break
        if active and active[0] != self._index:
            self._index, _start, self._length, self._current = active
            self._position_offset = 0.0
            self._boundaries = [b for b in self._boundaries if b[1] >= active[1]]
            self._emit_status()

        if self._ring.closed and self._ring.available_read() == 0:
            self._teardown_pipeline()
            self._set_state("stopped")

    def _line_up_next(self) -> None:
        """After the current ffmpeg finished: find the next playable queue
        item; same format → gapless append into the live ring, different
        format → park a pending switch; end of queue → close the ring."""
        index = (self._boundaries[-1][0] + 1) if self._boundaries else self._index + 1
        while True:
            item = self._queue.item_at(index)
            if item is None:
                self._ring.closed = True
                return
            src = self._source_url(item)
            probed = _probe(src) if src else None
            if probed is not None:
                break
            logger.error("track unplayable, skipping: %s — %s", item.artist, item.title)
            self._track_failures += 1
            if self._track_failures >= _MAX_CONSECUTIVE_TRACK_FAILURES:
                self._ring.closed = True
                self._error = "too many unplayable tracks"
                return
            index += 1
        rate, channels, duration = probed
        if rate == self._stream_rate and channels == self._stream_channels:
            with self._written_lock:
                start = self._written_frames
            self._boundaries.append(
                (index, start, item.duration_seconds or duration or 0.0, item))
            self._spawn_ffmpeg(src, rate, channels, 0.0)
            self._track_failures = 0
        else:
            self._pending_switch = index

    def _seek(self, seconds: float) -> None:
        if self._current is None or self._index < 1:
            return
        was_playing = self._state == "playing"
        index = self._index
        self._start_at(index, seek_to=max(0.0, seconds))
        if not was_playing and self._stream is not None:
            self._stream.stop()
            self._set_state("paused")

    def _resync_index(self) -> None:
        """The canonical queue mutated: re-locate the current item by object
        identity (queue mutations move item refs, never copy them), un-close
        a drained tail when new items were appended past it, and if the
        current item was removed play whatever took its slot — or stop at
        the queue end."""
        if self._current is None:
            return
        snapshot = self._queue.snapshot()
        located = None
        for i, it in enumerate(snapshot, start=1):
            if it is self._current:
                located = i
                break
        if located is None:
            if self._state != "stopped":
                self._start_at(min(self._index, len(snapshot)))
            return
        if located != self._index:
            self._index = located
            for j, b in enumerate(self._boundaries):
                if b[3] is self._current:
                    self._boundaries[j] = (located, b[1], b[2], b[3])
            self._emit_status()
        # End-of-queue reached but items were appended behind it (radio
        # refill, queue-tracks): reopen the tail while the stream is alive.
        if (self._ring is not None and self._ring.closed and self._ffmpeg is None
                and self._state == "playing"):
            last = self._boundaries[-1][0] if self._boundaries else self._index
            if self._queue.item_at(last + 1) is not None:
                self._ring.closed = False
                self._line_up_next()

    # -- state/status ---------------------------------------------------------------

    def _teardown_pipeline(self) -> None:
        self._pump_stop.set()
        if self._ffmpeg is not None:
            try:
                self._ffmpeg.kill()
            except Exception:
                pass
            self._ffmpeg = None
        if self._pump is not None:
            self._pump.join(timeout=2)
            self._pump = None
        if self._stream is not None:
            try:
                self._stream.abort()
                self._stream.close()
            except Exception as e:
                logger.debug("stream close: %s", e)
            self._stream = None
        self._ring = None
        self._stream_rate = 0
        self._stream_channels = 0
        self._boundaries = []
        self._pending_switch = None
        if self._underruns:
            logger.warning("local playback: %d underruns this stream", self._underruns)

    def _position(self) -> float:
        if self._ring is None:
            return 0.0
        start = None
        for b in self._boundaries:
            if b[0] == self._index:
                start = b[1]
                break
        if start is None:
            return 0.0
        played = max(0, self._ring.frames_read - start)
        return self._position_offset + played / float(self._stream_rate or 1)

    def _set_state(self, state: str, error: Optional[str] = None) -> None:
        self._state = state
        self._error = error
        if error:
            logger.error("local playback: %s", error)
        self._emit_status()

    def _emit_status(self) -> None:
        extra = {}
        if self._error:
            extra["error"] = self._error
        if self._underruns:
            extra["underruns"] = self._underruns
        self._emit(PlaybackStatus(
            state=self._state,
            position=self._position(),
            length=self._length if self._index >= 1 else 0.0,
            queue_index=self._index if self._index >= 1 else 0,
            volume=self._volume,
            extra=extra,
        ))
