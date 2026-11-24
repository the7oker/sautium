"""The nickname Sautium is kept for Support (SUPPORT-FIRST, BACKLOG B-132): no
new account takes it or a name that reads as it, at the boundaries where an
account is made — the Web UI's setup, a rotation into it, a start that makes
its account from P2P_USERNAME, the launcher's account step — and a node that
holds it keeps it. On a real database, with a throwaway key pinned in place of
the shipped master's; the Worker's own boundary is in tests/p2p/
test_worker_limits.py."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import psycopg2
import psycopg2.pool
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import config  # noqa: E402
import db_pool  # noqa: E402
import device_auth  # noqa: E402
import master_node as backend_master  # noqa: E402
import p2p_identity  # noqa: E402
from desktop import node_identity  # noqa: E402
from desktop.p2p import master_node  # noqa: E402

REFUSED = ["Sautium", "sautium", "SAUTIUM", "SAUTlUM", "Saut1um", "5autium", "Sau_tium",
           "Sautium-", "s-a-u-t-i-u-m", "SAUT1UM"]
TAKEN = ["SautiumFan", "Sautium_Support", "Saturn", "alice", "Sautiu", "Sautiums"]


class _Key:
    def __init__(self, username: str):
        self.key = Ed25519PrivateKey.generate()
        raw = self.key.public_key().public_bytes_raw()
        self.pub = raw.hex()
        self.username = username
        self.code = node_identity.make_invite_code(username, raw)

    def info(self) -> dict:
        return {"node_id": self.pub, "public_key_hex": self.pub, "username": self.username,
                "invite_code": self.code, "email": ""}


@pytest.fixture
def pin(monkeypatch):
    node = _Key("Sautium")
    for mod in (backend_master, master_node):
        monkeypatch.setattr(mod, "MASTER_PUBKEY_HEX", node.pub)
        monkeypatch.setattr(mod, "MASTER_INVITE_CODE", node.code)
    return node


@pytest.fixture
def db(scratch_dsn, monkeypatch):
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM user_settings WHERE key IN (%s, 'p2p.bound_identity')",
                    (node_identity.SIGNING_KEY_SETTING,))
    p2p_identity.forget_cached()
    yield conn
    p2p_identity.forget_cached()
    conn.close()
    pool.closeall()


def _record(conn, pubkey: str) -> None:
    with conn.cursor() as cur:
        cur.execute(node_identity.RECORD_SIGNING_KEY_SQL, {"key": json.dumps(pubkey)})


@pytest.fixture
def derive(monkeypatch):
    """The Web UI's setup derivation, answered by `derive.key` — and every
    call counted, so a refusal can be shown to cost none."""
    calls = []
    state = SimpleNamespace(key=None, calls=calls)

    def identity(username, password, email=""):
        calls.append(username)
        return {**state.key.info(), "username": username}

    monkeypatch.setattr(p2p_identity, "derive_identity", identity)
    monkeypatch.setattr(p2p_identity, "derive_private_key", lambda u, p: state.key.key)
    monkeypatch.setattr(device_auth, "_record_signing_key", lambda _pubkey: None)
    return state


def _settings(monkeypatch, **kw):
    for k, v in {"sautium_master": False, "p2p_username": "", "p2p_password": "",
                 "p2p_identity_dir": "", **kw}.items():
        monkeypatch.setattr(config.settings, k, v)


# --- the rule -----------------------------------------------------------------

@pytest.mark.parametrize("name", REFUSED)
def test_a_name_that_reads_as_sautium_is_refused_for_a_new_account(name):
    assert node_identity.is_reserved_username(name)
    with pytest.raises(ValueError, match="kept for Sautium's own Support"):
        node_identity.validate_new_username(name)


@pytest.mark.parametrize("name", TAKEN)
def test_a_name_beside_it_is_taken(name):
    assert not node_identity.is_reserved_username(name)
    node_identity.validate_new_username(name)


def test_the_shape_rule_still_answers_first_and_the_backend_reads_the_same_rule():
    with pytest.raises(ValueError, match="3-32 Latin letters"):
        node_identity.validate_new_username("Ѕаutium")          # Cyrillic letters: the shape refuses
    with pytest.raises(ValueError, match="3-32 Latin letters"):
        node_identity.validate_new_username("Sautium\n")       # `$` alone would take it
    assert p2p_identity.validate_username is node_identity.validate_username


def test_the_reserved_skeleton_is_supports_own_name():
    assert node_identity.username_skeleton(master_node.MASTER_USERNAME) in node_identity.RESERVED_SKELETONS


# --- the Web UI's setup (device_auth._write_identity) --------------------------

def test_a_new_account_under_the_name_is_refused_before_any_derivation(db, pin, derive, tmp_path,
                                                                       monkeypatch):
    _settings(monkeypatch)
    derive.key = _Key("Sautium")
    with pytest.raises(ValueError, match="kept for Sautium's own Support"):
        device_auth._write_identity(tmp_path, "Sautium", "a password")
    assert derive.calls == []
    assert not (tmp_path / "node_ed25519.key").exists()


def test_the_master_on_a_new_database_makes_its_account_and_another_key_there_does_not(
        db, pin, derive, tmp_path, monkeypatch):
    _settings(monkeypatch, sautium_master=True)
    derive.key = _Key("Sautium")                           # the pair derives another key
    with pytest.raises(ValueError, match="kept for Sautium's own Support"):
        device_auth._write_identity(tmp_path / "other", "Sautium", "a password")
    assert derive.calls == ["Sautium"]                     # the key decides: derived first
    assert not (tmp_path / "other" / "node_ed25519.key").exists()
    derive.key = pin
    info = device_auth._write_identity(tmp_path / "master", "Sautium", "the master's password")
    assert info["public_key_hex"] == pin.pub


def test_a_holder_brings_its_identity_back_and_its_records_name_no_other(db, pin, derive, tmp_path,
                                                                        monkeypatch):
    _settings(monkeypatch)
    holder = _Key("sautium")
    _record(db, holder.pub)
    derive.key = holder
    info = device_auth._write_identity(tmp_path / "back", "sautium", "its password")
    assert info["public_key_hex"] == holder.pub
    # a new identity over a holder's records takes the name no more than a stranger does
    derive.key = _Key("sautium")
    with pytest.raises(ValueError, match="kept for Sautium's own Support"):
        device_auth._write_identity(tmp_path / "new", "sautium", "another", new_identity=True)


def test_any_other_name_is_made_as_before(db, pin, derive, tmp_path, monkeypatch):
    _settings(monkeypatch)
    derive.key = _Key("alice")
    assert device_auth._write_identity(tmp_path, "alice", "a password")["username"] == "alice"


# --- a rotation (desktop/node_identity.rotate_identity) ------------------------

def _identity_folder(d: Path, node: _Key) -> Path:
    from cryptography.hazmat.primitives import serialization
    d.mkdir(parents=True, exist_ok=True)
    (d / "node_ed25519.key").write_bytes(node.key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    (d / "node_info.json").write_text(json.dumps({**node.info(), "algorithm": "Ed25519"}))
    return d


def test_a_rotation_into_the_name_is_refused_before_its_derivation(tmp_path, monkeypatch):
    seeds = []
    monkeypatch.setattr(node_identity, "derive_seed",
                        lambda u, p: seeds.append(u) or Ed25519PrivateKey.generate().private_bytes_raw())
    d = _identity_folder(tmp_path / "id", _Key("alice"))
    with pytest.raises(ValueError, match="kept for Sautium's own Support"):
        node_identity.rotate_identity(d, "SAUTIUM", "a new password", anonymous=False)
    assert seeds == []
    assert json.loads((d / "node_info.json").read_text())["username"] == "alice"


def test_a_holder_changes_its_password_and_keeps_its_name(tmp_path, monkeypatch):
    monkeypatch.setattr(node_identity, "derive_seed",
                        lambda u, p: Ed25519PrivateKey.generate().private_bytes_raw())
    d = _identity_folder(tmp_path / "id", _Key("Sautium"))
    result = node_identity.rotate_identity(d, "sautium", "a new password", anonymous=False)
    assert result["info"]["username"] == "sautium"


# --- a start that makes its account from P2P_USERNAME --------------------------

def _env_node(monkeypatch, node: _Key, tmp_path: Path):
    _settings(monkeypatch, p2p_username=node.username, p2p_password="from .env",
              p2p_identity_dir=str(tmp_path / "empty"))
    monkeypatch.setattr(device_auth, "_expected_pubkey", lambda: node.pub)


def test_a_start_making_its_account_under_the_name_with_another_key_refuses(db, pin, tmp_path,
                                                                            monkeypatch):
    node = _Key("Sautium")
    _env_node(monkeypatch, node, tmp_path)
    assert device_auth.reserved_name_refusal() == (
        "P2P_USERNAME names the reserved nickname Sautium, and this node's key is not Support's "
        "— the node does not start; choose another nickname in .env")


def test_a_node_made_by_its_env_before_this_release_keeps_starting(db, pin, tmp_path, monkeypatch):
    node = _Key("sautium")
    _record(db, node.pub)
    _env_node(monkeypatch, node, tmp_path)
    assert device_auth.reserved_name_refusal() is None


def test_a_node_made_by_its_env_keeps_starting_after_a_new_password(db, pin, tmp_path, monkeypatch):
    """Review round 2: the env's new P2P_PASSWORD derives another key — the
    holder's rotation, which keeps the name; the database names the old one."""
    _record(db, _Key("sautium").pub)
    _env_node(monkeypatch, _Key("sautium"), tmp_path)
    assert device_auth.reserved_name_refusal() is None


def test_the_master_and_any_other_name_start(db, pin, tmp_path, monkeypatch):
    _env_node(monkeypatch, pin, tmp_path)
    assert device_auth.reserved_name_refusal() is None
    _env_node(monkeypatch, _Key("alice"), tmp_path)
    assert device_auth.reserved_name_refusal() is None


def test_a_key_in_the_identity_folder_is_not_the_env_s_account(db, pin, tmp_path, monkeypatch):
    node = _Key("Sautium")
    _env_node(monkeypatch, node, tmp_path)
    _identity_folder(tmp_path / "empty", _Key("alice"))
    assert device_auth.reserved_name_refusal() is None
