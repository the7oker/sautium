"""The text encoder on the real MPS, measured: the texts, the memory reading and
the one scenario that needs a process of its own (tests/test_mps_encoder.py).

The allocator reads PYTORCH_MPS_*_WATERMARK_RATIO when it first starts, so a
limit lowered for a test is lowered in a child: `python tests/mps_probe.py
refusal`, which prints one JSON line.
"""

import json
import math
import random
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

MODEL = "BAAI/bge-m3"
GIB = 1024 ** 3
# Every word is one token for the encoder's tokenizer
VOCABULARY = (
    "the of and a to in is was that for it with as his her on be at by this had not are but from or "
    "have an they which one you were all we when there can more if no out so what up their about who "
    "get would make time first new also after band album released music formed group songs record "
    "tour live song debut singer guitar bass drums piano vocals studio label early years later "
    "members member played career style sound influenced critics success number chart charts single "
    "singles featured work works world year three two second third final major local young great "
    "large small long high public national popular original modern").split()


def text(tokens: int, rng: random.Random) -> str:
    """A text of `tokens` tokens: a word a token, and two for the ends."""
    return " ".join(rng.choices(VOCABULARY, k=tokens - 2))


def floor() -> int:
    """What the process's Metal memory holds beside the tensors in use, once
    PyTorch's cache is given back: the frameworks' own, which nothing but the
    end of the process returns."""
    import torch
    torch.mps.synchronize()
    torch.mps.empty_cache()
    torch.mps.synchronize()
    return torch.mps.driver_allocated_memory() - torch.mps.current_allocated_memory()


def cached(repo: str, filename: str = "config.json") -> bool:
    """Whether the hub's cache holds `repo`: the tests load what is on the machine."""
    from huggingface_hub import try_to_load_from_cache
    return isinstance(try_to_load_from_cache(repo, filename), str)


def refusal() -> dict:
    """The allocator's limit lowered to 0.5 of the working set (8.88 GiB on a
    24 GB Mac, for 30.19) and a pass of bio-sized chunks, which SDPA took past
    it in 23 shapes (2026-10-26): every allocation was refused from then on —
    256 bytes included — and the encoder went to its CPU copy. What the pass
    meets: how often the device refused, and how many times the CPU copy was
    taken."""
    import torch
    import text_embedder

    limit = int(0.5 * torch.mps.recommended_max_memory())
    held = []
    try:            # in pieces past the lowered limit: refused, or the limit is not in force
        for _ in range(limit // GIB + 3):
            held.append(torch.empty(GIB, dtype=torch.uint8, device="mps"))
        in_force = False
    except RuntimeError as e:
        in_force = "out of memory" in str(e)
    held.clear()

    refused, copies = [], []
    refused_by, copy_by = text_embedder._refused, text_embedder._cpu_copy
    text_embedder._refused = lambda device, why: refused.append(why) or refused_by(device, why)
    text_embedder._cpu_copy = lambda name: copies.append(name) or copy_by(name)
    model = text_embedder._load(MODEL, "mps", local_files_only=True)
    rng = random.Random(0)
    left = 0
    seen = set()
    model[0].register_forward_pre_hook(lambda module, args: seen.add(tuple(args[0]["input_ids"].shape)))
    for _ in range(12):             # a call of the pass: 16 chunks, 49 to 1730 tokens
        texts = [text(min(1730, max(49, int(rng.lognormvariate(math.log(470), 0.8)))), rng)
                 for _ in range(16)]
        left += len(text_embedder.encode(model, texts, batch_size=16, device="mps", name=MODEL)[1])
    return {"limit_in_force": in_force, "attention": model[0].auto_model.config._attn_implementation,
            "shapes": len(seen), "refusals": len(refused), "cpu_copies": len(copies),
            "left_out": left, "floor_gib": round(floor() / GIB, 2)}


if __name__ == "__main__":
    print(json.dumps(globals()[sys.argv[1]]()))
