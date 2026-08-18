"""Load meter — headroom and dormancy for this node's own footprint.

The most dangerous attack in the threat inventory is not denial of
service but resource exhaustion as a product-killer: "Sautium makes my PC
lag, I'll disable P2P". So the node's own footprint has a CEILING —
25 % of the machine's CPU on full/standard hardware, 15 % on lite — and
everything discretionary (gate machinery, verification, mining, DHT
sweeps) is priced by how close we are to it (P2P-SYNC-INTEGRITY.md
§ "Defense strategy": `load_mult(headroom)`, "dormant below a load
threshold", "HQP playing = low headroom = expensive").

    cpu_frac  = CPU time of THIS process tree / (wall × cores), EMA ~10 s
    headroom  = clamp(1 − cpu_frac / ceiling, 0, 1) × (½ if playback active)
    dormant   = headroom ≥ 0.5    → the market machinery may sleep

Playback is a priority signal, not a load: when the machine is doing the
thing the product exists for, discretionary work yields — the backend
probes its PlaybackManager on every sample; a `set_playback_active(True)`
from an event path is a 30 s lease, so a stalled poller cannot pin the
node in "playing" forever. Memory pressure is not folded into headroom:
the memory-heavy jobs (2 GiB verification, mining) already guard on
MemAvailable themselves; the meter exposes it in the snapshot.

Sampling is periodic by nature (a daemon thread every 2 s); consumers are
event-driven — `subscribe(cb)` fires only when the headroom band (0.1
steps), the dormant flag or the playback flag changes. Two consumers ship
with this module: `announce_pace()` (multiplier for the DHT announce
chunk pause) and `mining_hold()` (a reason to pause the identity miner —
playback only: mining is one nice'd core, and letting it pause itself on
its own CPU would oscillate).

Shared by the launcher and the Docker backend; psutil when importable
(both requirements list it), `os.times()` otherwise. One meter per
process: `install()` / `current()`.
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

CEILING_CPU = {"full": 0.25, "standard": 0.25, "lite": 0.15}
SAMPLE_INTERVAL = 2.0
EMA_ALPHA = 0.3                     # ~10 s memory at 2 s samples
DORMANT_THRESHOLD = 0.5
PLAYBACK_LEASE_SECONDS = 30.0
PLAYBACK_HEADROOM_FACTOR = 0.5
BAND = 0.1
PACE_MAX = 8.0

_current: Optional["LoadMeter"] = None


def install(meter: "LoadMeter") -> "LoadMeter":
    global _current
    _current = meter
    return meter


def current() -> Optional["LoadMeter"]:
    return _current


def default_profile() -> str:
    """The launcher has no hardware_profile module: lite when the machine
    is small (the hardware-tiers RAM rule), standard otherwise."""
    cores = os.cpu_count() or 1
    try:
        import psutil
        ram_gb = psutil.virtual_memory().total / 1e9
    except ImportError:
        ram_gb = 16.0
    return "lite" if ram_gb < 12 or cores < 4 else "standard"


def _tree_cpu_seconds() -> float:
    """CPU seconds consumed by this process and its live children."""
    try:
        import psutil
        proc = psutil.Process()
        total = 0.0
        with proc.oneshot():
            t = proc.cpu_times()
            total += t.user + t.system
        for child in proc.children(recursive=True):
            try:
                t = child.cpu_times()
                total += t.user + t.system
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return total
    except ImportError:
        t = os.times()
        return t.user + t.system + t.children_user + t.children_system


def _mem_available_kib() -> Optional[int]:
    from desktop.p2p.identity_pow import mem_available_kib
    return mem_available_kib()


class LoadMeter:
    def __init__(self, profile: Optional[str] = None, *,
                 playback_probe: Optional[Callable[[], bool]] = None,
                 sample_interval: float = SAMPLE_INTERVAL,
                 cpu_seconds: Callable[[], float] = _tree_cpu_seconds,
                 clock: Callable[[], float] = time.monotonic,
                 cores: Optional[int] = None,
                 mem_available_kib: Callable[[], Optional[int]] = _mem_available_kib):
        self.profile = profile or default_profile()
        self.ceiling = CEILING_CPU.get(self.profile, CEILING_CPU["standard"])
        self._playback_probe = playback_probe
        self._interval = sample_interval
        self._cpu_seconds = cpu_seconds
        self._clock = clock
        self._cores = cores or os.cpu_count() or 1
        self._mem_available_kib = mem_available_kib
        self._lock = threading.Lock()
        self._subs: list = []
        self._sample_subs: list = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_cpu = self._cpu_seconds()
        self._last_t = self._clock()
        self.cpu_frac = 0.0
        self._playback_lease_until = 0.0
        self._playback = False
        self._last_key = None
        self.updated_at = datetime.now(timezone.utc)

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="load-meter")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.sample()
            except Exception as e:                       # measurement must never take the host down
                logger.debug("load sample failed: %s", e)

    # -- sampling ----------------------------------------------------------------

    def sample(self) -> dict:
        """One measurement step (the thread calls it; tests call it directly)."""
        now = self._clock()
        cpu = self._cpu_seconds()
        dt = now - self._last_t
        if dt > 0:
            inst = max(0.0, (cpu - self._last_cpu) / dt / self._cores)
            self.cpu_frac += EMA_ALPHA * (inst - self.cpu_frac)
        self._last_cpu, self._last_t = cpu, now
        if self._playback_probe is not None:
            try:
                self._playback = bool(self._playback_probe())
            except Exception as e:
                logger.debug("playback probe failed: %s", e)
        self.updated_at = datetime.now(timezone.utc)
        self._notify_if_changed()
        with self._lock:
            sample_subs = list(self._sample_subs)
        for cb in sample_subs:                       # every sample (integrators), not band changes
            try:
                cb(None)
            except Exception as e:
                logger.debug("load sample subscriber failed: %s", e)
        return self.snapshot()

    def set_playback_active(self, active: bool) -> None:
        """Event path (a status observer): a 30 s lease, renewed on every
        call with True — a stalled poller cannot pin us in 'playing'."""
        self._playback_lease_until = self._clock() + PLAYBACK_LEASE_SECONDS if active else 0.0
        self._notify_if_changed()

    # -- state -------------------------------------------------------------------

    @property
    def playback_active(self) -> bool:
        return self._playback or self._clock() < self._playback_lease_until

    @property
    def headroom(self) -> float:
        h = max(0.0, min(1.0, 1.0 - self.cpu_frac / self.ceiling))
        return h * PLAYBACK_HEADROOM_FACTOR if self.playback_active else h

    @property
    def dormant(self) -> bool:
        return self.headroom >= DORMANT_THRESHOLD

    def announce_pace(self) -> float:
        """Multiplier for the DHT announce chunk pause: 1× when dormant,
        rising to 4× as headroom vanishes, doubled while playing, capped."""
        if self.dormant and not self.playback_active:
            return 1.0
        pace = 1.0 + 3.0 * (1.0 - self.headroom)
        if self.playback_active:
            pace *= 2.0
        return min(pace, PACE_MAX)

    def mining_hold(self) -> Optional[str]:
        return "playback" if self.playback_active else None

    def snapshot(self) -> dict:
        mem = self._mem_available_kib()
        return {
            "profile": self.profile,
            "ceiling": self.ceiling,
            "cpu_frac": round(self.cpu_frac, 4),
            "mem_available_mib": None if mem is None else mem // 1024,
            "playback": self.playback_active,
            "headroom": round(self.headroom, 3),
            "dormant": self.dormant,
            "pace": round(self.announce_pace(), 2),
            "updated_at": self.updated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    # -- subscriptions -----------------------------------------------------------

    def subscribe(self, cb: Callable[[dict], None]) -> None:
        with self._lock:
            self._subs.append(cb)

    def subscribe_samples(self, cb: Callable[[Optional[dict]], None]) -> None:
        """Fires on EVERY sample (for integrators such as the pricer's
        siege term) — unlike subscribe(), which fires on band changes."""
        with self._lock:
            self._sample_subs.append(cb)

    def _notify_if_changed(self) -> None:
        key = (int(self.headroom / BAND + 1e-9), self.dormant, self.playback_active)
        if key == self._last_key:
            return
        self._last_key = key
        snap = self.snapshot()
        with self._lock:
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(snap)
            except Exception as e:
                logger.debug("load subscriber failed: %s", e)
