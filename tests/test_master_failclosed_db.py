"""A master whose key is not the pin refuses to start (MASTER-FAILCLOSED,
BACKLOG B-113): the signals that say a backend is meant to be the master, the
role a start decides once, the refusal before anything writes — on a real
database, with a throwaway key pinned in place of the shipped master's."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import psycopg2
import psycopg2.pool
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import db_pool  # noqa: E402
import master_node as backend_master  # noqa: E402
import p2p_identity  # noqa: E402
from desktop import node_identity  # noqa: E402
from desktop.db_init import MIGRATIONS_TABLE  # noqa: E402
from desktop.p2p import master_node  # noqa: E402
from tests.conftest import PG, _recreate  # noqa: E402

EMPTY_DATABASE = "sautium_test_master_failclosed_empty"


class _Node:
    def __init__(self, key: Ed25519PrivateKey, username: str = "Sautium"):
        self.key = key
        raw = key.public_key().public_bytes_raw()
        self.pub = raw.hex()
        self.code = node_identity.make_invite_code(username, raw)
        self.username = username

    def place(self, d: Path) -> Path:
        """This node's key and info in the identity folder `d`."""
        d.mkdir(parents=True, exist_ok=True)
        (d / "node_ed25519.key").write_bytes(self.key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        (d / "node_info.json").write_text(json.dumps({
            "node_id": self.pub, "public_key_hex": self.pub, "username": self.username,
            "invite_code": self.code}))
        return d


@pytest.fixture
def pin(monkeypatch):
    """A test key in place of the shipped master's, in both mirrors."""
    node = _Node(Ed25519PrivateKey.generate())
    for mod in (backend_master, master_node):
        monkeypatch.setattr(mod, "MASTER_PUBKEY_HEX", node.pub)
        monkeypatch.setattr(mod, "MASTER_INVITE_CODE", node.code)
    return node


@pytest.fixture(autouse=True)
def _no_cached_identity():
    p2p_identity.forget_cached()
    yield
    p2p_identity.forget_cached()


def _pool(monkeypatch, dsn):
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    return pool


@pytest.fixture
def db(scratch_dsn, monkeypatch):
    pool = _pool(monkeypatch, scratch_dsn)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM user_settings WHERE key = %s", (node_identity.SIGNING_KEY_SETTING,))
    yield conn
    conn.close()
    pool.closeall()


@pytest.fixture
def empty_db(monkeypatch):
    """A database no migration ran on: a node's first start."""
    if not _recreate(EMPTY_DATABASE, create=True):
        pytest.skip("no PostgreSQL for a scratch database")
    pool = _pool(monkeypatch, f"postgresql://{PG['user']}:{PG['password']}@{PG['host']}:"
                              f"{PG['port']}/{EMPTY_DATABASE}")
    yield
    pool.closeall()
    _recreate(EMPTY_DATABASE, create=False)


def _record(conn, pubkey: str) -> None:
    with conn.cursor() as cur:
        cur.execute(node_identity.RECORD_SIGNING_KEY_SQL, {"key": json.dumps(pubkey)})


def _settings_rows(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT key, value::text, updated_at FROM user_settings ORDER BY key")
        return cur.fetchall()


def _settings(d: Path, **kw):
    base = dict(p2p_identity_dir=str(d), p2p_username="", p2p_password="", p2p_email="",
                sautium_master=False, p2p_trusted_front=False)
    return SimpleNamespace(**{**base, **kw})


def test_the_database_s_record_of_the_pin_refuses_another_key(db, pin, tmp_path):
    other = _Node(Ed25519PrivateKey.generate())
    d = other.place(tmp_path / "id")
    _record(db, pin.pub)
    before = _settings_rows(db)
    words = p2p_identity.master_refusal(_settings(d))
    assert words == (f"This backend is the Sautium master (its database records {pin.code} as its "
                     f"key), but its identity folder holds {other.code}. Check that the folder is "
                     f"the master's. Nothing was written, signed or announced; not starting.")
    assert master_node.is_master() is False
    assert _settings_rows(db) == before                       # read, never written


def test_the_flag_refuses_on_a_database_that_never_held_the_master(empty_db, pin, tmp_path):
    other = _Node(Ed25519PrivateKey.generate())
    d = other.place(tmp_path / "id")
    assert p2p_identity.master_refusal(_settings(d)) is None  # the documented gap
    words = p2p_identity.master_refusal(_settings(d, sautium_master=True))
    assert words.startswith(f"This backend is the Sautium master (SAUTIUM_MASTER=1: its key is "
                            f"{pin.code}), but its identity folder holds {other.code}.")


def test_both_signals_are_named_and_a_record_of_its_own_key_does_not_lift_the_flag(db, pin, tmp_path):
    other = _Node(Ed25519PrivateKey.generate())
    d = other.place(tmp_path / "id")
    _record(db, other.pub)
    assert "(SAUTIUM_MASTER=1: its key is" in p2p_identity.master_refusal(_settings(d, sautium_master=True))
    _record(db, pin.pub)
    assert (f"(its database records {pin.code} as its key; SAUTIUM_MASTER=1)"
            in p2p_identity.master_refusal(_settings(d, sautium_master=True)))


def test_a_name_the_nickname_rule_refuses_is_named_not_raised(db, pin, tmp_path):
    """A keyboard's layout switched mid-word (a Cyrillic "і") is a B-113 of the
    name: the refusal names it as given, with no traceback and no second
    derivation."""
    _record(db, pin.pub)
    (tmp_path / "id").mkdir()
    name = "Saut\u0456um"
    words = p2p_identity.master_refusal(_settings(tmp_path / "id", p2p_username=name,
                                                  p2p_password="the master's, maybe"))
    key = p2p_identity.derive_private_key(name, "the master's, maybe")
    code = node_identity.make_invite_code(name, key.public_key().public_bytes_raw())
    assert f"but its credentials derive {code}. Check P2P_USERNAME and P2P_PASSWORD" in words


def test_a_master_with_no_key_at_all_is_refused(db, pin, tmp_path):
    _record(db, pin.pub)
    (tmp_path / "id").mkdir()
    words = p2p_identity.master_refusal(_settings(tmp_path / "id"))
    assert "but it has no key: neither its identity folder nor P2P_USERNAME and P2P_PASSWORD give one" in words


@pytest.mark.parametrize("flag", [False, True])
def test_the_pin_is_the_master_whatever_the_record_or_the_flag(db, pin, tmp_path, flag):
    d = pin.place(tmp_path / "id")
    _record(db, _Node(Ed25519PrivateKey.generate()).pub)
    assert p2p_identity.master_refusal(_settings(d, sautium_master=flag)) is None
    assert master_node.is_master() is True


def test_an_ordinary_node_is_untouched_by_every_signal_it_does_not_carry(db, pin, tmp_path):
    """The master's username and a trusted front are no signal: a person may
    hold either, and is never refused a start for it."""
    namesake = _Node(Ed25519PrivateKey.generate(), username=backend_master.MASTER_USERNAME)
    d = namesake.place(tmp_path / "id")
    _record(db, namesake.pub)
    assert p2p_identity.master_refusal(_settings(d, p2p_trusted_front=True)) is None
    assert master_node.is_master() is False


def test_the_role_is_read_only_after_a_start_decided_it(monkeypatch):
    monkeypatch.setattr(master_node, "_role", None)
    with pytest.raises(RuntimeError):
        master_node.is_master()
    assert master_node.decide(None) is False and master_node.is_master() is False


def test_the_master_s_own_paths_read_the_decision_not_the_key(pin, monkeypatch):
    """§2: a start that decided "not the master" is not one, whatever its
    identity says later — and the other way round."""
    from fastapi import HTTPException

    from routers import peer_diag, support
    monkeypatch.setattr(peer_diag, "resolve_identity", lambda s: {"public_key_hex": pin.pub})
    master_node.decide("cd" * 32)
    assert peer_diag._is_master() is False
    with pytest.raises(HTTPException):
        support._require_master()
    master_node.decide(pin.pub)
    monkeypatch.setattr(peer_diag, "resolve_identity", lambda s: {"public_key_hex": "cd" * 32})
    assert peer_diag._is_master() is True
    support._require_master()


# A real start of the backend's lifespan, as uvicorn runs it, up to the refusal.
# The pin is the only thing the driver changes.
_DRIVER = r"""
import asyncio, sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
pin, code = sys.argv[3], sys.argv[4]
import master_node
from desktop.p2p import master_node as role
for module in (master_node, role):
    module.MASTER_PUBKEY_HEX, module.MASTER_INVITE_CODE = pin, code
import main

async def start():
    async with main.lifespan(main.app):
        print("STARTED", flush=True)

asyncio.run(start())
"""


def _snapshot(conn, d: Path):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM friends")
        friends = cur.fetchone()
        cur.execute("SELECT count(*) FROM signing_batches")
        batches = cur.fetchone()
        cur.execute(f"SELECT count(*) FROM {MIGRATIONS_TABLE}")
        ledger = cur.fetchone()
    files = {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}
    return _settings_rows(conn), friends, batches, ledger, files


def test_the_lifespan_refuses_before_anything_writes_and_again_at_the_restart(db, scratch_dsn, pin,
                                                                              tmp_path):
    from urllib.parse import urlparse
    username, password = "Sautium", "a-password-one-character-off"
    derived = p2p_identity.derive_identity(username, password)["invite_code"]
    d = tmp_path / "id"
    d.mkdir()
    (d / "birth_certificate.json").write_text('{"the master\'s": "certificate"}')
    _record(db, pin.pub)
    url = urlparse(scratch_dsn)
    env = dict(os.environ, POSTGRES_HOST=url.hostname, POSTGRES_PORT=str(url.port),
               POSTGRES_USER=url.username, POSTGRES_PASSWORD=url.password,
               POSTGRES_DB=url.path.lstrip("/"), P2P_IDENTITY_DIR=str(d),
               P2P_USERNAME=username, P2P_PASSWORD=password, SAUTIUM_MASTER="0")
    before = _snapshot(db, d)
    for _attempt in range(2):
        run = subprocess.run([sys.executable, "-c", _DRIVER, str(BACKEND), str(ROOT), pin.pub, pin.code],
                             cwd=BACKEND, env=env, capture_output=True, text=True, timeout=300)
        out = run.stdout + run.stderr
        from uvicorn.main import STARTUP_FAILURE
        assert run.returncode == STARTUP_FAILURE, out[-3000:]
        assert "STARTED" not in out and "Traceback" not in out, out[-3000:]
        refusals = [line for line in out.splitlines() if "This backend is the Sautium master" in line]
        assert len(refusals) == 1, out[-3000:]
        assert " - ERROR - " in refusals[0]
        assert f"its database records {pin.code} as its key" in refusals[0]
        assert f"but its credentials derive {derived}. Check P2P_USERNAME and P2P_PASSWORD" in refusals[0]
        assert _snapshot(db, d) == before
