"""Process-wide singleton for the AST+PaSST instrument ensemble tagger.

AST+PaSST together take several seconds to load and ~2GB on GPU. The
tagger is wrapped by InstrumentEnsembleTagger which exposes load() +
tag(). The tagger is loaded once on first acquire and kept resident for
the process lifetime — enrichment may run many times during a session and
load/unload cycles leave PyTorch caching allocator fragmentation that
nvidia-smi reports as growing VRAM.
"""

import logging
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[str, object] = {}
_load_events: dict[str, threading.Event] = {}


def get_instrument_tagger(device: str):
    """Acquire process-wide singleton InstrumentEnsembleTagger on device.

    Loads on first call; subsequent calls return the same instance. Single-
    flight: concurrent callers for the same device collapse onto one load.
    """
    key = device
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
        from ensemble_instruments import InstrumentEnsembleTagger
        tagger = InstrumentEnsembleTagger(device=device)
        tagger.load()
    except BaseException:
        with _lock:
            _load_events.pop(key, None)
        evt.set()
        raise

    with _lock:
        _cache[key] = tagger
        _load_events.pop(key, None)
    evt.set()
    return tagger


def release_instrument_tagger(device: str) -> None:
    """Drop the singleton and free its weights (AST+PaSST, ~0.5-2GB).

    Used on the standard/lite profiles after a bulk enrichment run and on
    trickle-mode idle — machines where the resident pair costs more than
    the reload latency. The next get_instrument_tagger() reloads. No-op if
    not loaded; never called on the full profile (keeps re-runs instant,
    and repeated load/unload cycles fragment the caching allocator)."""
    with _lock:
        tagger = _cache.pop(device, None)
    if tagger is not None:
        tagger.unload()
