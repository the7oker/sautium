"""The DHT's routing table kept between runs (desktop/p2p/dht_state.py), on a
real PostgreSQL (PI-NODE-2D §8.3, review B1).

The save that matters most is the last one, at the node's stop — and the
compose files stop PostgreSQL by SIGTERM, its smart shutdown: it serves the
clients it has until they leave and takes no new connection. The store
opened a connection for every call, so the stop's save was refused ("the
database system is shutting down") and the run's routing table was lost with
"DHT: state save failed".

A test cannot signal the suite's PostgreSQL, so the same state is made for
one database: `ALTER DATABASE … ALLOW_CONNECTIONS false` — every connection
it has goes on being served, and no new one is taken. The case under the
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
from types import SimpleNamespace

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from desktop.p2p.dht_state import DhtStateStore  # noqa: E402


@pytest.fixture
def admin(scratch_dsn):
    """A connection of the test's own, made before any is refused: it reads
    who is connected and puts the database back."""
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        name = cur.fetchone()[0]
        cur.execute("TRUNCATE p2p_dht_state")
        yield SimpleNamespace(cur=cur, name=name)
        cur.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')
    conn.close()


def _others(admin) -> set:
    admin.cur.execute("SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
                      "AND pid <> pg_backend_pid()")
    return {pid for (pid,) in admin.cur.fetchall()}


def _takes_no_new_connection(admin) -> None:
    admin.cur.execute(f'ALTER DATABASE "{admin.name}" ALLOW_CONNECTIONS false')


def _saved(admin):
    admin.cur.execute("SELECT state FROM p2p_dht_state WHERE id = 1")
    row = admin.cur.fetchone()
    return bytes(row[0]) if row else None


def test_the_store_saves_through_the_connection_it_has_held_since_its_load(scratch_dsn, admin):
    store = DhtStateStore(scratch_dsn)
    assert store.load() is None                                # the service's start
    held = _others(admin)
    assert len(held) == 1
    store.save(b"a periodic save")
    assert store.load() == b"a periodic save" and _others(admin) == held      # one connection, kept
    _takes_no_new_connection(admin)                            # PostgreSQL told to stop
    with pytest.raises(psycopg2.OperationalError):
        DhtStateStore(scratch_dsn).save(b"through a connection opened for the save")
    store.save(b"the routing table at the stop")
    assert _saved(admin) == b"the routing table at the stop" and _others(admin) == held
    # let go after the save: a PostgreSQL that waits for its clients waits for this one no longer
    store.close()
    assert _others(admin) == set()
    store.close()                                              # again: nothing to do


def test_the_services_stop_saves_through_the_held_connection_and_then_lets_it_go(scratch_dsn, admin, monkeypatch):
    module = importlib.import_module("desktop.p2p.dht_service")
    # libtorrent's own two calls, which hand the routing table out as bytes
    monkeypatch.setattr(module, "lt", SimpleNamespace(
        save_state_flags_t=SimpleNamespace(save_dht_state=1),
        write_session_params_buf=lambda state: state), raising=False)
    svc = module.DHTService(sharing_enabled=lambda: True, state_store=DhtStateStore(scratch_dsn))
    assert svc._state_store.load() is None                     # as start() does, before the session is made
    svc._session = SimpleNamespace(session_state=lambda flags: b"the run's routing table", pause=lambda: None)
    _takes_no_new_connection(admin)

    async def stop():
        svc._lane.start()
        await svc.stop()
    asyncio.run(stop())
    assert _saved(admin) == b"the run's routing table"
    assert _others(admin) == set()


def test_a_connection_cut_under_the_store_is_made_again_at_the_next_call(scratch_dsn, admin):
    store = DhtStateStore(scratch_dsn)
    store.save(b"before")
    (first,) = _others(admin)
    admin.cur.execute("SELECT pg_terminate_backend(%s)", (first,))            # PostgreSQL restarted under the node
    store.save(b"after")                                       # the periodic save finds it, and connects anew
    (second,) = _others(admin)
    assert second != first and _saved(admin) == b"after"
    # cut again, and no new connection taken: nothing to save through, and the caller is told
    _takes_no_new_connection(admin)
    admin.cur.execute("SELECT pg_terminate_backend(%s)", (second,))
    with pytest.raises(psycopg2.OperationalError):
        store.save(b"lost")
    admin.cur.execute(f'ALTER DATABASE "{admin.name}" ALLOW_CONNECTIONS true')
    assert store.load() == b"after"                            # and the store works on
    store.close()
