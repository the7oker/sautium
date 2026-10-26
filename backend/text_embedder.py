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
the device should not materialize or that it refuses (encode), and gives it
back at the pass's end (device.at_pass_end). Every forward there runs a shape
of a closed set — a rung of lengths, a power of two of texts (_rungs), and every
caller, the search queries too, goes through encode.
"""

import gc
import logging
import math
import threading
import weakref
from bisect import bisect_left
from typing import Dict, List, Optional, Set, Tuple

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
                                model_kwargs=_model_kwargs(device, dtype), **kwargs)
    logger.info(f"Text embedding model loaded ({dtype})")
    return model


def _model_kwargs(device: str, dtype) -> dict:
    """The model_kwargs of a load on `device` at `dtype`. The tier dtype where
    it is a half: loaded directly at it (historically fp32-always on bf16
    tiers — +1.1 GB VRAM; then briefly load-fp32-then-convert, which left the
    freed fp32 weights parked in the caching allocator as ~2.3 GB of
    reserved-but-idle VRAM until an empty_cache); ST >=5.x casts bf16 back to
    float32 before the numpy conversion in encode(), so callers see identical
    output dtype. And the device's attention (device.attn_implementation)."""
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

# MPS builds a forward's B x H x L x L attention scores in device memory,
# whichever attention it runs (SDPA's graph, eager's tensors: the device's, not
# the implementation's). Eager they are PyTorch tensors, ~8 bytes a score while
# the forward runs (7.3 to 8.6 measured at 268M to 806M scores), and they stay
# in PyTorch's cache until empty_cache (a tensor of 512 MiB or more in a heap
# of its own). A forward is cut to a share of the device's working set, and a
# text whose attention exceeds it alone goes to the CPU, whose attention
# streams (flash). The cost charged a score is the generous 40 it has always
# been, not the 8: at 10 a pass was slower (its bigger groups pad more: 0.13 ->
# 0.21 s a chunk) and, without the empty_cache after each forward, the cache
# grew to 12 GiB inside one pass.
_MATERIALIZES_ATTENTION = {"mps"}
_ATTENTION_SHARE = 1 / 8        # of the device's working set, for one forward
_BYTES_PER_SCORE = 40

# MPS also keeps, for every (batch, length) a forward has had, the graphs of
# its ops on the HOST heap: 4 to 7 MB and some 60,000 allocations a shape for
# this encoder (torch 2.12, measured 2026-10-26), none of it ever given back —
# torch has no way to evict that cache, and empty_cache does not touch it. A
# forward padded to its own longest text is a new shape at nearly every turn:
# 1,746 shapes, ~10.8 GB, in 28 passes of a node. So a forward on MPS has only
# a shape of a closed set: its length a rung of the ladder (_rungs: a text is
# padded up to the first rung at or above its own, and the attention mask
# leaves its vector as it was, to bf16), its batch a power of two. The ladder
# ends at the lesser of the longest text the share admits alone, which the
# machine decides, and the longest the encoder takes at all: at most 39 shapes
# at the working set of a 24 GB Mac and a batch of 16, ~0.3 GB for good (62 on
# a 64 GB Mac). A rung's texts are of one length, where a forward used to pack
# a short text after a long one, so a pass is no slower for it: 10 % faster (a
# pass of 500 bios: 55 s against 61). Steps of 128 tokens were no faster, and
# steps of 192, a ladder fitted to a node's lengths in ten rungs, and one of
# nine coarse rungs were 8, 11 and 19 % slower (measured).
_FIRST_RUNGS = (64, 128, 256)
_RUNG_STEP = 160                # tokens, from the last of those up to the top


