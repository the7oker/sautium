"""
HQPlayer's meter stream — the peaks behind the peak meter on the HQPlayer
screen (a gain-staging tool, opened from the Volume row).

The wire, from Signalyst's control SDK (`clMeterInterface`, hqp-control 6.0.1)
and live captures on Desktop 6.2.3 (2026-10-07). The client connects to the
control port + 1 (HQPlayer's default; its HQPLAYER_METERPORT can move it)
and sends nothing; HQPlayer streams frames:

    header   <IIIiffff  version (1), channels, xformLength, xformBits,
                        bandwidth, xformTime, xformGain, reserved
    per channel, in channel order:
             <ffff      peakMax, peak, rms, rmsMax — dB
             xformLength float32, twice — Re and Im of the block's spectrum

Frames are cut at the SOURCE rate, a hop of 1024 samples (43 a second at
44.1 kHz), and sent in bursts every 160 ms, ~20 ms after HQPlayer's reported
position passes them. While HQPlayer is paused they carry silence; while it
is stopped none come, so a quiet socket is not a dead one — TCP keepalive
finds a peer that vanished.

HQPlayer's own levels are taken AFTER its limiter: with an over its peak
reads 0.00 dB however far the music goes past (2026-10-07), so they cannot
say by how much. The spectra can: each is the 2048-point FFT of a periodic-
Hann-windowed block of the source itself — before adaptive gain, volume and
the limiter — scaled 2/N with Im negated, blocks a hop apart. An inverse FFT
and an overlap-add give the source back (to float32 precision against the
FLAC), so the meter rebuilds it, takes its true peak (4× oversampled) and
adds the gain HQPlayer applies ahead of the limiter — the volume and the
track's adaptive gain, from the status poller (`set_gain`; none while it
names no track, and then no reading). A reading covers READING_S of the
source rebuilt. Where nothing is limited this agrees with HQPlayer's own
peak burst for burst (MAD 0.013 dB); above 0 dBTP it is the over HQPlayer's
limiter takes away. A gain stage not in that sum (convolution, an EQ) is
not in the reading.

The stream costs HQPlayer an FFT per frame and the link ~0.7 MB/s per
44.1 kHz of source rate, so the socket exists only while a page has the
meter open and an HQPlayer backend is attached. A page states its interest
through PUT /api/hqplayer/meter for the /api/events connection it holds;
the interest dies with that connection, and a reconnect of the same page
takes the registration over (the browser renderer's rule). One thread owns
the socket and does every connect, read and close; everyone else changes
state under the lock and wakes it. It connects on edges only — an attach,
a page's interest, HQPlayer coming back to the status poller, one retry
when a stream that delivered frames drops — never on a timer: a port that
refuses while the control port answers is reported and left alone.
"""

import asyncio
import logging
import socket
import struct
import threading
from collections import deque
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.signal import resample_poly

logger = logging.getLogger(__name__)

_HEAD = struct.Struct("<IIIiffff")
_LEVELS = struct.Struct("<ffff")
VERSION = 1
# Bounds of a header worth waiting for: a foreign or corrupt one must not
# make the read buffer grow without end. 32 channels × 16 385 bins is a
# 4.2 MB frame; HQPlayer sends 1025 bins.
MAX_CHANNELS = 32
MAX_BINS = 16385
# Below this a rebuilt peak is silence: no needle reads it.
SILENT_DB = -120.0

CONNECT_TIMEOUT_S = 3.0
# A reading per this much of the source rebuilt — HQPlayer's own burst, and
# 6.25 readings a second whatever TCP does to the bursts on the way.
READING_S = 0.16
# Readings a page's stream may leave untaken — ~5 s while its generator does
# not run (a blocked loop, a send the OS has stopped taking) — before the
# page loses its interest; it is told, and asks again if it still wants.
PAGE_BACKLOG = 32


class MeterProtocolError(ValueError):
    """What arrived on the meter port is not HQPlayer's meter stream."""


