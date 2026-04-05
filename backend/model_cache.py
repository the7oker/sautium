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


def get_model(key: str, factory):
    """Get or create a cached model instance.

    Args:
        key: Cache key (e.g., "clap", "lyrics", "enrichment")
        factory: Callable that returns a loaded model instance.
                 Called only on cache miss.
    """
    with _lock:
        entry = _cache.get(key)
        if entry is not None:
            entry["last_used"] = time.monotonic()
            return entry["instance"]

    # Load outside lock is risky (double-load), but model loading is
    # idempotent and rare. Keep lock held to avoid double-load.
    with _lock:
        # Double-check after re-acquiring
        entry = _cache.get(key)
        if entry is not None:
            entry["last_used"] = time.monotonic()
            return entry["instance"]

        logger.info(f"Loading model '{key}' into cache (TTL={IDLE_TIMEOUT}s)")
        instance = factory()
        _cache[key] = {"instance": instance, "last_used": time.monotonic()}
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
