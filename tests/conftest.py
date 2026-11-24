"""Shared setup for the test tree.

The suite runs inside the backend container — the node's own environment,
its database named in POSTGRES_DB — and nothing here may reach that
database. A test that needs PostgreSQL takes `scratch_dsn` (a throwaway
database built from the migrations for its module) or builds its own, and
points db_pool and the SQLAlchemy sessions at it. Everything that connects
through config.settings instead lands in this session's database, created
empty before collection and dropped at the end: modules that read the
database at import still import (mb_backend takes an absent mb_artist for
"no dump", the same answer on every node), while anything that writes there
— a timer that outlives its test, a pool re-created lazily after a fixture
closed its scratch one — fails on the missing table instead of writing the
node's catalog. On 2026-10-09 a leaked queue persist overwrote the node's
player.queue, and the backend's next start crash-looped on it.

HQPlayer's folder on this machine (HQPLAYER_DATA_DIR) is the run's own, empty
and removed at the end, for the same reason: the diagnostics, the rate check
and the benchmark read HQPlayer's settings.xml and its log there, and a test
that needs either writes its own.

Two real nodes syncing are tested here in one process: the launcher's sync
server and the Docker peer surface, each served over TLS bound to its own key
on a database of its own, each node's walk pulling from the other and
carrying to it (test_two_node_sync.py, which says what the one process
shares between them). Between machines it is the two-node checks (launcher
stand <-> Docker) and the `--selftest` CLIs.

Run from the repo root: `python -m pytest tests/`.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
# Named per run when two runs share a cluster (a worktree beside the
# checkout). A module that builds a database of its own names it itself:
# two runs keep apart only while they run different ones of those.
SESSION_DATABASE = os.environ.get("SAUTIUM_TEST_PGDATABASE", "sautium_pytest_session")
# The run drops it and makes it afresh: never a database of the node's own
if not SESSION_DATABASE.startswith("sautium_pytest"):
    raise pytest.UsageError(f"SAUTIUM_TEST_PGDATABASE names {SESSION_DATABASE!r}: the run drops "
                            "its session database, so only a sautium_pytest* name is taken")

# Unset, HQPlayer's folder is the platform's own (~/.hqplayer on macOS): a
# machine with HQPlayer Desktop beside the checkout had its real settings and
# log decide what tests expect from nothing.
HQPLAYER_DIR = tempfile.mkdtemp(prefix="sautium-test-hqplayer-")

# Before any backend module builds config.settings.
os.environ.update(POSTGRES_HOST=PG["host"], POSTGRES_PORT=str(PG["port"]),
                  POSTGRES_USER=PG["user"], POSTGRES_PASSWORD=PG["password"],
                  POSTGRES_DB=SESSION_DATABASE, HQPLAYER_DATA_DIR=HQPLAYER_DIR)


def _admin():
    """The cluster's maintenance connection, or None where there is no
    cluster (no psycopg2, no server) — the database tests skip themselves."""
    try:
        import psycopg2
    except ImportError:
        return None
    try:
        conn = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError:
        return None
    conn.autocommit = True
    return conn


def _recreate(name: str, *, create: bool) -> bool:
    admin = _admin()
    if admin is None:
        return False
    from psycopg2 import sql
    database = sql.Identifier(name)   # quoted: the name connects exactly as spelled
    with admin.cursor() as cur:
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(database))
        if create:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(database))
    admin.close()
    return True


# The assistant's login is a cluster role, so the run makes one of its own,
# named after its session database: a run on a node's own cluster never
# touches the node's login (its password, its settings, its sessions).
ASSISTANT_ROLE = "sautium_assistant_" + SESSION_DATABASE.removeprefix("sautium_")


def pytest_configure(config):
    config.addinivalue_line("markers", "models_switch: the test reads the owner's switch "
                                       '"AI models on this machine" from its own database')


@pytest.fixture(autouse=True)
def _models_switch_never_touched(request, monkeypatch):
    """The owner's switch "AI models on this machine" is a row of
    user_settings the hardware profile reads when it is first resolved — on
    a node, after its migrations. A test that resolves the profile with no
    node's database behind it runs as a node whose owner never touched the
    switch; the tests of the switch itself (`pytest.mark.models_switch`)
    read the real row."""
    if request.node.get_closest_marker("models_switch") is not None:
        return
    try:
        import hardware_profile
    except ImportError:
        return                      # a test with no backend on its path resolves no profile
    monkeypatch.setattr(hardware_profile, "models_switch", lambda: True)


@pytest.fixture(autouse=True)
def _role_decided(monkeypatch):
    """Every process's start decides whether it is the master
    (desktop/p2p/master_node.decide) before anything reads it; a test runs as
    a process that decided it is not. A test of the master's paths decides
    the role itself, a test of the decision starts from none."""
    from desktop.p2p import master_node
    monkeypatch.setattr(master_node, "_role", False)


@pytest.fixture(autouse=True)
def _no_look_left_over():
    """What the play path's latest look observed of single files is process
    state (playback.substitute.observe), as a node's is: a test starts with
    none, as a process does. Read off the modules loaded — a test that never
    loaded the play path has none."""
    yield
    substitute = sys.modules.get("playback.substitute")
    if substitute is not None:
        substitute.observe(())


def pytest_sessionstart(session):
    if not _recreate(SESSION_DATABASE, create=True):
        return
    # A node's setup makes the login before any database work
    # (desktop/db_init.ensure_assistant_role); the code that grants to it (the
    # migration runner, the LB loader's swap) expects it. Every caller reads
    # db_init.ASSISTANT_ROLE when it runs.
    from desktop import db_init
    db_init.ASSISTANT_ROLE = ASSISTANT_ROLE
    admin = _admin()
    with admin.cursor() as cur:
        db_init.ensure_assistant_role(cur, PG["password"])
    admin.close()


def pytest_sessionfinish(session, exitstatus):
    try:
        if _recreate(SESSION_DATABASE, create=False):
            # Its grants went with the scratch databases and the session's own;
            # a scratch database a killed run left behind still holds some, and
            # the drop then fails, naming it.
            admin = _admin()
            with admin.cursor() as cur:
                cur.execute(f'DROP ROLE IF EXISTS "{ASSISTANT_ROLE}"')
            admin.close()
    finally:
        shutil.rmtree(HQPLAYER_DIR)


@pytest.fixture(scope="module")
def scratch_dsn(request):
    """A throwaway database built from the migrations for the requesting
    module, dropped when the module is done."""
    name = "sautium_test_" + request.module.__name__.rsplit(".", 1)[-1].removeprefix("test_")
    if not _recreate(name, create=True):
        pytest.skip("no PostgreSQL for a scratch database")
    import psycopg2
    from desktop import db_init
    conn = psycopg2.connect(dbname=name, **PG)
    db_init.apply_migrations(conn)
    conn.commit()
    conn.close()
    yield f"postgresql://{PG['user']}:{PG['password']}@{PG['host']}:{PG['port']}/{name}"
    _recreate(name, create=False)
