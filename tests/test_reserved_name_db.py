"""The nickname Sautium is kept for Support (SUPPORT-FIRST, BACKLOG B-132): no
new account takes it or a name that reads as it, at the boundaries where an
account is made — the Web UI's setup, a rotation into it, the launcher's
account step — and a node that
holds it keeps it. On a real database, with a throwaway key pinned in place of
the shipped master's; the Worker's own boundary is in tests/p2p/
test_worker_limits.py."""

import json
import sys
from pathlib import Path

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


class _Pair(_Key):
    """The key a nickname and a password make (at the suite's cost)."""

    def __init__(self, username: str, password: str):
        self.key = node_identity.key_from_seed(node_identity.derive_seed(username, password))
        raw = self.key.public_key().public_bytes_raw()
        self.pub, self.username, self.password = raw.hex(), username, password
        self.code = node_identity.make_invite_code(username, raw)


MASTER_PASSWORD = "the master's password amber lanterns ford"


@pytest.fixture
def pin(monkeypatch):
    node = _Pair("Sautium", MASTER_PASSWORD)
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
def setup(births, tmp_path, monkeypatch):
    """The Web UI's setup in a folder of `tmp_path`: `create(folder, name,
    password, **kw)` makes the account and ends its key's derivation; what
    it placed, or None."""
    from routers import p2p as p2p_router
    monkeypatch.setattr(device_auth, "_create_lock", __import__("asyncio").Lock())
    monkeypatch.setattr(device_auth, "_held", None)
    monkeypatch.setattr(device_auth, "_key_arrived", lambda pubkey: None)
    monkeypatch.setattr(device_auth, "_record_signing_key", lambda _pubkey: None)
    monkeypatch.setattr(p2p_router, "_publish_identity_state", lambda state: None)
    monkeypatch.setitem(device_auth._doors, "setup", {"failures": 0, "shut_until": 0.0, "in_flight": 0})

    def create(folder, username, password, **kw):
        import asyncio
        monkeypatch.setattr(config.settings, "p2p_identity_dir", str(folder))
        device_auth.prepare_setup()
        asyncio.run(device_auth.create_account(username, password, device_auth._setup_code, **kw))
        births.finish()
        info = folder / "node_info.json"
        return json.loads(info.read_text()) if (folder / "node_ed25519.key").exists() else None
    create.births = births
    return create


def _settings(monkeypatch, **kw):
    for k, v in {"sautium_master": False,
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


# --- the Web UI's setup (device_auth.create_account, its key's end) -------------

def test_a_new_account_under_the_name_is_refused_before_any_derivation(db, pin, setup, tmp_path,
                                                                       monkeypatch):
    _settings(monkeypatch)
    monkeypatch.setattr(config.settings, "p2p_identity_dir", str(tmp_path))
    device_auth.prepare_setup()
    with pytest.raises(ValueError, match="kept for Sautium's own Support"):
        import asyncio
        asyncio.run(device_auth.create_account("Sautium", "a password amber lanterns ford",
                                               device_auth._setup_code))
    assert setup.births == []
    assert not (tmp_path / "account.json").exists() and not (tmp_path / "node_ed25519.key").exists()


def test_the_master_on_a_new_database_makes_its_account_and_another_key_there_does_not(
        db, pin, setup, tmp_path, monkeypatch):
    _settings(monkeypatch, sautium_master=True)
    assert setup(tmp_path / "other", "Sautium", "a password amber lanterns ford") is None
    assert not (tmp_path / "other" / "account.json").exists()          # the key decided: taken back
    info = setup(tmp_path / "master", "Sautium", MASTER_PASSWORD)
    assert info["public_key_hex"] == pin.pub


def test_a_holder_brings_its_identity_back_and_its_records_name_no_other(db, pin, setup, tmp_path,
                                                                        monkeypatch):
    _settings(monkeypatch)
    holder = _Pair("sautium", "its password amber lanterns ford")
    _record(db, holder.pub)
    info = setup(tmp_path / "back", "sautium", holder.password)
    assert info["public_key_hex"] == holder.pub
    # a new identity over a holder's records takes the name no more than a stranger does
    assert setup(tmp_path / "new", "sautium", "another amber lanterns ford", new_identity=True) is None


def test_any_other_name_is_made_as_before(db, pin, setup, tmp_path, monkeypatch):
    _settings(monkeypatch)
    assert setup(tmp_path, "alice", "a password amber lanterns ford")["username"] == "alice"


# --- a rotation (desktop/node_identity.rotate_identity) ------------------------

def _identity_folder(d: Path, node: _Key) -> Path:
    from cryptography.hazmat.primitives import serialization
    d.mkdir(parents=True, exist_ok=True)
    (d / "node_ed25519.key").write_bytes(node.key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    (d / "node_info.json").write_text(json.dumps({**node.info(), "algorithm": "Ed25519"}))
    return d


def test_a_rotation_into_the_name_is_refused_before_its_key_is_made(tmp_path, monkeypatch):
    d = _identity_folder(tmp_path / "id", _Key("alice"))
    with pytest.raises(ValueError, match="kept for Sautium's own Support"):
        node_identity.check_rotation_name(d, "SAUTIUM")
    with pytest.raises(ValueError, match="kept for Sautium's own Support"):
        node_identity.rotate_identity(d, "SAUTIUM", Ed25519PrivateKey.generate(), False, None)
    assert json.loads((d / "node_info.json").read_text())["username"] == "alice"


def test_a_holder_changes_its_password_and_keeps_its_name(tmp_path, monkeypatch):
    d = _identity_folder(tmp_path / "id", _Key("Sautium"))
    node_identity.write_account(d, node_identity.new_account("Sautium", "an old one", source="form"))
    result = node_identity.rotate_identity(d, "sautium", Ed25519PrivateKey.generate(), False,
                                           node_identity.signin_hash("a new password"))
    assert result["info"]["username"] == "sautium"
    assert node_identity.read_account(d)["username"] == "sautium"
