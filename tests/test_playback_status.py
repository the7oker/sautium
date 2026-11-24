"""Playback status plumbing: the manager hears the ACTIVE backend only, the
DLNA liveness probe is decided by the first address that answers, and a
failure on our own disk is never read as the renderer going away."""

import asyncio
import errno
import socket
import sys
import time
import types
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from playback.base import Capabilities, PlaybackStatus, PlayerBackend  # noqa: E402
from async_upnp_client.exceptions import (  # noqa: E402
    UpnpConnectionError, UpnpConnectionTimeoutError)
from playback.dlna_backend import DlnaBackend, renderer_online  # noqa: E402
from playback.manager import PlaybackManager  # noqa: E402
from playback.queue import CanonicalQueue, QueueItem  # noqa: E402
from streaming import service as streaming_service  # noqa: E402
from streaming.proxy import MediaProxy  # noqa: E402


class _Stub(PlayerBackend):
    id = "stub"
    label = "stub"

    def start(self): pass
    def shutdown(self): pass
    def capabilities(self): return Capabilities()
    def play(self): return True
    def pause(self): return True
    def stop(self): return True
    def next(self): return True
    def previous(self): return True
    def select(self, index, track_id=None): return True


def test_manager_hears_the_active_backend_only():
    mgr = PlaybackManager()
    live = _Stub(mgr._on_backend_status)
    stale = _Stub(mgr._on_backend_status)
    mgr._active = live

    # The output the user just left fails its switch-away stop: its error
    # tick must not reach the status the new output owns.
    stale._emit(PlaybackStatus(state="stopped",
                               extra={"error": "'stale' stop failed"}))
    assert mgr.latest_status == {"state": "disconnected"}

    live._emit(PlaybackStatus(state="stopped", queue_index=0))
    assert mgr.latest_status["state"] == "stopped"
    assert "error" not in mgr.latest_status
    assert mgr.latest_status["output"]["label"] == "stub"


def test_only_a_slot_of_the_queue_sounding_ends_what_a_play_said_of_its_failures(monkeypatch):
    """A Play none of whose slots streams is said (`stream_event`) until
    something of the queue sounds. What plays at no slot of it is no such
    sound: another controller's entry on the output, a track of the queue
    before that a renderer plays on — each took the word back before."""
    from playback import tracker
    monkeypatch.setattr(tracker, "track_play_event", lambda *a, **k: None)
    mgr = PlaybackManager()
    mgr.lane.maintain = lambda: None
    out = _Stub(mgr._on_backend_status)
    mgr._active = out
    slot = QueueItem(track_id="t-1", media_file_id=None, source={"kind": "track"}, title="One",
                     play={"kind": "unplayable", "reason": "x", "code": "unknown", "lane": True})
    mgr.queue.replace([slot])
    mgr._play_started([slot])
    mgr._slot_failed(slot, 1, "unknown")
    said = mgr.latest_status["stream_event"]
    assert (said["kind"], said["all"]) == ("not_streamable", True)
    for extra in ({"source": "external"}, {"unslotted": True}):
        out._emit(PlaybackStatus(state="playing", position=12.0, length=200.0, queue_index=0,
                                 extra=extra))
        assert mgr.latest_status.get("stream_event") == said, extra
    out._emit(PlaybackStatus(state="playing", position=1.0, length=200.0, queue_index=1))
    assert "stream_event" not in mgr.latest_status


def test_renderer_online_is_decided_by_the_first_address_that_answers():
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        renderer = {
            # A stale address that swallows the connect (a device that left
            # this network answers nothing) must not hold up the live one.
            "location": "http://10.255.255.1:5500/desc.xml",
            "locations": [f"http://127.0.0.1:{port}/desc.xml"],
        }
        t0 = time.monotonic()
        assert renderer_online(renderer, timeout=2.0)
        assert time.monotonic() - t0 < 1.5
    assert not renderer_online(
        {"location": f"http://127.0.0.1:{port}/desc.xml"}, timeout=0.5)
    assert not renderer_online({}, timeout=0.5)


def test_a_disk_failure_is_not_the_renderer_leaving():
    backend = types.SimpleNamespace(label="KANN_ULTRA", _gone=False, _error=None,
                                    _emit_now=lambda state=None: None)
    backend._lose = lambda: setattr(backend, "_gone", True)
    # The library's drive unmounted under the node: the load cannot read the
    # file. The renderer answered nothing wrong — detaching it and telling
    # the user to wake it sent them after a device that was on.
    for e in (FileNotFoundError(errno.ENOENT, "No such file or directory",
                                "/music/A/B/01.flac"),
              OSError(errno.ENODEV, "No such device")):
        DlnaBackend._cmd_failed(backend, e, "load and play")
        assert backend._gone is False
        assert "did not respond" not in backend._error
    for e in (UpnpConnectionError("refused"), UpnpConnectionTimeoutError("timed out"),
              TimeoutError()):
        backend._gone = False
        DlnaBackend._cmd_failed(backend, e, "load and play")
        assert backend._gone is True


