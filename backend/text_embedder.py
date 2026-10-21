"""Process-wide singleton for sentence-transformers text embedding models.

BGE-M3 takes ~5s to load and ~2GB on GPU. Multiple subsystems need it:
TextEmbeddingGenerator (track metadata), LyricsEmbeddingGenerator (track
lyrics), _BaseEnrichmentGenerator (artist bios, genre descriptions), and
search/discovery handlers (query encoding). The model
is loaded once on first acquire and kept resident for the process lifetime
— PyTorch's caching allocator does not reliably return memory to the GPU
driver across load/unload cycles, which surfaced as growing nvidia-smi
VRAM with each enrichment trigger.
"""

import logging
import threading
from typing import List, Optional, Set, Tuple

import numpy as np

# Top-level import: sentence_transformers transitively touches the transformers
# lazy-module loader, which is not thread-safe. Lazy import here would race
# with sibling CLAP pre-warm (`from transformers import ClapProcessor`) and
# intermittently fail with ImportError.
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[Tuple[str, str], object] = {}
_load_events: dict[Tuple[str, str], threading.Event] = {}


def get_text_embedder(model_name: str, device: str):
    """Acquire process-wide singleton SentenceTransformer.

    Loads on first call; subsequent calls return the same instance. Single-
    flight: concurrent callers for the same key collapse onto one load. The
    model load runs OUTSIDE the lock so unrelated keys are not blocked while
    a multi-second cold load is in flight.
    """
    key = (model_name, device)
    while True:
        with _lock:
            entry = _cache.get(key)
            if entry is not None:
                return entry
            evt = _load_events.get(key)
            if evt is None:
                evt = threading.Event()
                _load_events[key] = evt
                break
        evt.wait()

    try:
        from device import get_model_dtype
        import torch
        dtype = get_model_dtype(device)
        logger.info(f"Loading text embedding model: {model_name} on {device} ({dtype})")
        # Load directly at the tier dtype (historically fp32-always on bf16
        # tiers — +1.1 GB VRAM; then briefly load-fp32-then-convert, which
        # left the freed fp32 weights parked in the caching allocator as
        # ~2.3 GB of reserved-but-idle VRAM until an empty_cache). ST >=5.x
        # casts bf16 back to float32 before the numpy conversion in
        # encode(), so callers see identical output dtype.
        load_kwargs = {}
        if dtype in (torch.float16, torch.bfloat16):
            load_kwargs["model_kwargs"] = {"torch_dtype": dtype}
        model = SentenceTransformer(model_name, device=device, **load_kwargs)
        logger.info(f"Text embedding model loaded ({dtype})")
    except BaseException:
        with _lock:
            _load_events.pop(key, None)
        evt.set()
        raise

    with _lock:
        _cache[key] = model
        _load_events.pop(key, None)
    evt.set()
    return model


class OutOfMemory(RuntimeError):
    """The device is full for the text encoder: a text of a few tokens did not
    fit either. The pass stops there, and what it did not embed is selected
    again by the next one."""


# Tried alone after a text that did not fit alone: whether the device can take
# a text at all tells a text too long for what it has free from a full device —
# never a text of the batch, whose fit depends on the batch (a pass whose
# first batch held only long texts stopped there on every pass)
_CANARY = "a"


def encode(model, texts: List[str], *, batch_size: int,
           device: str) -> Tuple[List[Optional[np.ndarray]], Set[int]]:
    """`texts` embedded and normalized, as every pass of the text encoder
    asks, and the indices of the texts left out (None in their place). Out
    of device memory, the device's cache is freed and the texts go one at a
    time: one that does not fit alone is left out — too long for what the
    device has free; its row fails, the rest are written — unless a text of
    a few tokens does not fit either: a full device, OutOfMemory, which
    every later batch would meet the same (one error line each, 314 in one
    pass on 2026-10-18, which then said it was done)."""
    from device import empty_cache
    vectors, full = _encoded(model, texts, batch_size)
    if full is None:
        return list(vectors), set()
    empty_cache(device)
    if len(texts) > 1:
        said = (device, batch_size) in _one_at_a_time
        _one_at_a_time.add((device, batch_size))
        (logger.debug if said else logger.warning)(
            "Text encoder out of memory on %s at %d texts a batch: such a batch goes one text at "
            "a time", device, batch_size)
    out: List[Optional[np.ndarray]] = [None] * len(texts)
    left_out: Set[int] = set()
    for i, text in enumerate(texts):
        if len(texts) > 1:          # a batch of one was its text alone
            one, _ = _encoded(model, [text], 1)
            if one is not None:
                out[i] = one[0]
                continue
            empty_cache(device)
        _, full = _encoded(model, [_CANARY], 1)
        if full is not None:
            empty_cache(device)
            raise OutOfMemory(f"out of memory on {device}: {full}")
        left_out.add(i)
        logger.debug("A text of %d characters does not fit on %s alone: left out", len(text),
                     device)
    return out, left_out


def _encoded(model, texts: List[str], batch_size: int) -> Tuple[Optional[np.ndarray], Optional[str]]:
    """(the vectors, None), or (None, why) when the device ran out of memory.
    Only the message is kept: the exception's traceback holds the tensors the
    try allocated, which the retry needs freed."""
    from device import out_of_memory
    try:
        return model.encode(texts, batch_size=batch_size, show_progress_bar=False,
                            normalize_embeddings=True), None
    except Exception as e:
        if not out_of_memory(e):
            raise
        return None, str(e)


# (device, batch size) whose fall-back to one text at a time was said: once a
# process, every batch of a pass may need it
_one_at_a_time: set = set()
