"""Identity certificate v2 — the three mirrors must agree byte-for-byte.

Pure-Python cases build certificates under a throwaway authority and check
both Python mirrors; the Worker contract case drives the REAL worker/verify.js
in Node (tests/p2p/worker_harness.mjs, in-memory KV, same throwaway
authority) and verifies what it emits with both mirrors.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop.p2p import birth_cert as launcher_mirror
from desktop.p2p import identity_pow
import backend.birth_authority as backend_mirror

MIRRORS = [launcher_mirror, backend_mirror]
HERE = Path(__file__).parent


def _authority():
    seed = os.urandom(32)
    key = Ed25519PrivateKey.from_private_bytes(seed)
    return seed, key, key.public_key().public_bytes_raw().hex()


def _subject_hex():
    return Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()


def _signed(mirror, key, issuer, **fields):
    cert = {"v": 2, "issuer": issuer, "email_token": None, "email_class": None}
    cert.update(fields)
    cert["sig"] = key.sign(mirror.canonical_payload(cert)).hex()
    return cert


def _pow_cert(mirror, key, issuer, **over):
    base = dict(pubkey=_subject_hex(), issued_at="2026-08-17T12:00:00Z",
                method="pow", difficulty=32, params_version=1)
    base.update(over)
    return _signed(mirror, key, issuer, **base)


def _email_cert(mirror, key, issuer, **over):
    base = dict(pubkey=_subject_hex(), issued_at="2026-08-17T12:00:00Z",
                method="email", difficulty=32, params_version=1,
                email_token="ab" * 32, email_class="major")
    base.update(over)
    return _signed(mirror, key, issuer, **base)


@pytest.mark.parametrize("mirror", MIRRORS, ids=["launcher", "backend"])
def test_valid_pow_and_email_certs(mirror):
    _, key, issuer = _authority()
    assert mirror.verify_certificate(_pow_cert(mirror, key, issuer), trusted=[issuer])
    assert mirror.verify_certificate(_email_cert(mirror, key, issuer), trusted=[issuer])


def test_mirrors_produce_identical_payloads():
    _, key, issuer = _authority()
    cert = _email_cert(launcher_mirror, key, issuer)
    assert launcher_mirror.canonical_payload(cert) == backend_mirror.canonical_payload(cert)
    assert backend_mirror.verify_certificate(cert, trusted=[issuer])
    pow_cert = _pow_cert(launcher_mirror, key, issuer)
    assert launcher_mirror.canonical_payload(pow_cert).endswith(b":32:1::")


@pytest.mark.parametrize("mirror", MIRRORS, ids=["launcher", "backend"])
def test_rejects_forgeries_and_bad_shapes(mirror):
    _, key, issuer = _authority()
    _, other_key, other_issuer = _authority()

    good = _pow_cert(mirror, key, issuer)
    assert not mirror.verify_certificate(good)                       # real TRUSTED_AUTHORITIES
    assert not mirror.verify_certificate(good, trusted=[other_issuer])
    foreign = _pow_cert(mirror, other_key, issuer)                    # claims our issuer, signed elsewhere
    assert not mirror.verify_certificate(foreign, trusted=[issuer])

    tampered = dict(good, difficulty=1)                               # cheaper than paid
    assert not mirror.verify_certificate(tampered, trusted=[issuer])
    tampered = dict(good, issued_at="2025-08-17T12:00:00Z")            # older than born
    assert not mirror.verify_certificate(tampered, trusted=[issuer])
    tampered = dict(good, method="email")
    assert not mirror.verify_certificate(tampered, trusted=[issuer])

    v1 = {"v": 1, "pubkey": good["pubkey"], "born_at": "2026-07-05T10:11:12Z",
          "issuer": issuer, "sig": good["sig"]}
    assert not mirror.verify_certificate(v1, trusted=[issuer])

    # Shape checks run before the signature check and must never raise on
    # network-supplied garbage.
    for bad in (0, -1, True, "32", 3.5, None):
        assert not mirror.verify_certificate(dict(good, difficulty=bad), trusted=[issuer])
    assert not mirror.verify_certificate(dict(good, params_version=0), trusted=[issuer])
    assert not mirror.verify_certificate(
        dict(good, issued_at="2026-08-17T12:00:00.000Z"), trusted=[issuer])
    assert not mirror.verify_certificate(dict(good, method="sms"), trusted=[issuer])
    # pow must not carry email fields; email must carry both, well-formed
    assert not mirror.verify_certificate(dict(good, email_token="ab" * 32), trusted=[issuer])
    email = _email_cert(mirror, key, issuer)
    assert not mirror.verify_certificate(dict(email, email_token=None), trusted=[issuer])
    assert not mirror.verify_certificate(dict(email, email_token="zz" * 32), trusted=[issuer])
    assert not mirror.verify_certificate(dict(email, email_class="corporate"), trusted=[issuer])
    assert not mirror.verify_certificate({}, trusted=[issuer])
    assert not mirror.verify_certificate({"v": 2, "issuer": issuer}, trusted=[issuer])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_worker_contract():
    seed, _, issuer = _authority()
    out = subprocess.run(
        ["node", str(HERE / "worker_harness.mjs"), seed.hex(), "test-email-pepper"],
        capture_output=True, text=True, timeout=120, check=True,
    ).stdout
    r = json.loads(out)
    assert r["authority_pub"] == issuer

    pow_cert = r["issue1"]["body"]
    assert r["issue1"]["status"] == 200
    assert pow_cert["method"] == "pow" and pow_cert["difficulty"] == 32
    assert pow_cert["params_version"] == 1
    assert pow_cert["email_token"] is None and pow_cert["email_class"] is None
    for mirror in MIRRORS:
        assert mirror.verify_certificate(pow_cert, trusted=[issuer])
        assert not mirror.verify_certificate(pow_cert)      # not the real authority
    # idempotent: POST again and GET return the identical certificate
    assert r["issue1Again"]["body"] == pow_cert
    assert r["read1"]["body"] == pow_cert
    # the PoW challenge is the authority signature — 64 raw bytes
    assert len(identity_pow.pow_challenge(pow_cert)) == 64

    email_cert = r["register"]["body"]["birth_cert"]
    assert r["register"]["status"] == 200
    assert email_cert["method"] == "email"
    assert email_cert["issued_at"] == pow_cert["issued_at"]      # the anchor never moves
    assert email_cert["email_class"] == "major"
    assert email_cert["difficulty"] == 32
    for mirror in MIRRORS:
        assert mirror.verify_certificate(email_cert, trusted=[issuer])
    assert r["read1AfterEmail"]["body"] == email_cert
    # same mailbox under dots / +tag / case / googlemail alias → same token
    assert r["register3"]["body"]["birth_cert"]["email_token"] == email_cert["email_token"]
    assert r["register3"]["body"]["birth_cert"]["pubkey"] != email_cert["pubkey"]
    # the verified-email record keeps the age anchor under its historic name
    assert r["verified_record"]["born_at"] == pow_cert["issued_at"]

    disposable = r["register4"]["body"]["birth_cert"]
    assert disposable["email_class"] == "disposable"
    assert disposable["email_token"] != email_cert["email_token"]
    for mirror in MIRRORS:
        assert mirror.verify_certificate(disposable, trusted=[issuer])

    legacy = r["legacyRead"]["body"]
    assert legacy["issued_at"] == "2026-07-05T10:11:12Z" and legacy["method"] == "pow"
    for mirror in MIRRORS:
        assert mirror.verify_certificate(legacy, trusted=[issuer])
    rec = r["legacyRecord"]
    assert rec["born_at"] == "2026-07-05T10:11:12Z" and rec["sig"] == "ab" * 64   # rollback-safe
    assert rec["sig_v2"] == legacy["sig"]

    # check-email returns the email cert for an upgraded identity…
    assert r["check1"]["body"]["verified"] is True
    assert r["check1"]["body"]["birth_cert"] == email_cert
    # …upgrades a pre-v2 identity (v1 record + verified record) in place…
    upgraded = r["check2"]["body"]["birth_cert"]
    assert r["check2"]["body"]["verified"] is True
    assert upgraded["method"] == "email" and upgraded["email_class"] == "other"
    assert upgraded["issued_at"] == "2026-07-05T10:11:12Z"
    for mirror in MIRRORS:
        assert mirror.verify_certificate(upgraded, trusted=[issuer])
    assert r["legacyRecordAfterCheck"]["method"] == "email"
    assert r["legacyRecordAfterCheck"]["born_at"] == "2026-07-05T10:11:12Z"
    # …and never for a key the verified record is not bound to
    assert r["check5"]["body"] == {"verified": True}      # no certificate for a foreign key

    # birth ledger (shadow): every first issuance is recorded and scored,
    # certificates keep the base difficulty while not armed
    assert all(b["status"] == 200 and b["body"]["difficulty"] == 32 for b in r["burst"])
    rows = r["ledgerRows"]
    assert len(rows) == 3 + 4                    # 3 earlier identities + the burst
    burst_rows = rows[3:]
    assert [row["n_sub24"] for row in burst_rows] == [0, 1, 2, 3]
    assert [row["n_asn1"] for row in burst_rows] == [0, 1, 2, 3]
    assert [row["m_shadow"] for row in burst_rows] == [1, 1, 2, 2]     # 3rd from one /24 in a day doubles
    assert all(row["asn"] == 64500 and row["cc"] == "UA" for row in burst_rows)
    assert [row["method"] for row in rows[:3]] == ["email", "email", "email"]   # upgrades marked
    assert [row["n_glob1"] for row in rows] == list(range(7))

    policy = r["stats"]["body"]["policy"]
    assert policy["cert_version"] == 2 and policy["pow_difficulty"] == 32
    assert policy["adaptive_armed"] is False and policy["adaptive_cap"] == 8
    day = next(iter(r["stats"]["body"]["days"].values()))
    assert day["births"] == 7 and day["email"] == 4       # 3 registrations + 1 check-email upgrade
    ledger = r["stats"]["body"]["ledger"]
    assert ledger["total"] == 7 and ledger["last_24h"]["births"] == 7
    # m2 = 3: two burst rows plus the third of the earlier identities — all
    # three of those were born from the same test IP within one second
    assert ledger["last_24h"]["email"] == 3 and ledger["last_24h"]["m2"] == 3
    assert ledger["last_24h"]["m4"] == 0
    assert ledger["top_asn_7d"][0] == {"asn": 64500, "cc": "UA", "births": 4}
    assert r["badSig"]["status"] == 403
