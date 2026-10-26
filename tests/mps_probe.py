"""The text encoder on the real MPS, measured: the texts, the memory reading and
the one scenario that needs a process of its own (tests/test_mps_encoder.py).

The allocator reads PYTORCH_MPS_*_WATERMARK_RATIO when it first starts, so a
limit lowered for a test is lowered in a child: `python tests/mps_probe.py
refusal`, run with the environment of lowered_limit(), which prints one JSON
line.
"""

import ctypes
import gc
import json
import math
import os
import random
import sys
import weakref
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

MODEL = "BAAI/bge-m3"
GIB = 1024 ** 3
# Where SDPA was refused on a 24 GB Mac in 23 shapes (2026-10-26): half of its
# working set
REFUSAL_LIMIT_GIB = 8.88
# Every word is one token for the encoder's tokenizer
# (test_a_text_is_as_many_tokens_as_it_says)
VOCABULARY = (
    "the of and a to in is was that for it with as his her on be at by this had not are but from or "
    "have an they which one you were all we when there can more if no out so what up their about who "
    "get would make time first new also after band album released music group songs record tour live "
    "song debut guitar piano studio label early years later members member played career style sound "
    "number chart single work works world year three two second third final major local young great "
    "large small long high public national popular original modern").split()


def text(tokens: int, rng: random.Random) -> str:
    """A text of `tokens` tokens: a word a token, and two for the ends."""
    return " ".join(rng.choices(VOCABULARY, k=tokens - 2))


def single_text_limit(model) -> int:
    """The longest text, in tokens, that `model` takes alone in a forward on
    this machine: a longer one goes to the CPU copy by design, which is no
    refusal (text_embedder._forwards)."""
    import device
    import text_embedder
    heads = model[0].auto_model.config.num_attention_heads
    share = (device.available_accel_memory_gb() * 1e9 * text_embedder._ATTENTION_SHARE
             / (text_embedder._BYTES_PER_SCORE * heads))
    return min(math.isqrt(int(share)), model.max_seq_length)


def floor() -> int:
    """What the process's Metal memory holds beside the tensors in use, once
    PyTorch's cache is given back: the frameworks' own, which nothing but the
    end of the process returns."""
    import torch
    torch.mps.synchronize()
    torch.mps.empty_cache()
    torch.mps.synchronize()
    return torch.mps.driver_allocated_memory() - torch.mps.current_allocated_memory()


class _MallocStatistics(ctypes.Structure):
    _fields_ = [("blocks_in_use", ctypes.c_uint), ("size_in_use", ctypes.c_size_t),
                ("max_size_in_use", ctypes.c_size_t), ("size_allocated", ctypes.c_size_t)]


def live_malloc() -> dict:
    """What this process holds in malloc, live: the allocations and bytes of every
    zone (malloc_zone_statistics; macOS). It is the host side of what MPS keeps per
    shape — the graphs of a forward's ops, 4 to 7 MB and some 60,000 allocations a
    shape, never given back — whatever the kernel does with the pages: footprint's
    Malloc Small follows those, and a graph can land on pages a pass left free.
    Exact: `vmmap --summary` prints the same bytes to one decimal of its unit,
    which from 1 GiB up is steps of 100 MB. On this macOS the default zone holds
    every size class; where nano malloc is in front of it, the blocks of up to 256
    bytes — most of a graph's — are in a zone of their own, which only the sum has."""
    libc = ctypes.CDLL(None)
    libc.malloc_zone_statistics.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MallocStatistics)]
    statistics = _MallocStatistics()
    libc.malloc_zone_statistics(None, ctypes.byref(statistics))
    return {"count": statistics.blocks_in_use, "mb": statistics.size_in_use / 1024 ** 2}


class Shapes:
    """The (rows, tokens) of every forward of a sentence-transformers model,
    through a hook on its Transformer module (sentence-transformers 5.5 calls
    that module's forward directly, so a hook on the Hugging Face model under
    it never fires)."""

    def __init__(self, model):
        self.seen = []
        self._hook = model[0].register_forward_pre_hook(
            lambda module, args: self.seen.append(tuple(args[0]["input_ids"].shape)))

    def remove(self) -> None:
        self._hook.remove()


def cached(repo: str, filename: str = "config.json") -> bool:
    """Whether the hub's cache holds `repo`: the tests load what is on the machine."""
    from huggingface_hub import try_to_load_from_cache
    return isinstance(try_to_load_from_cache(repo, filename), str)


