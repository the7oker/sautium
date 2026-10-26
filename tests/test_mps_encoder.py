"""What the real MPS keeps. test_text_embedding_passes.py's encoder is a fake;
here it is the model, on the device, and the device's own account of its
memory. The encoder and AST meet a new attention shape nearly every forward,
and nothing may be kept per shape: SDPA there kept 8 bytes a score for each of
them until the process ended — 32 GiB after four batches of bios on
2026-10-25 — and past torch's limit the process could allocate nothing on the
device.

Skipped where there is no MPS, or none of the models in the hub's cache. Every
reading is of `driver - current` once PyTorch's cache is given back
(mps_probe.floor), set against the same process earlier in the test: what
other tests left on the device does not count. A pass that grows by a GiB is
stopped there, which keeps these tests cheap, and safe, on a build whose
attention keeps what it takes. What they load they give back (mps_probe.Held,
text_embedder._end_pass): the models are gigabytes, and the module's globals
are the rest of the session's.
"""

import json
import os
import random
import subprocess
import sys
import types
from pathlib import Path

import numpy as np
import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import mps_probe  # noqa: E402
from mps_probe import GIB, MODEL  # noqa: E402

MIB = 1024 ** 2


def _mps() -> bool:
    import torch
    return getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available()


pytestmark = pytest.mark.skipif(not _mps(), reason="needs MPS")


@pytest.fixture(scope="module")
def encoder():
    """The text encoder as the node loads it on MPS."""
    if not mps_probe.cached(MODEL):
        pytest.skip(f"{MODEL} is not in the hub's cache")
    import text_embedder
    held = mps_probe.Held(text_embedder._load(MODEL, "mps", local_files_only=True))
    yield held
    held.release()


def test_the_encoder_runs_eager_attention_on_mps(encoder):
    assert encoder.model[0].auto_model.config._attn_implementation == "eager"


def test_a_text_is_as_many_tokens_as_it_says(encoder):
    """The lengths of these tests, and what a machine sends to the CPU copy,
    are in tokens of this encoder."""
    rng = random.Random(1)
    for tokens in (3, 20, 100, 700, 1730):
        text = mps_probe.text(tokens, rng)
        assert int(encoder.model.preprocess([text])["attention_mask"].sum()) == tokens


@pytest.fixture(scope="module")
def a_pass_of_new_shapes(encoder):
    """A pass whose forwards are new shapes nearly every time: 40 calls of 16
    texts of 20 tokens up, at the share of a machine of 2 GB (the forwards are
    a few texts, as on a smaller Mac). Every text is within what the encoder
    takes alone there, so none goes to the CPU copy: the pass is the device's
    alone. What it read after each call: the memory beyond tensors in use as
    the call left it, and once the cache is given back — set against the
    start."""
    import torch
    import text_embedder
    patch = pytest.MonkeyPatch()
    patch.setenv("SAUTIUM_ACCEL_MEMORY_GB", "2")
    copies = mps_probe.count_cpu_copies(patch)
    shapes = set()

    def met(module, args):
        features = args[0]
        shapes.add((tuple(features["input_ids"].shape), bool((features["attention_mask"] == 0).any())))

    hook = encoder.model[0].register_forward_pre_hook(met)
    longest = int(0.95 * mps_probe.single_text_limit(encoder.model))
    start = mps_probe.floor()
    rng = random.Random(3)
    left, floors, at_ten, stopped = [], [], None, None
    try:
        for call in range(40):
            texts = [mps_probe.text(rng.randint(20, longest), rng) for _ in range(16)]
            text_embedder.encode(encoder.model, texts, batch_size=16, device="mps", name=MODEL)
            left.append(torch.mps.driver_allocated_memory() - torch.mps.current_allocated_memory()
                        - start)
            floors.append(mps_probe.floor() - start)
            if at_ten is None and len(shapes) >= 10:
                at_ten = floors[-1]
            if floors[-1] > GIB:
                stopped = f"{floors[-1] / GIB:.2f} GiB kept after {len(shapes)} shapes, call {call}"
                break
    finally:
        hook.remove()
        patch.undo()
        text_embedder._end_pass()
    return types.SimpleNamespace(shapes=len(shapes), left=left, floors=floors, at_ten=at_ten,
                                 stopped=stopped, cpu_copies=len(copies))


