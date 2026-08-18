import os
import threading

from desktop.p2p import identity_pow
from desktop.p2p import identity_proof as ip
from desktop.p2p.identity_pow import PowParams

TEST_PARAMS = PowParams(version=0, memory_kib=8 * 1024, time_cost=1, parallelism=1)


def _cert(method="pow", difficulty=6):
    return {"v": 3, "pubkey": os.urandom(32).hex(), "issued_at": "2026-08-17T12:00:00Z",
            "method": method, "difficulty": difficulty, "params_version": 1,
            "email_token": None, "email_class": None, "email_domain_token": None,
            "issuer": "ab" * 32, "sig": os.urandom(64).hex()}


def _run(cert, path, stop=None, **kw):
    states = []
    kw.setdefault("mem_available_kib", lambda: 10 ** 9)
    kw.setdefault("battery", lambda: False)
    kw.setdefault("pause_seconds", 0.01)
    proof = ip.ensure_identity_proof(cert, path, stop=stop or threading.Event(),
                                     on_state=states.append, params=TEST_PARAMS, **kw)
    return proof, states


def test_proof_binding_and_verification(tmp_path):
    cert = _cert(difficulty=4)
    nonce = identity_pow.pow_mine(identity_pow.pow_challenge(cert), 4, TEST_PARAMS)
    proof = ip.make_proof(cert, nonce)
    assert ip.proof_binds(proof, cert)
    assert not ip.proof_binds(proof, dict(cert, sig=os.urandom(64).hex()))
    assert not ip.proof_binds(proof, dict(cert, difficulty=7))
    assert not ip.proof_binds(dict(proof, nonce="zz" * 16), cert)
    assert not ip.proof_binds(None, cert)
    assert ip.verify_proof(proof, cert, TEST_PARAMS)
    # a nonce that binds but does not meet the (much harder) target
    assert not ip.verify_proof(dict(proof, difficulty=1 << 40), dict(cert, difficulty=1 << 40), TEST_PARAMS)
    p = tmp_path / ip.PROOF_FILENAME
    ip.save_proof(p, proof)
    assert ip.load_proof(p) == proof
    assert ip.load_proof(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text("{not json")
    assert ip.load_proof(tmp_path / "bad.json") is None


def test_email_certificate_needs_no_work(tmp_path):
    proof, states = _run(_cert(method="email"), tmp_path / ip.PROOF_FILENAME)
    assert proof is None
    assert [s["status"] for s in states] == ["ready"]
    assert states[0]["detail"] == "email"
    assert not (tmp_path / ip.PROOF_FILENAME).exists()


def test_mines_saves_and_reports_progress(tmp_path):
    cert = _cert(difficulty=6)
    path = tmp_path / ip.PROOF_FILENAME
    proof, states = _run(cert, path)
    assert proof is not None and ip.load_proof(path) == proof
    assert ip.verify_proof(proof, cert, TEST_PARAMS)
    statuses = [s["status"] for s in states]
    assert statuses[0] == "mining" and statuses[-1] == "ready"
    assert states[-1]["detail"] == "proof" and states[-1]["proof_mined_at"] == proof["mined_at"]
    attempts = [s["attempts"] for s in states if s["status"] == "mining" and s["attempts"]]
    assert attempts == list(range(1, len(attempts) + 1))
    assert all(0 < s["p_done"] <= 1 for s in states if s["attempts"])


def test_valid_stored_proof_is_only_rechecked(tmp_path):
    cert = _cert(difficulty=4)
    path = tmp_path / ip.PROOF_FILENAME
    first, _ = _run(cert, path)
    again, states = _run(cert, path)
    assert again == first
    assert [s["status"] for s in states] == ["checking", "ready"]
    assert states[-1]["attempts"] == 0


def test_corrupt_stored_proof_is_remined(tmp_path):
    # A stored proof that BINDS but does not verify (bit rot, tampering) must
    # be replaced, never presented. Use a target no random nonce can meet so
    # the failure is deterministic, and stop as soon as re-mining starts.
    cert = _cert(difficulty=1 << 30)
    path = tmp_path / ip.PROOF_FILENAME
    ip.save_proof(path, ip.make_proof(cert, os.urandom(16)))
    stop = threading.Event()
    states = []

    def on_state(s):
        states.append(s)
        if s["status"] == "mining":
            stop.set()

    ip.ensure_identity_proof(cert, path, stop=stop, on_state=on_state, params=TEST_PARAMS,
                             mem_available_kib=lambda: 10 ** 9, battery=lambda: False,
                             pause_seconds=0.01)
    assert [s["status"] for s in states][:2] == ["checking", "mining"]


def test_gate_pauses_on_memory_and_battery(tmp_path):
    cert = _cert(difficulty=2)
    calls = {"mem": 0, "batt": 0}

    def mem():
        calls["mem"] += 1
        return 1024 if calls["mem"] <= 2 else 10 ** 9      # low, low, then fine

    def batt():
        calls["batt"] += 1
        return calls["batt"] == 1                            # on battery once

    proof, states = _run(cert, tmp_path / ip.PROOF_FILENAME, mem_available_kib=mem, battery=batt)
    assert proof is not None
    seq = [(s["status"], s["detail"]) for s in states]
    assert ("paused", "memory") in seq and ("paused", "battery") in seq
    assert seq[-1] == ("ready", "proof")
    assert seq.index(("paused", "memory")) < seq.index(("paused", "battery"))


def test_stop_before_start_and_during_pause(tmp_path):
    cert = _cert(difficulty=1 << 30)
    stop = threading.Event()
    stop.set()
    proof, states = _run(cert, tmp_path / ip.PROOF_FILENAME, stop=stop)
    assert proof is None and states[-1]["status"] == "stopped"

    stop = threading.Event()
    states = []

    def on_state(s):
        states.append(s)
        if s["status"] == "paused":
            stop.set()

    ip.ensure_identity_proof(cert, tmp_path / ip.PROOF_FILENAME, stop=stop, on_state=on_state,
                             params=TEST_PARAMS, mem_available_kib=lambda: 1, battery=lambda: False,
                             pause_seconds=0.01)
    assert [s["status"] for s in states] == ["mining", "paused", "stopped"]


def test_allocation_failure_pauses_instead_of_failing(tmp_path, monkeypatch):
    cert = _cert(difficulty=3)
    real = identity_pow.pow_digest
    fired = {"n": 0}

    def flaky(challenge, nonce, params):
        fired["n"] += 1
        if fired["n"] == 1:
            raise identity_pow.HashingError("cannot allocate")
        return real(challenge, nonce, params)

    monkeypatch.setattr(identity_pow, "pow_digest", flaky)
    proof, states = _run(cert, tmp_path / ip.PROOF_FILENAME)
    assert proof is not None
    seq = [(s["status"], s["detail"]) for s in states]
    assert ("paused", "memory") in seq and seq[-1] == ("ready", "proof")
