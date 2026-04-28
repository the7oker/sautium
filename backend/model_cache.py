"""
Cached ML model access for search queries.

Models stay loaded in GPU/CPU memory for IDLE_TIMEOUT seconds after
last use, then auto-unload to free resources. Prevents expensive
model load/unload cycles on every search request.

Typical first-request latency: 3-10s (model load).
Subsequent requests within TTL: <100ms (model already in memory).
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

IDLE_TIMEOUT = 300  # 5 minutes

_lock = threading.Lock()
_cache: dict[str, dict] = {}
# Per-key Event so concurrent get_model callers wait on a single load
# without holding `_lock` for the entire 30-180s factory call.
_load_events: dict[str, threading.Event] = {}


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
            entry = _cache.get(key)
            if entry is not None:
                entry["last_used"] = time.monotonic()
                return entry["instance"]
            evt = _load_events.get(key)
            if evt is None:
                # We win the race: register an Event and become the loader.
                evt = threading.Event()
                _load_events[key] = evt
                is_loader = True
                break
            # Another thread is already loading — wait below, then retry.
            is_loader = False
        # Outside lock: wait for the in-flight load to finish, then loop.
        evt.wait()

    # `is_loader` path — run factory outside the lock so other threads
    # (is_loaded, cleanup_idle, sibling get_model with different key)
    # are not blocked while the model is loading.
    logger.info(f"Loading model '{key}' into cache (TTL={IDLE_TIMEOUT}s)")
    try:
        instance = factory()
    except BaseException:
        with _lock:
            _load_events.pop(key, None)
        evt.set()  # release waiters (they will retry and hit empty cache → become loader themselves or error out)
        raise

    with _lock:
        _cache[key] = {"instance": instance, "last_used": time.monotonic()}
        _load_events.pop(key, None)
    evt.set()
    return instance


def cleanup_idle():
    """Unload models idle for more than IDLE_TIMEOUT seconds."""
    now = time.monotonic()
    with _lock:
        to_remove = []
        for key, entry in _cache.items():
            if (now - entry["last_used"]) > IDLE_TIMEOUT:
                logger.info(f"Unloading idle model: {key}")
                instance = entry["instance"]
                if hasattr(instance, "unload_model"):
                    instance.unload_model()
                to_remove.append(key)
        for key in to_remove:
            del _cache[key]


def shutdown():
    """Force unload all cached models."""
    with _lock:
        for key, entry in list(_cache.items()):
            logger.info(f"Shutting down model: {key}")
            instance = entry["instance"]
            if hasattr(instance, "unload_model"):
                instance.unload_model()
        _cache.clear()
