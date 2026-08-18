"""Mode A primitives against the wire-format vectors + the sampling economics."""

import json
import os
import random
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop.p2p import admission as adm

VECTORS = json.loads((Path(__file__).parent / "vectors" / "peer_wire_v1.json").read_text())
G = VECTORS["gate"]
SERVER = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(VECTORS["server_seed"]))
GATE_SECRET = bytes.fromhex(VECTORS["gate_secret"])
CORE = G["quote_response"]["quote"]
NONCE = bytes.fromhex(CORE["nonce"])
SMALL = adm.PowParams(version=0, memory_kib=8 * 1024, time_cost=1, parallelism=1)


def test_fresh_inputs_and_answers_match_vectors():
    inputs = adm.derive_fresh_inputs(GATE_SECRET, NONCE, CORE["client"], CORE["n"])
    assert [i.hex() for i in inputs] == G["quote_response"]["tasks"]
    assert adm.tasks_digest(inputs) == CORE["tasks_digest"]
    submission = json.loads(G["submission_json"])
    assert [adm.solve(i).hex() for i in inputs] == submission["answers"]
    assert [a.hex() for a in adm.solve_all(inputs, threads=3)] == submission["answers"]


def test_quote_signature_and_submission_header_match_vectors():
    assert adm.quote_message(CORE).decode() == G["quote_signed_message"]
    sig = adm.sign_quote(SERVER.sign, CORE)
    assert sig == G["quote_response"]["sig"]
    assert adm.verify_quote(CORE, sig, CORE["server"])
    assert not adm.verify_quote(dict(CORE, n=4), sig, CORE["server"])
    assert not adm.verify_quote(CORE, sig, "ab" * 32)
    assert not adm.verify_quote(CORE, "00" * 64, CORE["server"])
    answers = [bytes.fromhex(a) for a in json.loads(G["submission_json"])["answers"]]
    header = adm.encode_submission(CORE, sig, answers)
    assert header == G["header"][adm.HDR_GATE]
    decoded = adm.decode_submission(header)
    assert decoded == {"quote": CORE, "sig": sig, "answers": answers}
    assert adm.decode_submission("!!") is None
    assert adm.decode_submission(adm.encode_submission(CORE, sig, [])) == {"quote": CORE, "sig": sig, "answers": []}


def test_build_quote_core_reproduces_the_vector_shape():
    inputs = adm.derive_fresh_inputs(GATE_SECRET, NONCE, CORE["client"], 3)
    core = adm.build_quote_core(CORE["server"], CORE["client"], NONCE, inputs, action="mb.slice",
                                issued=CORE["issued"], deadline=CORE["deadline"])
    assert core == CORE
    auto = adm.build_quote_core(CORE["server"], CORE["client"], NONCE, inputs, issued=100)
    assert auto["action"] == ""
    assert auto["deadline"] == 100 + int(adm.QUOTE_BASE_TTL + adm.QUOTE_TTL_PER_TASK * 3)


def test_shuffle_is_a_deterministic_permutation():
    p = adm.shuffle_positions(GATE_SECRET, NONCE, 12)
    assert sorted(p) == list(range(12)) and p == adm.shuffle_positions(GATE_SECRET, NONCE, 12)
    assert p != adm.shuffle_positions(GATE_SECRET, os.urandom(16), 12)
    assert adm.shuffle_positions(GATE_SECRET, NONCE, 1) == [0]


def test_sampled_verification_and_cheating_economics():
    inputs = [os.urandom(32) for _ in range(20)]
    honest = adm.solve_all(inputs, SMALL, threads=1)
    idx = adm.sample_indices(20, 2)
    assert len(idx) == 2 and idx == sorted(idx) and all(0 <= i < 20 for i in idx)
    assert adm.verify_sampled(inputs, honest, idx, SMALL)
    assert adm.verify_sampled(inputs, honest, [], SMALL)
    assert not adm.verify_sampled(inputs, honest[:-1], idx, SMALL)                 # count mismatch
    garbage = [os.urandom(32) for _ in inputs]
    assert not adm.verify_sampled(inputs, garbage, [0], SMALL)
    # a cheater solving half the packet passes ≈ f^R of the time
    rng = random.Random(7)
    passes = 0
    trials = 400
    for _ in range(trials):
        cheat = [honest[i] if rng.random() < 0.5 else os.urandom(32) for i in range(20)]
        if adm.verify_sampled(inputs, cheat, adm.sample_indices(20, 2), SMALL):
            passes += 1
    rate = passes / trials
    assert 0.15 < rate < 0.36                                                        # ≈ 0.25
    assert adm.cheat_pass_probability(0.5, 2) == 0.25
    assert adm.sample_indices(3, 5) == [0, 1, 2]
