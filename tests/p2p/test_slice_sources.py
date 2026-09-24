"""The slice-source finder (desktop/p2p/slice_sources.py) both slice cycles
share — the tiers, the fallback, the kept set. No network, no DB: the walk's
connect, the DHT, the LAN table and the two hint services are stand-ins."""

import asyncio

import pytest

from desktop.p2p import slice_sources
from desktop.p2p.slice_sources import SourceFinder

DUMP = {"mb_dump": "20260920-001", "mb_slices": 0}
REPLICA = {"mb_dump": None, "mb_slices": 4210}
PLAIN = {"mb_dump": None, "mb_slices": 0}


class _Client:
    def __init__(self, base_url, health):
        self.base_url = base_url
        self.last_health = health


class _Net:
    """What answers where: addr → /health (None = dead or our own twin)."""

    def __init__(self, answers):
        self.answers = answers
        self.probed: list = []

    async def connect(self, addr):
        self.probed.append(addr)
        health = self.answers.get(addr)
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
        connect=net.connect, dht=dht, lan=lan, manual_peers=list(manual),
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


def test_no_source_is_never_kept(hints):
    net = _Net({})
    dht = _Dht([("203.0.113.5", 21001)])
    finder = _finder(net, dht=dht)

    async def go():
        assert await finder.find() == ([], True)
        assert await finder.find() == ([], True)
    asyncio.run(go())
    assert net.probed == ["203.0.113.5:21001"] * 2
