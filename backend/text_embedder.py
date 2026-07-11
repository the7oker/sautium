"""Process-wide singleton for sentence-transformers text embedding models.

BGE-M3 takes ~5s to load and ~2GB on GPU. Multiple subsystems need it:
TextEmbeddingGenerator (track metadata), LyricsEmbeddingGenerator (track
lyrics), _BaseEnrichmentGenerator (artist bios, album info, genre
descriptions), and search/discovery handlers (query encoding). The model
is loaded once on first acquire and kept resident for the process lifetime
— PyTorch's caching allocator does not reliably return memory to the GPU
driver across load/unload cycles, which surfaced as growing nvidia-smi
VRAM with each enrichment trigger.
"""

import logging
import threading
from typing import Tuple

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
