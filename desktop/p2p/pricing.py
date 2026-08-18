"""Pricing — how many gate tasks a request costs (P2P-SYNC-INTEGRITY.md
§ "Pricing formula v1"):

    price(action, client) = base(action) × load_mult(headroom) × sim_mult(cluster)

- `base(action)` — what the action costs THIS node, expressed in units of
  w (one 64 MiB task, calibrated per node at start): CPU ms of the
  endpoint's EMA (`p2p_action_costs`, contact_log.py) plus a bandwidth
  term (bytes out ÷ BYTES_PER_MS). Parity is the anchor: the requester
  spends about as much compute as we do; a lite node is dearer because its
  own costs are.
- `load_mult` — 1 while there is room, progressive to LOAD_MULT_MAX as
  the headroom of the load meter vanishes; playback halves headroom there.
- **dormant** (headroom ≥ 0.5): the price is 0 and the machinery sleeps —
  our own check costs R·w, so entry is free when we can afford it.
- **PI** — the proportional part is load_mult; the integral part answers a
  siege: pressure above the dormant threshold is integrated over time
  (`siege` seconds, decayed with a half-life), and multiplies the price
  further. It acts on the STRANGER market only — an identity-lane request
  (verified ∧ ripe) never pays; a legit long load rides its identity
  budget, not the market. Attrition favours us: the attacker pays
  superlinear × escalation, we pay R·w that he funded.
- `sim_mult` — similarity (Ф14); 1 for now.

Modes (`user_settings['p2p.gate_mode']`, default **shadow**): `off` — no
pricing at all; `shadow` — the would-be price is computed and logged
(contact events `gate_price`), the wire says 0; `enforce` — quotes carry
the price and unpaid market requests get 402. Arming is a decision taken
with the shadow distribution in hand, not a side effect.

Everything is capped: MAX_TASKS bounds an honest stranger's worst case
under a siege (a price is never a refusal — refusal is for deterministic
evidence only).
"""

import logging
import math
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

MODES = ("off", "shadow", "enforce")
DEFAULT_MODE = "shadow"
W_MS_DEFAULT = 40.0                 # until calibrated
BYTES_PER_MS = 20_000.0             # 1 MB out ≈ 50 ms of "our cost" — placeholder
MIN_BASE_TASKS = 1.0                # a priced request never costs less than one task
MAX_TASKS = 30
LOAD_MULT_MAX = 8.0
DORMANT_THRESHOLD = 0.5             # mirrors load_meter.DORMANT_THRESHOLD
PI_HALF_LIFE_S = 600.0              # the siege memory
PI_GAIN_PER_S = 1.0 / 300.0         # +1× per 300 s of full pressure
PI_MULT_MAX = 8.0                   # hard cap; at these constants the steady state under
                                    # full pressure is 1 + (half_life/ln2)·gain ≈ 3.9×

_current: Optional["Pricer"] = None


def install(pricer: "Pricer") -> "Pricer":
    global _current
    _current = pricer
    return pricer


def current() -> Optional["Pricer"]:
    return _current


class Pricer:
    def __init__(self, meter, *, costs: Callable[[], dict], mode: Callable[[], str],
                 sim_mult: Callable[[str], float] = lambda client: 1.0,
                 w_ms: Optional[float] = None, clock: Callable[[], float] = time.monotonic):
        """`meter` — load_meter.LoadMeter (headroom/dormant/playback);
        `costs()` → {endpoint: (ema_cpu_ms, ema_bytes_out)}; `mode()` →
        off|shadow|enforce (cached by the caller); `sim_mult(client)`."""
        self._meter = meter
        self._costs = costs
        self._mode = mode
        self._sim = sim_mult
        self._clock = clock
        self.w_ms = w_ms or W_MS_DEFAULT
        self._calibrated = w_ms is not None
        self._siege = 0.0
        self._last_t = clock()
        self._lock = threading.Lock()
        if meter is not None:
            meter.subscribe_samples(self.tick)

    # -- calibration -----------------------------------------------------------------

    def calibrate_w(self) -> float:
        """One 64 MiB task on this machine — the unit every base is priced in.
        Blocking (~40–200 ms); callers run it once off the hot path."""
        from desktop.p2p import admission
        import os
        t0 = time.perf_counter()
        admission.solve(os.urandom(32))
        self.w_ms = max(1.0, (time.perf_counter() - t0) * 1000.0)
        self._calibrated = True
        return self.w_ms

    # -- components ------------------------------------------------------------------

    def base_tasks(self, action: str) -> float:
        cpu_ms, bytes_out = self._costs().get(action, (None, None))
        if cpu_ms is None:
            return MIN_BASE_TASKS
        our_ms = float(cpu_ms) + float(bytes_out or 0.0) / BYTES_PER_MS
        return max(MIN_BASE_TASKS, our_ms / self.w_ms)

    def headroom(self) -> float:
        return 1.0 if self._meter is None else self._meter.headroom

    def dormant(self) -> bool:
        return self.headroom() >= DORMANT_THRESHOLD

    def load_mult(self) -> float:
        h = self.headroom()
        if h >= DORMANT_THRESHOLD:
            return 1.0
        return 1.0 + (LOAD_MULT_MAX - 1.0) * (DORMANT_THRESHOLD - h) / DORMANT_THRESHOLD

    def tick(self, snapshot: Optional[dict] = None) -> None:
        """Integrate pressure above the dormant threshold; decay always."""
        now = self._clock()
        dt = max(0.0, now - self._last_t)
        self._last_t = now
        pressure = max(0.0, DORMANT_THRESHOLD - self.headroom()) / DORMANT_THRESHOLD
        with self._lock:
            self._siege *= 0.5 ** (dt / PI_HALF_LIFE_S)
            self._siege += pressure * dt

    def pi_mult(self) -> float:
        with self._lock:
            return min(PI_MULT_MAX, 1.0 + self._siege * PI_GAIN_PER_S)

    # -- the price -------------------------------------------------------------------

    def would_be(self, client_pubkey: str, action: str, lane: str) -> int:
        """The price the mechanism WOULD charge, whatever the mode (shadow logs
        this): 0 for the identity lane and while dormant."""
        if lane == "identity" or self.dormant():
            return 0
        tasks = self.base_tasks(action) * self.load_mult() * self.pi_mult() * self._sim(client_pubkey)
        return int(min(MAX_TASKS, math.ceil(tasks)))

    def price(self, client_pubkey: str, action: str, lane: str) -> int:
        """What actually goes on the wire: only in enforce mode."""
        return self.would_be(client_pubkey, action, lane) if self.mode() == "enforce" else 0

    def mode(self) -> str:
        m = self._mode()
        return m if m in MODES else DEFAULT_MODE

    def snapshot(self) -> dict:
        return {
            "mode": self.mode(),
            "w_ms": round(self.w_ms, 1),
            "w_calibrated": self._calibrated,
            "headroom": round(self.headroom(), 3),
            "dormant": self.dormant(),
            "load_mult": round(self.load_mult(), 2),
            "pi_mult": round(self.pi_mult(), 2),
            "siege_s": round(self._siege, 1),
        }
