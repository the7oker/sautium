"""Device + dtype selection for ML model loading and inference.

Centralises the "which accelerator do we have" logic so model loaders
and inference call-sites pick consistent device/dtype/autocast settings.

Tiers:
- CUDA Ampere+ (SM 8.0+: RTX 30xx/40xx, A100, H100): bf16. Same 2-byte
  memory and Tensor Core speed as fp16 but fp32's dynamic range — avoids
  fp16 overflow in attention scores (CLAP HTSAT encoder, BERT-like models
  with large logits).
- CUDA Turing (SM 7.5: RTX 20xx, T4): fp16. No native bf16 Tensor Cores;
  bf16 would be emulated and slower than fp16.
- CUDA Pascal/Volta and older: fp32. No Tensor Cores; fp16 gives no speedup
  and risks numerical issues.
- MPS (Apple Silicon M1/M2/M3/M4/M5): bf16. Native on the Apple Neural
  Engine and GPU; PyTorch MPS supports bf16 autocast since 2.0.
- CPU: fp32. fp16 is software-emulated and slower than fp32 on x86/ARM.
"""

import collections
import contextlib
import errno
import logging
import functools
import os
import threading
from typing import Callable, List, Optional

import torch

logger = logging.getLogger(__name__)


def get_device() -> str:
    """Pick the best available accelerator: CUDA > MPS > CPU."""
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


@functools.lru_cache(maxsize=4)
def get_model_dtype(device: str) -> torch.dtype:
    """Best inference dtype for the device. See module docstring for tiers."""
    if device == "cuda":
        major, _ = torch.cuda.get_device_capability()
        if major >= 8:
            return torch.bfloat16
        if major == 7:  # Turing — fp16 Tensor Cores, no bf16
            return torch.float16
        return torch.float32
    if device == "mps":
        return torch.bfloat16
    return torch.float32


def autocast(device: str):
    """Forward-pass safety wrapper matching the model dtype tier."""
    if device in ("cuda", "mps"):
        return torch.autocast(device_type=device, dtype=get_model_dtype(device))
    return contextlib.nullcontext()


def cast_inputs(inputs: dict, dtype: torch.dtype) -> dict:
    """Cast floating-point input tensors to `dtype`; leave int tensors alone.

    When model weights are loaded with `torch_dtype=float16/bfloat16` and
    the HuggingFace processor returns fp32 tensors, the forward pass
    crashes on ops that autocast does not handle (notably `batch_norm`,
    which keeps its inputs at their declared dtype). This helper aligns
    audio/spectrogram inputs with model weights while preserving integer
    token IDs / attention masks.
    """
    out = {}
    for k, v in inputs.items():
        if torch.is_tensor(v) and v.is_floating_point():
            out[k] = v.to(dtype)
        else:
            out[k] = v
    return out


def empty_cache(device: Optional[str] = None) -> None:
    """Return the accelerator caching-allocator's free blocks to the OS.

    CUDA and MPS both keep a high-water pool of freed buffers for reuse and do
    not hand it back on their own; over a long enrichment run the process then
    holds its peak footprint (measured ~28 GB on MPS unified memory, 24 GB Mac)
    instead of the live per-batch working set. Call at batch seams. Model
    weights stay resident — only the free cache is released. No-op on CPU;
    `device` defaults to the detected accelerator."""
    dev = device or get_device()
    if dev == "cuda":
        torch.cuda.empty_cache()
    elif dev == "mps":
        torch.mps.empty_cache()


# What torch's RuntimeErrors say of memory it could not have (torch 2.12): CUDA
# and MPS out of memory, an MPS buffer over the device's largest, cuBLAS's
# workspace, the CPU allocator on POSIX and on Windows, a weights file's mmap
# (ENOMEM's own words)
_OUT_OF_MEMORY = ("out of memory", "invalid buffer size", "cublas_status_alloc_failed",
                  "can't allocate memory", "cannot allocate memory", "not enough memory")


def out_of_memory(exc: BaseException) -> bool:
    """Whether `exc` is the device out of memory for what was asked: CUDA's
    own error, Python's, the system's (ENOMEM), or one of torch's
    RuntimeErrors that says so."""
    if isinstance(exc, (torch.cuda.OutOfMemoryError, MemoryError)):
        return True
    if isinstance(exc, OSError) and exc.errno == errno.ENOMEM:
        return True
    text = str(exc).lower()
    return isinstance(exc, RuntimeError) and any(s in text for s in _OUT_OF_MEMORY)