def parse_frames(view: memoryview) -> Tuple[List[Tuple[float, np.ndarray]], int, int]:
    """The complete frames at the start of `view`: each one's source rate
    and spectra (a complex (channels, bins) array); the bytes they took; and
    the size of the incomplete frame that follows (0 when no header has
    arrived yet). A header is checked before its body is waited for: the
    version, sane bounds, and a hop (xformTime at the source rate) of half
    the FFT — the layout the rebuild relies on."""
    frames = []
    pos = 0
    end = len(view)
    while end - pos >= _HEAD.size:
        version, channels, bins, _bits, bandwidth, xform_time, *_ = _HEAD.unpack_from(view, pos)
        if version != VERSION or not 0 < channels <= MAX_CHANNELS or not 1 < bins <= MAX_BINS:
            raise MeterProtocolError(
                f"not an HQPlayer meter frame (version {version}, {channels} channels, {bins} bins)")
        if abs(xform_time * 2 * bandwidth - (bins - 1)) > 0.5:
            raise MeterProtocolError(
                f"an unknown meter layout: a hop of {xform_time * 2 * bandwidth:.1f} samples "
                f"for a {2 * (bins - 1)}-point FFT")
        stride = _LEVELS.size + 8 * bins
        size = _HEAD.size + channels * stride
        if end - pos < size:
            return frames, pos, size
        spectra = np.empty((channels, bins), dtype=np.complex128)
        at = pos + _HEAD.size + _LEVELS.size
        for c in range(channels):
            re = np.frombuffer(view, "<f4", bins, at)
            im = np.frombuffer(view, "<f4", bins, at + 4 * bins)
            spectra[c].real = re
            spectra[c].imag = -im
            at += stride
        frames.append((2.0 * bandwidth, spectra))
        pos += size
    return frames, pos, 0


class SourceRebuild:
    """The source, rebuilt from the frames' spectra one frame at a time, and
    its true peak. The window halves sum to one at a hop of N/2 (periodic
    Hann), so a frame completes the hop before it."""

    # Samples of context either side of the 4× interpolation: the samples
    # nearest a gap wait for the next frames rather than read against zeros.
    CONTEXT = 64

    def __init__(self, channels: int, bins: int) -> None:
        self.shape = (channels, bins)
        self.n = 2 * (bins - 1)
        self.hop = self.n // 2
        self._tail = np.zeros((channels, self.hop))
        self._left = np.zeros((channels, self.CONTEXT))
        self._pending: List[np.ndarray] = []

    def add(self, spectra: np.ndarray) -> None:
        block = np.fft.irfft(spectra * (self.n / 2), n=self.n, axis=1)
        self._pending.append(self._tail + block[:, :self.hop])
        self._tail = block[:, self.hop:]

    @property
    def pending(self) -> int:
        """Samples rebuilt and not yet in a peak."""
        return sum(b.shape[1] for b in self._pending)

    def peaks(self) -> Optional[np.ndarray]:
        """The true peak (linear, 4× oversampled) per channel of the samples
        completed since the last call, but for the last CONTEXT, which wait
        for theirs; None until a hop's worth arrived."""
        if not self._pending:
            return None
        new = np.concatenate(self._pending, axis=1)
        m = new.shape[1]
        if m <= self.CONTEXT:
            return None
        c = self.CONTEXT
        seg = np.concatenate((self._left, new), axis=1)
        up = resample_poly(seg, 4, 1, axis=1)
        peak = np.abs(up[:, 4 * c:4 * m]).max(axis=1)
        self._left = seg[:, m - c:m]
        self._pending = [seg[:, m:]]
        return peak


