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
  it again once it is back;
- both slice streams ask one source once a gap, across runs; an artist page
  opened is asked at once, the first time — a run waiting out the pace gives
  way to it.

No network and no database: the stores are stand-ins."""

import asyncio
import importlib
import time
from types import SimpleNamespace

import pytest

from desktop.p2p import lb_slice_cycle, mb_slice_cycle, sync_walk
from desktop.p2p.sync_walk import DeadAddresses, SyncWalk



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


def test_turned_off_during_a_peer_s_pull_nothing_more_is_asked_and_nothing_carried(monkeypatch):
    """The owner turns sharing off while a pull goes on: the pull asks the
    peer nothing more — no further inventory chunk, no further pull batch —
    and no carry follows it."""
    from desktop import sync_client as sc
    sharing = [True]
    walk = SyncWalk("postgresql://unused", identity=lambda: _Identity(),
                    sharing_enabled=lambda: sharing[0])
    pulled = []

    class _Peer:
        base_url, peer_pubkey, last_health = "https://198.51.100.5:21000", "cd" * 32, {"sharing": True}
        carry_offer = carry_push = staticmethod(_refuse)

        def get_health(self):
            return {"capabilities": ["segments", "carry"]}

        def sync_inventory(self, tracks):
            return dict(sc.EMPTY_INVENTORY, tracks=list(tracks), audio_features=list(tracks))

        def sync_pull(self, endpoint, uuids):
            pulled.append(list(uuids))
            sharing[0] = False            # the owner turns sharing off after the first batch
            return {"items": [{"track_uuid": uuids[0]}]}

    monkeypatch.setattr(sc.SyncClient, "_compute_needed",
                        lambda self, inventory, use_segments: {"audio_features": inventory["audio_features"]})
    monkeypatch.setattr(sc.SyncClient, "_import_items", lambda self, category, result: len(result["items"]))

    async def db(fn, *args):
        return False                      # peer_refused: not banned

    walk.db = db
    tracks = [f"t{i}" for i in range(1200)]
    out = asyncio.run(walk._sync_from_peer("198.51.100.5:21000", (tracks, []),
                                           lambda msg: None, None, peer_api=_Peer()))
    assert out == {"audio_features": 1, "error": "sharing_off"}     # what came before it counts
    assert len(pulled) == 1               # batches of 500: the second is never asked


def test_a_carry_part_refused_for_the_carrier_s_window_is_sent_again_only_while_sharing_is_on(monkeypatch):
    sharing = [True]
    walk = SyncWalk("postgresql://unused", identity=lambda: _Identity(),
                    sharing_enabled=lambda: sharing[0])
    pushed = []
    monkeypatch.setattr(sync_walk, "_connect", lambda dsn: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(sync_walk.sync_queries, "get_pushable_tracks",
                        lambda conn, limit: [{"recordings": ["r1"]}])
    monkeypatch.setattr(sync_walk.sync_queries, "PULL_HANDLERS",
                        {c: (lambda conn, uuids: {"items": list(uuids)})
                         for c in ("segments", "audio_features", "instruments", "track_mbids")})
    monkeypatch.setattr(sync_walk, "push_parts", lambda payload: [payload])

    def carry_push(category, part):
        pushed.append(category)
        sharing[0] = False                # turned off while the carrier's window is waited out
        return {"retry_after": 1}

    api = SimpleNamespace(carry_offer=lambda recordings: {"wanted": {"audio_features": ["u1"]}},
                          carry_push=carry_push)
    assert walk._push_to_carrier(api, lambda msg: None) == 0
    assert pushed == ["audio-features"]


def test_turned_off_during_a_carry_no_further_part_leaves(monkeypatch):
    sharing = [True]
    walk = SyncWalk("postgresql://unused", identity=lambda: _Identity(),
                    sharing_enabled=lambda: sharing[0])
    pushed = []
    monkeypatch.setattr(sync_walk, "_connect", lambda dsn: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(sync_walk.sync_queries, "get_pushable_tracks",
                        lambda conn, limit: [{"recordings": ["r1"]}])
    monkeypatch.setattr(sync_walk.sync_queries, "PULL_HANDLERS",
                        {c: (lambda conn, uuids: {"items": list(uuids)})
                         for c in ("segments", "audio_features", "instruments", "track_mbids")})
    monkeypatch.setattr(sync_walk, "push_parts", lambda payload: [payload, payload])

    def carry_push(category, part):
        pushed.append(category)
        sharing[0] = False                # turned off after the first part
        return {"imported": 1}

    api = SimpleNamespace(carry_offer=lambda recordings: {"wanted": {"audio_features": ["u1"],
                                                                     "track_mbids": ["u1"]}},
                          carry_push=carry_push)
    assert walk._push_to_carrier(api, lambda msg: None) == 1
    assert pushed == ["audio-features"]


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


def test_after_a_rate_limit_s_wait_the_mb_stream_asks_again_only_while_sharing_is_on(monkeypatch):
    sharing, published, asked = [True], [], []
    cycle = _mb_cycle(sharing, published, monkeypatch)
    api = SimpleNamespace(base_url="https://198.51.100.5:21000", session_deadline=0)

    async def find_sources():
        return [(api, "ef" * 32)]

    async def isolated(client, node, batch, avoid):
        asked.append(list(batch))
        sharing[0] = False                # turned off while the window is waited out
        return {"error": "rate limited", "retry_after": 1}

    cycle.find_sources, cycle._isolated = find_sources, isolated
    monkeypatch.setattr(mb_slice_cycle, "MBSliceClient",
                        lambda api, db_dsn, source_node: SimpleNamespace(close=lambda: None,
                                                                         finalize=lambda: None))
    asyncio.run(cycle.run())
    assert len(asked) == 1
    assert published[-1]["reason"] == "sharing_off"


def test_the_lb_cycle_with_sharing_off_probes_no_source_and_says_why(monkeypatch):
    published = []
    monkeypatch.setattr(lb_slice_cycle, "local_dump_available", lambda dsn: None)
    monkeypatch.setattr(lb_slice_cycle, "published_newest", _refuse)
    monkeypatch.setattr(lb_slice_cycle, "pending_slice_mbids",
                        lambda dsn, n, newest: [("0b0c25f4-f31c-46a5-a4fb-ccbf53d663bd", None, False)])
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

def test_the_rare_artist_tail_is_held_while_sharing_is_off(monkeypatch):
    module = importlib.import_module("desktop.p2p.dht_service")
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
    published, asked_at, sizes = [], [], []
    names = [f"name {i}" for i in range(120)]
    cycle = _mb_cycle([True], published, monkeypatch, pending=names)
    monkeypatch.setattr(mb_slice_cycle, "PACKET_GAP", 0.25)
    api = SimpleNamespace(base_url="https://198.51.100.5:21000", session_deadline=0)

    async def find_sources():
        return [(api, "ef" * 32)]

    async def isolated(client, node, batch, avoid):
        asked_at.append(time.monotonic())
        sizes.append(list(batch))
        return {"names": len(batch), "missing": []}

    cycle.find_sources, cycle._isolated = find_sources, isolated
    monkeypatch.setattr(mb_slice_cycle, "MBSliceClient",
                        lambda api, db_dsn, source_node: SimpleNamespace(close=lambda: None,
                                                                         finalize=lambda: None))

    async def go():
        await cycle.run()                 # five requests of PACKET_NAMES (25) names
        await cycle.run()                 # the next run's first waits its turn too
    asyncio.run(go())
    assert len(asked_at) == 10
    gaps = [b - a for a, b in zip(asked_at, asked_at[1:])]
    assert min(gaps) >= 0.24
    assert all(len(b) <= mb_slice_cycle.PACKET_NAMES for b in sizes)


def _lb_stream(monkeypatch, rows, newest=None, sources=None, sharing=None):
    """An LB cycle over `sources` (one by default), its pending set `rows`
    (mutable), and the statuses it published."""
    monkeypatch.setattr(lb_slice_cycle, "local_dump_available", lambda dsn: None)
    monkeypatch.setattr(lb_slice_cycle, "published_newest", lambda dsn, family: None)
    monkeypatch.setattr(lb_slice_cycle, "analyze_tables", lambda dsn: None)
    monkeypatch.setattr(lb_slice_cycle, "pending_slice_mbids", lambda dsn, n, version: list(rows))
    cycle = lb_slice_cycle.LbSliceCycle("postgresql://unused", connect=_refuse, dead=DeadAddresses(),
                                        sharing_enabled=lambda: sharing is None or sharing[0])
    sources = sources or [(SimpleNamespace(base_url="https://198.51.100.5:21000", session_deadline=0,
                                           last_health=None), "ef" * 32, None)]
    published = []

    async def find_sources(listed):
        return sources, newest

    async def publish(**state):
        published.append(state)

    cycle.find_sources, cycle._publish = find_sources, publish
    return cycle, published


def _mbids(prefix, n):
    return [f"{prefix}0000000-0000-4000-8000-{i:012d}" for i in range(n)]


def test_the_lb_stream_asks_a_source_once_a_gap_and_an_opened_page_first(monkeypatch):
    monkeypatch.setattr(lb_slice_cycle, "PACKET_GAP", 0.25)
    page, stale, fresh = _mbids("a", 2), _mbids("b", 60), _mbids("c", 60)
    rows = ([(m, None, True) for m in page] + [(m, "20261101-000000", False) for m in stale]
            + [(m, None, False) for m in fresh])
    cycle, published = _lb_stream(monkeypatch, rows, newest="20261108-000000")
    asked = []

    async def exchange(api, node, source_addr, batch, min_version):
        asked.append((time.monotonic(), list(batch), min_version))
        return {"imported": len(batch), "recordings": 0, "missing": [], "dropped": 0}

    cycle._exchange = exchange
    asyncio.run(cycle.run())
    _, first, version = asked[0]
    assert set(page) <= set(first) and len(first) == lb_slice_cycle.PAGE_MBIDS and version is None
    assert sorted(m for _, b, _ in asked for m in b) == sorted(page + stale + fresh)
    assert all(v == "20261108-000000" for _, b, v in asked if set(b) & set(stale))
    assert all(len(b) <= lb_slice_cycle.lb_slice_queries.MAX_MBIDS_PER_REQUEST for _, b, _ in asked)
    gaps = [b[0] - a[0] for a, b in zip(asked, asked[1:])]
    assert len(gaps) == 3 and min(gaps) >= 0.24
    assert (published[-1]["unserved"], published[-1]["reason"]) == (0, "ok")


def test_a_page_opened_while_the_lb_stream_waits_out_its_pace_is_asked_at_once(monkeypatch):
    monkeypatch.setattr(lb_slice_cycle, "PACKET_GAP", 30.0)
    opened = _mbids("a", 1)[0]
    rows = [(m, None, False) for m in _mbids("c", 120)]
    cycle, published = _lb_stream(monkeypatch, rows)
    asked = []

    async def exchange(api, node, source_addr, batch, min_version):
        asked.append((time.monotonic(), list(batch)))
        if len(asked) == 1:
            rows[:] = [(opened, None, True)]      # the page's request row, the rest served
            cycle.request("request")              # and its NOTIFY
        return {"imported": len(batch), "recordings": 0, "missing": [], "dropped": 0}

    cycle._exchange = exchange

    async def go():
        began = time.monotonic()
        await cycle.run()
        took = time.monotonic() - began
        await cycle.run()
        return took

    assert asyncio.run(go()) < 5
    # The run the page cut short published nothing: what it left is no miss.
    assert len(published) == 1 and published[0]["unserved"] == 0
    assert len(asked) == 2 and asked[1][1] == [opened] and asked[1][0] - asked[0][0] < 5


def test_a_page_asked_at_once_is_paced_when_asked_again(monkeypatch):
    monkeypatch.setattr(lb_slice_cycle, "PACKET_GAP", 0.3)
    opened = _mbids("a", 1)[0]
    cycle, published = _lb_stream(monkeypatch, [(opened, None, True)])
    asked = []

    async def exchange(api, node, source_addr, batch, min_version):
        asked.append(time.monotonic())
        return {"imported": 0, "recordings": 0, "missing": list(batch), "dropped": 0}

    cycle._exchange = exchange

    async def go():
        await cycle.run()                         # no source had it: it still waits
        await cycle.run()

    asyncio.run(go())
    assert len(asked) == 2 and asked[1] - asked[0] >= 0.29


def _source(n, dump=False):
    return SimpleNamespace(base_url=f"https://198.51.100.{n}:21000", session_deadline=0,
                           last_health={"lb_dump": "20261101-000000"} if dump else {})


REPLICA, DUMP = "aa" * 32, "bb" * 32


def _replica_ahead(asked):
    """A replica that holds none of what it is asked, and a dump node that
    holds all of it — the order find_sources gives them: replicas first."""
    async def exchange(api, node, source_addr, batch, min_version):
        asked.append((time.monotonic(), node, list(batch)))
        if node == REPLICA:
            return {"imported": 0, "recordings": 0, "missing": list(batch), "dropped": 0}
        return {"imported": len(batch), "recordings": 0, "missing": [], "dropped": 0}
    return [(_source(6), REPLICA, None), (_source(5, dump=True), DUMP, None)], exchange


def test_a_page_is_asked_of_the_dump_nodes_first_and_at_once(monkeypatch):
    """A replica rarely holds the artist a person just opened: the page goes
    first to a dump node, and the replica is not asked what it answered."""
    monkeypatch.setattr(lb_slice_cycle, "PACKET_GAP", 0.3)
    page = _mbids("a", 1)
    asked = []
    sources, exchange = _replica_ahead(asked)
    rows = [(m, None, True) for m in page] + [(m, None, False) for m in _mbids("c", 150)]
    cycle, published = _lb_stream(monkeypatch, rows, sources=sources)
    cycle._exchange = exchange
    began = time.monotonic()
    asyncio.run(cycle.run())
    at, node, first = asked[0]
    assert node == DUMP and page[0] in first and len(first) == lb_slice_cycle.PAGE_MBIDS
    assert at - began < 0.1
    assert not any(page[0] in b for _, _, b in asked[1:])
    # What the dump node answered for the page is not asked of the replica
    # after it; the rest goes replicas first, at the pace.
    assert not set(first) & {m for _, n, b in asked[1:] if n == REPLICA for m in b}
    assert published[-1]["unserved"] == 0


def test_pages_opened_one_after_another_reach_the_dump_node_while_the_person_browses(monkeypatch):
    monkeypatch.setattr(lb_slice_cycle, "PACKET_GAP", 0.5)
    pages, opened = _mbids("a", 6), []
    asked = []
    sources, exchange = _replica_ahead(asked)
    backlog = [(m, None, False) for m in _mbids("c", 150)]
    cycle, _ = _lb_stream(monkeypatch, backlog, sources=sources)
    monkeypatch.setattr(lb_slice_cycle, "pending_slice_mbids",
                        lambda dsn, n, version: [(m, None, True) for m in opened] + list(backlog))
    cycle._exchange = exchange

    opened_at = {}

    async def go():
        cycle.bind()
        dispatch = asyncio.create_task(cycle.dispatch_loop())
        for p in pages:                   # the person browses, a page every 0.4 s
            opened.append(p)
            opened_at[p] = time.monotonic()
            cycle.request("request")
            await asyncio.sleep(0.4)
        cycle.stop()
        await asyncio.wait_for(dispatch, 5)

    asyncio.run(go())
    # Each page reaches the dump node at once, while the person goes on
    # browsing — not after the replica's paced requests, which every next page
    # would cut short.
    for p in pages:
        at = min(t for t, node, b in asked if node == DUMP and p in b)
        assert at - opened_at[p] < 0.3, p


def test_a_page_opened_during_the_timer_s_run_is_asked_by_the_run_after_it(monkeypatch):
    """The timer's run goes through the dispatcher like any other: a page
    opened while it waits out the pace cuts it, and the run after it asks
    for the page — none is dropped for a run in progress."""
    monkeypatch.setattr(lb_slice_cycle, "PACKET_GAP", 0.5)
    monkeypatch.setattr(lb_slice_cycle, "FIRST_RUN_DELAY", 0)
    monkeypatch.setattr(lb_slice_cycle, "read_interval", lambda dsn, default, key=None: 60)
    opened = _mbids("a", 1)[0]
    rest = _mbids("c", 120)
    rows = [(m, None, False) for m in rest]
    cycle, _ = _lb_stream(monkeypatch, rows)
    asked = []

    async def exchange(api, node, source_addr, batch, min_version):
        asked.append(list(batch))
        if len(asked) == 1:
            rows[:] = [(opened, None, True)] + [(m, None, False) for m in rest if m not in batch]
            cycle.request("request")      # the page's NOTIFY, while the timer's run goes on
        return {"imported": len(batch), "recordings": 0, "missing": [], "dropped": 0}

    cycle._exchange = exchange

    async def go():
        cycle.bind()
        tasks = [asyncio.create_task(cycle.dispatch_loop()), asyncio.create_task(cycle.interval_loop())]
        for _ in range(60):
            if any(opened in b for b in asked):
                break
            await asyncio.sleep(0.05)
        cycle.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(go())
    assert any(opened in b for b in asked)


def test_after_a_rate_limit_s_wait_the_lb_stream_asks_again_only_while_sharing_is_on(monkeypatch):
    sharing, asked = [True], []
    cycle, published = _lb_stream(monkeypatch, [(m, None, False) for m in _mbids("c", 10)],
                                  sharing=sharing)

    async def exchange(api, node, source_addr, batch, min_version):
        asked.append(list(batch))
        sharing[0] = False                # turned off while the window is waited out
        return {"error": "rate limited", "retry_after": 1}

    cycle._exchange = exchange
    asyncio.run(cycle.run())
    assert len(asked) == 1
    assert published[-1]["reason"] == "sharing_off"


def test_a_page_a_dump_node_had_no_budget_for_is_asked_of_it_again_at_the_pace(monkeypatch):
    """A dump node's `missing` is its budget's of the moment (pages opened one
    after another spend this node's share of it): the page is asked of it
    again by the paced requests of the same run, not left for a later one."""
    monkeypatch.setattr(lb_slice_cycle, "PACKET_GAP", 0.2)
    page = _mbids("a", 1)
    rows = [(m, None, True) for m in page] + [(m, None, False) for m in _mbids("c", 30)]
    asked = []

    async def exchange(api, node, source_addr, batch, min_version):
        asked.append(list(batch))
        if len(asked) == 1:               # the budget spent: the page and its fill come back missing
            return {"imported": 0, "recordings": 0, "missing": list(batch), "dropped": 0}
        return {"imported": len(batch), "recordings": 0, "missing": [], "dropped": 0}

    cycle, published = _lb_stream(monkeypatch, rows, sources=[(_source(5, dump=True), DUMP, None)])
    cycle._exchange = exchange
    asyncio.run(cycle.run())
    assert len(asked) == 2 and page[0] in asked[1]
    assert published[-1]["unserved"] == 0


def test_the_page_goes_to_the_dump_nodes_in_the_order_they_were_found(monkeypatch):
    """The source finder's fallback names the master last among the dump nodes:
    the page keeps that order — the dump nodes before the replicas, never the
    whole list reversed."""
    page = _mbids("a", 1)
    first, master = "c1" * 32, "c2" * 32
    sources = [(_source(6), REPLICA, None), (_source(7, dump=True), first, None),
               (_source(8, dump=True), master, None)]
    asked = []

    async def exchange(api, node, source_addr, batch, min_version):
        asked.append(node)
        return {"imported": len(batch), "recordings": 0, "missing": [], "dropped": 0}

    cycle, _ = _lb_stream(monkeypatch, [(m, None, True) for m in page], sources=sources)
    cycle._exchange = exchange
    asyncio.run(cycle.run())
    assert asked == [first]
