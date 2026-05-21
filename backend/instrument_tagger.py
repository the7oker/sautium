"""Process-wide shared cache for the AST+PaSST instrument ensemble tagger.

AST+PaSST together take several seconds to load and ~2GB on GPU. The
tagger is wrapped by InstrumentEnsembleTagger which exposes load/unload
+ tag(). Multiple AudioAnalyzer instances can coexist (Phase 1 GPU
pipeline + TrackEnrichmentPipeline._get_audio_analyzer + CLI), each of
which would otherwise build its own ensemble. Refcount + single-flight
matches the text_embedder / clap_model pattern.
"""

import contextlib
import logging
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[str, dict] = {}
_load_events: dict[str, threading.Event] = {}


def get_instrument_tagger(device: str):
    """Acquire shared InstrumentEnsembleTagger on device; loads on first call."""
    key = device
    while True:
        with _lock:
            entry = _cache.get(key)
            if entry is not None:
                entry["refcount"] += 1
                return entry["tagger"]
            evt = _load_events.get(key)
            if evt is None:
                evt = threading.Event()
                _load_events[key] = evt
                is_loader = True
                break
            is_loader = False
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
        _cache[key] = {"tagger": tagger, "refcount": 1}
        _load_events.pop(key, None)
    evt.set()
    return tagger


def release_instrument_tagger(device: str) -> None:
    """Release shared tagger; unload AST+PaSST when refcount hits zero."""
    key = device
    to_drop = None
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            return
        entry["refcount"] -= 1
        if entry["refcount"] > 0:
            return
        to_drop = entry["tagger"]
        del _cache[key]

    if to_drop is not None:
        to_drop.unload()


@contextlib.contextmanager
def shared_instrument_tagger(device: str):
    """Hold a refcount across a block so inner acquires/releases don't free the tagger."""
    get_instrument_tagger(device)
    try:
        yield
    finally:
        release_instrument_tagger(device)
