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

import contextlib
import logging
import functools
import os
from typing import Optional

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
