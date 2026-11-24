"""A node with no identity dials no peer (desktop/p2p/sync_walk.py, both
slice cycles; the rule since 2026-11-24).

What the walk and the cycles ask says what the node's owner has and listens
to: the tracks with gaps, the names and MBIDs of the owned and engaged
artists. A node with no account has no one who agreed to that. Before
PI-NODE-2D such a dial died at the peer's certificate — true by accident;
once the peer client pinned with or without an identity, an install whose
`.env` names its music folder would have asked all of it of any host with a
node key of its own making, before any account existed. Now the walk and
both cycles make no connection while the node has no key — not for a /health
either — and run by themselves when the identity appears.

A throwaway peer surface over TLS, with a certificate bound to its node key,
counts who reaches its port; the walk's own `connect_peer` is the dial of all
three. The stores are stand-ins: a library with gaps, names and MBIDs
pending.
"""

import asyncio

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop.p2p import lb_slice_cycle, mb_slice_cycle, peer_auth, slice_sources, sync_walk
from desktop.p2p.sync_walk import SyncWalk
from tests.p2p.test_peer_auth import NODE_PUB, _make_cert
from tests.test_peer_client_unsigned import no_secret, surface  # noqa: F401 — fixtures

OWN = Ed25519PrivateKey.generate()
IDENTITY = peer_auth.PeerIdentity(pubkey=OWN.public_key().public_bytes_raw().hex(), sign=OWN.sign,
                                  cert_bundle=lambda: None)


@pytest.fixture
def node(surface, no_secret, monkeypatch):  # noqa: F811
    """A node with a library that has gaps and one peer to ask — a manual
    peer, as a LAN beacon's or a DHT holder's would be asked — whose identity
    the test gives and takes: `node.identity[0]`."""
    peer = surface(*_make_cert(), says=NODE_PUB, mb_slices=3, lb_slices=3, sharing=True)
    address = f"https://127.0.0.1:{peer.server_port}"
    identity = [None]
    recorded, followed = [], []

    async def after_run():
        followed.append(True)

    walk = SyncWalk("postgresql://unused", identity=lambda: identity[0], sharing_enabled=lambda: True,
                    manual_peers=[address], after_run=after_run)

    async def db(fn, *args):
        return {"incomplete_artist_uuids": ["a1"], "core_and_bulk": (["t1"], [])}.get(fn.__name__)
    walk.db = db

    class _Client:                                  # the pull itself is not this test's: the dial is
        peer_capabilities = ()

        def __init__(self, *args, **kwargs):
            pass

        def run_sync(self, *args, **kwargs):
            return {}

        def _close_conn(self):
            pass
    monkeypatch.setattr(sync_walk, "SyncClient", _Client)
    monkeypatch.setattr(sync_walk, "write_sync_status", lambda dsn, started, items: recorded.append(items))
    for module in (sync_walk, slice_sources):       # the Worker's directory is no peer, and not asked here
        monkeypatch.setattr(module.node_hints, "fetch", lambda *a, **kw: [])
    monkeypatch.setattr(slice_sources.master_hint, "fetch", lambda *a, **kw: None)

    for module in (mb_slice_cycle, lb_slice_cycle):
        monkeypatch.setattr(module, "local_dump_available", lambda dsn: None)
        monkeypatch.setattr(module, "published_newest", lambda dsn, family: None)
    monkeypatch.setattr(mb_slice_cycle, "write_settings", lambda dsn, values: None)
    monkeypatch.setattr(mb_slice_cycle, "notify", lambda dsn, channel: None)
    pending = {"mb": ["an owned artist", "an engaged one"],
               "lb": [("0b0c25f4-f31c-46a5-a4fb-ccbf53d663bd", None, False)]}
    monkeypatch.setattr(mb_slice_cycle, "pending_names", lambda dsn, n: list(pending["mb"]))
    monkeypatch.setattr(lb_slice_cycle, "pending_slice_mbids", lambda dsn, n, newest: list(pending["lb"]))
    published = []

    async def publish(**state):
        published.append(state)

    def cycle(family: str):
        common = dict(connect=walk.connect_peer, dead=walk.dead, sharing_enabled=lambda: True,
                      has_identity=walk.has_identity, manual_peers=[address],
                      load_bans_fn=lambda: (set(), set()))
        made = (mb_slice_cycle.MbSliceCycle("postgresql://unused", after_import=lambda: None, **common)
                if family == "mb" else lb_slice_cycle.LbSliceCycle("postgresql://unused", **common))
        made._publish = publish
        return made

    return type("Node", (), {"walk": walk, "cycle": staticmethod(cycle), "peer": peer, "identity": identity,
                             "recorded": recorded, "followed": followed, "pending": pending,
                             "published": published})


