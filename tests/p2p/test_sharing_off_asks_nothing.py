"""With P2P sharing off a node asks no peer for music data and announces no
artist tail: a node that does not give does not take.

- the walk reads no gap set, looks up no DHT key and asks no peer; turned off
  while a run goes on, it asks no further peer, does no rare-artist lookup and
  carries nothing; a peer whose own sharing is off is told none of this
  node's tracks;
- both slice cycles probe no source and ask no name or MBID, and say in
  their status how many wait and why; turned off mid-run, no further batch
  leaves; a source whose /health says it does not share is no source;
- both DHT services hold the rare-artist tail while sharing is off and drip
  it again once it is back.

No network and no database: the stores are stand-ins."""

import asyncio
import importlib
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop.p2p import lb_slice_cycle, mb_slice_cycle, sync_walk
from desktop.p2p.sync_walk import DeadAddresses, SyncWalk

BACKEND = Path(__file__).resolve().parents[2] / "backend"


class _Identity:
    pubkey = "ab" * 32


def _refuse(*args, **kwargs):
    raise AssertionError("asked while sharing is off")


# ---------------------------------------------------------------- the walk

def test_a_walk_with_sharing_off_reads_no_gap_and_asks_nothing():
    walk = SyncWalk("postgresql://unused", identity=lambda: _Identity(),
                    sharing_enabled=lambda: False)

    async def db(fn, *args):
        raise AssertionError(f"{fn.__name__} read while sharing is off")

    walk.db = db
    walk.dht = SimpleNamespace(is_available=True, lookup_nodes=_refuse,
                               lookup_artists_batch=_refuse)
    walk.manual_peers = ["https://198.51.100.5:21000"]
    walk._sync_from_peer = _refuse
    assert asyncio.run(walk.run()) == {"status": "sharing_off"}


def test_a_peer_step_after_sharing_went_off_asks_and_carries_nothing(monkeypatch):
    walk = SyncWalk("postgresql://unused", identity=lambda: _Identity(),
                    sharing_enabled=lambda: False)
    monkeypatch.setattr(sync_walk, "SyncClient", _refuse)
    api = SimpleNamespace(peer_pubkey="cd" * 32, last_health={"sharing": True},
                          carry_offer=_refuse, sync_inventory=_refuse)
    out = asyncio.run(walk._sync_from_peer("198.51.100.5:21000", (["t1"], []),
                                           lambda msg: None, None, peer_api=api))
    assert out == {"error": "sharing_off"}


def test_a_peer_that_does_not_share_is_told_no_track(monkeypatch):
    walk = SyncWalk("postgresql://unused", identity=lambda: _Identity(),
                    sharing_enabled=lambda: True)
    monkeypatch.setattr(sync_walk, "SyncClient", _refuse)

    async def db(fn, *args):
        assert fn is sync_walk.peer_refused
        return False

    walk.db = db
    api = SimpleNamespace(peer_pubkey="cd" * 32, last_health={"sharing": False})
    out = asyncio.run(walk._sync_from_peer("198.51.100.5:21000", (["t1"], []),
                                           lambda msg: None, None, peer_api=api))
    assert out == {"error": "not_sharing"}


def test_no_rare_artist_lookup_once_sharing_went_off_during_the_run(monkeypatch):
    sharing = [True]
    walk = SyncWalk("postgresql://unused", identity=lambda: _Identity(),
                    sharing_enabled=lambda: sharing[0])
    looked_up = []

    async def lookup_nodes():
        return [("198.51.100.20", 21000)]

    async def lookup_artists_batch(uuids):
        looked_up.append(list(uuids))
        return {}

    walk.dht = SimpleNamespace(is_available=True, tail_enabled=True, lookup_nodes=lookup_nodes,
                               lookup_artists_batch=lookup_artists_batch,
                               set_tail_enabled=lambda mode: None)
    walk._skip_addrs = None

    async def db(fn, *args):
        return {"incomplete_artist_uuids": ["a1"], "core_and_bulk": (["t1"], []),
                "rare_search_uuids": ["a1"], "read_setting": 0}.get(fn.__name__)

    async def connect(addr, is_lan=False, expected_pubkey=None):
        return SimpleNamespace(base_url=f"https://{addr}", peer_pubkey=None)

    async def pull(addr, gaps, progress, progress_cb, peer_api=None, is_lan=False):
        sharing[0] = False                # the owner turns sharing off meanwhile
        return {}

    async def measure(reply_size, sample, directory_total):
        return True                       # rare mode: the tier-B lookups would follow

    walk.db, walk.connect_peer, walk._sync_from_peer, walk._measure_network = db, connect, pull, measure
    monkeypatch.setattr(sync_walk.node_hints, "fetch", lambda cap: [])
    monkeypatch.setattr(sync_walk.node_hints, "total", lambda cap: None)
    monkeypatch.setattr(sync_walk.master_hint, "fetch", lambda: None)
    asyncio.run(walk.run())
    assert looked_up == []