def available_accel_memory_gb() -> float:
    """Memory actually usable for a batch, in GB.

    - CUDA: FREE dedicated VRAM (mem_get_info). We size to fit VRAM and avoid
      spilling to WDDM shared memory, which is 10-100x slower than VRAM.
    - MPS: the recommended working set on unified memory (shared with the OS;
      there is no hard limit — overshoot silently swaps — so we stay under it).
    - CPU: available system RAM.

    Override with SAUTIUM_ACCEL_MEMORY_GB to test another machine's profile
    (e.g. simulate a 16 GB laptop on a 32 GB dev box)."""
    override = os.environ.get("SAUTIUM_ACCEL_MEMORY_GB")
    if override:
        return float(override)
    if torch.cuda.is_available():
        free, _total = torch.cuda.mem_get_info()
        return free / 1e9
    if get_device() == "mps":
        try:
            return torch.mps.recommended_max_memory() / 1e9
        except Exception:
            pass
    import psutil
    return psutil.virtual_memory().available / 1e9


# Cost model for the audio pipeline: peak ≈ FIXED + COST_PER_MIN × batch_minutes.
# MPS calibrated on an M-series node: a full 759-track run at a 10-min budget
# peaked 13 GB, and the default 40-min budget peaks ~21 GB → fixed 10, cost 0.30
# (unified memory holds the whole-track audio buffers too, so the per-minute
# cost is high). CUDA sizes to free VRAM (audio buffers live in system RAM
# there, so the VRAM per-minute cost is low); its budget is capped at the tuned
# 40-min default so the master node is unchanged and only low-VRAM cards shrink.
# CUDA/CPU coefficients are provisional pending calibration.
_BUDGET_PARAMS = {
    "mps":  dict(fixed=10.0, cost_per_min=0.30, min_min=5, max_min=60),
    "cuda": dict(fixed=4.0, cost_per_min=0.12, min_min=5, max_min=40),
    "cpu":  dict(fixed=6.0, cost_per_min=0.30, min_min=5, max_min=40),
}


def audio_batch_budget_seconds() -> int:
    """Per-batch audio-seconds sized to the accelerator's free memory so the
    predicted peak stays under it: big machines get big batches (throughput),
    tight ones get small batches (no swap/OOM). Replaces the static 40/10-min
    split. Force a fixed value with SAUTIUM_AUDIO_BUDGET_MIN (minutes)."""
    override = os.environ.get("SAUTIUM_AUDIO_BUDGET_MIN")
    if override:
        return int(float(override) * 60)
    p = _BUDGET_PARAMS.get(get_device(), _BUDGET_PARAMS["cpu"])
    usable = available_accel_memory_gb() * 0.85 - p["fixed"]
    minutes = usable / p["cost_per_min"] if usable > 0 else 0.0
    minutes = max(p["min_min"], min(p["max_min"], minutes))
    return int(minutes * 60)


# ── One heavy pass at a time ────────────────────────────────────────────────
# The analysis run, the background text half and a stream's analysis each size
# their work as if the accelerator were theirs alone — the audio budget above is
# a share of the whole working set — and on MPS the framework keeps what a
# forward took for itself past its end. Two passes at once share memory neither
# planned for. So a heavy pass holds the accelerator, the others wait their
# turn in the order they asked, a long pass gives way at its batch seams, and
# what a pass took is given back before the next one goes.


class _Lane:
    """The accelerator's heavy passes: the one that holds it, and the tickets
    of those that wait, first first."""

    def __init__(self) -> None:
        self.cv = threading.Condition()
        self.holder: Optional[str] = None
        self.thread: Optional[int] = None
        self.queue: collections.deque = collections.deque()
        self.idle: List[Callable[[], None]] = []
        self.closed = False

    def wait_turn(self, name: str) -> bool:
        """Under cv: wait behind the passes that asked first; False when the
        node stops meanwhile."""
        ticket = object()
        self.queue.append(ticket)
        try:
            while not self.closed and (self.holder is not None or self.queue[0] is not ticket):
                self.cv.wait()
        finally:
            self.queue.remove(ticket)
            self.cv.notify_all()
        if self.closed:
            return False
        self.holder, self.thread = name, threading.get_ident()
        return True

    def mine(self) -> bool:
        with self.cv:
            return self.thread == threading.get_ident()


