"""Process-wide singleton for sentence-transformers text embedding models.

BGE-M3 takes ~5s to load and ~2GB on GPU. Multiple subsystems need it:
TextEmbeddingGenerator (track metadata), LyricsEmbeddingGenerator (track
lyrics), _BaseEnrichmentGenerator (artist bios, genre descriptions), and
search/discovery handlers (query encoding). The model
is loaded once on first acquire and kept resident for the process lifetime
— PyTorch's caching allocator does not reliably return memory to the GPU
driver across load/unload cycles, which surfaced as growing nvidia-smi
VRAM with each enrichment trigger.

On MPS the model loads with eager attention (device.attn_implementation), and
a pass also takes a copy of the model on the CPU for the texts whose attention
the device should not materialise or that it refuses (encode), and gives it
back at the pass's end (device.at_pass_end).
"""

import gc
import logging
import threading
import weakref
from typing import List, Optional, Set, Tuple

import numpy as np

# Top-level import: sentence_transformers transitively touches the transformers
# lazy-module loader, which is not thread-safe. Lazy import here would race
# with sibling CLAP pre-warm (`from transformers import ClapProcessor`) and
# intermittently fail with ImportError.
from sentence_transformers import SentenceTransformer

from device import at_pass_end

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[Tuple[str, str], object] = {}


def get_text_embedder(model_name: str, device: str):
    """Acquire process-wide singleton SentenceTransformer.

    Loads on first call; subsequent calls return the same instance. A load
    holds model_cache._factory_lock: transformers' loading patches
    process-wide state, so no other load of the process runs beside it — a
    search's factory, which loads this model holding that lock already,
    takes it again — and a caller for the same key waits on it and finds
    the model loaded.
    """
    import model_cache
    key = (model_name, device)
    with _lock:
        model = _cache.get(key)
    if model is not None:
        return model
    with model_cache._factory_lock:
        with _lock:
            model = _cache.get(key)
        if model is None:
            model = _load(model_name, device)
            with _lock:
                _cache[key] = model
    return model


def _load(model_name: str, device: str, **kwargs):
    """The model loaded, its caller holding model_cache._factory_lock."""
    from device import get_model_dtype
    dtype = get_model_dtype(device)
    logger.info(f"Loading text embedding model: {model_name} on {device} ({dtype})")
    model = SentenceTransformer(model_name, device=device,
                                model_kwargs=_model_kwargs(device, dtype) or None, **kwargs)
    logger.info(f"Text embedding model loaded ({dtype})")
    return model


def _model_kwargs(device: str, dtype) -> dict:
    """The model_kwargs of a load on `device` at `dtype`: the tier dtype where
    it is a half — loaded directly at it (historically fp32-always on bf16
    tiers — +1.1 GB VRAM; then briefly load-fp32-then-convert, which left the
    freed fp32 weights parked in the caching allocator as ~2.3 GB of
    reserved-but-idle VRAM until an empty_cache). ST >=5.x casts bf16 back to
    float32 before the numpy conversion in encode(), so callers see identical
    output dtype — and the device's attention (device.attn_implementation)."""
    import torch
    from device import attn_implementation
    kwargs = {}
    if dtype in (torch.float16, torch.bfloat16):
        kwargs["torch_dtype"] = dtype
    attention = attn_implementation(device)
    if attention:
        kwargs["attn_implementation"] = attention
    return kwargs


class OutOfMemory(RuntimeError):
    """The device is full for the text encoder: a text of a few tokens did not
    fit either. The pass stops there — on MPS only when the CPU is full too —
    and what it did not embed is selected again by the next one."""


# Tried alone after a text that did not fit alone: whether the device can take
# a text at all tells a text too long for what it has free from a full device —
# never a text of the batch, whose fit depends on the batch (a pass whose
# first batch held only long texts stopped there on every pass)
_CANARY = "a"

