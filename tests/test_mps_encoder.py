"""What the real MPS keeps. test_text_embedding_passes.py's encoder is a fake;
here it is the model, on the device, and the device's own account of its
memory. MPS keeps something for every shape a forward has had, and nothing
gives it back until the process ends: SDPA's graphs held 8 bytes a score on
the Metal side — 32 GiB after four batches of bios on 2026-10-25, and past
torch's limit the process could allocate nothing on the device — and every
attention leaves graphs of its ops on the host heap, 4 to 7 MB and some 60,000
allocations a shape (10.8 GB over the 1,746 shapes of 28 passes of a node,
2026-10-26). So the text encoder meets a closed set of shapes, and these tests
read both sides: the Metal side as driver - current, the host's as the live
malloc of the process (mps_probe.live_malloc).

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


SHARE_GB = "2"          # a machine's working set whose share makes the forwards a few texts


def _closed_set(share_gb: float) -> set:
    """The shapes the encoder may have at a working set of `share_gb`: a rung
    of its ladder, a power of two of texts of up to 16 that the share admits."""
    import text_embedder
    share = share_gb * 1e9 * text_embedder._ATTENTION_SHARE / (text_embedder._BYTES_PER_SCORE * 16)
    return {(rows, rung) for rung in text_embedder._rungs(share)
            for rows in (1, 2, 4, 8, 16) if rows * rung ** 2 <= share}


@pytest.fixture(scope="module")
def a_run_of_fresh_lengths(encoder):
    """A pass's kind of work at a machine of 2 GB's share (the forwards are a
    few texts, as on a smaller Mac): 40 calls of 16 texts of lengths nobody has
    met, 20 tokens up to what the encoder takes alone there, so none goes to
    the CPU copy. What it read: every forward's shape (a hook); after each call
    the Metal memory beyond tensors in use, as the call left it and once the
    cache is given back, against the start; and at four calls the live malloc
    of the process — the host's share of what MPS keeps."""
    import torch
    import text_embedder
    patch = pytest.MonkeyPatch()
    shapes = mps_probe.Shapes(encoder.model)
    left, floors, host, at_ten, stopped = [], [], [], None, None
    try:
        patch.setenv("SAUTIUM_ACCEL_MEMORY_GB", SHARE_GB)
        copies = mps_probe.count_cpu_copies(patch)
        longest = int(0.95 * mps_probe.single_text_limit(encoder.model))
        start = mps_probe.floor()
        rng = random.Random(3)
        for call in range(40):
            texts = [mps_probe.text(rng.randint(20, longest), rng) for _ in range(16)]
            text_embedder.encode(encoder.model, texts, batch_size=16, device="mps", name=MODEL)
            left.append(torch.mps.driver_allocated_memory() - torch.mps.current_allocated_memory()
                        - start)
            floors.append(mps_probe.floor() - start)
            if at_ten is None and len(set(shapes.seen)) >= 10:
                at_ten = floors[-1]
            if call in (0, 13, 26, 39):
                host.append((call + 1, set(shapes.seen), mps_probe.live_malloc()["mb"]))
            if floors[-1] > GIB:
                stopped = f"{floors[-1] / GIB:.2f} GiB kept after {len(set(shapes.seen))} shapes, call {call}"
                break
    finally:
        shapes.remove()
        patch.undo()
        text_embedder._end_pass()
    return types.SimpleNamespace(shapes=set(shapes.seen), left=left, floors=floors, at_ten=at_ten,
                                 stopped=stopped, cpu_copies=len(copies), host=host)


def test_the_forwards_of_a_run_of_fresh_lengths_are_the_closed_set(a_run_of_fresh_lengths):
    """Every forward is a rung of the ladder by a power of two of texts, and the
    run met most of them: fresh lengths are not fresh shapes."""
    p = a_run_of_fresh_lengths
    assert p.stopped is None, p.stopped
    assert p.cpu_copies == 0
    reachable = _closed_set(float(SHARE_GB))
    assert p.shapes <= reachable, sorted(p.shapes - reachable)
    assert len(p.shapes) >= len(reachable) // 2, (len(p.shapes), len(reachable))


def test_the_host_keeps_nothing_more_once_the_set_is_warm(a_run_of_fresh_lengths):
    """At most the graphs of the shapes of the set that the run had not met
    yet (12 MB each is above the 7 measured) and a little else: between any two
    of the checkpoints, and from the start to the end. It is the invariant, not
    a count of what happened: forwards of lengths that make shapes of their own
    grow the host by 50 MB a call here."""
    p = a_run_of_fresh_lengths
    assert p.stopped is None, p.stopped
    reachable = len(_closed_set(float(SHARE_GB)))
    for (_, met, before), (_, _, after) in zip(p.host, p.host[1:]):
        assert after - before <= 12 * (reachable - len(met)) + 20, (before, after, len(met))
    assert p.host[-1][2] - p.host[0][2] <= 12 * reachable + 20, p.host


