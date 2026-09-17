"""
Play tracking (playback/tracker.py) — pure logic: the history INSERT and the
Last.fm calls are recorded. Run inside the backend container:

    python -m pytest test_play_tracker.py -q
"""

import pytest

from playback import tracker
from playback.queue import QueueItem


def _item(track_id, *, excerpt=False):
    return QueueItem(track_id=track_id, media_file_id=None,
                     source={"kind": "proxy", "token": "tok" + track_id},
                     title=track_id, artist="A", duration_seconds=200.0,
                     preview=True, provider="deezer_preview" if excerpt else "youtube",
                     excerpt=excerpt)


@pytest.fixture
def recorder(monkeypatch):
    writes, lastfm = [], []
    monkeypatch.setattr(tracker, "_db_execute", lambda sql, params=None: writes.append(sql))
    monkeypatch.setattr(tracker, "_scrobble_async", lambda method, **kw: lastfm.append(method))
    monkeypatch.setattr(tracker, "_scrobbling_enabled", lambda: True)
    monkeypatch.setattr(tracker, "_play_session", None)
    return writes, lastfm


def _history_rows(writes):
    return [w for w in writes if "INSERT INTO listening_history" in w]


def test_an_excerpt_is_not_a_listen_but_ends_the_one_before_it(recorder):
    writes, lastfm = recorder
    tracker.track_play_event("playing", 10.0, 200.0, _item("t1"))
    tracker.track_play_event("playing", 150.0, 200.0, _item("t1"))
    assert tracker._play_session is not None and lastfm == ["update_now_playing", "scrobble"]
    # The excerpt starts: the listen of t1 is written, and nothing opens.
    tracker.track_play_event("playing", 1.0, 30.0, _item("t2", excerpt=True))
    assert len(_history_rows(writes)) == 1 and tracker._play_session is None
    for pos in (10.0, 29.0):
        tracker.track_play_event("playing", pos, 30.0, _item("t2", excerpt=True))
    tracker.track_play_event("stopped", 0.0, 0.0, None)
    assert len(_history_rows(writes)) == 1                 # no row for the excerpt
    assert lastfm == ["update_now_playing", "scrobble"]    # no now-playing, no scrobble for it


def test_a_full_stream_after_an_excerpt_opens_its_own_listen(recorder):
    writes, lastfm = recorder
    tracker.track_play_event("playing", 20.0, 30.0, _item("t2", excerpt=True))
    tracker.track_play_event("playing", 2.0, 200.0, _item("t3"))
    assert tracker._play_session is not None and tracker._play_session.track_id == "t3"
    assert lastfm == ["update_now_playing"] and _history_rows(writes) == []