def count_cpu_copies(patch) -> list:
    """The names the encoder asks its CPU copy for, from here on: text_embedder's
    _cpu_copy, counted through the pytest.MonkeyPatch given, the real one called."""
    import text_embedder
    taken, taken_by = [], text_embedder._cpu_copy
    patch.setattr(text_embedder, "_cpu_copy", lambda name: taken.append(name) or taken_by(name))
    return taken


class Held:
    """A model whose only reference is this one. A fixture's value stays
    referenced until its teardown has run, and a sentence-transformers model
    and its model card refer to each other, a cycle only a full collection
    frees (text_embedder._end_pass parts them for the CPU copy): `release`
    parts them, drops the model, collects what is left of it and gives
    PyTorch's cache back."""

    def __init__(self, model):
        self.model = model

    def release(self) -> None:
        import device
        model, self.model = self.model, None
        card = getattr(model, "model_card_data", None)
        if card is not None:
            card.model = None
        gone = weakref.ref(model)
        del model, card
        if gone() is not None:
            gc.collect()
        device.empty_cache("mps")


def lowered_limit() -> dict:
    """The environment of a child whose allocator refuses early: the limit at
    0.5 of the working set, or at REFUSAL_LIMIT_GIB where that is less. The
    child's pass is a fixed 12 calls, and what SDPA pins in them levels off as
    the machine grows (bigger forwards, fewer shapes) while half the working
    set does not: a replay of the old code's pins puts it short of half the
    working set on a 64 GB Mac. The fixed code, with such a machine's share
    emulated, peaked at ~3 GiB of Metal memory, far under the limit."""
    import torch
    high = min(0.5, REFUSAL_LIMIT_GIB * GIB / torch.mps.recommended_max_memory())
    return {"PYTORCH_MPS_HIGH_WATERMARK_RATIO": f"{high:.4f}",
            "PYTORCH_MPS_LOW_WATERMARK_RATIO": f"{0.8 * high:.4f}"}


def refusal() -> dict:
    """A pass of bio-sized chunks under the allocator's limit lowered
    (lowered_limit, in this process's environment): on a 24 GB Mac SDPA took it
    past the limit in 23 shapes (2026-10-26), every allocation was refused from
    then on — 256 bytes included — and the encoder went to its CPU copy. The chunks are kept
    within what this machine's encoder takes alone, so the CPU copy taken at
    all is a refusal, not a text over the share. What the pass meets: how often
    the device refused, and how many times the CPU copy was taken."""
    import torch
    import text_embedder

    limit = int(float(os.environ["PYTORCH_MPS_HIGH_WATERMARK_RATIO"])
                * torch.mps.recommended_max_memory())
    held = []
    try:            # in pieces past the lowered limit: refused, or the limit is not in force
        for _ in range(limit // GIB + 3):
            held.append(torch.empty(GIB, dtype=torch.uint8, device="mps"))
        in_force = False
    except RuntimeError as e:
        in_force = "out of memory" in str(e)
    # Its pieces' 1 GiB heaps, left in PyTorch's cache, would take the model's
    # weights in, and their slack could not be given back while those live
    held.clear()
    torch.mps.empty_cache()

    refused, copies = [], []
    refused_by, copy_by = text_embedder._refused, text_embedder._cpu_copy
    text_embedder._refused = lambda device, why: refused.append(why) or refused_by(device, why)
    text_embedder._cpu_copy = lambda name: copies.append(name) or copy_by(name)
    model = text_embedder._load(MODEL, "mps", local_files_only=True)
    longest = min(1730, int(0.9 * single_text_limit(model)))
    rng = random.Random(0)
    left = 0
    shapes = Shapes(model)
    for _ in range(12):             # a call of the pass: 16 chunks, 49 tokens up
        texts = [text(min(longest, max(49, int(rng.lognormvariate(math.log(470), 0.8)))), rng)
                 for _ in range(16)]
        left += len(text_embedder.encode(model, texts, batch_size=16, device="mps", name=MODEL)[1])
    seen = set(shapes.seen)
    return {"limit_in_force": in_force, "attention": model[0].auto_model.config._attn_implementation,
            "shapes": len(seen), "refusals": len(refused), "cpu_copies": len(copies),
            "left_out": left, "floor_gib": round(floor() / GIB, 2)}


if __name__ == "__main__":
    print(json.dumps(globals()[sys.argv[1]]()))
