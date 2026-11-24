"""The Library stream (routers/settings.py) and what wakes it (B-138).

Until 2026-11-24 every NOTIFY on sautium_identity and sautium_notices woke
the stream, the load meter said its band changes on sautium_identity — one
nearly every two-second sample under any activity — and the page asked
GET /api/settings/guidance at every wake. Now each channel wakes the readers
of what changed, the load has a channel of its own that reaches the stream as
a named event, and the guidance trail is computed once a wake for every
stream and said only when its set changed.

The stream's generator is driven as the server drives it; the trail's rules
are stood in for (no database here).
"""

import asyncio
import json
import sys
import threading
import time

import pytest

from tests.conftest import REPO_ROOT

if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

settings_router = pytest.importorskip("routers.settings")

WAKE = "data: {}\n\n"


class Note:
    def __init__(self, channel: str, payload: str = "") -> None:
        self.channel, self.payload = channel, payload


@pytest.fixture
def trail(monkeypatch):
    """A trail of its own over rules the test sets: `trail.rules` is what
    _guidance_state finds now, `trail.computed` how often it was asked."""
    trail = settings_router._GuidanceTrail()
    trail.rules, trail.computed = ["audio_output"], 0

    def state():
        trail.computed += 1
        return {"tasks": list(trail.rules)}
    monkeypatch.setattr(settings_router, "_guidance_state", state)
    monkeypatch.setattr(settings_router, "_guidance", trail)
    monkeypatch.setattr(settings_router, "_library_streams", [])
    yield trail
    trail.stop()


class Page:
    """One connection of the stream, read event by event."""

    async def open(self) -> "Page":
        self.events = (await settings_router.library_stream()).body_iterator
        return self

    async def next(self) -> str:
        return await asyncio.wait_for(self.events.__anext__(), 5)

    async def named(self, event: str) -> dict:
        said = await self.next()
        head, _, data = said.partition("\n")
        assert head == f"event: {event}", said
        assert data.startswith("data: ") and data.endswith("\n\n"), said
        return json.loads(data[len("data: "):])

    async def close(self) -> None:
        await self.events.aclose()


def _load(headroom: float) -> str:
    return json.dumps({"load": {"headroom": headroom, "dormant": True}, "gate": None})


# -- each channel wakes its own readers ------------------------------------------

@pytest.mark.parametrize("channel, woken", [
    ("sautium_sync_done", ["library"]),
    ("sautium_identity", ["identity", "trail"]),
    ("sautium_notices", ["notices", "trail"]),
    ("sautium_load", ["streams: load"]),
])
def test_a_notify_wakes_the_readers_of_its_channel_and_no_others(trail, monkeypatch, channel, woken):
    """The identity's and the notices' subscribers are the page's event
    stream's; the trail reads both, so it is computed again; neither is a
    wake of the Library stream, and the load is no wake of anything."""
    heard = []
    monkeypatch.setattr(settings_router, "notify_library_subscribers", lambda: heard.append("library"))
    monkeypatch.setattr(settings_router, "notify_identity_subscribers", lambda: heard.append("identity"))
    monkeypatch.setattr(settings_router, "notify_notice_subscribers", lambda: heard.append("notices"))
    monkeypatch.setattr(settings_router, "_tell_library_streams",
                        lambda kind: heard.append(f"streams: {kind}"))
    monkeypatch.setattr(trail, "moved", lambda: heard.append("trail"))
    settings_router._on_sync_notify([Note(channel, _load(0.9))])
    assert heard == woken


def test_the_listener_hears_the_loads_channel():
    assert "sautium_load" in settings_router._sync_listener.channels
    from desktop.p2p import load_meter
    assert load_meter.CHANNEL == "sautium_load"
    assert json.loads(load_meter.message({"headroom": 0.4}, {"mode": "shadow"})) == {
        "load": {"headroom": 0.4}, "gate": {"mode": "shadow"}}


def test_a_library_wake_has_the_trail_computed_again(trail):
    assert not trail._moved.is_set()
    settings_router.notify_library_subscribers()
    assert trail._moved.is_set()


# -- the stream ------------------------------------------------------------------

def test_a_load_band_change_is_a_named_event_and_no_wake(trail):
    async def scenario():
        page = await Page().open()
        assert await page.next() == WAKE                       # the connect's ping
        assert (await page.named("guidance"))["tasks"] == ["audio_output"]
        computed = trail.computed
        # two band changes heard in one burst: the newest is what the screen shows
        settings_router._on_sync_notify([Note("sautium_load", _load(0.9)), Note("sautium_load", _load(0.7))])
        assert await page.named("load") == {"load": {"headroom": 0.7, "dormant": True}, "gate": None}
        assert trail.computed == computed and not trail._moved.is_set()
        # ... and a wake after it is the next thing the stream says: nothing rode along
        settings_router.notify_library_subscribers()
        assert await page.next() == WAKE
        await page.close()
    asyncio.run(scenario())