def test_a_library_gone_from_disk_is_walked_past_without_recursing(monkeypatch):
    monkeypatch.setattr(streaming_service, "_proxy",
                        MediaProxy(port=0, advertised_host="127.0.0.1",
                                   file_token_key=b"test-key"))
    monkeypatch.setattr(DlnaBackend, "_quality", staticmethod(lambda: "lossless"))
    queue = CanonicalQueue()
    # Every owned slot of an unmounted library is one to skip, and a queue
    # longer than the old per-slot recursion could go (~1000) must end in
    # the reason, not a RecursionError.
    queue.replace([QueueItem(track_id=f"t{i}", media_file_id=i,
                             source={"kind": "file", "path": f"/nonexistent/{i}.flac",
                                     "format": "FLAC"},
                             title=f"T{i}", artist="A")
                   for i in range(1, 1201)])
    statuses = []
    backend = DlnaBackend(emit=lambda b, s: statuses.append(s), queue=queue,
                          renderer={"udn": "uuid:stub", "name": "KANN_ULTRA",
                                    "location": "http://127.0.0.1:9/desc.xml"})
    backend._dmr = types.SimpleNamespace(is_subscribed=True, transport_state=None,
                                         volume_level=None, sink_protocol_info=[])
    assert asyncio.run(backend._load_seq(1, halts=backend._halts)) is False
    assert backend._error.startswith("1200 queued track(s) could not be read")
    assert backend._gone is False
    assert statuses[-1].state == "stopped"


def test_empty_backend_tags_fall_back_to_the_queue_item():
    mgr = PlaybackManager()
    mgr.queue.replace([QueueItem(
        track_id=None, media_file_id=7,
        source={"kind": "file", "path": "E:/Music/A/01.flac", "format": "FLAC"},
        title="Song", artist="Artist", album="Album")])
    live = _Stub(mgr._on_backend_status)
    mgr._active = live
    # HQPlayer knows an http-served slot only as 'HTTP stream' and reports
    # empty tags for it: the queue item names the track.
    live._emit(PlaybackStatus(state="stopped", queue_index=1,
                              extra={"artist": "", "album": "", "song": ""}))
    st = mgr.latest_status
    assert (st["artist"], st["album"], st["song"]) == ("Artist", "Album", "Song")


def test_a_next_block_takes_its_places_at_once_in_its_order(monkeypatch):
    """A block queued Next (a CUE album, an m4a set, a streamed album) is
    committed whole, in one mutation: right behind the playing slot, in its
    own order — not at the end, and not backwards."""

    def item(n):
        return QueueItem(track_id=f"t{n}", media_file_id=n,
                         source={"kind": "file", "path": f"/m/{n}.flac",
                                 "format": "FLAC"},
                         title=f"T{n}", artist="A")

    mgr = PlaybackManager()
    # The test database is the live one: the queue snapshot stays out of it.
    monkeypatch.setattr(mgr, "_schedule_persist", lambda: None)
    mgr.queue.replace([item(1), item(2)])
    mgr._latest_status = {"state": "playing", "track_index": 1}

    assert mgr.append([item(10), item(11), item(12)], "next") == 3

    assert [it.track_id for it in mgr.queue.snapshot()] == [
        "t1", "t10", "t11", "t12", "t2"]


def test_a_seek_the_output_refused_answers_in_its_words(monkeypatch):
    """The output refused the Seek in its own words: the route answers ok
    false with them, as it does a Pause, so the person is told what it said."""
    import routers.player as player

    class Refusing:
        def seek(self, seconds):
            return False

        def refusal(self):
            return "HQPlayer refused Seek"

        def refused_attempt(self):
            return None

        def pressing(self):
            return None

    monkeypatch.setattr(player, "manager", types.SimpleNamespace(backend=Refusing))
    assert player.seek(player.SeekRequest(position=30)) == {"ok": False, "refusal": "HQPlayer refused Seek"}


