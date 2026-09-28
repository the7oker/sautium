"""The queue re-read for each output (playback.substitute, QueueItem.play):
the origin of a slot never changes, the way in does."""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

pytest.importorskip("sqlalchemy")
from playback.queue import CanonicalQueue, QueueItem   # noqa: E402


def _held(n, endpoint=7):
    return QueueItem(track_id=f"held-{n}", media_file_id=None,
                     source={"kind": "hqp", "path": f"/media/x/{n}.flac", "format": "FLAC",
                             "endpoint": endpoint},
                     title=f"Held {n}", artist="A")


def _file(n):
    return QueueItem(track_id=f"file-{n}", media_file_id=n,
                     source={"kind": "file", "path": f"E:/Music/{n}.flac", "format": "FLAC"},
                     title=f"File {n}", artist="A")


def test_reresolve_and_set_play_keep_the_origin_and_bump_the_version():
    q = CanonicalQueue()
    q.replace([_file(1), _held(2)])
    v = q.version
    pending = q.reresolve(lambda items: [None if it.source["kind"] == "file" else {"kind": "pending"}
                                         for it in items])
    assert (pending, q.version) == (1, v + 1)
    assert q.item_at(1).opener() is q.item_at(1).source
    assert q.item_at(2).opener() == {"kind": "pending"}
    assert q.payload()["tracks"][1]["play"] == "pending"

    assert q.set_play(q.item_at(2), {"kind": "proxy", "token": "t1"}) == 2
    assert q.item_at(2).source["kind"] == "hqp"          # the origin never changes
    row = q.payload()["tracks"][1]
    assert (row["play"], row["preview"], row["track_id"]) == ("proxy", True, "held-2")
    # the proxy's ready hook reaches a substituted slot as it reaches a queued stream
    assert q.refresh_proxy_items("t1", provider="youtube", excerpt=False, duration_seconds=200.0)
    assert (q.item_at(2).provider, q.item_at(2).duration_seconds) == ("youtube", 200.0)
    # an item that left the queue lands nothing
    assert q.set_play(_held(9), {"kind": "proxy", "token": "zz"}) is None


def test_substitutes_fetch_a_lead_window_and_land_on_their_slots(monkeypatch):
    import threading
    from playback.substitute import LEAD, Substitutes
    import routers.player as player
    from streaming import service as streaming_service

    class FakeEntry:
        def __init__(self, audio):
            self.audio = audio

    class FakeProxy:
        def __init__(self):
            self.added, self.bound = [], []

        def add_tracks(self, pairs, front=False):
            toks = [f"tok-{q.track_id}" for q, _ in pairs]
            self.added += toks
            return toks

        def bind(self, toks, gen):
            self.bound.append((list(toks), gen))

        def wait_ready(self, tok, timeout=None):
            return FakeEntry(audio=None if tok.endswith("held-3") else object())

    class Query:
        def __init__(self, tid):
            self.track_id = tid

    proxy = FakeProxy()
    monkeypatch.setattr(streaming_service, "is_enabled", lambda: True)
    monkeypatch.setattr(streaming_service, "providers_preferred", lambda: ["yt"])
    monkeypatch.setattr(streaming_service, "get_proxy", lambda: proxy)
    monkeypatch.setattr(player, "_phantom_track_query", lambda tid, album_id=None: Query(tid))
    monkeypatch.setattr(player, "_resolve_waterfall", lambda queries: [["yt"] for _ in queries])

    q = CanonicalQueue()
    q.replace([_held(n) for n in range(1, 7)])
    q.reresolve(lambda items: [{"kind": "pending"} for _ in items])
    ready, landed = [], threading.Event()

    def notify(index):
        ready.append(index)
        if len(ready) >= 3:
            landed.set()

    subs = Substitutes(q, notify)
    subs.maintain(1)                       # slots 1 .. 1 + LEAD
    assert landed.wait(5)
    assert sorted(ready) == [1, 2, 4]      # slot 3's fetch failed, the rest stream
    assert q.item_at(1).opener() == {"kind": "proxy", "token": "tok-held-1"}
    assert q.item_at(3).opener()["kind"] == "unplayable"
    assert q.item_at(5).opener() == {"kind": "pending"}
    assert proxy.bound == [(["tok-held-1", "tok-held-2", "tok-held-3", "tok-held-4"], q.generation)]
    assert LEAD == 3

    # the playhead moves: the window slides and the next slots are fetched once
    ready.clear()
    second = threading.Event()

    def notify2(index):
        ready.append(index)
        if len(ready) >= 2:
            second.set()

    subs._notify = notify2
    subs.maintain(4)
    subs.maintain(4)
    assert second.wait(5)
    assert sorted(ready) == [5, 6]
    assert len(proxy.added) == 6

    # an output switch since: an older fetch lands nothing
    subs.reset()
    subs._settle(q.item_at(6), {"kind": "proxy", "token": "late"}, epoch=0)
    assert q.item_at(6).opener() == {"kind": "proxy", "token": "tok-held-6"}
