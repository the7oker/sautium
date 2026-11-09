"""Pure-logic slice of the identity registry. Everything that touches the
database runs as `python -m desktop.p2p.identity_registry --selftest`
against the live Postgres (throwaway keys, cleaned up) — the gate's queue
below runs on fakes of it."""

import asyncio
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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


def _evaluator(monkeypatch, true_nonce="aa"):
    """verify_relayed before a fresh process, its 2 GiB evaluation faked (a
    proof verifies when its nonce is `true_nonce`) and the load meter absent;
    returns the nonces evaluated and the keys verified."""
    from desktop.p2p import load_meter
    evaluated, verified = [], []
    monkeypatch.setattr(reg, "_RELAYED_FAILED", {})
    monkeypatch.setattr(reg, "_RELAY_FAILED", {})
    monkeypatch.setattr(reg, "_REJECTED", set())
    monkeypatch.setattr(reg.identity_proof, "proof_binds", lambda proof, cert: True)
    monkeypatch.setattr(reg.identity_proof, "verify_proof",
                        lambda proof, cert: evaluated.append(proof["nonce"]) or proof["nonce"] == true_nonce)
    monkeypatch.setattr(reg.identity_pow, "mem_available_kib", lambda: None)
    monkeypatch.setattr(reg, "mark_verified", lambda conn, key, nonce: verified.append((key, nonce)))
    monkeypatch.setattr(load_meter, "current", lambda: None)
    return evaluated, verified


def test_a_key_s_relayed_proofs_are_evaluated_three_nonces_a_day_and_a_true_one_still_verifies(monkeypatch):
    """Fifty fake nonces for one key cost three 2 GiB evaluations, not fifty;
    a nonce that failed is never evaluated again; and an honest relay's true
    proof, coming after a forger's fake one the same day, still verifies the
    author."""
    evaluated, verified = _evaluator(monkeypatch)
    forger, honest = "f0" * 32, "e0" * 32
    cert = {"pubkey": "cd" * 32}
    assert reg.verify_relayed(None, cert, {"nonce": "01"}, forger) == "failed"
    assert reg.verify_relayed(None, cert, {"nonce": "01"}, honest) == "failed"   # never evaluated again
    assert reg.verify_relayed(None, cert, {"nonce": "aa"}, honest) == "verified"  # the true proof
    assert verified == [(cert["pubkey"], "aa")]
    forged = {"pubkey": "ab" * 32}
    out = [reg.verify_relayed(None, forged, {"nonce": f"{n:02x}"}, f"{n:02x}" * 32) for n in range(1, 51)]
    assert out == ["failed"] * 3 + ["unverified"] * 47
    assert evaluated == ["01", "aa", "01", "02", "03"]
    other = {"pubkey": "ef" * 32}
    assert reg.verify_relayed(None, other, {"nonce": "01"}, honest) == "failed"   # another key is its own
    assert evaluated[-1] == "01" and len(evaluated) == 6


def test_one_relay_s_false_proofs_cost_three_evaluations_a_day_whatever_authors_it_names(monkeypatch):
    """A node that brings a false proof for each of fifty authors costs three
    2 GiB evaluations, not fifty; another relay's proofs of those authors are
    still evaluated."""
    evaluated, verified = _evaluator(monkeypatch)
    forger, honest = "f0" * 32, "e0" * 32
    authors = [{"pubkey": f"{n:02x}" * 32} for n in range(1, 51)]
    out = [reg.verify_relayed(None, a, {"nonce": "01"}, forger) for a in authors]
    assert out == ["failed"] * 3 + ["unverified"] * 47
    assert len(evaluated) == 3
    assert reg.verify_relayed(None, authors[10], {"nonce": "aa"}, honest) == "verified"
    assert verified == [(authors[10]["pubkey"], "aa")]


def test_false_proofs_brought_for_an_author_never_keep_its_own_presentation_from_verifying(monkeypatch):
    """Anyone can serve an honest author's slices beside a false nonce:
    three of them stop other relays' proofs of that author from being
    evaluated that day, never the proof the author presents itself."""
    evaluated, verified = _evaluator(monkeypatch)
    author = {"pubkey": "cd" * 32}
    for n, forger in enumerate(("f1" * 32, "f2" * 32, "f3" * 32), start=1):
        assert reg.verify_relayed(None, author, {"nonce": f"{n:02x}"}, forger) == "failed"
    assert reg.verify_relayed(None, author, {"nonce": "aa"}, "e0" * 32) == "unverified"   # the bound
    assert evaluated == ["01", "02", "03"]
    assert reg.verify_relayed(None, author, {"nonce": "aa"}, author["pubkey"].upper()) == "verified"
    assert evaluated[-1] == "aa" and verified == [(author["pubkey"], "aa")]
    # An author presenting false proofs of itself is bounded as any relay is.
    liar = {"pubkey": "ab" * 32}
    out = [reg.verify_relayed(None, liar, {"nonce": f"{n:02x}"}, liar["pubkey"]) for n in range(1, 6)]
    assert out == ["failed"] * 3 + ["unverified"] * 2


def test_a_relayed_proof_waits_while_the_load_meter_is_at_its_ceiling(monkeypatch):
    """As the identity gate's admit answers busy at the ceiling, a relayed
    proof is not evaluated then — kept for a later try, nothing counted —
    and the permit is not taken."""
    from desktop.p2p import load_meter
    evaluated, verified = _evaluator(monkeypatch)
    permit = _Permit()
    monkeypatch.setattr(reg, "_EVALUATION", permit)
    meter = SimpleNamespace(headroom=load_meter.VERIFY_MIN_HEADROOM / 2)
    monkeypatch.setattr(load_meter, "current", lambda: meter)
    author = {"pubkey": "cd" * 32}
    assert reg.verify_relayed(None, author, {"nonce": "aa"}, author["pubkey"]) == "unverified"
    assert evaluated == [] and permit.blocking == []
    meter.headroom = load_meter.VERIFY_MIN_HEADROOM
    assert reg.verify_relayed(None, author, {"nonce": "aa"}, author["pubkey"]) == "verified"
    assert evaluated == ["aa"] and permit.blocking == [False]
