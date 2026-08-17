"""Wire format v1 — the implementation must reproduce the fixed-seed vectors
byte for byte (tests/p2p/vectors/peer_wire_v1.json)."""

import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop.p2p import peer_auth as pa
from desktop.p2p import birth_cert, identity_proof

VECTORS = json.loads((Path(__file__).parent / "vectors" / "peer_wire_v1.json").read_text())
CLIENT = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(VECTORS["client_seed"]))
SERVER_PUB = VECTORS["server_pubkey"]


def test_canonical_message_and_signature_match_vectors():
    for v in VECTORS["requests"]:
        body = v["body_utf8"].encode("utf-8")
        msg = pa.canonical_message(SERVER_PUB, v["method"], v["request_target"], v["ts"], body)
        assert msg.decode() == v["canonical_message"]
        headers = pa.sign_headers(CLIENT.sign, VECTORS["client_pubkey"], SERVER_PUB,
                                  v["method"], v["request_target"], body, ts=v["ts"])
        assert headers == v["headers"]


def test_verify_accepts_vectors_and_rejects_everything_else():
    for v in VECTORS["requests"]:
        body = v["body_utf8"].encode("utf-8")
        h = dict(v["headers"])
        ok = lambda hh, **kw: pa.verify_request(hh, SERVER_PUB, v["method"], v["request_target"], body, now=v["ts"] + kw.get("skew", 0))
        assert ok(h) == (VECTORS["client_pubkey"], None)
        assert ok(h, skew=pa.TS_WINDOW) == (VECTORS["client_pubkey"], None)
        assert ok(h, skew=pa.TS_WINDOW + 1) == (None, "identity signature stale")
        assert ok(h, skew=-(pa.TS_WINDOW + 1)) == (None, "identity signature stale")
        # a different recipient, method, target or body invalidates the signature
        assert pa.verify_request(h, "ab" * 32, v["method"], v["request_target"], body, now=v["ts"])[1] == "identity signature invalid"
        assert pa.verify_request(h, SERVER_PUB, "PUT", v["request_target"], body, now=v["ts"])[1] == "identity signature invalid"
        assert pa.verify_request(h, SERVER_PUB, v["method"], v["request_target"] + "&x=1", body, now=v["ts"])[1] == "identity signature invalid"
        assert pa.verify_request(h, SERVER_PUB, v["method"], v["request_target"], body + b" ", now=v["ts"])[1] == "identity signature invalid"
        # incomplete / malformed
        assert pa.verify_request({}, SERVER_PUB, v["method"], v["request_target"], body) == (None, None)
        assert pa.verify_request({pa.HDR_PUBKEY: h[pa.HDR_PUBKEY]}, SERVER_PUB, v["method"], v["request_target"], body)[1] == "identity signature incomplete"
        assert pa.verify_request(dict(h, **{pa.HDR_SIG: "zz"}), SERVER_PUB, v["method"], v["request_target"], body, now=v["ts"])[1] == "identity signature malformed"
        assert pa.verify_request(dict(h, **{pa.HDR_PUBKEY: "ab" * 32}), SERVER_PUB, v["method"], v["request_target"], body, now=v["ts"])[1] == "identity signature invalid"


def test_cert_bundle_round_trip_matches_vector():
    bundle = json.loads(VECTORS["cert_bundle"]["json"])
    encoded = pa.encode_cert_bundle(bundle["cert"], bundle["proof"])
    assert encoded == VECTORS["cert_bundle"]["header"][pa.HDR_CERT]
    decoded = pa.decode_cert_bundle(encoded)
    assert decoded == bundle
    assert birth_cert.verify_certificate(decoded["cert"], trusted=[VECTORS["authority_pubkey"]])
    assert identity_proof.proof_binds(decoded["proof"], decoded["cert"])
    assert pa.decode_cert_bundle("not base64!") is None
    assert pa.decode_cert_bundle(pa.encode_cert_bundle({"v": 2}, "junk")) is None   # proof must be an object
    assert pa.decode_cert_bundle(pa.encode_cert_bundle({"v": 2}, None)) == {"cert": {"v": 2}, "proof": None}


def test_identity_bound_paths():
    assert pa.is_identity_bound("/api/sync/inventory")
    assert pa.is_identity_bound("/api/mb/search?q=x")
    assert not pa.is_identity_bound("/health")
    assert not pa.is_identity_bound("/api/chat/handshake")
    assert not pa.is_identity_bound("/api/relay/voucher")
