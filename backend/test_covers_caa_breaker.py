"""Cover Art Archive front — circuit-breaker unit tests, pure logic (the
upstream fetch is stubbed; no DB, no network). Run:

    python -m pytest test_covers_caa_breaker.py -q
"""

import asyncio

import pytest

from routers import covers


@pytest.fixture(autouse=True)
def _reset_breaker(monkeypatch):
    covers._caa_cache.clear()
    covers._caa_inflight.clear()
    monkeypatch.setattr(covers, "_caa_failures", 0)
    monkeypatch.setattr(covers, "_caa_open", False)


class _Upstream:
    """Scripted stand-in for _caa_fetch: `outcomes` is consumed per call
    (an int status, or an asyncio.Event to hold the call until released,
    followed by the status)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def __call__(self, rg):
        self.calls.append(rg)
        step = self.outcomes.pop(0)
        if isinstance(step, tuple):
            gate, status = step
            await gate.wait()
        else:
            status = step
        if status == 200:
            return 200, "image/jpeg", b"jpeg"
        return status, "", b""


def _run(coro):
    return asyncio.run(coro)


def test_trips_after_consecutive_failures_then_probes_one_at_a_time(monkeypatch):
    async def scenario():
        gate = asyncio.Event()
        up = _Upstream(502, 502, 502, (gate, 502))
        monkeypatch.setattr(covers, "_caa_fetch", up)

        for rg in ("a", "b"):
            assert (await covers._caa_lookup(rg))[0] == 502
        assert not covers._caa_open
        assert (await covers._caa_lookup("c"))[0] == 502
        assert covers._caa_open

        probe = asyncio.create_task(covers._caa_lookup("d"))
        await asyncio.sleep(0)               # the probe is in flight
        deferred = await asyncio.gather(*(covers._caa_lookup(rg) for rg in "efg"))
        assert [d[0] for d in deferred] == [503, 503, 503]
        assert up.calls == ["a", "b", "c", "d"]   # nobody but the probe reached upstream

        gate.set()
        assert (await probe)[0] == 502
        assert covers._caa_open

    _run(scenario())


def test_burst_settles_then_first_definite_answer_closes(monkeypatch):
    async def scenario():
        gate = asyncio.Event()
        up = _Upstream(*[(gate, 502)] * 6, (asyncio.Event(), 200), 404)
        monkeypatch.setattr(covers, "_caa_fetch", up)

        burst = [asyncio.create_task(covers._caa_lookup(rg)) for rg in "abcdef"]
        await asyncio.sleep(0)
        assert len(covers._caa_inflight) == 6    # closed breaker: the burst went out
        gate.set()
        assert {r[0] for r in await asyncio.gather(*burst)} == {502}
        assert covers._caa_open

        probe_gate = up.outcomes[0][0]
        probe = asyncio.create_task(covers._caa_lookup("g"))
        await asyncio.sleep(0)
        assert (await covers._caa_lookup("h"))[0] == 503
        probe_gate.set()
        assert (await probe)[0] == 200
        assert not covers._caa_open
        assert covers._caa_cache["g"][0] == 200

        assert (await covers._caa_lookup("h"))[0] == 404   # closed again: fetched
        assert up.calls[-1] == "h"

    _run(scenario())


def test_sporadic_failures_do_not_trip(monkeypatch):
    async def scenario():
        up = _Upstream(502, 200, 502, 502, 404, 502, 502)
        monkeypatch.setattr(covers, "_caa_fetch", up)
        for rg in "abcdefg":
            await covers._caa_lookup(rg)
        assert not covers._caa_open
        assert covers._caa_failures == 2

    _run(scenario())


def test_cached_answers_are_served_while_open(monkeypatch):
    async def scenario():
        gate = asyncio.Event()
        up = _Upstream(200, 502, 502, 502, (gate, 502))
        monkeypatch.setattr(covers, "_caa_fetch", up)
        assert (await covers._caa_lookup("cached"))[0] == 200
        for rg in "abc":
            await covers._caa_lookup(rg)
        assert covers._caa_open
        probe = asyncio.create_task(covers._caa_lookup("d"))
        await asyncio.sleep(0)
        assert (await covers._caa_lookup("cached"))[0] == 200
        gate.set()
        await probe

    _run(scenario())
