"""The identity's appearance starts the walk, with no restart, on a real
PostgreSQL (backend/main.py, desktop/p2p/sync_walk.py).

A node with no identity asks no peer (tests/p2p/test_no_identity_no_dial.py).
When its account is made the process that made it says so to its followers
(p2p_identity.changed), and one of them asks for a run on the walk's own
channel — which the walk hears wherever it runs: in this backend on a Docker
node, in the launcher's process in launcher mode. An event the node already
had, never a timer: the walk's interval would meet the identity too, at its
next tick.
"""

import asyncio
import sys

import pytest

from tests.conftest import REPO_ROOT

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.pool  # noqa: E402

BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def test_the_identitys_event_asks_the_walk_for_a_run(scratch_dsn, monkeypatch):
    monkeypatch.chdir(BACKEND)        # main mounts static/ relative to its directory
    import db_pool
    import main
    import p2p_identity
    from desktop.p2p import sync_walk
    pool = psycopg2.pool.ThreadedConnectionPool(1, 3, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    # the start registers it with the peer surface's, the announce's and the gate's
    assert main._requesters_follow_identity in main._IDENTITY_FOLLOWERS
    monkeypatch.setattr(p2p_identity, "_followers", [main._requesters_follow_identity])
    asked = []

    async def scenario():
        running, listening, heard = [True], asyncio.Event(), asyncio.Event()

        def request():                 # as main and P2PManager wire it: walk.request("request")
            asked.append("request")
            heard.set()
        listener = asyncio.create_task(sync_walk.listen_notifications(
            scratch_dsn, {"sautium_sync_request": request}, lambda: running[0], on_listening=listening.set))
        await asyncio.wait_for(listening.wait(), 10)
        assert asked == []
        await p2p_identity.changed()   # routers/auth.create_account, device_auth._after_rotation
        await asyncio.wait_for(heard.wait(), 10)
        running[0] = False
        listener.cancel()
    try:
        asyncio.run(scenario())
    finally:
        pool.closeall()
    assert asked == ["request"]
