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
_load_events: dict[Tuple[str, str], threading.Event] = {}


def get_clap_model(model_name: str, device: str):
    """Acquire process-wide singleton (ClapProcessor, ClapModel) on device.

    Loads on first call; subsequent calls return the same instance. Single-
    flight: concurrent callers for the same key collapse onto one load.
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
        dtype = get_model_dtype(device)
        logger.info(f"Loading CLAP model: {model_name} on {device} ({dtype})")
        processor = ClapProcessor.from_pretrained(model_name)
        model = ClapModel.from_pretrained(model_name, torch_dtype=dtype).to(device)
        model.eval()
        if device == "cuda":
            import torch
            mem = torch.cuda.memory_allocated() / 1e9
            logger.info(f"CLAP loaded, GPU memory: {mem:.2f} GB")
    except BaseException:
        with _lock:
            _load_events.pop(key, None)
        evt.set()
        raise

    with _lock:
        _cache[key] = (processor, model)
        _load_events.pop(key, None)
    evt.set()
    return processor, model