def _keepalive(sock: socket.socket) -> None:
    """hqplayer_client's timers: a peer gone without a reset is found in
    ~35 s, the only liveness signal a stopped HQPlayer's silence leaves."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for opt, val in (("TCP_KEEPIDLE", 20), ("TCP_KEEPINTVL", 5),
                     ("TCP_KEEPCNT", 3), ("TCP_KEEPALIVE", 20)):
        if hasattr(socket, opt):
            try:
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
            except OSError:
                pass  # platform exposes the constant but rejects it


class Connection:
    """One /api/events connection of a page: what it has yet to send. Lives
    on that connection's event loop — `offer` and `drain` run there."""

    def __init__(self, meter: "HqpMeter", tab: str, evt: asyncio.Event,
                 loop: asyncio.AbstractEventLoop) -> None:
        self._meter = meter
        self.tab = tab
        self.evt = evt
        self.loop = loop
        self._pending: deque = deque()

    def offer(self, msg: dict) -> None:
        if len(self._pending) >= PAGE_BACKLOG:
            # The page stopped reading. Its interest goes, and the page is
            # told when it reads again — it asks anew if it still wants.
            self._pending.clear()
            self._pending.append({"state": "dropped"})
            self.evt.set()
            self._meter._withdraw(self)
            return
        self._pending.append(msg)
        self.evt.set()

    def drain(self) -> List[dict]:
        out = list(self._pending)
        self._pending.clear()
        return out

    def send(self, msg: dict) -> None:
        """Thread-safe: queue `msg` on this connection's loop."""
        self.loop.call_soon_threadsafe(self.offer, msg)


class _Page:
    __slots__ = ("conn", "on", "seq")

    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self.on = False
        self.seq = -1


