"""Pure-logic tests for the identity rotation core (desktop/node_identity)."""
import base64
import json
from pathlib import Path

import pytest

from desktop import node_identity as ni

pytestmark = pytest.mark.skipif(not ni.HAS_CRYPTO, reason="cryptography missing")


def _mint(tmp: Path, username="first", password="hunter2xx", email="v@example.com"):
    private, pub_hex, invite = ni.derive_account_identity(username, password)
    info = {"node_id": pub_hex, "public_key_hex": pub_hex, "algorithm": "Ed25519",
            "username": username, "invite_code": invite, "email": email,
            "email_verified": True, "anonymous": False}
    ni._save_keypair(private, info, tmp)
    (tmp / "birth_certificate.json").write_text('{"pubkey": "%s"}' % pub_hex)
    (tmp / "identity_proof.json").write_text('{"pubkey": "%s"}' % pub_hex)
    return pub_hex


def test_rotation_archives_the_old_identity_and_signs_the_notice(tmp_path):
    old_pub = _mint(tmp_path)
    result = ni.rotate_identity(tmp_path, "second", "correct-horse", anonymous=False)
    info, rotation = result["info"], result["rotation"]

    assert info["public_key_hex"] != old_pub
    assert info["username"] == "second" and info["invite_code"].startswith("second#")
    assert info["email"] == "v@example.com" and info["email_verified"] is False
    assert info["previous"][0]["public_key_hex"] == old_pub
    assert json.loads((tmp_path / "node_info.json").read_text()) == info

    archive = tmp_path / ni.PREVIOUS_DIRNAME / info["previous"][0]["dir"]
    for name in ni._ARCHIVED_FILES:
        assert (archive / name).exists(), name
        assert not (tmp_path / name).exists() or name.startswith("node_"), name
    assert not (tmp_path / "birth_certificate.json").exists()   # the new key fetches its own
    assert not (tmp_path / "identity_proof.json").exists()

    message = base64.b64decode(rotation["message"])
    notice = ni.parse_rotation_notice(
        message, bytes.fromhex(rotation["old_signature"]),
        bytes.fromhex(rotation["new_signature"]))
    assert notice["old_public_key"] == old_pub
    assert notice["new_public_key"] == info["public_key_hex"]
    assert notice["new_invite_code"] == info["invite_code"]
    assert ni.rotation_record(old_pub, tmp_path) == rotation


def test_notice_needs_both_keys_and_untouched_bytes(tmp_path):
    _mint(tmp_path)
    rotation = ni.rotate_identity(tmp_path, "second", "correct-horse", False)["rotation"]
    message = base64.b64decode(rotation["message"])
    old_sig = bytes.fromhex(rotation["old_signature"])
    new_sig = bytes.fromhex(rotation["new_signature"])

    assert ni.parse_rotation_notice(message, old_sig, new_sig) is not None
    assert ni.parse_rotation_notice(message, new_sig, old_sig) is None      # swapped
    assert ni.parse_rotation_notice(message, old_sig, old_sig) is None      # no possession
    tampered = json.loads(message)
    tampered["new_public_key"] = "ab" * 32
    assert ni.parse_rotation_notice(
        json.dumps(tampered, separators=(",", ":"), sort_keys=True).encode(),
        old_sig, new_sig) is None
    # a body re-serialised differently is not the signed message
    assert ni.parse_rotation_notice(json.dumps(json.loads(message)).encode(),
                                    old_sig, new_sig) is None


def test_retired_seeds_come_newest_first_and_chain(tmp_path):
    first = _mint(tmp_path)
    second = ni.rotate_identity(tmp_path, "second", "correct-horse", False)["info"]["public_key_hex"]
    third = ni.rotate_identity(tmp_path, "third", "", True)

    assert third["info"]["anonymous"] is True
    previous = ni.previous_identities(tmp_path)
    assert [e["public_key_hex"] for e in previous] == [first, second]
    seeds = ni.load_previous_seeds(tmp_path)
    assert len(seeds) == 2 and all(len(s) == 32 for s in seeds)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    newest = Ed25519PrivateKey.from_private_bytes(seeds[0]).public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw).hex()
    assert newest == second
    assert ni.rotation_record(second, tmp_path)["new_public_key"] == third["info"]["public_key_hex"]
    assert ni.rotation_record("00" * 32, tmp_path) is None


def test_same_pair_is_refused_and_nothing_moves(tmp_path):
    _mint(tmp_path, "first", "hunter2xx")
    before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(ValueError):
        ni.rotate_identity(tmp_path, "first", "hunter2xx", False)
    assert sorted(p.name for p in tmp_path.iterdir()) == before
