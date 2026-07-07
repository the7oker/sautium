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