def test_the_metal_side_keeps_nothing_per_shape_either(a_run_of_fresh_lengths):
    """The invariant: after the shapes of the whole run the device holds what it
    held after 10."""
    p = a_run_of_fresh_lengths
    assert p.stopped is None, p.stopped
    assert p.cpu_copies == 0
    assert len(p.shapes) >= 10, len(p.shapes)
    assert p.floors[-1] - p.at_ten <= 64 * MIB, (p.floors[-1] - p.at_ten) / MIB
    assert max(p.floors) <= 256 * MIB, max(p.floors) / MIB


def test_the_cache_is_given_back_between_forwards(a_run_of_fresh_lengths):
    """Without the encoder's empty_cache after each forward the cache of a
    pass stands at 1 GiB and more between them."""
    p = a_run_of_fresh_lengths
    assert p.stopped is None, p.stopped
    assert max(p.left) <= 256 * MIB, max(p.left) / MIB


def test_queries_of_every_length_are_one_shape_and_the_callers_meet_the_set(encoder, monkeypatch):
    """A search query is a forward of the set like a pass's text: 1 to 64 tokens
    are one shape, (1, 64), and the three callers that encode a query — the bio
    search, the lyrics search, the track-text search — reach the encoder through
    the same function, so the host keeps no graph for a length of a query."""
    import model_cache
    import search
    import text_embedder
    from lyrics_embeddings import LyricsEmbeddingGenerator
    from text_embeddings import TextEmbeddingGenerator
    gen = types.SimpleNamespace(model=encoder.model, device="mps", model_name=MODEL)
    monkeypatch.setattr(model_cache, "get_model", lambda key, factory: gen)
    shapes = mps_probe.Shapes(encoder.model)
    rng = random.Random(8)
    try:
        before = mps_probe.live_malloc()["mb"]
        for tokens in range(1, 65):
            query = mps_probe.text(max(tokens, 3), rng)     # a text is 3 tokens at the least
            vector = text_embedder.encode_query(encoder.model, query, device="mps", name=MODEL)
            assert vector.shape == (1024,) and abs(float(np.linalg.norm(vector)) - 1) < 0.01
        assert set(shapes.seen) == {(1, 64)}
        search._encode_enrichment_query(mps_probe.text(30, rng))
        for generator in (LyricsEmbeddingGenerator, TextEmbeddingGenerator):
            g = generator(device="mps")
            g.model = encoder.model
            g.query_to_embedding(mps_probe.text(40, rng))
        assert set(shapes.seen) == {(1, 64)}
        assert mps_probe.live_malloc()["mb"] - before <= 12 + 20       # that shape's graphs, if new
    finally:
        shapes.remove()
        text_embedder._end_pass()


@pytest.fixture(scope="module")
def clap():
    """CLAP's text tower as the sound search loads it."""
    if not mps_probe.cached("laion/clap-htsat-unfused"):
        pytest.skip("CLAP is not in the hub's cache")
    from embeddings import AudioEmbeddingGenerator
    generator = AudioEmbeddingGenerator(device="mps")
    generator.load_model()
    return generator


def test_clap_queries_of_every_length_are_a_few_shapes_and_the_same_vectors(clap):
    """A sound query of each length from 1 to 77 words was a shape of its own
    — 74 of them, 318 MB on the host, measured — and is one of four now,
    16 to 128 tokens; the vectors are the unpadded ones to bf16."""
    import torch
    from device import autocast, cast_inputs
    seen = []
    hook = clap.model.text_model.register_forward_pre_hook(
        lambda module, args, kwargs: seen.append(tuple(kwargs["input_ids"].shape)), with_kwargs=True)
    rng = random.Random(6)
    queries = [" ".join(rng.choices(mps_probe.VOCABULARY, k=n)) for n in range(1, 78)]
    try:
        clap.text_to_embedding(queries[0])          # the tower's fixed graphs
        before = mps_probe.live_malloc()["mb"]
        got = [clap.text_to_embedding(q) for q in queries]
        grown = mps_probe.live_malloc()["mb"] - before
        assert set(seen[1:]) <= {(1, 16), (1, 32), (1, 64), (1, 128)}, set(seen)
        assert grown <= 4 * 12 + 20, grown
        for query, vector in zip(queries[::7], got[::7]):
            inputs = clap.processor(text=[query], return_tensors="pt", padding=True)
            inputs = {k: v.to("mps") for k, v in cast_inputs(inputs, clap.model.dtype).items()}
            with torch.no_grad(), autocast("mps"):
                exact = clap.model.get_text_features(**inputs)
            exact = torch.nn.functional.normalize(exact.float(), p=2, dim=1)[0].cpu().numpy()
            assert float(np.dot(vector, exact)) >= 0.999
    finally:
        hook.remove()