class HqpMeter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._pages: Dict[str, _Page] = {}
        # (host, meter port) of the attached HqpBackend; bumped on every
        # attach and detach so a connect that raced one is dropped.
        self._target: Optional[Tuple[str, int]] = None
        self._generation = 0
        self._due = False
        self._sock: Optional[socket.socket] = None
        self._connecting = False
        self._unavailable: Optional[dict] = None
        self._last: Optional[dict] = None
        # dB HQPlayer applies ahead of its limiter (set_gain); None until the
        # status poller has said
        self._gain_db: Optional[float] = None
        self._thread: Optional[threading.Thread] = None

    # -- the HqpBackend's lifecycle --------------------------------------------

    def attach(self, host: str, port: int) -> None:
        """An HQPlayer backend attached: its meter lives at host:port."""
        with self._cond:
            self._target = (host, port)
            self._generation += 1
            self._unavailable = None
            self._last = None
            self._gain_db = None
            self._due = True
            self._ensure_thread()
            self._cond.notify()

    def detach(self) -> None:
        """The backend went (an output switch, a benchmark's hold, a new
        address): the socket closes, pages keep their interest for the
        next attach."""
        with self._cond:
            self._target = None
            self._generation += 1
            self._unavailable = None
            self._last = None
            self._due = False
            self._stop_locked()
            self._publish_locked(self._state_locked())

    def hqplayer_back(self) -> None:
        """The status poller found HQPlayer again after losing it."""
        with self._cond:
            if self._sock is None and not self._connecting and self._target is not None:
                self._unavailable = None
                self._due = True
                self._cond.notify()

    def metering(self) -> bool:
        """The socket is open — HQPlayer is computing meters for us."""
        return self._sock is not None

    def set_gain(self, gain_db: Optional[float]) -> None:
        """What HQPlayer applies ahead of its limiter, from every status
        tick: its volume plus the track's adaptive gain. None while it names
        no track (stopped): no reading is made at a gain not known for what
        plays next, and the last one, made before, is not shown again."""
        with self._cond:
            self._gain_db = gain_db
            if gain_db is None:
                self._last = None

    # -- pages (the /api/events generator and PUT /api/hqplayer/meter) ---------

    def register(self, tab: str, evt: asyncio.Event,
                 loop: asyncio.AbstractEventLoop) -> Connection:
        """A page's event stream (re)connected. A newer connection of the
        same page takes the registration over with its interest; the old
        one, if still open, hears nothing more."""
        conn = Connection(self, tab, evt, loop)
        with self._cond:
            page = self._pages.get(tab)
            if page is None:
                self._pages[tab] = _Page(conn)
            else:
                page.conn = conn
                if page.on:
                    self._snapshot_locked(conn)
            self._ensure_thread()
        return conn

    def unregister(self, conn: Connection) -> None:
        with self._cond:
            page = self._pages.get(conn.tab)
            if page is None or page.conn is not conn:
                return
            del self._pages[conn.tab]
            if not self._wanted_locked():
                self._stop_locked()

    def want(self, tab: str, on: bool, seq: int) -> bool:
        """A page's wish, in the page's own order (`seq`). False when the
        page holds no event stream — it asks again once one is open."""
        with self._cond:
            page = self._pages.get(tab)
            if page is None:
                return False
            if seq <= page.seq:
                return True
            page.seq = seq
            was_on = page.on
            page.on = on
            if on:
                if self._sock is None and not self._connecting and self._target is not None:
                    # also a Retry: a page asking while the port is
                    # unavailable is a fresh edge — and the snapshot below
                    # says so, not the failure before it
                    self._unavailable = None
                    self._due = True
                    self._cond.notify()
                if not was_on:
                    self._snapshot_locked(page.conn)
            elif not self._wanted_locked():
                self._stop_locked()
            return True

    def _withdraw(self, conn: Connection) -> None:
        with self._cond:
            page = self._pages.get(conn.tab)
            if page is None or page.conn is not conn or not page.on:
                return
            page.on = False
            if not self._wanted_locked():
                self._stop_locked()

    # -- internals ---------------------------------------------------------------

    def _ensure_thread(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="hqp-meter")
            self._thread.start()

    def _wanted_locked(self) -> bool:
        return any(p.on for p in self._pages.values())

    def _state_locked(self) -> dict:
        if self._target is None:
            return {"state": "waiting"}
        if self._sock is not None:
            return {"state": "open"}
        if self._unavailable is not None:
            return dict(self._unavailable)
        return {"state": "connecting"}

    def _snapshot_locked(self, conn: Connection) -> None:
        conn.send(self._state_locked())
        if self._sock is not None and self._last is not None:
            conn.send(self._last)

    def _publish_locked(self, msg: dict) -> None:
        for page in self._pages.values():
            if page.on:
                page.conn.send(msg)

    def _stop_locked(self) -> None:
        # Wake the owner's blocked read: a shutdown does on Linux and macOS,
        # Windows leaves the read blocked until the socket is closed
        # (hqp_benchmark's Link.interrupt). Under the lock, so the owner has
        # not let go of it in between; its own close after that is a no-op.
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError as e:
                # ENOTCONN: the peer reset the connection first; the owner's
                # read has ended (or is about to) on that reset already.
                logger.debug("meter socket already down: %s", e)
            sock.close()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not (self._due and self._target is not None and self._wanted_locked()):
                    self._cond.wait()
                self._due = False
                self._connecting = True
                target, generation = self._target, self._generation
                self._publish_locked({"state": "connecting"})
            try:
                self._session(target, generation)
            except Exception as e:
                # The meter's one thread outlives any session: what broke
                # this one is logged and shown, and the next wish connects.
                logger.exception("HQPlayer meter session failed")
                self._fail(generation, "error", target[1], f"{type(e).__name__}: {e}")

    def _session(self, target: Tuple[str, int], generation: int) -> None:
        host, port = target
        try:
            sock = socket.create_connection(target, timeout=CONNECT_TIMEOUT_S)
        except ConnectionRefusedError:
            self._fail(generation, "refused", port, f"{host}:{port} refused the connection")
            return
        except (socket.timeout, TimeoutError):
            self._fail(generation, "timeout", port, f"no answer from {host}:{port}")
            return
        except (OSError, UnicodeError) as e:      # UnicodeError: a name IDNA cannot encode
            self._fail(generation, "unreachable", port, f"{host}:{port}: {e}")
            return
        try:
            try:
                sock.settimeout(None)
                _keepalive(sock)
            except OSError as e:                  # reset before it was set up
                self._fail(generation, "lost", port, f"{host}:{port}: {e}")
                return
            with self._cond:
                self._connecting = False
                if generation != self._generation or not self._wanted_locked():
                    return
                self._sock = sock
                # a reconnect edge that came while connecting is spent here
                self._due = False
                self._publish_locked({"state": "open"})
            logger.info("HQPlayer meter stream open (%s:%d)", host, port)
            delivered, ended = self._read(sock)
        finally:
            with self._cond:
                self._connecting = False
                deliberate = self._sock is not sock
                if not deliberate:
                    self._sock = None
                    self._last = None
            sock.close()
        if deliberate:
            logger.info("HQPlayer meter stream closed")
            return
        if ended is not None and ended[0] == "protocol":
            self._fail(generation, "protocol", port, ended[1])
        elif delivered:
            # It was flowing: one immediate attempt — a restarting meter
            # server answers it, a quitting HQPlayer refuses it.
            with self._cond:
                if generation == self._generation:
                    self._due = True
                    self._cond.notify()
            logger.info("HQPlayer meter stream ended (%s) — reconnecting once",
                        ended[1] if ended else "closed by HQPlayer")
        elif ended is not None:
            self._fail(generation, "lost", port, f"{host}:{port}: {ended[1]}")
        else:
            self._fail(generation, "closed", port, f"{host}:{port} closed the meter connection")

    def _read(self, sock: socket.socket) -> Tuple[bool, Optional[Tuple[str, str]]]:
        """Read until the stream ends or is shut: (frames seen, how it ended —
        None for an end of stream, ("lost", the error) for a connection that
        failed, ("protocol", why) for what is not a meter stream). A reading
        goes out per READING_S of the source rebuilt."""
        buf = bytearray(1 << 20)
        view = memoryview(buf)
        fill = 0
        delivered = False
        rebuild = None
        while True:
            try:
                n = sock.recv_into(view[fill:])
            except OSError as e:
                return delivered, ("lost", str(e) or type(e).__name__)
            if n == 0:
                return delivered, None
            fill += n
            try:
                frames, used, need = parse_frames(view[:fill])
            except MeterProtocolError as e:
                return delivered, ("protocol", str(e))
            for rate, spectra in frames:
                if rebuild is None or rebuild.shape != spectra.shape:
                    rebuild = SourceRebuild(*spectra.shape)
                rebuild.add(spectra)
                delivered = True
                if rebuild.pending >= READING_S * rate:
                    self._reading(sock, rebuild.peaks())
            if used:
                buf[:fill - used] = buf[used:fill]     # same length: allowed while viewed
                fill -= used
            if need > len(buf):
                buf = buf[:fill] + bytearray(need - fill)
                view = memoryview(buf)

    def _reading(self, sock: socket.socket, peaks: Optional[np.ndarray]) -> None:
        floor = 10 ** (SILENT_DB / 20)
        with self._cond:
            gain = self._gain_db
            if peaks is None or gain is None or self._sock is not sock:
                return
            # `g` names the gain the reading was made with: a volume step
            # shows in it, and a hold kept across it would mix two volumes
            reading = {"p": [round(float(20 * np.log10(p)) + gain, 1) if p > floor else None
                             for p in peaks],
                       "g": round(gain, 2)}
            self._last = reading
            self._publish_locked(reading)

    def _fail(self, generation: int, reason: str, port: int, detail: str) -> None:
        with self._cond:
            self._connecting = False
            if generation != self._generation:
                return
            self._unavailable = {"state": "unavailable", "reason": reason,
                                 "port": port, "detail": detail}
            self._publish_locked(self._unavailable)
        # refused, silent or closed: HQPlayer away or not listening — what a
        # meter meets in normal life; the rest is something going wrong
        level = logging.INFO if reason in ("refused", "timeout", "closed", "unreachable") else logging.WARNING
        logger.log(level, "HQPlayer meter unavailable (%s): %s", reason, detail)


meter = HqpMeter()
