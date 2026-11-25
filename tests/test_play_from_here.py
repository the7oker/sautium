"""PLAYBACK-UI-1, the parts with no database: a new queue played from a slot
of it at a second (B-156's "from here", B-158's Resume) on the browser
output; Previous's one rule (B-157) — past the first 3 s it restarts the
track, within them the track before — decided by the manager for every
output; and where a queue's listen stopped (tracker.stop_in), the fact the
archive and the status read."""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

pytest.importorskip("sqlalchemy")
from playback.queue import QueueItem   # noqa: E402


def _file(n, seconds=200.0):
    return QueueItem(track_id=f"t-{n}", media_file_id=n,
                     source={"kind": "file", "path": f"E:/Music/{n}.flac", "format": "FLAC"},
                     title=f"File {n}", artist="A", duration_seconds=seconds)


@pytest.fixture
def mgr(monkeypatch):
    """The real PlaybackManager on the browser output, a tab holding it, no
    database: every slot opens as it was queued, no listen recorded, no
    queue persisted. `mgr.pushed`: what the tab was told; `mgr.looked`: the
    slots each start's look was asked about."""
    import routers.settings as settings_router
    from playback import substitute, tracker
    from playback.browser_backend import BrowserBackend
    from playback.manager import PlaybackManager
    monkeypatch.setattr(settings_router, "_read", lambda key: None)
    monkeypatch.setattr(substitute, "native_plays", lambda items, output, endpoint: [None] * len(items))
    monkeypatch.setattr(tracker, "_save_play_session", lambda s: None)
    monkeypatch.setattr(tracker, "_scrobble_async", lambda *a, **k: True)
    monkeypatch.setattr(tracker, "_play_session", None)
    monkeypatch.setattr(tracker, "_last_heard", None, raising=False)
    m = PlaybackManager()
    monkeypatch.setattr(m, "_schedule_persist", lambda: None)
    monkeypatch.setattr(m.lane, "maintain", lambda: None)
    b = BrowserBackend(emit=m._on_backend_status, queue=m.queue)
    b._channel = object()                       # a tab holds the output
    m._active = b
    monkeypatch.setattr(m, "ensure_active", lambda: b)
    m.pushed = []
    monkeypatch.setattr(b, "_push", m.pushed.append)
    m.looked = []
    monkeypatch.setattr(m, "_first_opens",
                        lambda backend, items, held=False: m.looked.append(list(items)) or False)
    m.b = b
    return m


def _loads(m):
    return [d for d in m.pushed if d.get("cmd") == "load"]


# -- a new queue played from a slot (B-156, B-158) -------------------------------


def test_a_new_queue_played_from_a_slot_starts_there_at_its_second(mgr):
    items = [_file(n) for n in (1, 2, 3, 4)]
    added, _gen = mgr.replace_queue(items, play=True, start=(items[2], 12.0))
    assert added == 4 and mgr.queue.snapshot() == items          # the whole queue
    load = _loads(mgr)[-1]
    assert (load["queue_index"], load["position"], load["play"]) == (3, 12.0, True)
    # what the start reads is the queue from the start on: the look, the
    # stream lane's anchor
    assert mgr.looked[-1] == items[2:]
    assert (mgr._anchor_item, mgr._anchor_slot) == (items[2], 3)


def test_a_new_queue_with_no_start_plays_from_its_first_slot_as_before(mgr):
    items = [_file(n) for n in (1, 2)]
    mgr.replace_queue(items, play=True)
    load = _loads(mgr)[-1]
    assert (load["queue_index"], load["position"]) == (1, 0.0)
    assert mgr.looked[-1] == items


def test_a_start_is_the_slot_by_identity_never_by_an_equal_track(mgr):
    """A track a queue holds twice is two slots: the start names one."""
    one, other = _file(5), _file(5)
    items = [_file(1), one, _file(2), other]
    mgr.replace_queue(items, play=True, start=(other, 0.0))
    assert _loads(mgr)[-1]["queue_index"] == 4


# -- Previous (B-157) --------------------------------------------------------------


def _at(mgr, state, position, index=3, **extra):
    """The status the screen shows: the browser output at `index`, `position`."""
    items = [_file(n) for n in (1, 2, 3, 4)]
    mgr.queue.replace(items)
    mgr.b._index = index
    mgr.b._state = state
    mgr._latest_status = {"state": state, "position": position, "track_index": index, **extra}
    mgr.pushed.clear()
    return items


@pytest.mark.parametrize("state", ["playing", "paused"])
def test_previous_past_three_seconds_restarts_the_track(mgr, state):
    _at(mgr, state, 10.0)
    seeks = []
    mgr.b.seek = lambda s: seeks.append(s) or True
    mgr.b.previous = lambda: pytest.fail("stepped back past 3 s")
    assert mgr.previous(mgr.b) is True
    assert seeks == [0]


