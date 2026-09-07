"""The network-size verdict (pure arithmetic) and the traversal lane's two
promises: a floor between initiations, and half the lane to each side when
both are waiting."""

import asyncio
import time

import pytest

from desktop.p2p import network_size as ns
from desktop.p2p.dht_service import _TraversalLane


def test_estimate_is_the_sample_while_the_reply_is_exhaustive():
    assert ns.estimate(marked=900, sample=7, recaptured=7, exhaustive=True) == 7
    assert ns.estimate(marked=0, sample=40, recaptured=0, exhaustive=False) == 40


def test_estimate_scales_the_ledger_by_the_unknown_share():
    # Half of a 100-node sample already known → the ledger covers ~half.
    assert ns.estimate(marked=600, sample=100, recaptured=50, exhaustive=False) == 1189
    # Nothing recaptured: finite, and large — the ledger is a sliver.
    assert ns.estimate(marked=600, sample=100, recaptured=0, exhaustive=False) == 60700
    # Everything recaptured: the ledger IS the population.
    assert ns.estimate(marked=100, sample=100, recaptured=100, exhaustive=False) == 100


def test_rare_mode_hysteresis():
    assert ns.rare_mode(ns.RARE_ON, previous=False) is True
    assert ns.rare_mode(ns.RARE_OFF, previous=True) is False
    between = (ns.RARE_ON + ns.RARE_OFF) // 2
    assert ns.rare_mode(between, previous=True) is True
    assert ns.rare_mode(between, previous=False) is False


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_lane_spaces_initiations_and_alternates_sides():
    async def scenario():
        spacing = 0.02
        lane = _TraversalLane(spacing, lambda: 1.0)
        lane.start()
        grants: list[tuple[str, float]] = []

        async def caller(side, n):
            for _ in range(n):
                await lane.slot(side)
                grants.append((side, time.monotonic()))

        await asyncio.gather(caller("tail", 3), caller("search", 3))
        await lane.stop()
        return grants

    grants = _run(scenario())
    assert len(grants) == 6
    gaps = [b - a for (_, a), (_, b) in zip(grants, grants[1:])]
    assert all(gap >= 0.02 * 0.9 for gap in gaps), gaps          # the floor
    sides = [side for side, _ in grants]
    assert sides == ["tail", "search"] * 3                         # half each


def test_lane_is_work_conserving_and_pace_stretches_it():
    async def scenario():
        pace = {"x": 1.0}
        lane = _TraversalLane(0.01, lambda: pace["x"])
        lane.start()
        t0 = time.monotonic()
        for _ in range(3):
            await lane.slot("search")                              # alone: every slot
        alone = time.monotonic() - t0
        pace["x"] = 4.0
        t1 = time.monotonic()
        await lane.slot("search")
        await lane.slot("search")
        stretched = time.monotonic() - t1
        await lane.stop()
        return alone, stretched

    alone, stretched = _run(scenario())
    assert alone < 0.1
    assert stretched >= 0.04 * 0.9


def test_lane_stop_cancels_waiters():
    async def scenario():
        lane = _TraversalLane(10.0, lambda: 1.0)
        lane.start()
        await lane.slot("tail")                                    # first grant is immediate
        waiter = asyncio.ensure_future(lane.slot("search"))
        await asyncio.sleep(0)
        await lane.stop()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    _run(scenario())
