"""The DHT's routing table kept between runs (desktop/p2p/dht_state.py), on a
real PostgreSQL (PI-NODE-2D §8.3, review B1).

The save that matters most is the last one, at the node's stop — and the
compose files stop PostgreSQL by SIGTERM, its smart shutdown: it serves the
clients it has until they leave and takes no new connection. The store
opened a connection for every call, so the stop's save was refused ("the
database system is shutting down") and the run's routing table was lost with
"DHT: state save failed".

A test cannot signal the suite's PostgreSQL, so the same state is made for
one database: `ALTER DATABASE … ALLOW_CONNECTIONS false`, asked from another
database of the cluster — every connection it has goes on being served, and
no new one is taken. The case under the
real signal, PostgreSQL's and the backend's containers stopped at once, is
the slice's lab table (stop_lab_dht.sh, node_stop_lab.sh).

- The store saves through the one connection it has held since its load;
  a store that must connect for the save is refused.
- The service's stop saves through it and only then lets it go.
- A connection cut under the store is made again at the next call — while a
  new one is taken; where none is, the failure is the caller's.
"""

import asyncio
import importlib
import time
from types import SimpleNamespace

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from desktop.p2p.dht_state import DhtStateStore  # noqa: E402
from tests.conftest import PG, SESSION_DATABASE  # noqa: E402


@pytest.fixture
def admin(scratch_dsn):
    """Two connections of the test's own, made before any is refused: one in
    another database of the cluster, which says who is connected to the
    scratch one and whether it takes new connections (PostgreSQL does not
    let a database be closed from inside it), one in the scratch database to
    read what was saved. Every store a test made is closed at its end."""
    name = scratch_dsn.rsplit("/", 1)[1]
    reader = psycopg2.connect(scratch_dsn)
    outside = psycopg2.connect(dbname=SESSION_DATABASE, **PG)
    reader.autocommit = outside.autocommit = True
    stores = []

    def store():
        stores.append(DhtStateStore(scratch_dsn))
        return stores[-1]
    with reader.cursor() as read, outside.cursor() as cur:
        read.execute("TRUNCATE p2p_dht_state")
        read.execute("SELECT pg_backend_pid()")
        try:
            yield SimpleNamespace(cur=cur, read=read, name=name, own=read.fetchone()[0], store=store)
        finally:
            cur.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')
            for made in stores:
                made.close()
    reader.close()
    outside.close()


def _others(admin) -> set:
    """The connections to the scratch database but the test's own reader."""
    admin.cur.execute("SELECT pid FROM pg_stat_activity WHERE datname = %s AND pid <> %s", (admin.name, admin.own))
    return {pid for (pid,) in admin.cur.fetchall()}


def _left(admin, pids: set) -> bool:
    """Whether those connections are gone from the server — which ends a
    backend a moment after its client closed, or after it was cut."""
    deadline = time.monotonic() + 10
    while _others(admin) & pids and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _others(admin) & pids


def _takes_no_new_connection(admin) -> None:
    admin.cur.execute(f'ALTER DATABASE "{admin.name}" ALLOW_CONNECTIONS false')


def _saved(admin):
    admin.read.execute("SELECT state FROM p2p_dht_state WHERE id = 1")
    row = admin.read.fetchone()
    return bytes(row[0]) if row else None


def test_the_store_saves_through_the_connection_it_has_held_since_its_load(scratch_dsn, admin):
    store = admin.store()
    assert store.load() is None                                # the service's start
    held = _others(admin)
    assert len(held) == 1
    store.save(b"a periodic save")
    assert store.load() == b"a periodic save" and _others(admin) == held      # one connection, kept
    _takes_no_new_connection(admin)                            # PostgreSQL told to stop
    with pytest.raises(psycopg2.OperationalError, match="not currently accepting connections"):
        admin.store().save(b"through a connection opened for the save")
    store.save(b"the routing table at the stop")
    assert _saved(admin) == b"the routing table at the stop" and _others(admin) == held
    # let go after the save: a PostgreSQL that waits for its clients waits for this one no longer
    store.close()
    assert _left(admin, held)
    store.close()                                              # again: nothing to do


def test_the_services_stop_saves_through_the_held_connection_and_then_lets_it_go(scratch_dsn, admin, monkeypatch):
    module = importlib.import_module("desktop.p2p.dht_service")
    # libtorrent's own two calls, which hand the routing table out as bytes
    monkeypatch.setattr(module, "lt", SimpleNamespace(
        save_state_flags_t=SimpleNamespace(save_dht_state=1),
        write_session_params_buf=lambda state: state), raising=False)
    svc = module.DHTService(sharing_enabled=lambda: True, state_store=admin.store())
    assert svc._state_store.load() is None                     # as start() does, before the session is made
    svc._session = SimpleNamespace(session_state=lambda flags: b"the run's routing table", pause=lambda: None)
    held = _others(admin)
    assert len(held) == 1
    _takes_no_new_connection(admin)

    async def stop():
        svc._lane.start()
        await svc.stop()
    asyncio.run(stop())
    assert _saved(admin) == b"the run's routing table"
    assert _left(admin, held)


def test_a_connection_cut_under_the_store_is_made_again_at_the_next_call(scratch_dsn, admin):
    store = admin.store()
    store.save(b"before")
    first = _others(admin)
    assert len(first) == 1
    admin.cur.execute("SELECT pg_terminate_backend(%s)", tuple(first))        # PostgreSQL restarted under the node
    assert _left(admin, first)
    store.save(b"after")                                       # the periodic save finds it, and connects anew
    second = _others(admin)
    assert len(second) == 1 and second != first and _saved(admin) == b"after"
    # cut again, and no new connection taken: nothing to save through, and the caller is told
    _takes_no_new_connection(admin)
    admin.cur.execute("SELECT pg_terminate_backend(%s)", tuple(second))
    assert _left(admin, second)
    with pytest.raises(psycopg2.OperationalError, match="not currently accepting connections"):
        store.save(b"lost")
    admin.cur.execute(f'ALTER DATABASE "{admin.name}" ALLOW_CONNECTIONS true')
    assert store.load() == b"after"                            # and the store works on
    store.close()
