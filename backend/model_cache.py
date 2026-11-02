"""
Cached ML model access for search queries.

Wraps the underlying singleton model caches (clap_model, text_embedder,
instrument_tagger) with per-generator wrappers used by search/discovery.
The underlying models are process-wide singletons — never unloaded — so
this cache is effectively a process-lifetime wrapper registry.

Typical first-request latency: 3-10s (cold model load).
Subsequent requests: <100ms (singleton already resident).
"""

import contextlib
import logging
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[str, object] = {}
# Per-key Event so concurrent get_model callers wait on a single load
# without holding `_lock` for the entire 30-180s factory call.
_load_events: dict[str, threading.Event] = {}
# ALL factory() calls serialize on this lock: HuggingFace loading machinery
# (accelerate's init_empty_weights meta-device context under the default
# low_cpu_mem_usage path) mutates process-global state and is not
# thread-safe — two concurrent from_pretrained calls can hand one loader
# meta tensors ("Cannot copy out of meta tensor" on .to(device)). The
# passes' loads take it too — the text encoder's (a pass's CPU copy among
# them), CLAP's, the instrument pair's — and a factory that loads one of
# them takes it again: re-entrant.
_factory_lock = threading.RLock()
# The startup pre-warm chain (main.lifespan), from its first load to its
# last — the gaps between two loads included.
_warming = False

# The commit each Hugging Face model loads at. A model's id in the database
# and on the network is uuid5 of its name alone (uuid_utils.embedding_model_id),
# so the commit is what that id means: moving one is a new model — a new
# identity, migrated as one — never an edit of its line here.
HUB_REVISIONS = {
    "laion/clap-htsat-unfused": "8fa0f1c6d0433df6e97c127f64b2a1d6c0dcda8a",
    "BAAI/bge-m3": "5617a9f61b028005a4858fdac845db406aefb181",
    "MIT/ast-finetuned-audioset-10-10-0.4593": "f826b80d28226b62986cc218e5cec390b1096902",
    "google/madlad400-3b-mt": "fa184c675da0b5c9e1c8694fccd4e12e2d422094",
    "Nextcloud-AI/madlad400-3b-mt-ct2-int8": "aa32bbdeba7880eff2096ec044cb155a340a9400",
}


def from_hub(load, name: str, **kwargs):
    """`load(name, …)` — a from_pretrained, or a constructor that takes the
    same `revision` and `local_files_only` — at the model's pinned commit:
    from the cache alone once it holds every file that commit needs, from
    the Hub otherwise (a first start, a download that broke off). A model
    with no pinned commit is refused (KeyError)."""
    revision = HUB_REVISIONS[name]
    try:
        return load(name, revision=revision, local_files_only=True, **kwargs)
    except OSError:
        logger.info("%s at %s is not in the cache whole: fetching it", name, revision[:8])
        return load(name, revision=revision, **kwargs)


@contextlib.contextmanager
def warming_up():
    global _warming
    _warming = True
    try:
        yield
    finally:
        _warming = False


def loading() -> bool:
    """A model loads in this process now, or the startup chain is still
    going: CPU, disk and memory this machine's other work shares — a
    benchmark of an HQPlayer here refuses to start meanwhile."""
    with _lock:
        return _warming or bool(_load_events)


def is_loaded(key: str) -> bool:
    """Non-blocking check: is the model already in cache?

    Used by latency-sensitive endpoints (e.g., Discovery search) to
    decide whether to dispatch a query against the model or report
    `status: "loading"` and let the user proceed with results from
    non-ML blocks immediately. Acquires `_lock` only briefly — never
    blocks on a model load.
    """
    with _lock:
        return key in _cache


def is_loading(key: str) -> bool:
    """True iff a `get_model` / `kick_load` is currently running for this key."""
    with _lock:
        return key in _load_events


def kick_load(key: str, factory) -> None:
    """Schedule a background model load if not already loaded/loading.

    Returns immediately — does NOT block on the load. Coordinates
    with `get_model` via the shared `_load_events` registry, so a
    foreground `get_model` call already in flight is not duplicated.
    """
    with _lock:
        if key in _cache or key in _load_events:
            return

    def _bg_load():
        try:
            get_model(key, factory)
            logger.info(f"Background model load complete: '{key}'")
        except Exception as e:
            logger.error(f"Background model load failed for '{key}': {e}")

    threading.Thread(
        target=_bg_load, daemon=True, name=f"model-load-{key}",
    ).start()


def get_model(key: str, factory):
    """Get or create a cached model instance.

    Single-flight: concurrent callers for the same key collapse onto
    a single `factory()` call. The factory runs OUTSIDE `_lock` so
    `is_loaded()` and other lock-protected operations stay non-
    blocking for the entire model-load duration (30-180s for ML
    models). Coordination uses a per-key Event registered under
    `_load_events` while the load is in flight.

    Args:
        key: Cache key (e.g., "clap", "lyrics", "enrichment")
        factory: Callable that returns a loaded model instance.
                 Called only on cache miss.
    """
    while True:
        with _lock:
            instance = _cache.get(key)
            if instance is not None:
                return instance
            evt = _load_events.get(key)
            if evt is None:
                evt = threading.Event()
                _load_events[key] = evt
                break
        evt.wait()

    logger.info(f"Loading model '{key}' into cache")
    try:
        with _factory_lock:
            instance = factory()
    except BaseException:
        with _lock:
            _load_events.pop(key, None)
        evt.set()
        raise

    with _lock:
        _cache[key] = instance
        _load_events.pop(key, None)
    evt.set()
    return instance


def shutdown():
    """Clear the wrapper registry on process shutdown.

    Underlying singleton models are owned by clap_model / text_embedder /
    instrument_tagger and live for the process lifetime; this just drops
    the wrapper references.
    """
    with _lock:
        _cache.clear()
