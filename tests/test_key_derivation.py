"""The account's key and its sign-in (SEC-S5): one derivation every road
calls, the password read as NFKC before it, the product's cost pinned; the
account before its key — its file, the sign-in hash, the token's id; the
worker that makes the key once, in the background; the identity folder the
only place an identity comes from."""

import asyncio
import json
import sys
import threading
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import device_auth  # noqa: E402
import p2p_identity  # noqa: E402
from desktop import identity_birth  # noqa: E402
from desktop import node_identity as ni  # noqa: E402

PASSWORD = "пароль й ﬁx１２"


def _pubkey(username, password):
    return ni.public_hex(ni.key_from_seed(ni.derive_seed(username, password)))


@pytest.mark.real_key_cost
def test_the_products_cost_is_pinned():
    """Part of every key: a change here is a new key for every account
    (SECURITY-PRE-RELEASE §9.13: t=8000 over 128 MiB, two lanes); the
    sign-in hash at the same memory, the first release's time (t=8)."""
    assert (ni.ARGON2_TIME_COST, ni.ARGON2_MEMORY_COST, ni.ARGON2_PARALLELISM, ni.ARGON2_HASH_LEN) == (
        8000, 131072, 2, 32)
    assert (ni.SIGNIN_TIME_COST, ni.SIGNIN_MEMORY_COST, ni.SIGNIN_PARALLELISM) == (8, 131072, 2)
    assert "$m=131072,t=8,p=2$" in ni.signin_hash("a made-up password")


def test_a_password_in_another_unicode_form_is_the_same_key_and_the_same_sign_in():
    composed = unicodedata.normalize("NFC", PASSWORD)
    decomposed = unicodedata.normalize("NFD", PASSWORD)
    assert composed != decomposed
    assert ni.derive_seed("owner", composed) == ni.derive_seed("owner", decomposed)
    assert ni.derive_seed("owner", "ﬁx１２") == ni.derive_seed("owner", "fix12")
    assert ni.signin_matches(ni.signin_hash(composed), decomposed)
    assert not ni.signin_matches(ni.signin_hash(composed), "пароль и fix12")


def test_the_one_derivation_at_the_suites_cost():
    """A known answer at the cost the suite derives at (tests/conftest)."""
    assert ni.derive_seed("kat-owner", "tangerine volcano quiet").hex() == (
        "6ceb91baa49cd6eed3942301829133d0d5989cd3525a940f6a9768bb3cfa58ff")


def test_the_launcher_and_a_docker_node_make_one_key_from_one_password(tmp_path, monkeypatch):
    """The launcher's wizard runs the worker in its own process; a Docker
    node's setup runs it through device_auth — the same derivation and the
    same files, from the password typed in either Unicode form."""
    made = {}
    done = threading.Event()
    ni.write_account(tmp_path / "l", ni.new_account("owner", unicodedata.normalize("NFC", PASSWORD),
                                                    source="wizard"))

    def placed(seed):
        made["launcher"] = ni.place_account_key(tmp_path / "l", ni.key_from_seed(seed),
                                                ni.read_account(tmp_path / "l"))["public_key_hex"]
        done.set()
    assert identity_birth.Birth().start("owner", unicodedata.normalize("NFC", PASSWORD),
                                        purpose="create", done=placed, said=lambda s: None,
                                        failed=lambda e: done.set())
    assert done.wait(30)
    docker = tmp_path / "d"
    monkeypatch.setattr(device_auth, "_identity_dir", lambda: docker)
    monkeypatch.setattr(device_auth, "_holds_identity", lambda pubkey=None: False)
    monkeypatch.setattr(device_auth, "_record_signing_key", lambda pubkey: None)
    monkeypatch.setattr(device_auth, "_key_arrived", lambda pubkey: made.setdefault("docker", pubkey))
    account = ni.new_account("owner", unicodedata.normalize("NFD", PASSWORD), source="form")
    ni.write_account(docker, account)
    device_auth._born(account, False, None)(ni.derive_seed("owner", unicodedata.normalize("NFD", PASSWORD)))
    assert made["launcher"] == made["docker"] == _pubkey("owner", PASSWORD)
    for d in (tmp_path / "l", docker):
        assert ni.read_account(d)["pubkey"] == made["docker"]
        assert json.loads((d / "node_info.json").read_text())["public_key_hex"] == made["docker"]


def test_the_worker_makes_one_key_at_a_time_and_says_its_estimate_once():
    birth = identity_birth.Birth()
    said, ends, gate = [], [], threading.Event()

    def done(seed):
        gate.wait(10)
        ends.append(seed)
    assert birth.start("owner", "a made-up one", purpose="create", done=done, said=said.append,
                       failed=lambda e: ends.append(e))
    assert not birth.start("owner", "another", purpose="create", done=done, said=said.append,
                           failed=lambda e: None)
    while not said:
        threading.Event().wait(0.01)
    assert said[0]["status"] == "deriving" and said[0]["purpose"] == "create"
    assert said[0]["expected_s"] >= 0 and said[0]["started_at"].endswith("Z")
    gate.set()
    while not ends:
        threading.Event().wait(0.01)
    assert ends == [ni.derive_seed("owner", "a made-up one")] and birth.state() is None


