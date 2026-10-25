"""Process-wide singleton for CLAP (laion/clap-htsat-unfused) processor + model.

CLAP takes ~3s to load and ~1.5GB on GPU. Multiple subsystems need it:
AudioEmbeddingGenerator (audio embeddings 512d), AudioAnalyzer (zero-shot
mood/vocal/dance classification), and search/discovery handlers. The model
is loaded once on first acquire and kept resident for the process lifetime
— enrichment is bursty and the load/unload cycle leaves PyTorch caching
allocator fragmentation that nvidia-smi reports as growing VRAM.
"""

import logging
import threading
from typing import Tuple

# Top-level import: the transformers lazy-module loader is not thread-safe;
# putting `from transformers import ...` inside get_clap_model would race with
# sibling pre-warms (BGE-M3 via sentence_transformers, which also touches the
# transformers lazy loader) and intermittently fail with ImportError.
from transformers import ClapProcessor, ClapModel

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[Tuple[str, str], Tuple] = {}


def get_clap_model(model_name: str, device: str):
    """Acquire process-wide singleton (ClapProcessor, ClapModel) on device.

    Loads on first call; subsequent calls return the same instance. A load
    holds model_cache._factory_lock: transformers' loading patches
    process-wide state, so no other load of the process runs beside it — the
    search's CLAP factory, which loads this model holding that lock already,
    takes it again — and a caller for the same key waits on it and finds the
    model loaded.
    """
    import model_cache
    key = (model_name, device)
    with _lock:
        entry = _cache.get(key)
    if entry is not None:
        return entry
    with model_cache._factory_lock:
        with _lock:
            entry = _cache.get(key)
        if entry is not None:
            return entry
        from device import get_model_dtype
        dtype = get_model_dtype(device)
        logger.info(f"Loading CLAP model: {model_name} on {device} ({dtype})")
        processor = ClapProcessor.from_pretrained(model_name)
        model = ClapModel.from_pretrained(model_name, torch_dtype=dtype).to(device)
        model.eval()
        if device == "cuda":
            import torch
            mem = torch.cuda.memory_allocated() / 1e9
            logger.info(f"CLAP loaded, GPU memory: {mem:.2f} GB")
        with _lock:
            _cache[key] = (processor, model)
    return processor, model
