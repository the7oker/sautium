"""Process-wide shared cache for sentence-transformers text embedding models.

BGE-M3 takes ~5s to load and ~2GB on GPU. Multiple subsystems need it:
TextEmbeddingGenerator (track metadata), LyricsEmbeddingGenerator (track
lyrics), _BaseEnrichmentGenerator (artist bios, album info, genre
descriptions), and search/discovery handlers (query encoding). Without
sharing, the enrichment pipeline reloads the model three times in
sequence and search/discovery hold two distinct copies in GPU memory.

Refcounting: each acquire pairs with a release; the model is unloaded
only when the refcount drops to zero. Callers that span multiple
sub-steps (e.g. enrichment Phase 2) can use `shared_text_embedder` to
hold an outer reference so refcount never dips to zero between steps.
"""

import contextlib
import logging
import threading
from typing import Tuple

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[Tuple[str, str], dict] = {}
# Per-key Event so concurrent get_text_embedder callers wait on a single
# load instead of each loading a duplicate copy (~2.3GB BGE-M3 each).
_load_events: dict[Tuple[str, str], threading.Event] = {}


def get_text_embedder(model_name: str, device: str):
    """Acquire shared SentenceTransformer; loads on first call.

    Single-flight: concurrent callers for the same key collapse onto one
    load. The model load runs OUTSIDE the lock so unrelated keys are not
    blocked while a multi-second cold load is in flight.
    """
    key = (model_name, device)
    while True:
        with _lock:
            entry = _cache.get(key)
            if entry is not None:
                entry["refcount"] += 1
                return entry["model"]
            evt = _load_events.get(key)
            if evt is None:
                evt = threading.Event()
                _load_events[key] = evt
                is_loader = True
                break
            is_loader = False
        evt.wait()

    try:
        from sentence_transformers import SentenceTransformer
        logger.info(f"Loading text embedding model: {model_name} on {device}")
        model = SentenceTransformer(model_name, device=device)
        logger.info("Text embedding model loaded")
    except BaseException:
        with _lock:
            _load_events.pop(key, None)
        evt.set()
        raise

    with _lock:
        _cache[key] = {"model": model, "refcount": 1}
        _load_events.pop(key, None)
    evt.set()
    return model


def release_text_embedder(model_name: str, device: str) -> None:
    """Release a previously acquired embedder; unload when refcount hits zero."""
    key = (model_name, device)
    model_to_drop = None
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            return
        entry["refcount"] -= 1
        if entry["refcount"] > 0:
            return
        model_to_drop = entry["model"]
        del _cache[key]

    del model_to_drop
    if device == "cuda":
        import torch
        torch.cuda.empty_cache()
    logger.info(f"Text embedding model unloaded: {model_name}")


@contextlib.contextmanager
def shared_text_embedder(model_name: str, device: str):
    """Hold a refcount across a block so child acquires/releases don't free the model."""
    get_text_embedder(model_name, device)
    try:
        yield
    finally:
        release_text_embedder(model_name, device)