def test_a_worker_that_fails_says_so_and_holds_nothing(caplog):
    birth = identity_birth.Birth()
    failed = threading.Event()
    with caplog.at_level("ERROR", logger="desktop.identity_birth"):
        def placed(seed):
            raise OSError(28, "No space left on device")
        birth.start("owner", "x", purpose="create", done=placed,
                    said=lambda s: None, failed=lambda e: failed.set())
        assert failed.wait(10)
    assert birth.state() is None and "could not be made" in caplog.text


def test_the_account_exists_before_its_key(tmp_path, monkeypatch):
    """Its file from the first second — the name, the hash, the id — read by
    the node's user alone; the key's place empty; the token named by the id."""
    monkeypatch.setattr(device_auth, "_identity_dir", lambda: tmp_path)
    monkeypatch.setattr(device_auth, "_account_id_cache", None)
    monkeypatch.setattr(device_auth, "_epoch_cache", 3)
    before = device_auth.current_token(b"secret")
    account = ni.new_account("owner", "a made-up one", source="form")
    ni.write_account(tmp_path, account)
    assert (tmp_path / "account.json").stat().st_mode & 0o077 == 0
    assert device_auth.account_configured() and not device_auth.key_ready()
    assert device_auth.key_state() == "interrupted"          # nothing makes it in this process
    assert device_auth.account_username() == "owner"
    token = device_auth.current_token(b"secret")
    assert token != before and len(account["account_id"]) == 32
    other = ni.new_account("owner", "a made-up one", source="form")
    device_auth._account_written(other)
    assert device_auth.current_token(b"secret") != token     # another account, another token


def test_a_new_account_is_refused_over_another_and_a_random_key_has_no_password(tmp_path):
    info = ni.anonymous_account(tmp_path, "anonymous-1a2b", source="wizard")
    account = ni.read_account(tmp_path)
    assert account["anonymous"] and account["signin"] is None and account["pubkey"] == info["public_key_hex"]
    assert info["anonymous"] is True and (tmp_path / "node_ed25519.key").exists()
    with pytest.raises(FileExistsError):
        ni.anonymous_account(tmp_path, "anonymous-2b3c", source="wizard")


def test_the_launcher_never_mints_over_an_account_whose_key_is_not_made(tmp_path, monkeypatch):
    monkeypatch.setattr(ni, "_identity_dir", lambda: tmp_path)
    ni.write_account(tmp_path, ni.new_account("owner", "a made-up one", source="wizard"))
    assert ni.ensure_identity("dbname=never-asked") == "account"
    assert not (tmp_path / "node_ed25519.key").exists()


def test_the_identity_is_the_identity_folders_alone(tmp_path, monkeypatch):
    """SEC-S5 §18: no setting names an identity — the master's too is a
    random key in its folder, made by one command (source "console")."""
    from config import settings
    assert not hasattr(settings, "p2p_username") and not hasattr(settings, "p2p_password")
    monkeypatch.setattr(settings, "p2p_identity_dir", str(tmp_path))
    p2p_identity.forget_cached()
    try:
        assert p2p_identity.load_signing_key(settings) is None
        assert p2p_identity.resolve_identity(settings) is None
        info = ni.anonymous_account(tmp_path, "Sautium", source="console")
        p2p_identity.forget_cached()
        assert ni.public_hex(p2p_identity.load_signing_key(settings)) == info["public_key_hex"]
        assert p2p_identity.resolve_identity(settings)["invite_code"] == info["invite_code"]
    finally:
        p2p_identity.forget_cached()


# --- the key's making, stopped and followed (review round 1) -------------------

@pytest.fixture
def unborn(tmp_path, births, monkeypatch):
    """A Docker node whose account is made and whose key is not."""
    from routers import p2p as p2p_router
    monkeypatch.setattr(device_auth, "_identity_dir", lambda: tmp_path)
    monkeypatch.setattr(device_auth, "_holds_identity", lambda pubkey=None: False)
    monkeypatch.setattr(device_auth, "_expected_pubkey",
                        lambda: ni.pubkey_on_disk(tmp_path) if (tmp_path / "node_ed25519.key").exists() else None)
    monkeypatch.setattr(device_auth, "_record_signing_key", lambda pubkey: None)
    monkeypatch.setattr(device_auth, "launcher_mode", lambda: False)
    monkeypatch.setattr(device_auth, "_held", None)
    said, arrived = [], []
    monkeypatch.setattr(p2p_router, "_publish_identity_state", said.append)
    monkeypatch.setattr(device_auth, "_key_arrived", arrived.append)
    ni.write_account(tmp_path, ni.new_account("owner", "a made-up one amber", source="form"))
    return SimpleNamespace(folder=tmp_path, births=births, said=said, arrived=arrived)