def encode(model, texts: List[str], *, batch_size: int, device: str, name: str,
           cpu_copy: bool = True) -> Tuple[List[Optional[np.ndarray]], Set[int]]:
    """`texts` embedded and normalized, as every pass of the text encoder
    asks, and the indices of the texts left out (None in their place). On a
    device that materializes its attention (MPS), the texts go in forwards
    of the closed set of shapes (_rungs) within its share and each
    forward's cache is given back, and to `name` on the CPU go a text over the
    share alone, a text the device cannot take alone, and every text after it
    refused a text of a few tokens. Out of memory, a forward's texts go one at
    a time (_encode_on). Without `cpu_copy` what would go to the CPU is
    OutOfMemory instead: a pass gives the copy back at its end, a search is no
    pass."""
    from device import empty_cache
    if device not in _MATERIALIZES_ATTENTION:
        return _encode_on(model, texts, batch_size, device)
    out: List[Optional[np.ndarray]] = [None] * len(texts)
    forwards, cpu = _forwards(model, texts, batch_size)
    for n, (rung, group) in enumerate(forwards):
        try:
            vectors, left = _encode_on(model, [texts[i] for i in group], len(group), device, rung)
        except OutOfMemory as e:
            if not cpu_copy:
                raise
            # its words: a log handler that keeps its records would keep the
            # error, and through its frames the CPU copy this call loads below
            _refused(device, str(e))
            cpu += [i for _, later in forwards[n:] for i in later]
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
    if not cpu_copy:
        raise OutOfMemory(f"{len(cpu)} of {len(texts)} texts are over what {device} takes alone")
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


def encode_query(model, text: str, *, device: str, name: str) -> np.ndarray:
    """A search query's normalized vector, through the forwards of a pass's
    texts: on MPS a query is a shape of the closed set like any other, not one
    more for the host to keep until the process ends. Never through the CPU copy
    — a search runs between passes, and what it loaded would stay, 2.3 GB, until
    the next pass ends: a query the device cannot take fails, as it did."""
    vectors, left_out = encode(model, [text], batch_size=1, device=device, name=name,
                               cpu_copy=False)
    if left_out:
        raise OutOfMemory(f"a query of {len(text)} characters does not fit on {device} alone")
    return vectors[0]


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


def _rungs(share: float, longest: int) -> Tuple[int, ...]:
    """The lengths a forward may have on this machine: _FIRST_RUNGS, then steps
    of _RUNG_STEP tokens, up to the longest text the share admits alone, and not
    past `longest`, what the encoder takes at all (its max_seq_length: the
    tokenizer cuts every text there, and a text padded beyond its position table
    does not run) — the last rung, which the machine decides: a text between the
    last step and it is padded to it and runs alone (as every text there did,
    more or less)."""
    top = min(math.isqrt(int(share)), longest)
    rungs = [r for r in _FIRST_RUNGS if r < top]
    rung = _FIRST_RUNGS[-1] + _RUNG_STEP
    while rung < top:
        rungs.append(rung)
        rung += _RUNG_STEP
    return tuple(rungs) + (top,)


