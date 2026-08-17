import os
import threading

import pytest

from desktop.p2p import identity_pow as pow
from desktop.p2p.identity_pow import PowParams

# 8 MiB keeps one evaluation at a few milliseconds; the maths is identical to
# the 2 GiB production shape, only the memory_kib differs.
TEST_PARAMS = PowParams(version=0, memory_kib=8 * 1024, time_cost=1, parallelism=1)

# Computed once with argon2-cffi 25.1.0 on CPython 3.13 (WSL) and confirmed
# on CPython 3.11 (Docker image). Prover and verifier are different processes
# on different machines — this vector is the contract between them.
KNOWN_CHALLENGE = bytes.fromhex("0f" * 64)
KNOWN_NONCE = bytes(range(16))
KNOWN_DIGEST = "95f7574ea66f2a2d323232a6894fe4090ff66b04589bbab14449829636f781c4"
KNOWN_DIGEST_16MIB = "0d166d2667fb262f2858e903da312a88a4600371862c4f15e29c9ba3bc607d65"


def test_known_vector_is_stable_across_processes():
    assert pow.pow_digest(KNOWN_CHALLENGE, KNOWN_NONCE, TEST_PARAMS).hex() == KNOWN_DIGEST


def test_memory_parameter_changes_the_digest():
    p16 = PowParams(version=0, memory_kib=16 * 1024, time_cost=1, parallelism=1)
    assert pow.pow_digest(KNOWN_CHALLENGE, KNOWN_NONCE, p16).hex() == KNOWN_DIGEST_16MIB


def test_production_params_are_pinned():
    p = pow.POW_PARAMS[pow.CURRENT_PARAMS_VERSION]
    assert p.memory_kib == 2 * 1024 * 1024
    assert p.time_cost == 1
    assert p.parallelism == 1
    assert p.hash_len == 32


def test_pow_challenge_is_the_authority_signature():
    sig = os.urandom(64)
    assert pow.pow_challenge({"pubkey": "ab" * 32, "sig": sig.hex()}) == sig


def test_target_and_meets_target():
    assert pow.meets_target(b"\xff" * 32, 1)
    assert not pow.meets_target(b"\xff" * 32, 2)
    top_byte_zero = b"\x00" + b"\xff" * 31
    assert pow.meets_target(top_byte_zero, 256)
    assert not pow.meets_target(top_byte_zero, 257)
    assert pow.target(1) == 1 << 256
    assert pow.target(3) == (1 << 256) // 3       # non-power-of-two scale


def test_mine_then_verify_roundtrip():
    challenge = os.urandom(64)
    seen = []
    nonce = pow.pow_mine(challenge, 12, TEST_PARAMS,
                         on_attempt=lambda n, t: seen.append((n, t)))
    assert nonce is not None and len(nonce) == pow.NONCE_LEN
    assert pow.pow_verify(challenge, nonce, 12, TEST_PARAMS)
    assert [n for n, _ in seen] == list(range(1, len(seen) + 1))
    assert all(b >= a for (_, a), (_, b) in zip(seen, seen[1:]))


def test_verify_rejects_forgeries_and_malformed_input():
    challenge = os.urandom(64)
    # A random nonce passes E = 2^40 with probability 2^-40.
    assert not pow.pow_verify(challenge, os.urandom(16), 1 << 40, TEST_PARAMS)
    assert not pow.pow_verify(challenge, os.urandom(15), 1, TEST_PARAMS)
    assert not pow.pow_verify(challenge, os.urandom(17), 1, TEST_PARAMS)
    assert not pow.pow_verify(challenge, os.urandom(16), 0, TEST_PARAMS)
    assert not pow.pow_verify(challenge, os.urandom(16), pow.MAX_DIFFICULTY + 1, TEST_PARAMS)


def test_difficulty_one_accepts_first_attempt():
    calls = []
    nonce = pow.pow_mine(os.urandom(64), 1, TEST_PARAMS,
                         on_attempt=lambda n, t: calls.append(n))
    assert nonce is not None
    assert calls == [1]


def test_stop_event_aborts_before_first_evaluation():
    stop = threading.Event()
    stop.set()
    calls = []
    assert pow.pow_mine(os.urandom(64), 1 << 30, TEST_PARAMS, stop=stop,
                        on_attempt=lambda n, t: calls.append(n)) is None
    assert calls == []


def test_stop_event_aborts_between_evaluations():
    stop = threading.Event()

    def stop_after_three(n, _):
        if n == 3:
            stop.set()

    calls = []
    result = pow.pow_mine(os.urandom(64), 1 << 60, TEST_PARAMS, stop=stop,
                          on_attempt=lambda n, t: (calls.append(n), stop_after_three(n, t)))
    assert result is None
    assert calls == [1, 2, 3]


def test_progress_maths():
    for e in (16, 45, 256, 4096):
        assert pow.completion_probability(e, e) == pytest.approx(1 - (1 - 1 / e) ** e)
        assert 0.63 < pow.completion_probability(e, e) < 0.65   # -> 1 - 1/e from above
        p50, p90, p99 = (pow.attempts_for_quantile(q, e) for q in (0.5, 0.9, 0.99))
        assert 1 <= p50 < p90 < p99
        assert pow.completion_probability(p90, e) >= 0.9
        assert pow.completion_probability(p90 - 1, e) < 0.9
    assert pow.completion_probability(0, 256) == 0.0
    assert pow.completion_probability(1, 1) == 1.0
    assert pow.attempts_for_quantile(0.99, 1) == 1
