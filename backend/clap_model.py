"""Process-wide shared cache for CLAP (laion/clap-htsat-unfused) processor + model.

CLAP takes ~3s to load and ~1.5GB on GPU. Multiple subsystems need it:
AudioEmbeddingGenerator (audio embeddings 512d) and AudioAnalyzer (zero-shot
mood/vocal/dance classification). Without sharing, the enrichment Phase 1
GPU pipeline holds its own copy while model_cache also keeps a pre-warmed
one (~3GB total). Refcounting + single-flight loading mirrors the
text_embedder pattern.
"""

import contextlib
import logging
import threading
from typing import Tuple

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[Tuple[str, str], dict] = {}
_load_events: dict[Tuple[str, str], threading.Event] = {}


def get_clap_model(model_name: str, device: str):
    """Acquire shared (ClapProcessor, ClapModel) on device; loads on first call.

    Single-flight: concurrent callers for the same key collapse onto one load.
    """
    key = (model_name, device)
    while True:
        with _lock:
            entry = _cache.get(key)
            if entry is not None:
                entry["refcount"] += 1
                return entry["processor"], entry["model"]
            evt = _load_events.get(key)
            if evt is None:
                evt = threading.Event()
                _load_events[key] = evt
                is_loader = True
                break
            is_loader = False
        evt.wait()

    try:
        from transformers import ClapProcessor, ClapModel
        logger.info(f"Loading CLAP model: {model_name} on {device}")
        processor = ClapProcessor.from_pretrained(model_name)
        model = ClapModel.from_pretrained(model_name).to(device)
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
        _cache[key] = {"processor": processor, "model": model, "refcount": 1}
        _load_events.pop(key, None)
    evt.set()
    return processor, model


def release_clap_model(model_name: str, device: str) -> None:
    """Release a previously acquired CLAP; unload when refcount hits zero."""
    key = (model_name, device)
    to_drop = None
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            return
        entry["refcount"] -= 1
        if entry["refcount"] > 0:
            return
        to_drop = (entry["processor"], entry["model"])
        del _cache[key]

    del to_drop
    if device == "cuda":
        import torch
        torch.cuda.empty_cache()
    logger.info(f"CLAP model unloaded: {model_name}")


@contextlib.contextmanager
def shared_clap_model(model_name: str, device: str):
    """Hold a refcount across a block so child acquires/releases don't free the model."""
    get_clap_model(model_name, device)
    try:
        yield
    finally:
        release_clap_model(model_name, device)