def test_a_walk_with_no_identity_makes_no_connection_and_records_no_sync(node, caplog):
    async def scenario():
        node.walk.bind()
        assert await node.walk.run() == {"status": "no_identity"}
        await node.walk.run_once("auto")
        await node.walk.run_once("lan-peer")
        await asyncio.sleep(0)
    with caplog.at_level("INFO", logger=sync_walk.__name__):
        asyncio.run(scenario())
    assert node.peer.connections == 0 and node.peer.seen == []
    # no run took place: no "last sync" for the trail to read, nothing for the cycles to follow
    assert node.recorded == [] and node.followed == []
    said = [r.getMessage() for r in caplog.records if "no identity yet" in r.getMessage()]
    assert len(said) == 1                                   # once, not at every interval


@pytest.mark.parametrize("family", ["mb", "lb"])
def test_a_slice_cycle_with_no_identity_makes_no_connection(node, family):
    """Names and MBIDs are pending, a source is at hand: nothing is probed —
    the cycles look for their sources before they look at what is pending —
    and nothing is said of a wait nobody can see."""
    async def scenario():
        cycle = node.cycle(family)
        cycle.bind()
        assert await cycle.run() == {}
        await cycle.run_once("pending")
        if family == "mb":
            await cycle.refresh_sources()                   # the backend's "no source answered" probe
    asyncio.run(scenario())
    assert node.peer.connections == 0 and node.peer.seen == []
    assert node.published == []


def test_the_identity_appears_and_the_next_run_dials_with_no_restart(node):
    """The same walk and the same cycles, as the process built them at its
    start: a run asked for after the account is made dials, signs what it
    asks, and its end has the cycles follow."""
    async def scenario():
        node.walk.bind()
        cycles = [node.cycle("mb"), node.cycle("lb")]
        for cycle in cycles:
            cycle.bind()
        dispatcher = asyncio.create_task(node.walk.dispatch_loop())
        node.walk.request("auto")
        await _until(lambda: not node.walk._reasons and not node.walk._lock.locked())
        before = node.peer.connections
        node.identity[0] = IDENTITY                         # the account is made
        node.walk.request("request")                        # what the identity's event asks for
        await _until(lambda: node.followed)
        walked = [path for path, _ in node.peer.seen]
        node.pending["mb"], node.pending["lb"] = [], []      # the cycles' asking itself is not this test's
        for cycle in cycles:
            await cycle.run()
        node.walk.stop()
        await dispatcher
        return before, walked
    before, walked = asyncio.run(scenario())
    assert before == 0
    assert walked == ["/health"] and node.recorded == [0]
    # each cycle found its source by a dial of its own
    assert [path for path, _ in node.peer.seen] == ["/health"] * 3
    assert node.peer.connections >= 3
    assert [state["sources"] for state in node.published] == [1, 1]


def test_a_node_with_an_identity_walks_as_it_did(node):
    node.identity[0] = IDENTITY

    async def scenario():
        node.walk.bind()
        await node.walk.run_once("auto")
        await asyncio.sleep(0)
    asyncio.run(scenario())
    assert [path for path, _ in node.peer.seen] == ["/health"]
    assert node.recorded == [0] and node.followed == [True]


async def _until(done) -> None:
    for _ in range(500):
        if done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the run never came")