def test_the_stream_says_the_trail_at_connect_and_when_its_set_changes(trail):
    async def scenario():
        page = await Page().open()
        assert await page.next() == WAKE
        first = await page.named("guidance")
        assert first["tasks"] == ["audio_output"] and first["run"] == trail._id

        # a wake that changes nothing: the wake, the computation, no word of the trail
        settings_router.notify_library_subscribers()
        assert await page.next() == WAKE
        computed = trail.computed
        await asyncio.to_thread(trail.compute)
        assert trail.computed == computed + 1
        settings_router._on_sync_notify([Note("sautium_load", _load(0.5))])
        await page.named("load")                               # the next thing said: no guidance before it

        # a task appears with a notice: no Library wake, and the trail said once
        trail.rules.append("notices")
        settings_router._on_sync_notify([Note("sautium_notices")])
        assert trail._moved.is_set()
        await asyncio.to_thread(trail.compute)
        appeared = await page.named("guidance")
        assert appeared["tasks"] == ["audio_output", "notices"] and appeared["seq"] > first["seq"]

        # a task goes with the identity's state
        trail.rules.remove("audio_output")
        settings_router._on_sync_notify([Note("sautium_identity")])
        await asyncio.to_thread(trail.compute)
        gone = await page.named("guidance")
        assert gone["tasks"] == ["notices"] and gone["seq"] > appeared["seq"]

        # a page that connects now is told what stands
        other = await Page().open()
        assert await other.next() == WAKE
        assert (await other.named("guidance"))["tasks"] == ["notices"]
        # ... and the first page hears nothing of that connect's computation
        settings_router._on_sync_notify([Note("sautium_load", _load(0.4))])
        await page.named("load")
        await page.close()
        await other.close()
        assert settings_router._library_streams == []
    asyncio.run(scenario())


def test_a_stream_stands_when_its_connect_cannot_compute_the_trail(trail, monkeypatch, caplog):
    """The database away at a connect: the stream wakes its page as before,
    and the trail is said at the first computation that succeeds."""
    async def scenario():
        failing = True

        def state():
            if failing:
                raise RuntimeError("no database")
            return {"tasks": ["music_folder"]}
        monkeypatch.setattr(settings_router, "_guidance_state", state)
        page = await Page().open()
        assert await page.next() == WAKE
        settings_router.notify_library_subscribers()
        assert await page.next() == WAKE
        failing = False
        await asyncio.to_thread(trail.compute)
        assert (await page.named("guidance"))["tasks"] == ["music_folder"]
        await page.close()
    asyncio.run(scenario())
    assert "not computed for a stream's connect" in caplog.text


# -- the answers' order ----------------------------------------------------------

def test_every_answer_is_numbered_in_the_order_it_was_computed(trail, monkeypatch):
    """The first read, a "seen" and the stream's word may reach a page in any
    order: each carries the computation's number, and the route that changes
    the trail says it to the streams too."""
    store = {}
    monkeypatch.setattr(settings_router, "_write", store.__setitem__)

    async def scenario():
        page = await Page().open()
        assert await page.next() == WAKE
        connect = await page.named("guidance")
        read = await asyncio.to_thread(settings_router.get_guidance)
        assert read["tasks"] == ["audio_output"] and read["seq"] > connect["seq"]
        assert read["run"] == connect["run"]
        trail.rules.clear()                                    # what the visit retires
        seen = await asyncio.to_thread(settings_router.post_guidance_seen, "audio_output")
        assert store == {"guidance.audio_output_seen": True}
        assert seen["tasks"] == [] and seen["seq"] > read["seq"]
        assert await page.next() == WAKE                       # the write's own wake
        assert await page.named("guidance") == seen
        await page.close()
    asyncio.run(scenario())


# -- the trail's thread ----------------------------------------------------------

def _settled(trail) -> None:
    """The thread took the last `moved`."""
    deadline = time.monotonic() + 5
    while trail._moved.is_set():
        assert time.monotonic() < deadline, "the trail's thread never took its wake"
        time.sleep(0.005)


def test_one_computation_serves_a_burst_of_wakes(trail, monkeypatch):
    monkeypatch.setattr(trail, "PACE_S", 0.01)
    monkeypatch.setattr(settings_router, "_library_streams", [type("S", (), {"owe": lambda self, kind: None})()])
    entered, release, again = threading.Event(), threading.Event(), threading.Event()

    def state():
        trail.computed += 1
        if trail.computed == 1:
            entered.set()
            assert release.wait(5)
        else:
            again.set()
        return {"tasks": []}
    monkeypatch.setattr(settings_router, "_guidance_state", state)
    trail.start()
    trail.moved()
    assert entered.wait(5)
    for _ in range(12):                 # an enrichment's first second, while the first computation runs
        trail.moved()
    release.set()
    assert again.wait(5)
    _settled(trail)
    trail.stop()
    assert trail.computed == 2


def test_with_no_page_open_nothing_is_computed(trail):
    trail.start()
    trail.moved()
    _settled(trail)
    trail.stop()
    assert trail.computed == 0


def test_a_computation_that_fails_is_said_and_the_next_wake_computes(trail, monkeypatch, caplog):
    monkeypatch.setattr(trail, "PACE_S", 0.01)
    told = []
    monkeypatch.setattr(settings_router, "_library_streams",
                        [type("S", (), {"owe": lambda self, kind: told.append(kind)})()])
    done = threading.Event()

    def state():
        trail.computed += 1
        if trail.computed == 1:
            raise RuntimeError("no database")
        done.set()
        return {"tasks": ["notices"]}
    monkeypatch.setattr(settings_router, "_guidance_state", state)
    trail.start()
    trail.moved()
    _settled(trail)
    trail.moved()
    assert done.wait(5)
    trail.stop()
    assert "The guidance trail was not computed" in caplog.text
    assert told == ["guidance"] and trail.said["tasks"] == ["notices"]