def test_previous_within_three_seconds_goes_to_the_track_before(mgr):
    _at(mgr, "playing", 2.0)
    mgr.b.seek = lambda s: pytest.fail("restarted within 3 s")
    assert mgr.previous(mgr.b) is True
    assert _loads(mgr)[-1]["queue_index"] == 2


def test_previous_while_a_start_is_under_way_steps_as_before(mgr, monkeypatch):
    """A start under way has no second of its own yet: the step moves it."""
    _at(mgr, "playing", 40.0)
    monkeypatch.setattr(mgr.b, "pressing", lambda: {"index": 3, "track_id": "t-3", "position": 0})
    mgr.b.seek = lambda s: pytest.fail("restarted a start under way")
    assert mgr.previous(mgr.b) is True
    assert _loads(mgr)[-1]["queue_index"] == 2


def test_previous_while_another_app_plays_steps_as_before(mgr):
    """What another controller plays is none of the queue's: its second is
    not the slot's."""
    _at(mgr, "playing", 40.0, source="external")
    mgr.b.seek = lambda s: pytest.fail("restarted another app's stream")
    mgr.previous(mgr.b)


def test_previous_on_a_spot_the_output_rests_on_moves_it_to_the_start(mgr):
    """Stopped on a held spot 40 s in (a switch's, a takeover's): the spot
    goes to 0:00, nothing sounds — the output's own Seek of a spot."""
    _at(mgr, "stopped", 0.0, resume={"index": 3, "position": 40})
    seeks = []
    mgr.b.seek = lambda s: seeks.append(s) or True
    assert mgr.previous(mgr.b) is True and seeks == [0]


def test_previous_stopped_with_no_spot_steps_as_before(mgr):
    _at(mgr, "stopped", 0.0)
    mgr.b.seek = lambda s: pytest.fail("restarted with nothing to restart")
    mgr.previous(mgr.b)
    assert _loads(mgr)[-1]["queue_index"] == 2


def test_the_previous_route_and_the_lock_screen_ask_the_one_rule(mgr, monkeypatch):
    """The Now Playing button, the lock screen's `previoustrack`
    (player.js: playerCmd('previous')) and the assistant's tool are one
    route: POST /previous."""
    import routers.player as player
    monkeypatch.setattr(player, "manager", mgr)
    _at(mgr, "playing", 30.0)
    seeks = []
    monkeypatch.setattr(mgr.b, "seek", lambda s: seeks.append(s) or True)
    assert player.previous_track() == {"ok": True}
    assert seeks == [0]
    js = (BACKEND / "static" / "player.js").read_text()
    assert "setActionHandler('previoustrack', () => playerCmd('previous'))" in js


# -- where a listen stopped (tracker.stop_in) --------------------------------------


def _heard(items, at, second, state="playing", length=None):
    from playback import tracker
    tracker.track_play_event(state, second, length if length is not None else 200.0, items[at])


def test_the_stop_is_the_last_slot_heard_and_its_second(mgr):
    from playback import tracker
    items = [_file(n) for n in (1, 2, 3)]
    _heard(items, 1, 75.0)
    assert tracker.stop_in(items) == (1, 75.0)
    # a Stop, a pause, the listen's end do not move it
    tracker.track_play_event("paused", 75.0, 200.0, items[1])
    tracker.track_play_event("stopped", 0.0, 0.0, None)
    assert tracker.stop_in(items) == (1, 75.0)


def test_a_stop_in_its_tracks_last_ten_seconds_is_the_next_slots_start(mgr):
    from playback import tracker
    items = [_file(n) for n in (1, 2, 3)]
    _heard(items, 1, 192.0)
    assert tracker.stop_in(items) == (2, 0.0)
    _heard(items, 2, 195.0)
    assert tracker.stop_in(items) is None              # heard to its end


def test_a_queue_nothing_of_which_sounded_has_no_stop(mgr):
    from playback import tracker
    heard = [_file(n) for n in (1, 2)]
    _heard(heard, 0, 30.0)
    assert tracker.stop_in([_file(1), _file(2)]) is None   # equal tracks, other slots


def test_an_excerpts_stop_is_its_tracks_start(mgr):
    from playback import tracker
    items = [_file(n) for n in (1, 2)]
    items[1].excerpt = True
    _heard(items, 1, 20.0, length=30.0)
    assert tracker.stop_in(items) == (1, 0.0)


def test_a_stopped_status_says_where_the_queues_listen_stopped(mgr):
    from playback.base import PlaybackStatus
    items = [_file(n) for n in (1, 2, 3)]
    mgr.replace_queue(items, play=True)
    mgr._on_backend_status(mgr.b, PlaybackStatus(state="playing", position=61.0, length=200.0,
                                                 queue_index=2, item=items[1]))
    assert "heard" not in mgr.latest_status
    mgr._on_backend_status(mgr.b, PlaybackStatus(state="stopped", position=0.0, length=0.0,
                                                 queue_index=2))
    assert mgr.latest_status["heard"] == {"index": 2, "track_id": "t-2", "position": 61.0}
