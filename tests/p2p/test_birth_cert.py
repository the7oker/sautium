"""Identity certificate v4 — the three mirrors must agree byte-for-byte.

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
    cert = {"v": 4, "issuer": issuer, "email_token": None, "email_class": None,
            "email_domain_token": None, "predecessor": None}
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
                email_token="ab" * 32, email_class="major", email_domain_token="cd" * 32)
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
    assert launcher_mirror.canonical_payload(pow_cert).endswith(b":32:1::::")
    assert launcher_mirror.canonical_payload(pow_cert).startswith(b"sautium-birth:v4:")
    successor = _email_cert(launcher_mirror, key, issuer, predecessor="ef" * 32)
    assert launcher_mirror.canonical_payload(successor).endswith(b":" + b"ef" * 32)
    assert backend_mirror.verify_certificate(successor, trusted=[issuer])


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
    assert not mirror.verify_certificate(dict(good, v=2), trusted=[issuer])

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
    assert not mirror.verify_certificate(dict(email, email_domain_token="zz" * 32), trusted=[issuer])
    # a migrated email record may lack the domain token (empty) — signed as such
    assert mirror.verify_certificate(_email_cert(mirror, key, issuer, email_domain_token=None), trusted=[issuer])
    assert not mirror.verify_certificate(dict(good, email_domain_token="cd" * 32), trusted=[issuer])
    # predecessor: email only, a real foreign pubkey, and signed like everything else
    assert not mirror.verify_certificate(dict(good, predecessor="ef" * 32), trusted=[issuer])
    assert not mirror.verify_certificate(dict(email, predecessor="ef" * 32), trusted=[issuer])   # unsigned claim
    assert not mirror.verify_certificate(_email_cert(mirror, key, issuer, predecessor="zz" * 32), trusted=[issuer])
    own = _subject_hex()
    assert not mirror.verify_certificate(_email_cert(mirror, key, issuer, pubkey=own, predecessor=own), trusted=[issuer])
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
    assert pow_cert["v"] == 4
    assert pow_cert["method"] == "pow" and pow_cert["difficulty"] == 32
    assert pow_cert["params_version"] == 1
    assert pow_cert["email_token"] is None and pow_cert["email_class"] is None
    assert pow_cert["email_domain_token"] is None and pow_cert["predecessor"] is None
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
    assert email_cert["predecessor"] is None                    # first holder of the mailbox
    # same mailbox under dots / +tag / case / googlemail alias → same token
    successor = r["register3"]["body"]["birth_cert"]
    assert successor["email_token"] == email_cert["email_token"]
    assert successor["pubkey"] != email_cert["pubkey"]
    # succession: the mailbox moved to s3, whose certificate names s1 as predecessor
    assert successor["predecessor"] == email_cert["pubkey"]
    for mirror in MIRRORS:
        assert mirror.verify_certificate(successor, trusted=[issuer])
    assert r["mailboxAfter3"]["pubkey"] == successor["pubkey"]
    # s1 takes the mailbox back: predecessor s3, anchor unchanged, index follows the registration
    retake = r["retake"]["body"]["birth_cert"]
    assert r["retake"]["status"] == 200
    assert retake["predecessor"] == successor["pubkey"] and retake["issued_at"] == email_cert["issued_at"]
    assert retake["pubkey"] == email_cert["pubkey"] and retake["sig"] != email_cert["sig"]
    for mirror in MIRRORS:
        assert mirror.verify_certificate(retake, trusted=[issuer])
    assert r["mailboxAfterRetake"]["pubkey"] == email_cert["pubkey"]
    assert r["retakeAgain"]["body"]["birth_cert"] == retake         # same key, same mailbox → no change
    # the domain token: shared by every gmail identity, distinct from the address token,
    # and exactly what the node-side table computes under the same pepper
    from desktop.p2p import email_domains
    assert email_cert["email_domain_token"] == email_domains.compute_token("test-email-pepper", "gmail.com")
    assert len(email_cert["email_domain_token"]) == 64
    assert r["register3"]["body"]["birth_cert"]["email_domain_token"] == email_cert["email_domain_token"]
    assert email_cert["email_domain_token"] != email_cert["email_token"]
    # the verified-email record keeps the age anchor under its historic name
    assert r["verified_record"]["born_at"] == pow_cert["issued_at"]

    disposable = r["register4"]["body"]["birth_cert"]
    assert disposable["email_class"] == "disposable"
    assert disposable["email_token"] != email_cert["email_token"]
    assert disposable["email_domain_token"] != email_cert["email_domain_token"]
    for mirror in MIRRORS:
        assert mirror.verify_certificate(disposable, trusted=[issuer])

    legacy = r["legacyRead"]["body"]
    assert legacy["issued_at"] == "2026-07-05T10:11:12Z" and legacy["method"] == "pow"
    for mirror in MIRRORS:
        assert mirror.verify_certificate(legacy, trusted=[issuer])
    rec = r["legacyRecord"]
    assert rec["born_at"] == "2026-07-05T10:11:12Z" and rec["sig"] == "ab" * 64   # rollback-safe
    assert rec["sig_v4"] == legacy["sig"]
    # a v2 email record (pre-domain-token) is served as v4 with the field empty,
    # keeps its v2 signature for rollback, and gains the token on check-email
    v2 = r["legacyV2Read"]["body"]
    assert v2["v"] == 4 and v2["method"] == "email" and v2["email_domain_token"] is None
    assert v2["predecessor"] is None
    assert v2["issued_at"] == "2026-07-06T00:00:00Z"
    for mirror in MIRRORS:
        assert mirror.verify_certificate(v2, trusted=[issuer])
    assert r["legacyV2Record"]["sig_v2"] == "cd" * 64 and r["legacyV2Record"]["sig_v4"] == v2["sig"]
    filled = r["legacyV2Check"]["body"]["birth_cert"]
    assert filled["email_domain_token"] and len(filled["email_domain_token"]) == 64
    assert filled["issued_at"] == v2["issued_at"]            # the anchor never moves
    assert len(filled["email_token"]) == 64                   # recomputed from the real mailbox

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
    assert [row["n_addr24"] for row in burst_rows] == [0, 0, 0, 0]     # distinct addresses in the burst
    assert [row["m_shadow"] for row in burst_rows] == [1, 1, 2, 2]     # 3rd from one /24 in a day doubles
    # the three earlier identities came from ONE test address → the exact-address axis fires
    assert [row["n_addr24"] for row in rows[:3]] == [0, 1, 2]
    assert rows[2]["m_shadow"] == 4                                    # addr (≥2) × subnet (≥2)
    assert all(row["addr"] for row in rows)
    assert all(row["asn"] == 64500 and row["cc"] == "UA" for row in burst_rows)
    assert [row["method"] for row in rows[:3]] == ["email", "email", "email"]   # upgrades marked
    assert [row["n_glob1"] for row in rows] == list(range(7))

    policy = r["stats"]["body"]["policy"]
    assert policy["cert_version"] == 4 and policy["pow_difficulty"] == 32
    assert policy["adaptive_armed"] is False and policy["adaptive_cap"] == 8
    day = next(iter(r["stats"]["body"]["days"].values()))
    assert day["births"] == 11 and day["email"] == 4      # +2 native v6, +2 tunnels; 3 reg + 1 upgrade
    assert day["succession"] == 2                          # s3 took alice's mailbox, s1 took it back
    ledger = r["stats"]["body"]["ledger"]
    assert ledger["total"] == 11 and ledger["last_24h"]["births"] == 11
    assert ledger["last_24h"]["distinct_addr"] == 9
    # m2 = 3: two burst rows plus the third of the earlier identities — all
    # three of those were born from the same test IP within one second
    assert ledger["last_24h"]["email"] == 3 and ledger["last_24h"]["m2"] == 3
    assert ledger["last_24h"]["m4"] == 1
    assert ledger["top_asn_7d"][0] == {"asn": 64500, "cc": "UA", "births": 4}
    assert r["badSig"]["status"] == 403


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_worker_mailbox_contract():
    """The master mailbox (Ф16): certified senders park E2E ciphertext for
    the offline master; only the master (signed, fresh) drains and acks."""
    seed, _, _ = _authority()
    r = json.loads(subprocess.run(
        ["node", str(HERE / "worker_harness.mjs"), seed.hex(), "test-email-pepper"],
        capture_output=True, text=True, timeout=120, check=True,
    ).stdout)
    assert r["mail1"]["body"] == {"stored": True, "id": 1}
    assert r["mail2"]["body"] == {"stored": True, "id": 2}
    assert r["mailDup"]["body"] == {"stored": False, "duplicate": True}     # message_uuid dedup
    assert r["mail3"]["status"] == 200
    assert r["mailCap"]["status"] == 429                                    # per-sender/day cap (3 in the harness)
    assert r["mailNoCert"]["status"] == 403                                 # no birth certificate → no free keys
    assert r["mailBadSig"]["status"] == 403
    assert r["mailWrongTo"]["status"] == 404                                # only the master has a mailbox
    assert r["mailTooBig"]["status"] == 413
    drained = r["drain"]["body"]["messages"]
    assert [m["id"] for m in drained] == [1, 2, 3] and r["drain"]["body"]["more"] is False
    assert all(m["from_public_key"] == r["issue1"]["body"]["pubkey"] for m in drained)
    assert set(drained[0]) == {"id", "message_uuid", "from_public_key", "encrypted", "timestamp", "received_at"}
    assert r["drainForeign"]["status"] == 403 and r["drainStale"]["status"] == 403
    assert r["ack"]["body"] == {"deleted": 3}
    assert r["drainAfterAck"]["body"] == {"messages": [], "more": False}
    assert r["wakeNoUpgrade"]["status"] == 426
    # the master address hint: empty before, set by a signed wake with a port
    # (edge IP + declared port), immune to replays from elsewhere
    assert r["hintEmpty"]["body"] == {}
    assert r["wakeWithPort"]["status"] == 426                # signature+hint accepted, upgrade still required
    assert r["hintSet"]["body"]["host"] == "198.51.100.77" and r["hintSet"]["body"]["port"] == 8801
    assert r["hintReplayPort"]["status"] == 403              # the port is inside the signature
    assert r["hintReplaySame"]["status"] == 426              # sig valid — but the nonce already burned:
    assert r["hintAfterReplay"]["body"]["host"] == "198.51.100.77"   # the replayer's address never lands
    assert r["hintBadPort"]["status"] == 426                 # a junk port never touches the hint
    # a wake over IPv6 fills the host6 slot and PRESERVES the v4 one
    assert r["wake6"]["status"] == 426
    assert r["hintDual"]["body"]["host"] == "198.51.100.77"
    assert r["hintDual"]["body"]["host6"] == "2a00:db8::99"
    # the fam sensor: v6 births counted, and both spellings of one /48 share a bucket
    assert r["v6birth"]["status"] == 200 and r["v6birth2"]["status"] == 200
    rows6 = r["ledgerRowsV6"]
    assert len(rows6) == 2 and rows6[0]["sub"] == rows6[1]["sub"]
    assert rows6[1]["n_sub24"] == 1                          # the second v6 birth saw the first in its /48
    assert r["stats"]["body"]["ledger"]["last_24h"]["v6"] == 2
    # 6to4 (2002::/16) and Teredo (2001:0::/32) are v6 syntactically but NOT
    # a v6-peer signal: recorded fam=4, never counted in the prize metric
    assert r["tun6to4"]["status"] == 200 and r["tunTeredo"]["status"] == 200
    assert [row["fam"] for row in r["tunnelFams"]] == [4, 4]
    # capability directory: certified volunteers in, the edge decides the address
    assert r["reg1"]["body"] == {"registered": True}
    assert r["reg3"]["body"] == {"registered": True}
    assert r["regNoCert"]["status"] == 403                   # no certificate → no listing
    assert r["regBadSig"]["status"] == 403
    assert r["masterReg"]["status"] == 403                   # the master publishes /master-hint instead
    assert r["regReplay"]["status"] == 403                   # single-use signature: a replay from
    dump = r["hintsDump"]["body"]["nodes"]                   # another host cannot re-point the entry
    assert [ (n["pubkey"], n["host"], n["port"]) for n in dump ] == [
        (r["issue1"]["body"]["pubkey"], "198.51.100.50", 20246)]
    relay = r["hintsRelay"]["body"]["nodes"]
    assert len(relay) == 1 and relay[0]["host"] is None and relay[0]["host6"] == "2a00:db8::77"
    assert r["hintsSlices"]["body"]["nodes"] == []           # nobody registered that capability
    assert r["hintsBadCap"]["status"] == 400
    assert [n["host"] for n in r["hintsAfterStale"]["body"]["nodes"]] == ["198.51.100.50"]   # TTL hides the stale row
