"""The slice-source finder (desktop/p2p/slice_sources.py) both slice cycles
share — the tiers, the fallback, the kept set — and the one DHT interface
both runtimes hand it. No network, no DB: the walk's connect, the DHT, the
LAN table and the two hint services are stand-ins."""

import asyncio
import inspect

import pytest

from desktop.p2p import slice_sources
from desktop.p2p.slice_sources import SourceFinder, network_pass
from desktop.p2p.sync_walk import DeadAddresses

DUMP = {"mb_dump": "20260920-001", "mb_slices": 0}
REPLICA = {"mb_dump": None, "mb_slices": 4210}
PLAIN = {"mb_dump": None, "mb_slices": 0}


class _Client:
    def __init__(self, base_url, health):
        self.base_url = base_url
        self.last_health = health


class _Net:
    """What answers where: addr → /health (None = dead or our own twin). Its
    connect feeds `dead` as the walk's connect_peer does: a bare (discovered)
    address that does not answer starts backing off."""

    def __init__(self, answers):
        self.answers = answers
        self.probed: list = []
        self.dead = DeadAddresses()

    async def connect(self, addr):
        self.probed.append(addr)
        health = self.answers.get(addr)
        if "://" not in addr:
            if health:
                self.dead.answered(addr)
            else:
                self.dead.failed(addr)
        url = addr if "://" in addr else f"https://{addr}"
        return _Client(url, health) if health else None


class _Dht:
    def __init__(self, holders):
        self.holders = holders
        self.asked: list = []

    async def lookup_capability(self, capability, want_all=False):
        self.asked.append((capability, want_all))
        return list(self.holders)


class _Lan:
    def __init__(self, peers):
        self.peers = [(ip, port) for ip, port, _ in peers]
        self._scheme = {(ip, port): scheme for ip, port, scheme in peers}

    def get_peer_info(self, ip, port):
        return {"scheme": self._scheme[(ip, port)]}


@pytest.fixture
def hints(monkeypatch):
    """The Worker directory and the master hint; `asked` records the calls."""
    state = {"directory": {}, "master": None, "asked": []}

    def fetch_directory(cap):
        state["asked"].append(cap)
        return state["directory"].get(cap, [])

    def fetch_master():
        state["asked"].append("master")
        return state["master"]

    monkeypatch.setattr(slice_sources.node_hints, "fetch", fetch_directory)
    monkeypatch.setattr(slice_sources.master_hint, "fetch", fetch_master)
    return state


def _finder(net, *, dht=None, lan=None, manual=(), bans=(set(), set())):
    return SourceFinder(
        "MB slice", capability="mbdump", directory=("mbslices", "mbdump"),
        usable=lambda h: bool(h.get("mb_dump") or h.get("mb_slices")),
        connect=net.connect, dead=net.dead, dht=dht, lan=lan, manual_peers=list(manual),
        load_bans=lambda: bans, addr_uuid=lambda addr: addr)


def _nodes(found):
    return [node for _, node, _ in found]


def test_every_holder_of_the_dht_key_is_asked(hints):
    dht = _Dht([("203.0.113.5", 21001)])
    net = _Net({"203.0.113.5:21001": {**DUMP, "node_id": "d1"}})
    found, probed = asyncio.run(_finder(net, dht=dht).find())
    assert dht.asked == [("mbdump", True)]
    assert probed and _nodes(found) == ["d1"]
    assert hints["asked"] == []                  # a usable primary skips the fallback


def test_primary_candidates_that_serve_nothing_fall_back_to_the_directory(hints):
    # The DHT names a stale port, the LAN a node without the catalog and
    # this node's own twin (same account, same key → connect says None):
    # candidates, but no source. The directory and the master still answer.
    dht = _Dht([("203.0.113.5", 21001)])
    lan = _Lan([("192.0.2.10", 22000, "http"), ("192.0.2.11", 22001, "https")])
    net = _Net({
        "http://192.0.2.10:22000": {**PLAIN, "node_id": "plain"},
        "198.51.100.20:8801": {**REPLICA, "node_id": "volunteer"},
        "198.51.100.1:8801": {**DUMP, "node_id": "master"},
    })
    hints["directory"] = {"mbslices": [("198.51.100.20", 8801, "pk")]}
    hints["master"] = ("198.51.100.1", 8801)
    found, _ = asyncio.run(_finder(net, dht=dht, lan=lan).find())
    assert _nodes(found) == ["volunteer", "master"]     # the master hint LAST
    assert hints["asked"] == ["mbslices", "mbdump", "master"]
    assert net.probed[:3] == ["http://192.0.2.10:22000", "https://192.0.2.11:22001",
                              "203.0.113.5:21001"]


def test_one_node_behind_two_addresses_is_one_source(hints):
    lan = _Lan([("192.0.2.10", 22000, "https")])
    dht = _Dht([("203.0.113.5", 22000), ("203.0.113.5", 22000)])
    net = _Net({"https://192.0.2.10:22000": {**DUMP, "node_id": "d1"},
                "203.0.113.5:22000": {**DUMP, "node_id": "d1"}})
    found, _ = asyncio.run(_finder(net, dht=dht, lan=lan).find())
    assert _nodes(found) == ["d1"]
    assert found[0][0].base_url == "https://192.0.2.10:22000"   # the first tier's address
    assert net.probed.count("203.0.113.5:22000") == 1    # a repeated address is probed once