def test_a_start_says_a_key_whose_making_stopped_and_a_sign_in_makes_it_again(unborn, monkeypatch):
    device_auth.say_key_at_start()
    assert unborn.said == [{"status": "interrupted", "purpose": "resume"}]

    async def signed_in():
        return device_auth.resume_birth("a made-up one amber")
    assert asyncio.run(signed_in()) is True
    assert [b["purpose"] for b in unborn.births] == ["resume"]
    unborn.births.finish()
    assert unborn.arrived == [_pubkey("owner", "a made-up one amber")]
    assert ni.read_account(unborn.folder)["pubkey"] == unborn.arrived[0]


def test_a_launchers_backend_resumes_no_key_and_says_nothing_of_it(unborn, monkeypatch):
    monkeypatch.setattr(device_auth, "launcher_mode", lambda: True)
    device_auth.say_key_at_start()

    async def signed_in():
        return device_auth.resume_birth("a made-up one amber")
    assert asyncio.run(signed_in()) is False and unborn.said == [] and unborn.births == []


def test_a_key_the_launcher_made_is_followed_once(tmp_path, monkeypatch):
    key = {"pub": None}
    arrived = []
    monkeypatch.setattr(device_auth, "_expected_pubkey", lambda: key["pub"])
    monkeypatch.setattr(device_auth, "_record_signing_key", lambda pubkey: None)
    monkeypatch.setattr(device_auth, "_key_arrived", arrived.append)
    monkeypatch.setattr(device_auth, "_followed", None)
    device_auth._born_elsewhere()                      # the listener's first connect: no key yet
    key["pub"] = "ab" * 32
    device_auth._born_elsewhere([object()])            # the launcher's NOTIFY
    device_auth._born_elsewhere()                      # a reconnect: the same key
    assert arrived == ["ab" * 32]


def test_the_handshake_is_vouched_for_by_the_device_token(tmp_path, monkeypatch):
    """A browser that holds the token takes a handshake only the node that
    issued it makes (auth.js pins on it); with no account, no tag."""
    import hashlib
    import hmac as hmac_mod
    import base64
    monkeypatch.setattr(device_auth, "_identity_dir", lambda: tmp_path)
    monkeypatch.setattr(device_auth, "_account_id_cache", None)
    monkeypatch.setattr(device_auth, "_epoch_cache", 1)
    monkeypatch.setattr(p2p_identity, "load_signing_key", lambda settings: None)
    assert device_auth.open_channel(lambda: b"secret")["tag"] == ""
    ni.write_account(tmp_path, ni.new_account("owner", "a made-up one", source="form"))
    hs = device_auth.open_channel(lambda: b"secret")
    signed = (b"sautium-pair:v1" + base64.b64decode(hs["eph"]) + hs["expires"].to_bytes(8, "big")
              + hs["node_pubkey"].encode())
    token = device_auth.current_token(b"secret").encode()
    assert hs["tag"] == hmac_mod.new(token, signed, hashlib.sha256).hexdigest()
    assert hs["tag"] != hmac_mod.new(b"another token", signed, hashlib.sha256).hexdigest()


def test_a_random_key_not_placed_before_a_stop_is_made_again_at_the_start(unborn, monkeypatch):
    """A random key loses nothing by being made again: at the start, placed —
    or held again for the owner's word over another identity's records."""
    ni.write_account(unborn.folder, {**ni.new_account("owner", "", source="form", anonymous=True)})
    device_auth.say_key_at_start()
    assert (unborn.folder / "node_ed25519.key").exists() and unborn.said == []
    assert ni.read_account(unborn.folder)["pubkey"] == ni.pubkey_on_disk(unborn.folder)
    (unborn.folder / "node_ed25519.key").unlink()
    (unborn.folder / "node_info.json").unlink()
    monkeypatch.setattr(device_auth, "_holds_identity", lambda pubkey=None: True)
    device_auth.say_key_at_start()
    assert unborn.said == [{"status": "another_identity", "weak": None}]
    assert device_auth._held is not None and not (unborn.folder / "node_ed25519.key").exists()


def test_no_random_identity_is_minted_beside_an_account_waiting_for_its_key(tmp_path, monkeypatch):
    """Review round 3: the launcher's "make a new identity" over an account
    file would leave a key its password does not make."""
    monkeypatch.setattr(ni, "_identity_dir", lambda: tmp_path)
    ni.write_account(tmp_path, ni.new_account("owner", "a made-up one", source="form"))
    with pytest.raises(FileExistsError):
        ni.generate_identity()
    assert not (tmp_path / "node_ed25519.key").exists()


def test_a_launchers_backend_leaves_even_a_random_key_to_the_launcher(unborn, monkeypatch):
    (unborn.folder / "account.json").unlink()
    monkeypatch.setattr(device_auth, "launcher_mode", lambda: True)
    made = device_auth._random_account(unborn.folder, "owner", "", False)
    assert made == {"username": "owner", "key": "interrupted"}
    assert ni.read_account(unborn.folder)["anonymous"] is True
    assert not (unborn.folder / "node_ed25519.key").exists() and unborn.arrived == []