def test_a_pass_of_new_shapes_keeps_nothing_per_shape(a_pass_of_new_shapes):
    """The invariant: after 150 shapes the device holds what it held after 10."""
    p = a_pass_of_new_shapes
    assert p.stopped is None, p.stopped
    assert p.cpu_copies == 0
    assert p.shapes >= 150, p.shapes
    assert p.floors[-1] - p.at_ten <= 64 * MIB, (p.floors[-1] - p.at_ten) / MIB
    assert max(p.floors) <= 256 * MIB, max(p.floors) / MIB


def test_the_cache_is_given_back_between_forwards(a_pass_of_new_shapes):
    """Without the encoder's empty_cache after each forward the cache of a
    pass stands at 1 GiB and more between them."""
    p = a_pass_of_new_shapes
    assert p.stopped is None, p.stopped
    assert max(p.left) <= 256 * MIB, max(p.left) / MIB


def test_the_vectors_are_the_cpus_to_within_bf16(encoder):
    """CPU fp32 is the reference: the node's CPU copy encodes in it. Texts of
    49 to 1730 tokens, those this machine's encoder takes alone, each in its
    forward; and six of up to 393 in one padded forward."""
    import text_embedder
    from sentence_transformers import SentenceTransformer
    limit = mps_probe.single_text_limit(encoder.model)
    rng = random.Random(5)
    texts = [mps_probe.text(n, rng) for n in (49, 142, 193, 267, 325, 393, 650, 954, 1414, 1730)
             if n <= limit]
    assert len(texts) >= 8, limit
    patch = pytest.MonkeyPatch()
    copies = mps_probe.count_cpu_copies(patch)
    cpu = mps_probe.Held(SentenceTransformer(MODEL, device="cpu", local_files_only=True))
    try:
        reference = np.concatenate([cpu.model.encode([t], batch_size=1, normalize_embeddings=True,
                                                     show_progress_bar=False) for t in texts])

        def cosines(vectors, rows):
            got = np.stack(vectors)
            return (got * reference[rows]).sum(1) / (np.linalg.norm(got, axis=1)
                                                     * np.linalg.norm(reference[rows], axis=1))

        alone, left_out = text_embedder.encode(encoder.model, texts, batch_size=1, device="mps",
                                               name=MODEL)
        assert left_out == set()
        assert cosines(alone, slice(None)).min() >= 0.998
        padded, left_out = text_embedder.encode(encoder.model, texts[:6], batch_size=16,
                                                device="mps", name=MODEL)
        assert left_out == set()
        assert cosines(padded, slice(0, 6)).min() >= 0.998
        assert copies == []         # else some of this compared the CPU with itself
    finally:
        patch.undo()
        text_embedder._end_pass()
        cpu.release()


@pytest.fixture(scope="module")
def instruments():
    """AST and PaSST as the analysis run loads them."""
    import torch
    import ensemble_instruments
    checkpoints = Path(torch.hub.get_dir()) / "checkpoints"
    if not (mps_probe.cached(ensemble_instruments.InstrumentEnsembleTagger.AST_MODEL_NAME)
            and any(checkpoints.glob("passt*.pt"))):
        pytest.skip("AST or PaSST is not on this machine")
    tagger = ensemble_instruments.InstrumentEnsembleTagger(device="mps")
    tagger.load()
    yield tagger
    tagger.unload()