def test_the_vectors_are_the_cpus_to_within_bf16(encoder, monkeypatch):
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
    copies = mps_probe.count_cpu_copies(monkeypatch)
    cpu = None
    try:
        cpu = mps_probe.Held(SentenceTransformer(MODEL, device="cpu", local_files_only=True))
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
        text_embedder._end_pass()
        if cpu is not None:
            cpu.release()


def test_padding_to_a_rung_changes_the_vectors_by_no_more_than_bf16(encoder):
    """The attention mask makes the padded tokens count for nothing, so a text
    padded up to its rung is the text alone: the same encoder's vectors for the
    exact length, a text a forward, differ by the noise of a bf16 matmul over
    another shape — 1 for the short, 0.9993 at the longest of these, where MPS
    itself is 0.9992 from the CPU's fp32."""
    import text_embedder
    limit = mps_probe.single_text_limit(encoder.model)
    rng = random.Random(12)
    lengths = [n for n in (3, 14, 25, 63, 64, 65, 120, 256, 257, 400, 700, 1100, 1500, 1730) if n <= limit]
    texts = [mps_probe.text(n, rng) for n in lengths]
    exact = np.concatenate([encoder.model.encode([t], batch_size=1, normalize_embeddings=True,
                                                 show_progress_bar=False) for t in texts])
    padded, left_out = text_embedder.encode(encoder.model, texts, batch_size=16, device="mps", name=MODEL)
    assert left_out == set()
    got = np.stack(padded)
    cosine = (got * exact).sum(1) / (np.linalg.norm(got, axis=1) * np.linalg.norm(exact, axis=1))
    assert cosine.min() >= 0.998, dict(zip(lengths, cosine))
    assert cosine.mean() >= 0.9995, cosine.mean()
    text_embedder._end_pass()


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


def test_ast_keeps_nothing_per_batch_size_on_the_metal_side_and_little_on_the_host(instruments):
    """tag_full_track's batches are 12 windows and a tail of 1 to 11: SDPA
    kept 0.14 GB a window for each size (1.7 GB for the 12; 11 GB over the
    twelve, added up). The twelve sizes are all there are — a closed set
    already, not worth a ladder: ~3 MB of graphs on the host each (37 MB for AST,
    56 for PaSST, measured), where padding the tail up to {1, 2, 4, 8, 12}
    would save a few tens of MB and spend windows of a forward of 0.1 to 1.1 s."""
    assert instruments.ast.config._attn_implementation == "eager"
    rng = np.random.default_rng(0)
    base = mps_probe.floor()
    instruments._ast_probs([rng.standard_normal(16000 * 10).astype(np.float32) * 0.05])
    host = mps_probe.live_malloc()["mb"]
    floors = []
    for windows in range(1, 13):
        instruments._ast_probs([rng.standard_normal(16000 * 10).astype(np.float32) * 0.05
                                for _ in range(windows)])
        floors.append(mps_probe.floor() - base)
        assert floors[-1] <= GIB, f"{floors[-1] / GIB:.2f} GiB kept after batches of 1 to {windows}"
    assert floors[-1] - floors[0] <= 64 * MIB, (floors[-1] - floors[0]) / MIB
    assert mps_probe.live_malloc()["mb"] - host <= 100


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
                                                    torch_dtype=torch.float32,
                                                    local_files_only=True).eval()
    with torch.no_grad():
        inputs = instruments.ast_extractor(windows, sampling_rate=rate, return_tensors="pt")
        reference = torch.sigmoid(cpu(**inputs).logits).numpy()
    diff = np.abs(mine - reference)
    assert diff[:, instruments.instrument_ids].max() <= 0.01, diff[:, instruments.instrument_ids].max()
    assert diff.max() <= 0.03, diff.max()


def test_a_pass_of_bio_chunks_under_the_allocators_limit_lowered_is_not_refused():
    """The failure itself, at a scale the machine takes: the limit at half the
    working set, or 8.88 GiB where that is less (the allocator reads it when it
    starts, hence a child; mps_probe.lowered_limit), where SDPA was refused on a
    24 GB Mac in the 23rd shape — 256 bytes too — and the encoder went to its
    CPU copy for good. The chunks are within what the machine's encoder takes alone, so the
    copy taken at all is a refusal."""
    if not mps_probe.cached(MODEL):
        pytest.skip(f"{MODEL} is not in the hub's cache")
    env = {**os.environ, **mps_probe.lowered_limit(), "HF_HUB_OFFLINE": "1"}
    env.pop("SAUTIUM_ACCEL_MEMORY_GB", None)        # the machine's own working set
    done = subprocess.run([sys.executable, mps_probe.__file__, "refusal"], env=env,
                          capture_output=True, text=True, timeout=900)
    assert done.returncode == 0, done.stderr[-2000:]
    got = json.loads(done.stdout.strip().splitlines()[-1])
    assert got["limit_in_force"], got       # else this passes for want of a limit
    assert got["attention"] == "eager"
    assert (got["refusals"], got["cpu_copies"], got["left_out"]) == (0, 0, 0), got