def test_the_status_a_connection_opens_with_carries_the_floor_of_its_events(monkeypatch):
    """The page's event stream: the status it opens with carries
    `event_floor`, an id of the events' kind minted at the connection — an
    event with an id at or under it (the one that status carries; one that
    comes back on a later status) happened before the connection, and the
    page tells none of them (`output_event`, ROON-COEX §12.6); an event
    minted after it is above the floor. A status pushed after is a live one,
    with no floor, and the floor is the stream's own, never kept on the
    manager's status."""
    import json

    from routers import player as player_router
    from routers import settings as settings_router
    monkeypatch.setattr(settings_router, "_notices_state", lambda: {})
    monkeypatch.setattr(settings_router, "identity_event", lambda: {"status": None})
    from playback.base import new_id
    mgr = player_router.manager
    before = new_id()
    monkeypatch.setattr(mgr, "_latest_status", {"state": "stopped", "output_event": {"id": before, "kind": "took"}})

    async def run():
        stream = (await player_router.events_stream(tab=None)).body_iterator
        assert await stream.__anext__() == 'data: {"t": "hello"}\n\n'
        first = json.loads((await stream.__anext__())[6:])
        assert first["t"] == "status" and first["d"]["output_event"] == {"id": before, "kind": "took"}
        floor = first["d"]["event_floor"]
        assert before < floor < new_id()
        slots = json.loads((await stream.__anext__())[6:])
        assert slots["t"] == "slots" and slots["d"] == {"version": mgr.queue.version, "slots": []}
        assert json.loads((await stream.__anext__())[6:])["t"] == "reach"
        assert json.loads((await stream.__anext__())[6:])["t"] == "notice"
        assert json.loads((await stream.__anext__())[6:])["t"] == "identity"
        mgr._push_status({"state": "stopped", "output": {}, "output_event": {"id": new_id(), "kind": "refused"}})
        live = json.loads((await asyncio.wait_for(stream.__anext__(), 3))[6:])
        assert live["t"] == "status" and "event_floor" not in live["d"]
        assert live["d"]["output_event"]["id"] > floor and "event_floor" not in mgr.latest_status
        reading = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0.05)
        reading.cancel()
        try:
            await reading
        except (StopAsyncIteration, asyncio.CancelledError):
            pass

    asyncio.run(run())


def test_a_queue_edit_while_stopped_keeps_the_outputs_words_on_the_status(monkeypatch):
    """The manager's own stopped status after a queue edit (an append while
    nothing plays) is about the queue; what the output said last stands on
    it until the backend's next tick — a page ends a toast with the status
    that says its condition no more: without them an append under a
    takeover's spot ended that toast, and a silent HQPlayer's next status
    raised "not answering" again for the person who had closed it
    (ROON-COEX §12.7, review round 1)."""
    mgr = PlaybackManager()
    mgr._active = _Stub(mgr._on_backend_status)
    mgr.queue.replace([QueueItem(track_id="t1", media_file_id=1, source={"kind": "file", "path": "/m/1.flac"},
                                 title="One", artist="A")])
    said = {"diagnosis": {"id": 7, "code": "not_played"}, "output_event": {"id": 9, "kind": "took"},
            "resume": {"index": 1, "position": 47}, "building": {"since": 1.0, "silent": True},
            "not_answering": "HQPlayer is not answering", "rebuilding": True}
    monkeypatch.setattr(mgr, "_latest_status", {"state": "stopped", "process_speed": 1.0, **said})
    mgr._emit_idle_status()
    assert {k: mgr.latest_status.get(k) for k in said} == said
    assert mgr.latest_status["song"] == "One" and mgr.latest_status["process_speed"] == 0.0
    # ...and none of them is made up where the output said none
    monkeypatch.setattr(mgr, "_latest_status", {"state": "stopped"})
    mgr._emit_idle_status()
    assert not set(said) & set(mgr.latest_status)
    # With no output attached the output's other words go — there is none to say them again — but
    # the spot a failed attach held stays through an append, which moves no slot (the next Play
    # starts there); a removal moves the slot it names, and nothing follows to correct it: it goes.
    mgr._active = None
    monkeypatch.setattr(PlaybackManager, "output_info", property(lambda self: {"type": None, "label": None}))
    monkeypatch.setattr(mgr, "_latest_status", {"state": "disconnected", **said})
    mgr._emit_idle_status()
    assert {k for k in said if k in mgr.latest_status} == {"diagnosis", "resume"}
    monkeypatch.setattr(mgr, "_latest_status", {"state": "disconnected", **said})
    mgr._emit_idle_status(slots_moved=True)             # as a removal with no output attached says it
    assert {k for k in said if k in mgr.latest_status} == {"diagnosis"}
    import inspect
    assert "self._emit_idle_status(slots_moved=True)" in inspect.getsource(PlaybackManager.remove)


def test_no_playback_class_defines_a_method_twice():
    """Two slices that each add a method of one name to a class rebase onto
    each other without a conflict, and the later definition shadows the
    earlier in silence: a takeover's spot was left by another slice's method
    of the same name, with other arguments (ROON-COEX-4 on atlas-release,
    2026-11-12; before it `_heard`, `_rest_at`). No class of the playback
    package defines a method twice."""
    import ast
    import collections
    twice = {}
    for path in sorted((BACKEND / "playback").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                names = collections.Counter(f.name for f in node.body
                                            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                                            and not any(isinstance(d, ast.Attribute) and d.attr in ("setter", "getter")
                                                        for d in f.decorator_list))
                twice.update({f"{path.name}:{node.name}.{name}": n for name, n in names.items() if n > 1})
    assert twice == {}