_lane = _Lane()
_at_end: List[Callable[[], None]] = []


def at_pass_end(fn: Callable[[], None]) -> None:
    """fn() at the end of every heavy pass and at every seam where it gives
    way, before the next one goes: what a pass takes for itself alone is given
    back there."""
    _at_end.append(fn)


@contextlib.contextmanager
def heavy_pass(name: str, *, wait: bool = True):
    """Hold the accelerator for one heavy model pass: True inside once held.
    While a pass holds it or waits for it, the next one waits its turn — or,
    with `wait=False`, does not hold it and is False inside at once. Once the
    node stops (close_heavy_passes) every pass is False inside. The end gives
    back what the pass took (at_pass_end, the allocator's free blocks), then
    frees the accelerator."""
    lane = _lane
    with lane.cv:
        if lane.thread == threading.get_ident():
            raise RuntimeError(f"{name}: this thread holds the accelerator already ({lane.holder})")
        if lane.closed:
            held = False
        elif lane.holder is None and not lane.queue:
            lane.holder, lane.thread = name, threading.get_ident()
            held = True
        elif not wait:
            held = False
        else:
            logger.info("%s waits for the accelerator: %s holds it, %d before it", name,
                        lane.holder or "none", len(lane.queue))
            held = lane.wait_turn(name)
    if not held:
        yield False
        return
    try:
        yield True
    finally:
        # A pass that gave way and found the node stopping holds nothing now
        if lane.mine():
            _leave(lane)


def give_way() -> bool:
    """At a batch seam of the pass that holds the accelerator: the passes
    waiting for it go first — given back what this one took, as at its end —
    and this one holds it again after them. False once the node stops: the
    pass ends there, holding nothing (asked again, it has nothing to give).
    A seam must not hold a write open: a pass that goes meanwhile may wait
    for it, and this one for that pass."""
    lane = _lane
    with lane.cv:
        if lane.thread != threading.get_ident() or (not lane.queue and not lane.closed):
            return not lane.closed
        name = lane.holder
    _give_back()
    with lane.cv:
        lane.holder = lane.thread = None
        lane.cv.notify_all()
        return lane.wait_turn(name)


def _leave(lane: _Lane) -> None:
    try:
        _give_back()
    finally:
        with lane.cv:
            lane.holder = lane.thread = None
            lane.cv.notify_all()
            idle = []
            if not lane.queue:
                idle, lane.idle[:] = lane.idle[:], []
    for fn in idle:
        _call(fn)


def _give_back() -> None:
    for fn in (*_at_end, empty_cache):
        _call(fn)


def _call(fn: Callable[[], None]) -> None:
    # A subscriber's failure is said with its traceback, and neither passed
    # on to the pass that happened to end nor kept from the subscribers after it
    try:
        fn()
    except Exception:
        logger.exception("After a heavy pass, %s failed", getattr(fn, "__qualname__", fn))


def heavy_pass_wanted() -> bool:
    """A pass waits for the accelerator, or the node stops: a pass that
    yields to the others stops at its next batch."""
    with _lane.cv:
        return bool(_lane.queue) or _lane.closed


def call_when_idle(fn: Callable[[], None]) -> None:
    """fn() once no pass holds the accelerator or waits for it: at once if
    none does, else in the thread of the last of them to end — once, however
    often it was asked for meanwhile. Not asked once the node stops."""
    with _lane.cv:
        if _lane.closed:
            return
        if _lane.holder is not None or _lane.queue:
            if fn not in _lane.idle:
                _lane.idle.append(fn)
            return
    _call(fn)


def close_heavy_passes() -> None:
    """The node stops: no heavy pass goes from now on, a waiting one is told
    so (False inside heavy_pass) instead of waiting out the one ahead, and the
    one that holds the accelerator ends at its next seam (give_way)."""
    with _lane.cv:
        _lane.closed = True
        _lane.idle.clear()
        _lane.cv.notify_all()
