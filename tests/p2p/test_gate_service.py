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
                         price=lambda c, a: price, clock=clock or Clock())
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


class Meter:
    def __init__(self, headroom=1.0, profile="standard"):
        self.headroom = headroom
        self.profile = profile
        self.sample_subs = []

    @property
    def dormant(self):
        return self.headroom >= 0.5

    def subscribe_samples(self, cb):
        self.sample_subs.append(cb)


def test_failed_payments_are_rate_limited_per_key_and_expire(monkeypatch):
    monkeypatch.setattr(gs, "FAILS_PER_HOUR", 3)
    clock = Clock()
    svc, _ = _service(price=4, clock=clock)
    client, other = "ef" * 32, "fe" * 32
    keys = [client, "subnet:1"]
    for _ in range(3):
        _, header = _pay(svc, client, tamper="garbage")
        assert asyncio.run(svc.check_payment(header, client, keys)).status == "failed"
    q, header = _pay(svc, client)                                   # honest now — too late for an hour
    v = asyncio.run(svc.check_payment(header, client, keys))
    assert v.status == "rate_limited" and v.http_status == 429 and v.retry_after == gs.RETRY_RATE_LIMITED_SECONDS
    assert v.error == "gate_rate_limited"
    assert q["quote"]["nonce"] not in svc._seen                     # refused before the nonce — nothing consumed
    clock.t += 3601                                                 # the window passes (that quote has expired by now)
    assert asyncio.run(svc.check_payment(_pay(svc, client)[1], client, keys)).status == "ok"
    # the subnet key is enough — a fresh pubkey from the same subnet inherits the block
    for _ in range(3):
        _, header = _pay(svc, other, tamper="garbage")
        asyncio.run(svc.check_payment(header, other, [other, "subnet:2"]))
    _, header = _pay(svc, "0a" * 32)
    assert asyncio.run(svc.check_payment(header, "0a" * 32, ["0a" * 32, "subnet:2"])).status == "rate_limited"
    assert asyncio.run(svc.check_payment(_pay(svc, "0b" * 32)[1], "0b" * 32, ["0b" * 32, "subnet:3"])).status == "ok"


def test_verification_yields_to_the_ceiling_and_releases_the_nonce():
    server = Ed25519PrivateKey.generate()
    pub = server.public_key().public_bytes_raw().hex()
    meter = Meter(headroom=0.05)
    svc = gs.GateService(pub, server.sign, admission.derive_gate_secret(server.private_bytes_raw()),
                         price=lambda c, a: 3, meter=meter, clock=Clock())
    svc._params = SMALL
    assert meter.sample_subs == []                                  # no pool → no seeding hook
    client = "1a" * 32
    q, header = _pay(svc, client)
    v = asyncio.run(svc.check_payment(header, client))
    assert v.status == "busy" and v.http_status == 503 and v.detail == "node at its ceiling"
    meter.headroom = 0.4                                            # relief: the same quote still pays
    assert asyncio.run(svc.check_payment(header, client)).status == "ok"
    assert asyncio.run(svc.check_payment(header, client)).status == "replay"
    # a zero-price packet never reaches the verifier — the ceiling does not refuse it
    svc._price = lambda c, a: 0
    meter.headroom = 0.0
    q0 = svc.quote(client)
    assert asyncio.run(svc.check_payment(admission.encode_submission(q0["quote"], q0["sig"], []), client)).status == "ok"


def test_idle_seed_only_runs_dormant_and_with_a_pool():
    server = Ed25519PrivateKey.generate()
    pub = server.public_key().public_bytes_raw().hex()
    meter = Meter(headroom=0.9)
    minted = []

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def factory():
        return Conn()

    def fake_pool(self, fn, *args, **kwargs):
        if fn is gs.gate_pool.free_seed_gold:
            return len(minted)
        if fn is gs.gate_pool.mint:
            minted.append(kwargs); return len(minted)
        raise AssertionError(fn)

    svc = gs.GateService(pub, server.sign, admission.derive_gate_secret(server.private_bytes_raw()),
                         conn_factory=factory, meter=meter, clock=Clock())
    svc._params = SMALL
    svc._pool = fake_pool.__get__(svc)
    assert meter.sample_subs == [svc.idle_seed]
    meter.headroom = 0.2                                            # busy → nothing
    svc.idle_seed(None); assert minted == []
    meter.headroom = 0.9
    for _ in range(gs.SEED_GOLD_TARGET + 5):                        # one per sample, stops at the target
        svc.idle_seed(None)
    assert len(minted) == gs.SEED_GOLD_TARGET
    m = minted[0]
    assert m["klass"] == "gold" and m["origin"] == "seed" and m["recipient_keys"] == []
    assert m["source_pubkey"] == pub and m["answer"] == admission.solve(m["task_input"], SMALL)
