"""GateService — quote → pay → verify, in-process, both roles, with the
price forced above zero and a fake clock."""

import asyncio
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop.p2p import admission, gate_service as gs

SMALL = admission.PowParams(version=1, memory_kib=8 * 1024, time_cost=1, parallelism=1)


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def _service(price=5, clock=None, params=SMALL):
    server = Ed25519PrivateKey.generate()
    pub = server.public_key().public_bytes_raw().hex()
    seed = server.private_bytes_raw()
    svc = gs.GateService(pub, server.sign, admission.derive_gate_secret(seed),
                         price=lambda c: price, clock=clock or Clock())
    svc._params = params                       # cheap Argon2 for the test
    return svc, pub


def _pay(svc, client_pubkey, tamper=None):
    q = svc.quote(client_pubkey)
    inputs = [bytes.fromhex(t) for t in q["tasks"]]
    answers = admission.solve_all(inputs, svc._params, threads=1)
    if tamper == "half":
        answers = [a if i % 2 else os.urandom(32) for i, a in enumerate(answers)]
    if tamper == "garbage":
        answers = [os.urandom(32) for _ in answers]
    if tamper == "short":
        answers = answers[:-1]
    return q, admission.encode_submission(q["quote"], q["sig"], answers)


def test_quote_shape_and_dormant_zero_price():
    svc, pub = _service(price=0)
    q = svc.quote("ab" * 32)
    assert q["quote"]["n"] == 0 and q["tasks"] == [] and q["quote"]["server"] == pub
    assert admission.verify_quote(q["quote"], q["sig"], pub)
    header = admission.encode_submission(q["quote"], q["sig"], [])
    v = asyncio.run(svc.check_payment(header, "ab" * 32))
    assert v.status == "ok" and v.n == 0 and v.http_status == 200


def test_honest_payment_passes_and_cannot_be_replayed():
    svc, _ = _service(price=6)
    client = "cd" * 32
    q, header = _pay(svc, client)
    assert q["quote"]["n"] == 6 and len(q["tasks"]) == 6
    assert asyncio.run(svc.check_payment(header, client)).status == "ok"
    replay = asyncio.run(svc.check_payment(header, client))
    assert replay.status == "replay" and replay.http_status == 403 and replay.error == "gate_replay"


def test_wrong_client_signature_or_digest_are_invalid():
    svc, pub = _service(price=3)
    q, header = _pay(svc, "cd" * 32)
    assert asyncio.run(svc.check_payment(header, "ef" * 32)).status == "invalid"     # not the quoted client
    assert asyncio.run(svc.check_payment(header, None)).status == "invalid"          # unsigned request
    other, _ = _service(price=3)
    assert asyncio.run(other.check_payment(header, "cd" * 32)).status == "invalid"   # not our quote
    core = dict(q["quote"], n=2)                                                       # tampered core
    assert asyncio.run(svc.check_payment(admission.encode_submission(core, q["sig"], []), "cd" * 32)).status == "invalid"
    assert asyncio.run(svc.check_payment("!!!", "cd" * 32)).status == "invalid"
    assert asyncio.run(svc.check_payment(None, "cd" * 32)).status == "none"


def test_bad_answers_fail_and_burn_the_quote():
    svc, _ = _service(price=8)
    client = "cd" * 32
    _, header = _pay(svc, client, tamper="garbage")
    v = asyncio.run(svc.check_payment(header, client))
    assert v.status == "failed" and v.error == "gate_failed"
    assert asyncio.run(svc.check_payment(header, client)).status == "replay"          # burned
    _, short = _pay(svc, client, tamper="short")
    assert asyncio.run(svc.check_payment(short, client)).status == "failed"
    # half-solved: passes only sometimes (≈ f^R); over trials it must fail at least once
    outcomes = {asyncio.run(svc.check_payment(_pay(svc, client, tamper="half")[1], client)).status for _ in range(12)}
    assert "failed" in outcomes


def test_expiry_and_future_quotes():
    clock = Clock()
    svc, _ = _service(price=2, clock=clock)
    client = "cd" * 32
    q, header = _pay(svc, client)
    clock.t = q["quote"]["deadline"] + 1
    v = asyncio.run(svc.check_payment(header, client))
    assert v.status == "expired" and v.http_status == 410 and v.error == "gate_expired"
    clock.t = q["quote"]["issued"] - gs.CLOCK_SKEW - 5                                 # server clock behind
    assert asyncio.run(svc.check_payment(header, client)).status == "invalid"


def test_transient_failure_releases_the_nonce(monkeypatch):
    svc, _ = _service(price=2)
    client = "cd" * 32
    _, header = _pay(svc, client)
    monkeypatch.setattr(gs.identity_pow, "mem_available_kib", lambda: 1)
    busy = asyncio.run(svc.check_payment(header, client))
    assert busy.status == "busy" and busy.http_status == 503 and busy.retry_after
    monkeypatch.setattr(gs.identity_pow, "mem_available_kib", lambda: 10 ** 9)
    assert asyncio.run(svc.check_payment(header, client)).status == "ok"              # quote survived