def _forwards(model, texts: List[str],
              batch_size: int) -> Tuple[List[Tuple[int, List[int]]], List[int]]:
    """The texts' forwards on a device that materializes its attention —
    (rung, indices), the longest rung first: the texts of a rung in forwards of
    a power of two of them, as many as the batch size and the share allow, a
    remainder split (11 = 8 + 2 + 1) so that no forward needs a row of nothing —
    and the texts whose attention alone exceeds the share."""
    from device import available_accel_memory_gb
    # Tokenized as sentence-transformers does it: a call with other settings
    # would change the tokenizer's, which another thread's search may be
    # using that moment ("Already borrowed")
    lengths = [int(n) for n in model.preprocess(texts)["attention_mask"].sum(1)]
    heads = model[0].auto_model.config.num_attention_heads
    # a forward's texts times its padded length squared
    share = available_accel_memory_gb() * 1e9 * _ATTENTION_SHARE / (_BYTES_PER_SCORE * heads)
    rungs = _rungs(share, model.max_seq_length)
    by_rung: Dict[int, List[int]] = {}
    too_long: List[int] = []
    for i in sorted(range(len(texts)), key=lambda i: -lengths[i]):
        if lengths[i] > rungs[-1]:
            too_long.append(i)
        else:
            by_rung.setdefault(rungs[bisect_left(rungs, lengths[i])], []).append(i)
    forwards: List[Tuple[int, List[int]]] = []
    for rung in sorted(by_rung, reverse=True):
        left = by_rung[rung]
        cap = _power_of_two_within(min(batch_size, int(share // rung ** 2)))
        while left:
            size = min(cap, _power_of_two_within(len(left)))
            forwards.append((rung, left[:size]))
            left = left[size:]
    return forwards, too_long


def _power_of_two_within(n: int) -> int:
    return 1 << (n.bit_length() - 1)


def _encode_on(model, texts: List[str], batch_size: int, device: str,
               rung: Optional[int] = None) -> Tuple[List[Optional[np.ndarray]], Set[int]]:
    """`texts` on `model`, which runs on `device`, padded to `rung` tokens when
    it is given (the closed set of MPS: every try below, the text of a few
    tokens too, is a shape of it). Out of device memory, the device's cache is
    freed and the texts go one at a time: one that does not fit alone is left
    out — too long for what the device has free; its row fails, the rest are
    written — unless a text of a few tokens does not fit either: a full
    device, OutOfMemory, which every later batch would meet the same (one
    error line each, 314 in one pass on 2026-10-18, which then said it was
    done)."""
    from device import empty_cache
    vectors, full = _encoded(model, texts, batch_size, rung)
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
    canary_rung = None if rung is None else min(_FIRST_RUNGS[0], rung)      # the smallest rung
    for i, text in enumerate(texts):
        if len(texts) > 1:          # a batch of one was its text alone
            one, _ = _encoded(model, [text], 1, rung)
            if one is not None:
                out[i] = one[0]
                continue
            empty_cache(device)
        _, full = _encoded(model, [_CANARY], 1, canary_rung)
        if full is not None:
            empty_cache(device)
            raise OutOfMemory(f"out of memory on {device}: {full}")
        left_out.add(i)
        logger.debug("A text of %d characters does not fit on %s alone", len(text), device)
    return out, left_out


def _encoded(model, texts: List[str], batch_size: int,
             rung: Optional[int] = None) -> Tuple[Optional[np.ndarray], Optional[str]]:
    """(the vectors, None), or (None, why) when the device ran out of memory.
    Only the message is kept: the exception's traceback holds the tensors the
    try allocated, which the retry needs freed."""
    from device import out_of_memory
    try:
        if rung is not None:
            return _padded(model, texts, rung), None
        return model.encode(texts, batch_size=batch_size, show_progress_bar=False,
                            normalize_embeddings=True), None
    except Exception as e:
        said = out_of_memory(e)
        if said is None:
            raise
        return None, said


def _padded(model, texts: List[str], rung: int) -> np.ndarray:
    """`texts` in ONE forward of len(texts) x `rung` tokens: what
    sentence-transformers' encode does for a batch, with the batch padded to
    the rung after the tokenizer. The tokenizer keeps its default settings
    for every call of every thread — a pass's and a search's meet on this one,
    and a per-call padding length would change it under the other."""
    import torch
    import torch.nn.functional as F
    with torch.inference_mode():
        features = model.preprocess(texts)
        pad = rung - features["input_ids"].shape[1]
        if pad < 0:         # cropped, not refused, by pad(): the vectors would be of fewer tokens
            raise ValueError(f"a text of {features['input_ids'].shape[1]} tokens is over its rung, {rung}")
        padding = {"input_ids": model.tokenizer.pad_token_id}
        features = {k: F.pad(v, (0, pad), value=padding.get(k, 0)) if torch.is_tensor(v) and v.dim() == 2
                    else v for k, v in features.items()}
        features = {k: v.to(model.device) if torch.is_tensor(v) else v for k, v in features.items()}
        model.eval()
        vectors = F.normalize(model(features)["sentence_embedding"], p=2, dim=1)
        return vectors.cpu().float().numpy()


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