# ------------------------------------------------------------- the cycles

def _mb_cycle(sharing, published, monkeypatch, pending=("owned one", "gone quiet")):
    monkeypatch.setattr(mb_slice_cycle, "local_dump_available", lambda dsn: None)
    monkeypatch.setattr(mb_slice_cycle, "published_newest", lambda dsn, family: None)
    monkeypatch.setattr(mb_slice_cycle, "pending_names", lambda dsn, n: list(pending))
    monkeypatch.setattr(mb_slice_cycle, "reopened_authors", lambda dsn, names: {})
    cycle = mb_slice_cycle.MbSliceCycle(
        "postgresql://unused", connect=_refuse, dead=DeadAddresses(), after_import=lambda: None,
        sharing_enabled=lambda: sharing[0])

    async def publish(**state):
        published.append(state)

    cycle._publish = publish
    return cycle


def test_the_mb_cycle_with_sharing_off_probes_no_source_and_says_why(monkeypatch):
    published = []
    cycle = _mb_cycle([False], published, monkeypatch)
    cycle.find_sources = _refuse
    assert asyncio.run(cycle.run()) == {}
    assert published == [{"pending": 2, "served": 0, "unserved": 2,
                          "reason": "sharing_off", "sources": 0}]


def test_the_mb_cycle_turned_off_mid_run_sends_no_further_batch(monkeypatch):
    sharing, published, asked = [True], [], []
    names = [f"name {i}" for i in range(120)]
    cycle = _mb_cycle(sharing, published, monkeypatch, pending=names)
    monkeypatch.setattr(mb_slice_cycle, "PACKET_GAP", 0.0)
    api = SimpleNamespace(base_url="https://198.51.100.5:21000", session_deadline=0)

    async def find_sources():
        return [(api, "ef" * 32)]

    async def isolated(client, node, batch, avoid):
        asked.append(list(batch))
        sharing[0] = False
        return {"names": len(batch), "missing": []}

    cycle.find_sources, cycle._isolated = find_sources, isolated
    monkeypatch.setattr(mb_slice_cycle, "MBSliceClient",
                        lambda api, db_dsn, source_node: SimpleNamespace(close=lambda: None,
                                                                         finalize=lambda: None))
    asyncio.run(cycle.run())
    assert len(asked) == 1
    assert published[-1]["reason"] == "sharing_off"
    assert published[-1]["unserved"] == len(names) - len(asked[0])


def test_the_lb_cycle_with_sharing_off_probes_no_source_and_says_why(monkeypatch):
    published = []
    monkeypatch.setattr(lb_slice_cycle, "local_dump_available", lambda dsn: None)
    monkeypatch.setattr(lb_slice_cycle, "published_newest", _refuse)
    monkeypatch.setattr(lb_slice_cycle, "pending_slice_mbids",
                        lambda dsn, n, newest: [("0b0c25f4-f31c-46a5-a4fb-ccbf53d663bd", None)])
    cycle = lb_slice_cycle.LbSliceCycle("postgresql://unused", connect=_refuse, dead=DeadAddresses(),
                                        sharing_enabled=lambda: False)
    cycle.find_sources = _refuse

    async def publish(**state):
        published.append(state)

    cycle._publish = publish
    assert asyncio.run(cycle.run()) == {}
    assert published == [{"pending": 1, "served": 0, "unserved": 1, "reason": "sharing_off",
                          "sources": 0, "newest": None}]


@pytest.mark.parametrize("family", ["mb", "lb"])
def test_a_node_whose_sharing_is_off_is_no_slice_source(family):
    if family == "mb":
        cycle = mb_slice_cycle.MbSliceCycle("postgresql://unused", connect=_refuse, dead=DeadAddresses(),
                                            after_import=lambda: None, sharing_enabled=lambda: True)
    else:
        cycle = lb_slice_cycle.LbSliceCycle("postgresql://unused", connect=_refuse, dead=DeadAddresses(),
                                            sharing_enabled=lambda: True)
    usable = cycle._sources._usable
    held = {f"{family}_slices": 3}
    assert usable({**held, "sharing": True})
    assert usable(held)
    assert not usable({**held, "sharing": False})
    assert not usable({f"{family}_dump": "20261101-001500", "sharing": False})