def test_ast_keeps_nothing_per_batch_size(instruments):
    """tag_full_track's batches are 12 windows and a tail of 1 to 11: SDPA
    kept 0.14 GB a window for each size (1.7 GB for the 12; 11 GB over the
    twelve, added up)."""
    assert instruments.ast.config._attn_implementation == "eager"
    rng = np.random.default_rng(0)
    base = mps_probe.floor()
    floors = []
    for windows in range(1, 13):
        instruments._ast_probs([rng.standard_normal(16000 * 10).astype(np.float32) * 0.05
                                for _ in range(windows)])
        floors.append(mps_probe.floor() - base)
        assert floors[-1] <= GIB, f"{floors[-1] / GIB:.2f} GiB kept after batches of 1 to {windows}"
    assert floors[-1] - floors[0] <= 64 * MIB, (floors[-1] - floors[0]) / MIB


def test_ast_scores_are_the_cpus_to_within_bf16(instruments):
    """The scores are sealed and shared. Over these windows eager moved the
    instrument classes' probabilities by 0.0015 at most from SDPA's on MPS, less
    than MPS is from the CPU's fp32 (0.0031 for eager and for SDPA alike; over
    all 527 classes 0.0154 and 0.0130)."""
    import torch
    from transformers import ASTForAudioClassification
    rate, t = 16000, np.arange(160000) / 16000
    rng = np.random.default_rng(0)
    stack = lambda f, k: sum(np.sin(2 * np.pi * f * (i + 1) * t) / (i + 1) ** 1.3 for i in range(k)) * 0.2
    windows = [rng.standard_normal(t.size) * 0.05,
               np.cumsum(rng.standard_normal(t.size)) * 0.0005,
               stack(220, 6) * np.exp(-t * 0.25),
               np.sin(2 * np.pi * (100 * 60 ** (t / 10)) * t / 2) * 0.3,
               np.sign(np.sin(2 * np.pi * 440 * t)) * 0.25,
               stack(330, 4) * 0.7 + rng.standard_normal(t.size) * 0.02]
    windows = [np.clip(w, -1, 1).astype(np.float32) for w in windows]
    mine = instruments._ast_probs(windows)
    cpu = ASTForAudioClassification.from_pretrained(instruments.AST_MODEL_NAME,
                                                    torch_dtype=torch.float32).eval()
    with torch.no_grad():
        inputs = instruments.ast_extractor(windows, sampling_rate=rate, return_tensors="pt")
        reference = torch.sigmoid(cpu(**inputs).logits).numpy()
    diff = np.abs(mine - reference)
    assert diff[:, instruments.instrument_ids].max() <= 0.01, diff[:, instruments.instrument_ids].max()
    assert diff.max() <= 0.03, diff.max()


def test_a_pass_of_bio_chunks_under_the_allocators_limit_lowered_is_not_refused():
    """The failure itself, at a scale the machine takes: the limit at 0.5 of
    the working set (the allocator reads it when it starts, hence a child),
    where SDPA was refused in the 23rd shape — 256 bytes too — and the
    encoder went to its CPU copy for good. The chunks are within what the
    machine's encoder takes alone, so the copy taken at all is a refusal."""
    if not mps_probe.cached(MODEL):
        pytest.skip(f"{MODEL} is not in the hub's cache")
    env = {**os.environ, "PYTORCH_MPS_HIGH_WATERMARK_RATIO": "0.5",
           "PYTORCH_MPS_LOW_WATERMARK_RATIO": "0.4", "HF_HUB_OFFLINE": "1"}
    env.pop("SAUTIUM_ACCEL_MEMORY_GB", None)        # the machine's own working set
    done = subprocess.run([sys.executable, mps_probe.__file__, "refusal"], env=env,
                          capture_output=True, text=True, timeout=900)
    assert done.returncode == 0, done.stderr[-2000:]
    got = json.loads(done.stdout.strip().splitlines()[-1])
    assert got["limit_in_force"], got       # else this passes for want of a limit
    assert got["attention"] == "eager"
    assert (got["refusals"], got["cpu_copies"], got["left_out"]) == (0, 0, 0), got