# Where the attention is eager (device.attn_implementation: MPS) a forward's
# B x H x L x L scores are PyTorch tensors, ~8 bytes a score while it runs (7.3
# to 8.6 measured at 268M to 806M scores), and they stay in PyTorch's cache
# until empty_cache (a tensor of 512 MiB or more in a heap of its own). A
# forward is cut to a share of the device's working set, and a text whose
# attention exceeds it alone goes to the CPU, whose attention streams (flash).
# The cost charged a score is the generous 40 it has always been, not the 8: at
# 10 a pass was slower (its bigger groups pad more: 0.13 -> 0.21 s a chunk)
# and, without the empty_cache after each forward, the cache grew to 12 GiB
# inside one pass.
_ATTENTION_SHARE = 1 / 8        # of the device's working set, for one forward
_BYTES_PER_SCORE = 40


def encode(model, texts: List[str], *, batch_size: int, device: str,
           name: str) -> Tuple[List[Optional[np.ndarray]], Set[int]]:
    """`texts` embedded and normalized, as every pass of the text encoder
    asks, and the indices of the texts left out (None in their place). On a
    device that materialises its attention (eager: MPS), the texts go in
    forwards within its share and each forward's cache is given back, and to
    `name` on the CPU go a text over the share alone, a text the device
    cannot take alone, and every text after it refused a text of a few
    tokens. Out of memory, a forward's texts go one at a time (_encode_on)."""
    from device import attn_implementation, empty_cache
    if attn_implementation(device) != "eager":
        return _encode_on(model, texts, batch_size, device)
    out: List[Optional[np.ndarray]] = [None] * len(texts)
    forwards, cpu = _forwards(model, texts, batch_size)
    for n, group in enumerate(forwards):
        try:
            vectors, left = _encode_on(model, [texts[i] for i in group], len(group), device)
        except OutOfMemory as e:
            # its words: a log handler that keeps its records would keep the
            # error, and through its frames the CPU copy this call loads below
            _refused(device, str(e))
            cpu += [i for later in forwards[n:] for i in later]
            break
        # The forward's scores are in PyTorch's cache now, none in use: across
        # forwards of different sizes that cache grew to 12 GiB inside one pass
        # at a relaxed share. Given back where the forward ends
        empty_cache(device)
        _took_now.add(device)
        for j, i in enumerate(group):
            out[i] = vectors[j]
        cpu += [group[j] for j in sorted(left)]
    if not cpu:
        return out, set()
    logger.debug("%d of %d texts on the CPU: over %s's share alone, or refused by it", len(cpu),
                 len(texts), device)
    # One at a time, each its own try: these are the call's longest texts — a
    # padded batch of them builds its mask at texts x length² (XLM-R's
    # attention path) — and one the CPU cannot take costs none before it
    model = _cpu_copy(name)
    left_out: Set[int] = set()
    for i in cpu:
        vectors, left = _encode_on(model, [texts[i]], 1, "cpu")
        out[i] = vectors[0]
        if left:
            left_out.add(i)
    return out, left_out


# The devices whose refusal was said, until a pass encodes on them without
# one: a device that stays full refuses every pass, and one line says so
_refusing: set = set()
# The devices the pass under way encoded on, and those that refused it
_took_now: set = set()
_refused_now: set = set()


def _refused(device: str, why: str) -> None:
    _refused_now.add(device)
    if device not in _refusing:
        _refusing.add(device)
        logger.warning("Text encoder: %s refused a text of a few tokens (%s) — what it refuses "
                       "is encoded on the CPU", device, why)


def _forwards(model, texts: List[str], batch_size: int) -> Tuple[List[List[int]], List[int]]:
    """The texts' forwards on a device that materialises its attention — groups
    of indices, the longest first, each padded to its first text's length and
    within the share — and the texts whose attention alone exceeds it."""
    from device import available_accel_memory_gb
    # Tokenized as sentence-transformers does it: a call with other settings
    # would change the tokenizer's, which another thread's search may be
    # using that moment ("Already borrowed")
    lengths = [int(n) for n in model.preprocess(texts)["attention_mask"].sum(1)]
    heads = model[0].auto_model.config.num_attention_heads
    # a forward's texts times its padded length squared
    share = available_accel_memory_gb() * 1e9 * _ATTENTION_SHARE / (_BYTES_PER_SCORE * heads)
    forwards: List[List[int]] = []
    too_long: List[int] = []
    for i in sorted(range(len(texts)), key=lambda i: -lengths[i]):
        if lengths[i] ** 2 > share:
            too_long.append(i)
        elif (forwards and len(forwards[-1]) < batch_size
              and (len(forwards[-1]) + 1) * lengths[forwards[-1][0]] ** 2 <= share):
            forwards[-1].append(i)
        else:
            forwards.append([i])
    return forwards, too_long