# ------------------------------------------------------------- the DHT tail

def _dht_module(which: str):
    if which == "backend":
        if str(BACKEND) not in sys.path:
            sys.path.insert(0, str(BACKEND))
        return importlib.import_module("dht_service")
    return importlib.import_module("desktop.p2p.dht_service")


@pytest.mark.parametrize("which", ["launcher", "backend"])
def test_the_rare_artist_tail_is_held_while_sharing_is_off(which, monkeypatch):
    module = _dht_module(which)
    if not module.HAS_LIBTORRENT:
        pytest.skip("libtorrent is not installed here")
    monkeypatch.setattr(module, "MIN_ANNOUNCE_SPACING", 0.0)
    monkeypatch.setattr(module, "REANNOUNCE_INTERVAL", 0.01)
    monkeypatch.setattr(module, "TAIL_IDLE_RECHECK", 0.01)
    sharing = [False]
    svc = module.DHTService(sharing_enabled=lambda: sharing[0])
    announced = []
    svc._session = SimpleNamespace(dht_announce=lambda ih, port, flags: announced.append(ih))
    svc._running = True
    svc._tail_enabled = True
    svc._announced = {"11111111-1111-4111-8111-111111111111"}

    async def go():
        svc._lane.start()
        task = asyncio.ensure_future(svc._reannounce_tail())
        await asyncio.sleep(0.2)
        held = len(announced)
        sharing[0] = True
        await asyncio.sleep(0.2)
        svc._running = False
        task.cancel()
        await svc._lane.stop()
        return held

    held = asyncio.run(go())
    assert held == 0
    assert announced                      # back on: the drip goes on


# ------------------------------------------------------- the stream's pace

def test_a_run_that_filled_its_cap_streams_on_whatever_waits(monkeypatch):
    """Not the owner's files or the imported scrobbles alone: any reason that
    fills a run keeps the stream going, at its one pace."""
    published = []
    names = [f"name {i}" for i in range(mb_slice_cycle.PENDING_PER_CYCLE)]
    cycle = _mb_cycle([True], published, monkeypatch, pending=names)
    monkeypatch.setattr(mb_slice_cycle, "PACKET_GAP", 0.0)
    api = SimpleNamespace(base_url="https://198.51.100.5:21000", session_deadline=0)

    async def find_sources():
        return [(api, "ef" * 32)]

    async def isolated(client, node, batch, avoid):
        return {"names": len(batch), "missing": []}

    cycle.find_sources, cycle._isolated = find_sources, isolated
    monkeypatch.setattr(mb_slice_cycle, "MBSliceClient",
                        lambda api, db_dsn, source_node: SimpleNamespace(close=lambda: None,
                                                                         finalize=lambda: None))

    async def go():
        cycle.bind()
        await cycle.run()
        return set(cycle._reasons)

    assert asyncio.run(go()) == {"stream"}


def test_one_source_is_asked_at_the_stream_s_pace_across_runs(monkeypatch):
    published, asked_at = [], []
    names = [f"name {i}" for i in range(120)]
    cycle = _mb_cycle([True], published, monkeypatch, pending=names)
    monkeypatch.setattr(mb_slice_cycle, "PACKET_GAP", 0.25)
    api = SimpleNamespace(base_url="https://198.51.100.5:21000", session_deadline=0)

    async def find_sources():
        return [(api, "ef" * 32)]

    async def isolated(client, node, batch, avoid):
        asked_at.append(time.monotonic())
        return {"names": len(batch), "missing": []}

    cycle.find_sources, cycle._isolated = find_sources, isolated
    monkeypatch.setattr(mb_slice_cycle, "MBSliceClient",
                        lambda api, db_dsn, source_node: SimpleNamespace(close=lambda: None,
                                                                         finalize=lambda: None))

    async def go():
        await cycle.run()                 # three requests of 50, 50 and 20 names
        await cycle.run()                 # the next run's first waits its turn too
    asyncio.run(go())
    assert len(asked_at) == 6
    gaps = [b - a for a, b in zip(asked_at, asked_at[1:])]
    assert min(gaps) >= 0.24

