"""The credential channel (backend/device_auth.py): one handshake, one box,
consumed on use — the exchange that keeps a password, a PIN and the device
token off a plain-HTTP LAN. Pure PyNaCl, no database."""

import base64
import json
import time

import pytest
from nacl.public import Box, PrivateKey, PublicKey

# The backend's own import form (PYTHONPATH=/app in the container runner):
# device_auth reaches p2p_identity as a top-level module, so the patches below
# must land on that same module object, not on a `backend.`-prefixed twin.
import device_auth
import p2p_identity


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _seal(handshake: dict, payload: dict, client_key: PrivateKey | None = None):
    client_key = client_key or PrivateKey.generate()
    box = Box(client_key, PublicKey(base64.b64decode(handshake["eph"])))
    sealed = box.encrypt(json.dumps(payload).encode("utf-8"))
    return client_key, dict(
        eph=handshake["eph"], client=_b64(bytes(client_key.public_key)),
        nonce=_b64(sealed.nonce), box=_b64(sealed.ciphertext))


@pytest.fixture(autouse=True)
def _no_identity(monkeypatch):
    # The channel signs with the node key when there is one; these tests run
    # the unsigned (first-run) shape and check the signed one separately.
    monkeypatch.setattr(device_auth, "_expected_pubkey", lambda: None)
    monkeypatch.setattr(p2p_identity, "load_signing_key", lambda settings: None)
    device_auth._channels.clear()


def test_roundtrip_and_reply():
    hs = device_auth.open_channel()
    assert hs["sig"] == "" and hs["node_pubkey"] == ""
    assert hs["expires"] > time.time()
    client_key, envelope = _seal(hs, {"password": "hunter22"})
    payload, reply = device_auth.unbox(**envelope)
    assert payload == {"password": "hunter22"}
    answer = reply({"token": "t0k"})
    box = Box(client_key, PublicKey(base64.b64decode(hs["eph"])))
    opened = box.decrypt(base64.b64decode(answer["box"]), base64.b64decode(answer["nonce"]))
    assert json.loads(opened) == {"token": "t0k"}


def test_handshake_is_consumed_on_first_use():
    hs = device_auth.open_channel()
    _, envelope = _seal(hs, {"code": "ABCD-EFGH"})
    device_auth.unbox(**envelope)
    with pytest.raises(device_auth.ChannelError, match="unknown or expired"):
        device_auth.unbox(**envelope)


def test_expired_handshake_is_refused(monkeypatch):
    hs = device_auth.open_channel()
    _, envelope = _seal(hs, {"code": "x"})
    monkeypatch.setattr(device_auth.time, "time",
                        lambda: hs["expires"] + 1)
    with pytest.raises(device_auth.ChannelError, match="unknown or expired"):
        device_auth.unbox(**envelope)


def test_wrong_client_key_does_not_open():
    hs = device_auth.open_channel()
    _, envelope = _seal(hs, {"password": "p"})
    envelope["client"] = _b64(bytes(PrivateKey.generate().public_key))
    with pytest.raises(device_auth.ChannelError, match="does not open"):
        device_auth.unbox(**envelope)


def test_malformed_envelope():
    hs = device_auth.open_channel()
    _, envelope = _seal(hs, {"password": "p"})
    with pytest.raises(device_auth.ChannelError, match="malformed"):
        device_auth.unbox(**{**envelope, "client": "not base64!"})
    with pytest.raises(device_auth.ChannelError, match="malformed"):
        device_auth.unbox(**{**envelope, "client": _b64(b"short")})


def test_payload_must_be_an_object():
    hs = device_auth.open_channel()
    client_key = PrivateKey.generate()
    box = Box(client_key, PublicKey(base64.b64decode(hs["eph"])))
    sealed = box.encrypt(b"[1, 2]")
    with pytest.raises(device_auth.ChannelError, match="not an object"):
        device_auth.unbox(hs["eph"], _b64(bytes(client_key.public_key)),
                          _b64(sealed.nonce), _b64(sealed.ciphertext))


def test_cap_evicts_oldest():
    first = device_auth.open_channel()
    for _ in range(device_auth.CHANNEL_CAP):
        device_auth.open_channel()
    _, envelope = _seal(first, {"code": "x"})
    with pytest.raises(device_auth.ChannelError, match="unknown or expired"):
        device_auth.unbox(**envelope)
    assert len(device_auth._channels) == device_auth.CHANNEL_CAP


def test_signed_handshake_verifies(monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw).hex()
    monkeypatch.setattr(p2p_identity, "load_signing_key", lambda settings: key)
    monkeypatch.setattr(device_auth, "_expected_pubkey", lambda: pub)
    hs = device_auth.open_channel()
    assert hs["node_pubkey"] == pub
    signed = (b"sautium-pair:v1" + base64.b64decode(hs["eph"])
              + hs["expires"].to_bytes(8, "big"))
    key.public_key().verify(bytes.fromhex(hs["sig"]), signed)   # raises if wrong
