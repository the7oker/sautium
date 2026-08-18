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


# ---------------------------------------------------------------------------
# Staged upgrades (grace policy): a bump never leaves an identity pair-less
# ---------------------------------------------------------------------------

def _paths(tmp_path):
    return tmp_path / "birth_certificate.json", tmp_path / ip.PROOF_FILENAME


def _write(path, obj):
    import json
    path.write_text(json.dumps(obj), encoding="utf-8")


def _mined(cert):
    nonce = identity_pow.pow_mine(identity_pow.pow_challenge(cert), cert["difficulty"], TEST_PARAMS)
    return ip.make_proof(cert, nonce)


ACCEPT_ALL = lambda cert: True


def test_stage_certificate_decides_what_to_present(tmp_path):
    cert_path, proof_path = _paths(tmp_path)
    old = _cert(difficulty=4)
    # nothing stored → adopt
    assert ip.stage_certificate(None, old, cert_path, proof_path, ACCEPT_ALL) == old
    assert ip._load_cert(cert_path, ACCEPT_ALL, old["pubkey"]) == old
    # same signature → keep, nothing staged
    assert ip.stage_certificate(old, dict(old), cert_path, proof_path, ACCEPT_ALL) == old
    assert not ip.next_cert_path(cert_path).exists()
    # a re-signed pow certificate while the old pair is complete → staged, old presented
    ip.save_proof(proof_path, _mined(old))
    new = dict(old, v=5, sig=os.urandom(64).hex())
    assert ip.stage_certificate(old, new, cert_path, proof_path, ACCEPT_ALL) == old
    assert ip._load_cert(cert_path, ACCEPT_ALL, None) == old and ip._load_cert(ip.next_cert_path(cert_path), ACCEPT_ALL, None) == new
    # an email certificate needs no proof → adopted at once, the staged file goes
    email = dict(new, method="email", email_token="ab" * 32, email_class="major", sig=os.urandom(64).hex())
    assert ip.stage_certificate(old, email, cert_path, proof_path, ACCEPT_ALL) == email
    assert ip._load_cert(cert_path, ACCEPT_ALL, None) == email and not ip.next_cert_path(cert_path).exists()
    # pow again but no complete pair to protect (proof does not bind to current) → adopt at once
    newer = dict(old, sig=os.urandom(64).hex())
    assert ip.stage_certificate(email, newer, cert_path, proof_path, ACCEPT_ALL) == newer
    assert ip._load_cert(cert_path, ACCEPT_ALL, None) == newer and not ip.next_cert_path(cert_path).exists()
    # an unverifiable fetch changes nothing
    assert ip.stage_certificate(newer, dict(newer, sig="00" * 64), cert_path, proof_path, lambda c: c["sig"] != "00" * 64) == newer
    assert ip._load_cert(cert_path, ACCEPT_ALL, "ff" * 32) is None                    # foreign key → not ours


def test_worker_mines_for_the_staged_certificate_and_promotes_it(tmp_path):
    cert_path, proof_path = _paths(tmp_path)
    old = _cert(difficulty=4)
    old_proof = _mined(old)
    _write(cert_path, old)
    ip.save_proof(proof_path, old_proof)
    new = dict(old, v=5, sig=os.urandom(64).hex())
    assert ip.stage_certificate(old, new, cert_path, proof_path, ACCEPT_ALL) == old
    states = []
    proof = ip.run_worker(cert_path, proof_path, verify=ACCEPT_ALL, own_pubkey=old["pubkey"],
                          stop=threading.Event(), on_state=states.append, params=TEST_PARAMS,
                          mem_available_kib=lambda: 10 ** 9, battery=lambda: False, pause_seconds=0.01)
    assert proof is not None and ip.proof_binds(proof, new) and ip.verify_proof(proof, new, TEST_PARAMS)
    assert ip._load_cert(cert_path, ACCEPT_ALL, None) == new                          # promoted
    assert not ip.next_cert_path(cert_path).exists() and ip.load_proof(proof_path) == proof
    assert any(s.get("upgrade") and s["status"] == "mining" for s in states)          # the UI saw an upgrade
    assert states[-1]["status"] == "ready" and states[-1]["detail"] == "proof"
    # idempotent: a second run only re-checks the promoted pair
    states2 = []
    assert ip.run_worker(cert_path, proof_path, verify=ACCEPT_ALL, own_pubkey=old["pubkey"],
                         stop=threading.Event(), on_state=states2.append, params=TEST_PARAMS,
                         mem_available_kib=lambda: 10 ** 9, battery=lambda: False, pause_seconds=0.01) == proof
    assert [s["status"] for s in states2] == ["checking", "ready"]


def test_worker_heals_a_crash_between_the_two_promotion_writes(tmp_path):
    cert_path, proof_path = _paths(tmp_path)
    old = _cert(difficulty=4)
    new = dict(old, v=5, sig=os.urandom(64).hex())
    _write(cert_path, old)
    _write(ip.next_cert_path(cert_path), new)
    ip.save_proof(proof_path, _mined(new))                                            # proof written, rename never happened
    states = []
    proof = ip.run_worker(cert_path, proof_path, verify=ACCEPT_ALL, own_pubkey=None,
                          stop=threading.Event(), on_state=states.append, params=TEST_PARAMS,
                          mem_available_kib=lambda: 10 ** 9, battery=lambda: False, pause_seconds=0.01)
    assert ip.proof_binds(proof, new) and ip._load_cert(cert_path, ACCEPT_ALL, None) == new
    assert not any(s["status"] == "mining" for s in states)                           # no re-mining
    # a stop during the staged mining leaves the OLD pair intact and presentable
    cert_path2, proof_path2 = tmp_path / "c2.json", tmp_path / "p2.json"
    _write(cert_path2, old)
    old_proof = _mined(old)
    ip.save_proof(proof_path2, old_proof)
    _write(ip.next_cert_path(cert_path2), dict(old, difficulty=1 << 40, sig=os.urandom(64).hex()))
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    assert ip.run_worker(cert_path2, proof_path2, verify=ACCEPT_ALL, own_pubkey=None, stop=stop,
                         on_state=lambda s: None, params=TEST_PARAMS,
                         mem_available_kib=lambda: 10 ** 9, battery=lambda: False, pause_seconds=0.01) is None
    assert ip._load_cert(cert_path2, ACCEPT_ALL, None) == old and ip.load_proof(proof_path2) == old_proof
    assert ip.next_cert_path(cert_path2).exists()                                     # resumes next start
