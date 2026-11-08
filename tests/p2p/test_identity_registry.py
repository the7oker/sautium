"""Pure-logic slice of the identity registry. Everything that touches the
database runs as `python -m desktop.p2p.identity_registry --selftest`
against the live Postgres (throwaway keys, cleaned up) — the gate's queue
below runs on fakes of it."""

import asyncio
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from desktop.p2p import identity_registry as reg


def test_ripening_is_computed_from_the_authority_anchor():
    now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)
    fresh = {"issued_at": "2026-08-17T11:30:00Z"}
    ripe = {"issued_at": "2026-08-17T10:59:59Z"}
    assert not reg.is_ripe(fresh, now)
    assert reg.is_ripe(ripe, now)
    edge = {"issued_at": (now - timedelta(seconds=reg.RIPENING_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    assert reg.is_ripe(edge, now)


def test_parse_issued_at_is_utc_seconds():
    dt = reg.parse_issued_at("2026-07-05T10:06:23Z")
    assert dt == datetime(2026, 7, 5, 10, 6, 23, tzinfo=timezone.utc)


def test_admission_maps_to_http():
    assert reg.Admission("verified").http_status == 200
    assert reg.Admission("busy", "x", 30).http_status == 503
    assert reg.Admission("rate_limited", "x", 3600).http_status == 429
    for s in ("invalid", "proof_required", "failed", "banned"):
        assert reg.Admission(s).http_status == 403


def test_addr_uuid_is_the_shared_node_addr_formula():
    assert reg.addr_uuid(None) is None
    assert reg.addr_uuid("203.0.113.7") == reg.addr_uuid("HTTPS://203.0.113.7:8801/x")
    assert reg.addr_uuid("203.0.113.7") != reg.addr_uuid("203.0.113.8")


class _Permit:
    """The process-wide evaluation permit, recording how each taker asked."""

    def __init__(self):
        self._s = threading.Semaphore(1)
        self.blocking = []

    def acquire(self, blocking=True, timeout=None):
        self.blocking.append(blocking)
        return self._s.acquire(True, timeout) if blocking else self._s.acquire(False)

    def release(self):
        self._s.release()


@contextmanager
def _no_db():
    yield None


def _gate(monkeypatch, permit):
    """An identity gate before a `pow` identity it has not verified yet, its
    database and its 2 GiB evaluation faked (the proof verifies)."""
    from desktop.p2p import load_meter
    monkeypatch.setattr(reg, "_EVALUATION", permit)
    monkeypatch.setattr(reg, "is_banned", lambda conn, pubkey: False)
    monkeypatch.setattr(reg, "observe", lambda conn, cert, **kw: {"status": "unverified", "anomaly": False})
    monkeypatch.setattr(reg, "mark_verified", lambda conn, pubkey, nonce: None)
    monkeypatch.setattr(reg.identity_proof, "proof_binds", lambda proof, cert: True)
    monkeypatch.setattr(reg.identity_proof, "verify_proof", lambda proof, cert: True)
    monkeypatch.setattr(load_meter, "current", lambda: None)
    cert = {"pubkey": "ab" * 32, "difficulty": 32}
    return reg.IdentityGate(_no_db, lambda c: True, mem_available_kib=lambda: None), cert


def test_a_request_waiting_for_the_evaluator_parks_no_thread(monkeypatch):
    permit = _Permit()
    gate, cert = _gate(monkeypatch, permit)
    assert asyncio.run(gate.admit(cert["pubkey"], cert, {"nonce": "00"}, None)).status == "verified"
    assert permit.blocking == [False]


def test_while_the_slice_cycle_evaluates_a_request_is_told_busy_at_once(monkeypatch):
    permit = _Permit()
    gate, cert = _gate(monkeypatch, permit)
    assert permit.acquire(False)                      # a relayed proof under evaluation
    try:
        out = asyncio.run(asyncio.wait_for(gate.admit(cert["pubkey"], cert, {"nonce": "00"}, None), 5))
    finally:
        permit.release()
    assert (out.status, out.retry_after) == ("busy", reg.RETRY_BUSY_SECONDS)


def test_a_key_s_relayed_proofs_are_evaluated_three_nonces_a_day_and_a_true_one_still_verifies(monkeypatch):
    """Fifty fake nonces for one key cost three 2 GiB evaluations, not fifty;
    a nonce that failed is never evaluated again; and an honest relay's true
    proof, coming after a forger's fake one the same day, still verifies the
    author."""
    evaluated, verified = [], []
    monkeypatch.setattr(reg, "_RELAYED_FAILED", {})
    monkeypatch.setattr(reg, "_REJECTED", set())
    monkeypatch.setattr(reg.identity_proof, "proof_binds", lambda proof, cert: True)
    monkeypatch.setattr(reg.identity_proof, "verify_proof",
                        lambda proof, cert: evaluated.append(proof["nonce"]) or proof["nonce"] == "aa")
    monkeypatch.setattr(reg.identity_pow, "mem_available_kib", lambda: None)
    monkeypatch.setattr(reg, "mark_verified", lambda conn, key, nonce: verified.append((key, nonce)))
    cert = {"pubkey": "cd" * 32}
    assert reg.verify_relayed(None, cert, {"nonce": "01"}) == "failed"
    assert reg.verify_relayed(None, cert, {"nonce": "01"}) == "failed"      # never evaluated again
    assert reg.verify_relayed(None, cert, {"nonce": "aa"}) == "verified"    # the true proof
    assert verified == [(cert["pubkey"], "aa")]
    forged = {"pubkey": "ab" * 32}
    out = [reg.verify_relayed(None, forged, {"nonce": f"{n:02x}"}) for n in range(1, 51)]
    assert out == ["failed"] * 3 + ["unverified"] * 47
    assert evaluated == ["01", "aa", "01", "02", "03"]
    other = {"pubkey": "ef" * 32}
    assert reg.verify_relayed(None, other, {"nonce": "01"}) == "failed"   # another key is its own
    assert evaluated[-1] == "01" and len(evaluated) == 6