def test_banned_addresses_are_not_probed_and_banned_keys_not_kept(hints):
    dht = _Dht([("203.0.113.5", 21001), ("203.0.113.6", 21001), ("203.0.113.7", 21001)])
    net = _Net({"203.0.113.5:21001": {**DUMP, "node_id": "banned-key"},
                "203.0.113.6:21001": {**DUMP, "node_id": "d2"},
                "203.0.113.7:21001": {**DUMP, "node_id": "d3"}})
    finder = _finder(net, dht=dht, bans=({"banned-key"}, {"203.0.113.7:21001"}))
    found, _ = asyncio.run(finder.find())
    assert _nodes(found) == ["d2"]
    assert "203.0.113.7:21001" not in net.probed


def test_the_kept_set_serves_until_a_source_fails(hints):
    dht = _Dht([("203.0.113.5", 21001), ("203.0.113.6", 21001)])
    net = _Net({"203.0.113.5:21001": {**REPLICA, "node_id": "r1"},
                "203.0.113.6:21001": {**DUMP, "node_id": "d1"}})
    finder = _finder(net, dht=dht)

    async def go():
        first, probed = await finder.find()
        assert probed and _nodes(first) == ["r1", "d1"]
        again, probed = await finder.find()
        assert not probed and _nodes(again) == ["r1", "d1"]
        assert len(net.probed) == 2 and len(dht.asked) == 1   # no second probe
        finder.drop("r1")
        rest, probed = await finder.find()
        assert not probed and _nodes(rest) == ["d1"]
        finder.drop("d1")                                     # nothing left → probe
        fresh, probed = await finder.find()
        assert probed and _nodes(fresh) == ["r1", "d1"]
        assert len(net.probed) == 4
    asyncio.run(go())


def test_the_kept_set_ages_out_and_forget_drops_it(hints, monkeypatch):
    dht = _Dht([("203.0.113.5", 21001)])
    net = _Net({"203.0.113.5:21001": {**DUMP, "node_id": "d1"}})
    finder = _finder(net, dht=dht)
    clock = [1000.0]
    monkeypatch.setattr(slice_sources.time, "monotonic", lambda: clock[0])

    async def go():
        await finder.find()
        clock[0] += slice_sources.SOURCES_TTL - 1
        assert (await finder.find())[1] is False
        clock[0] += 2
        assert (await finder.find())[1] is True
        finder.forget()
        assert (await finder.find())[1] is True
    asyncio.run(go())
    assert len(net.probed) == 3


def test_an_empty_search_serves_local_wakes_until_a_network_pass(hints, monkeypatch):
    # A history import woke the cycles every few seconds on local data, and
    # with no source anywhere every wake searched the network again.
    clock = [1000.0]
    monkeypatch.setattr(slice_sources.time, "monotonic", lambda: clock[0])
    dht = _Dht([("203.0.113.5", 21001)])                 # a holder that is gone
    net = _Net({})
    finder = _finder(net, dht=dht)

    async def go():
        assert await finder.find() == ([], True)
        assert await finder.find() == ([], False)        # new MBIDs: no search
        assert len(dht.asked) == 1
        assert await finder.find(refresh=True) == ([], True)    # a network pass
        assert len(dht.asked) == 2
        clock[0] += slice_sources.SOURCES_TTL
        assert (await finder.find())[1] is True          # aged out like a found set
    asyncio.run(go())
    assert net.probed == ["203.0.113.5:21001"]           # dead: dialled once, not per search


def test_one_dead_address_record_for_the_walk_and_both_families(hints):
    dht = _Dht([("203.0.113.5", 21001)])
    net = _Net({})
    mb, lb = _finder(net, dht=dht), _finder(net, dht=dht)

    async def go():
        await mb.find()
        await lb.find()
    asyncio.run(go())
    assert net.probed == ["203.0.113.5:21001"]           # the other family did not dial it
    assert net.dead.backing_off("203.0.113.5:21001")     # and the walk skips it too


def test_manual_and_lan_peers_are_asked_on_every_search(hints):
    lan = _Lan([("192.0.2.10", 22000, "https")])
    net = _Net({})
    finder = _finder(net, lan=lan, manual=["https://192.0.2.20:22000"])

    async def go():
        await finder.find()
        await finder.find(refresh=True)
    asyncio.run(go())
    assert net.probed == ["https://192.0.2.20:22000", "https://192.0.2.10:22000"] * 2
    assert not net.dead.backing_off("https://192.0.2.10:22000")


def test_a_network_pass_is_a_walk_the_timer_or_this_nodes_dump():
    assert all(network_pass(t) for t in ("sync", "auto", "sources", "pending+sync"))
    assert not any(network_pass(t) for t in ("pending", "request", "enrich+scrobbles"))


def test_both_runtimes_dht_take_the_same_calls():
    """The walk and the finder are handed either runtime's DHTService. A
    method both copies define takes the same arguments: `want_all` reached
    only the launcher's lookup_capability on 2026-09-24, and on Docker the
    finder's DHT tier raised TypeError on every run, logged at debug."""
    import backend.dht_service as docker
    from desktop.p2p import dht_service as launcher

    def public(cls):
        return {name for name, _ in inspect.getmembers(cls, inspect.isfunction)
                if not name.startswith("_")}
    shared = public(docker.DHTService) & public(launcher.DHTService)
    assert "lookup_capability" in shared
    for name in sorted(shared):
        assert (inspect.signature(getattr(docker.DHTService, name))
                == inspect.signature(getattr(launcher.DHTService, name))), name
