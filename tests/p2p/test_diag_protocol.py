"""Support diagnostics wire protocol — warrant signing/verification, the
Box payloads, report encoding and log scrubbing (desktop/p2p/diag_protocol.py)."""

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from nacl.exceptions import CryptoError

from desktop.p2p import diag_protocol as dp

MASTER_SEED = bytes.fromhex("11" * 32)
NODE_SEED = bytes.fromhex("22" * 32)
OTHER_SEED = bytes.fromhex("33" * 32)


def _pub(seed: bytes) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw).hex()


MASTER = Ed25519PrivateKey.from_private_bytes(MASTER_SEED)
MASTER_PUB = _pub(MASTER_SEED)
NODE_PUB = _pub(NODE_SEED)
OTHER_PUB = _pub(OTHER_SEED)
NOW = 1_800_000_000


def _warrant(**kw):
    args = dict(target=NODE_PUB, scopes=["logs", "system"], since=NOW - 86400,
                issuer=MASTER_PUB, now=NOW, warrant_id="0d9a5f4e-7e2b-4c3b-9a0e-1f2c3d4e5f60")
    args.update(kw)
    return dp.sign_warrant(MASTER.sign, **args)


def test_payload_is_canonical_and_deterministic():
    p = dp.warrant_payload("id-1", MASTER_PUB.upper(), NODE_PUB, NOW, NOW + 10,
                           ["system", "logs"], None)
    assert p == (f"sautium-diag-warrant:v1:id-1:{MASTER_PUB}:{NODE_PUB}:{NOW}:{NOW + 10}:"
                 f"logs,system:").encode()
    w1, w2 = _warrant(), _warrant()
    assert w1 == w2                                  # Ed25519 is deterministic
    assert w1["scopes"] == ["logs", "system"] and w1["expires_at"] == NOW + dp.WARRANT_TTL_DEFAULT_S


def test_verify_accepts_and_rejects():
    w = _warrant()
    ok = lambda ww, **kw: dp.verify_warrant(ww, kw.pop("target", NODE_PUB),
                                            now=kw.pop("now", NOW + 5), issuer=MASTER_PUB)
    assert ok(w) == (True, None)
    assert ok(w, now=w["expires_at"] - 1) == (True, None)
    assert ok(w, now=w["expires_at"])[1] == "expired"
    assert ok(w, now=NOW - dp.WARRANT_ISSUE_SKEW_S - 1)[1] == "issued in the future"
    assert ok(w, target=OTHER_PUB)[1] == "addressed to another node"
    assert dp.verify_warrant(w, NODE_PUB, now=NOW, issuer=OTHER_PUB)[1] == "not from the master"
    assert ok(dict(w, scopes=["logs", "everything"]))[1] == "unknown scope"
    assert ok(dict(w, scopes=[]))[1] == "unknown scope"
    assert ok(dict(w, sig="00" * 64))[1] == "bad signature"
    assert ok(dict(w, since=None))[1] == "bad signature"          # since is signed
    assert ok(dict(w, v=2))[1] == "unsupported version"
    assert ok(dict(w, id="not-a-uuid"))[1] == "malformed"
    assert ok("junk")[1] == "unsupported version"
    forged = dp.sign_warrant(Ed25519PrivateKey.from_private_bytes(OTHER_SEED).sign,
                             target=NODE_PUB, scopes=["logs"], issuer=MASTER_PUB, now=NOW)
    assert ok(forged)[1] == "bad signature"
    with pytest.raises(ValueError):
        dp.sign_warrant(MASTER.sign, target=NODE_PUB, scopes=["nope"], issuer=MASTER_PUB)


def test_frame_shape():
    frame = dp.warrant_frame(_warrant())
    assert frame["type"] == dp.FRAME_TYPE == "diag_warrant"
    assert frame["warrant"]["target"] == NODE_PUB


def test_box_round_trip_and_wrong_party():
    data = b"\x00tar.gz bytes" * 1000
    boxed = dp.encrypt_to(NODE_SEED, MASTER_PUB, data)
    assert boxed != data and len(boxed) == len(data) + 24 + 16
    assert dp.decrypt_from(MASTER_SEED, NODE_PUB, boxed) == data
    with pytest.raises(CryptoError):
        dp.decrypt_from(MASTER_SEED, OTHER_PUB, boxed)          # wrong sender
    with pytest.raises(CryptoError):
        dp.decrypt_from(OTHER_SEED, NODE_PUB, boxed)            # wrong recipient
    with pytest.raises(CryptoError):
        dp.decrypt_from(MASTER_SEED, NODE_PUB, boxed[:-1])      # tampered


def test_report_strips_local_only_and_bounds_detail():
    events = [
        {"kind": "backend.crashed", "ts": "2026-08-26T10:00:00+00:00",
         "detail": {"rc": 1, "log_tail": "Traceback…", "uptime_s": 3}},
        {"kind": "node.started", "ts": "2026-08-26T10:01:00+00:00",
         "detail": {"blob": "x" * (dp.EVENT_DETAIL_MAX_CHARS + 1)}},
    ]
    decoded = dp.decode_report(dp.encode_report(events))
    assert decoded[0]["detail"] == {"rc": 1, "uptime_s": 3}
    assert decoded[1]["detail"] == {"truncated": True, "keys": ["blob"]}
    assert "log_tail" not in dp.encode_report(events).decode()


def test_decode_report_rejects_bad_input():
    good = {"v": 1, "events": [{"kind": "sync.failed", "ts": "2026-08-26T10:00:00+00:00",
                                "detail": {"error": "boom"}}]}
    assert dp.decode_report(json.dumps(good).encode())[0]["kind"] == "sync.failed"
    bad = [
        b"not json",
        json.dumps({"v": 2, "events": []}).encode(),
        json.dumps({"v": 1, "events": []}).encode(),
        json.dumps({"v": 1, "events": [dict(good["events"][0], kind="rm.rf")]}).encode(),
        json.dumps({"v": 1, "events": [dict(good["events"][0], ts="yesterday")]}).encode(),
        json.dumps({"v": 1, "events": [dict(good["events"][0], detail="str")]}).encode(),
        json.dumps({"v": 1, "events": good["events"] * (dp.REPORT_MAX_EVENTS + 1)}).encode(),
        json.dumps({"v": 1, "events": [dict(good["events"][0],
                                            detail={"x": "y" * dp.EVENT_DETAIL_MAX_CHARS})]}).encode(),
    ]
    for raw in bad:
        with pytest.raises(ValueError):
            dp.decode_report(raw)


def test_scrub_secrets():
    text = ("key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123 used\n"
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig\n"
            'api_key="abc123def456" password=hunter2 token: tkn_9\n'
            "dsn postgresql://sautium:s3cret@localhost:15432/sautium\n"
            f"sig={'ab' * 64} pubkey={'cd' * 32}\n")
    out = dp.scrub_secrets(text)
    for secret in ("sk-ant-api03", "eyJhbGci", "abc123def456", "hunter2", "tkn_9", "s3cret", "ab" * 64):
        assert secret not in out
    assert "cd" * 32 in out                     # a node pubkey is not a secret
    assert "postgresql://sautium:[redacted]@localhost" in out
    assert 'api_key="[redacted]' in out