def _encode_on(model, texts: List[str], batch_size: int,
               device: str) -> Tuple[List[Optional[np.ndarray]], Set[int]]:
    """`texts` on `model`, which runs on `device`. Out of device memory, the
    device's cache is freed and the texts go one at a time: one that does not
    fit alone is left out — too long for what the device has free; its row
    fails, the rest are written — unless a text of a few tokens does not fit
    either: a full device, OutOfMemory, which every later batch would meet the
    same (one error line each, 314 in one pass on 2026-10-18, which then said
    it was done)."""
    from device import empty_cache
    vectors, full = _encoded(model, texts, batch_size)
    if full is None:
        return list(vectors), set()
    empty_cache(device)
    if len(texts) > 1:
        said = device in _one_at_a_time
        _one_at_a_time.add(device)
        (logger.debug if said else logger.warning)(
            "Text encoder out of memory on %s at %d texts a batch: such a batch goes one text at "
            "a time", device, batch_size)
    out: List[Optional[np.ndarray]] = [None] * len(texts)
    left_out: Set[int] = set()
    for i, text in enumerate(texts):
        if len(texts) > 1:          # a batch of one was its text alone
            one, _ = _encoded(model, [text], 1)
            if one is not None:
                out[i] = one[0]
                continue
            empty_cache(device)
        _, full = _encoded(model, [_CANARY], 1)
        if full is not None:
            empty_cache(device)
            raise OutOfMemory(f"out of memory on {device}: {full}")
        left_out.add(i)
        logger.debug("A text of %d characters does not fit on %s alone", len(text), device)
    return out, left_out


def _encoded(model, texts: List[str], batch_size: int) -> Tuple[Optional[np.ndarray], Optional[str]]:
    """(the vectors, None), or (None, why) when the device ran out of memory.
    Only the message is kept: the exception's traceback holds the tensors the
    try allocated, which the retry needs freed."""
    from device import out_of_memory
    try:
        return model.encode(texts, batch_size=batch_size, show_progress_bar=False,
                            normalize_embeddings=True), None
    except Exception as e:
        said = out_of_memory(e)
        if said is None:
            raise
        return None, said


# The devices whose fall-back to one text at a time was said: once a process,
# every batch of a pass may need it
_one_at_a_time: set = set()

_cpu_lock = threading.Lock()
_cpu: Optional[Tuple[str, object]] = None     # (model name, the model on the CPU)


def _cpu_copy(name: str):
    """The encoder `name` on the CPU for what the accelerator should not hold:
    loaded at the pass's first such text, given back at its end. A CPU with no
    memory for it is a full device: OutOfMemory, not a load tried every batch."""
    import model_cache
    from device import out_of_memory
    global _cpu
    with _cpu_lock:
        if _cpu is None or _cpu[0] != name:
            try:
                # The device's model's own snapshot (no hub check every pass),
                # loaded as every load of the process: one at a time
                with model_cache._factory_lock:
                    _cpu = (name, _load(name, "cpu", local_files_only=True))
            except Exception as e:
                said = out_of_memory(e)
                if said is None:
                    raise
                raise OutOfMemory(f"out of memory loading {name} on the cpu: {said}") from None
        return _cpu[1]


def _end_pass() -> None:
    """A heavy pass ends or gives way: the CPU copy is given back, and the
    refusal of a device the pass encoded on without one is forgotten."""
    global _cpu
    with _cpu_lock:
        dropped, _cpu = _cpu, None
    _refusing.difference_update(_took_now - _refused_now)
    _took_now.clear()
    _refused_now.clear()
    if dropped is None:
        return
    name, model = dropped
    del dropped
    # sentence-transformers' model and its model card refer to each other:
    # parted, the model goes with its last reference; a cycle of another kind
    # waits for no full collection either
    card = getattr(model, "model_card_data", None)
    if card is not None:
        card.model = None
    gone = weakref.ref(model)
    del model
    if gone() is not None:
        gc.collect()
    logger.info("Text embedding model on the CPU released: %s", name)


at_pass_end(_end_pass)
