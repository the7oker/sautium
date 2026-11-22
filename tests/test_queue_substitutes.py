"""The queue re-read for each output (playback.substitute, QueueItem.play):
the origin of a slot never changes, the way in does — and the fetch lane's
queue side: a streamed slot's stream resolved and fetched in the QUEUE's
order, a lead window from the playing slot, landed or failed on its slot,
cancelled when the slot leaves."""

import sys
import threading
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

pytest.importorskip("sqlalchemy")
from playback.queue import CanonicalQueue, QueueItem   # noqa: E402
from streaming.base import (FetchedAudio, ProviderManifest, Refused,  # noqa: E402
                            StreamProvider, TrackQuery)
from streaming.proxy import MediaProxy   # noqa: E402


def _held(n, endpoint=7):
    return QueueItem(track_id=f"held-{n}", media_file_id=None,
                     source={"kind": "hqp", "path": f"/media/x/{n}.flac", "format": "FLAC",
                             "endpoint": endpoint},
                     title=f"Held {n}", artist="A")


def _file(n):
    return QueueItem(track_id=f"file-{n}", media_file_id=n,
                     source={"kind": "file", "path": f"E:/Music/{n}.flac", "format": "FLAC"},
                     title=f"File {n}", artist="A")


def _track(n):
    return QueueItem(track_id=f"t-{n}", media_file_id=None, source={"kind": "track"},
                     title=f"Track {n}", artist="A", album="Alb", album_id="alb")


def test_reresolve_and_swap_play_keep_the_origin_and_bump_the_version():
    q = CanonicalQueue()
    q.replace([_file(1), _held(2)])
    v = q.version
    pending = q.reresolve(lambda items: [None if it.source["kind"] == "file" else {"kind": "pending"}
                                         for it in items])
    assert (pending, q.version) == (1, v + 1)
    assert q.item_at(1).opener() is q.item_at(1).source
    assert q.item_at(2).opener() == {"kind": "pending"}
    assert q.payload()["tracks"][1]["play"] == "pending"

    held = q.item_at(2)
    assert not q.swap_play(held, {"kind": "pending"}, {"kind": "proxy", "token": "t1"})  # not the same dict
    v = q.version
    assert q.swap_play(held, held.play, {"kind": "pending", "token": "t1"}, bump=False)
    assert q.version == v                                # the lane's own mark moves no version
    assert q.swap_play(held, held.play, {"kind": "proxy", "token": "t1"})
    assert q.version == v + 1 and held.source["kind"] == "hqp"   # the origin never changes
    row = q.payload()["tracks"][1]
    assert (row["play"], row["preview"], row["track_id"]) == ("proxy", True, "held-2")
    assert held.stream_token == "t1"
    # the lane's landing reaches a substituted slot as it reaches a queued stream
    assert q.refresh_proxy_items("t1", provider="youtube", excerpt=False, duration_seconds=200.0)
    assert (held.provider, held.duration_seconds) == ("youtube", 200.0)
    # an item that left the queue lands nothing
    assert not q.swap_play(_held(9), None, {"kind": "proxy", "token": "zz"})
    # a way in that is a stream no more: what the stream told of itself goes
    assert q.swap_play(held, held.play, {"kind": "unplayable", "reason": "x"})
    assert (held.provider, held.excerpt, held.stream_token) == (None, False, None)


def test_a_streamed_slot_is_a_preview_row_from_the_tap_with_its_state():
    q = CanonicalQueue()
    q.replace([_file(1), _track(2)])
    rows = q.payload(states=lambda: [{"index": 2, "track_id": "t-2", "state": "buffering",
                                      "progress": 0.4}])["tracks"]
    assert rows[1]["preview"] is True and rows[1]["stream"] == {"state": "buffering", "progress": 0.4}
    assert "stream" not in rows[0]


class _Gated(StreamProvider):
    """Every fetch waits for its title to be released; the order the lane
    asks in is recorded."""
    manifest = ProviderManifest(id="gated", name="Gated", kind="direct_url", lossless=False)

    def __init__(self):
        self.gates, self.asked = {}, []
        self.cv = threading.Condition()
        self.refuse: dict = {}

    def fetch(self, query):
        with self.cv:
            self.asked.append(query.track_id)
            self.cv.notify_all()
        self.gates.setdefault(query.track_id, threading.Event()).wait(10)
        if query.track_id in self.refuse:
            raise Refused(self.refuse[query.track_id], "refused")
        return FetchedAudio(data=b"fLaC" + b"\x00" * 64, mime="audio/flac")

    def release(self, *tids):
        for t in tids:
            self.gates.setdefault(t, threading.Event()).set()

    def wait_asked(self, n):
        with self.cv:
            assert self.cv.wait_for(lambda: len(self.asked) >= n, timeout=10), self.asked


@pytest.fixture
def lane(monkeypatch):
    """A Lane on a real MediaProxy (no port) and a gated provider; the
    resolve answers every track with that provider at once."""
    from playback.substitute import Lane
    import routers.player as player
    from streaming import service as streaming_service
    prov = _Gated()
    proxy = MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"k")
    monkeypatch.setattr(streaming_service, "is_enabled", lambda: True)
    monkeypatch.setattr(streaming_service, "providers_preferred", lambda: [prov])
    monkeypatch.setattr(streaming_service, "get_proxy", lambda: proxy)
    monkeypatch.setattr(player, "_phantom_track_query", lambda tid, album_id=None: TrackQuery(
        artist="A", title=tid, album="Alb", duration=100.0, track_id=tid, album_id=album_id))
    monkeypatch.setattr(player, "_resolve_waterfall",
                        lambda queries: [[] if q.track_id.endswith("nowhere") else [(prov, None)]
                                         for q in queries])
    monkeypatch.setattr(player, "_refused_code", lambda tid: None)
    q = CanonicalQueue()
    told = {"ready": [], "failed": [], "changed": 0}
    anchor = {"slot": 1}
    ln = Lane(q, told["ready"].append, lambda: anchor["slot"],
              lambda: told.__setitem__("changed", told["changed"] + 1),
              lambda item, index, code: told["failed"].append((index, code)))
    ln.attach(proxy)

    def settle():
        ln.maintain()
    return ln, q, prov, proxy, told, anchor, settle


def _wait(pred, timeout=10):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _native(q):
    from playback.substitute import native_plays
    q.reresolve(lambda items: native_plays(items, "browser", None))


def test_the_lane_fetches_the_window_in_the_queues_order_and_lands_on_the_slots(lane, monkeypatch):
    from playback import substitute
    monkeypatch.setattr(substitute, "db_query", lambda sql, params: [])
    ln, q, prov, proxy, told, anchor, settle = lane
    q.replace([_track(n) for n in range(1, 8)])
    _native(q)
    assert all(it.opener() == {"kind": "pending"} for it in q.snapshot())
    settle()
    prov.wait_asked(1)
    assert prov.asked == ["t-1"]
    # waiting is never buffering: the rows say which one runs
    assert _wait(lambda: [s["state"] for s in ln.slots()][:3] == ["buffering", "waiting", "waiting"])
    prov.release("t-1")
    assert _wait(lambda: 1 in told["ready"])
    assert q.item_at(1).opener()["kind"] == "proxy" and q.item_at(1).stream_token
    prov.wait_asked(2)
    for n in range(2, 6):
        prov.release(f"t-{n}")
    assert _wait(lambda: sorted(told["ready"]) == [1, 2, 3, 4, 5])
    # the window is the playing slot and LEAD after it: slot 6 waits
    assert prov.asked == ["t-1", "t-2", "t-3", "t-4", "t-5"]
    assert q.item_at(6).opener() == {"kind": "pending"}
    assert [s["state"] for s in ln.slots()] == ["ready"] * 5 + ["waiting"] * 2
    # the playhead moves: the window follows
    anchor["slot"] = 3
    settle()
    prov.wait_asked(6)
    prov.release("t-6", "t-7")
    assert _wait(lambda: sorted(told["ready"]) == [1, 2, 3, 4, 5, 6, 7])


def test_a_moved_slot_is_fetched_in_its_new_order(lane, monkeypatch):
    from playback import substitute
    monkeypatch.setattr(substitute, "db_query", lambda sql, params: [])
    ln, q, prov, proxy, told, anchor, settle = lane
    q.replace([_track(n) for n in range(1, 6)])
    _native(q)
    settle()
    prov.wait_asked(1)                    # slot 1 fetches; 2..5 resolved, waiting
    assert _wait(lambda: all((it.play or {}).get("token") for it in q.snapshot()))
    items = q.snapshot()
    q.reorder([items[0], items[4], items[1], items[2], items[3]])   # t-5 moved nearer
    settle()
    prov.release("t-1")
    prov.wait_asked(2)
    assert prov.asked[1] == "t-5"
    prov.release("t-2", "t-3", "t-4", "t-5")
    assert _wait(lambda: len(told["ready"]) == 5)
    assert prov.asked == ["t-1", "t-5", "t-2", "t-3", "t-4"]


def test_a_removed_slot_cancels_its_own_fetch_and_one_never_begun_is_never_fetched(lane, monkeypatch):
    from playback import substitute
    monkeypatch.setattr(substitute, "db_query", lambda sql, params: [])
    ln, q, prov, proxy, told, anchor, settle = lane
    q.replace([_track(n) for n in range(1, 4)])
    _native(q)
    settle()
    prov.wait_asked(1)
    assert _wait(lambda: all((it.play or {}).get("token") for it in q.snapshot()))
    buffering, waiting = q.item_at(1), q.item_at(2)
    tok1, tok2 = buffering.play["token"], waiting.play["token"]
    q.remove(2)                           # the waiting one
    q.remove(1)                           # the buffering one
    ln.prune()
    assert proxy._peek(tok1) is None and proxy._peek(tok2) is None
    prov.release("t-1")                   # its download returns: discarded
    prov.wait_asked(2)
    assert prov.asked == ["t-1", "t-3"]   # t-2 never fetched; the lane went on to t-3
    prov.release("t-3")
    assert _wait(lambda: q.item_at(1).opener()["kind"] == "proxy")
    assert told["ready"] == [1]


def test_a_failed_slot_says_why_and_a_track_queued_twice_waits_for_one_fetch(lane, monkeypatch):
    from playback import substitute
    monkeypatch.setattr(substitute, "db_query", lambda sql, params: [])
    ln, q, prov, proxy, told, anchor, settle = lane
    twice = _track(1)
    q.replace([_track(1), QueueItem(track_id="t-nowhere", media_file_id=None,
                                    source={"kind": "track"}, title="x"), twice, _track(4)])
    _native(q)
    prov.refuse["t-4"] = "region"
    settle()
    assert _wait(lambda: (2, "nowhere") in told["failed"])
    assert q.item_at(2).opener()["kind"] == "unplayable"
    prov.release("t-1", "t-4")
    assert _wait(lambda: (4, "region") in told["failed"])
    assert _wait(lambda: q.item_at(1).opener()["kind"] == "proxy"
                 and q.item_at(3).opener()["kind"] == "proxy")
    assert q.item_at(1).play["token"] == q.item_at(3).play["token"]
    assert prov.asked.count("t-1") == 1
    states = {s["index"]: s for s in ln.slots()}
    assert (states[2]["state"], states[2]["reason"]) == ("failed", "nowhere")
    assert (states[4]["state"], states[4]["reason"]) == ("failed", "region")


def test_a_streams_way_in_is_kept_across_every_reread(lane, monkeypatch):
    """A mirroring, an output switch: the stream the lane brought a slot is
    every output's, never asked for again."""
    from playback import substitute
    monkeypatch.setattr(substitute, "db_query", lambda sql, params: [])
    ln, q, prov, proxy, told, anchor, settle = lane
    q.replace([_track(1)])
    _native(q)
    settle()
    prov.release("t-1")
    assert _wait(lambda: q.item_at(1).opener()["kind"] == "proxy")
    landed = q.item_at(1).play
    _native(q)
    from playback.substitute import native_plays
    q.reresolve(lambda items: native_plays(items, "hqplayer", 3))
    assert q.item_at(1).play is landed


def test_a_streamed_slot_survives_a_restart_as_its_identity(monkeypatch):
    """F9: a slot that is the track itself is persisted as that — its way in
    (a token of this process) left behind — and restored as a stream to come
    again; a queued stream of the old kind is not kept."""
    from playback.manager import PlaybackManager
    from routers import settings as settings_router
    stored = {}
    monkeypatch.setattr(settings_router, "_write", lambda key, value: stored.__setitem__(key, value))
    monkeypatch.setattr(settings_router, "_read", lambda key: stored.get(key))
    mgr = PlaybackManager()
    landed = _track(1)
    landed.play, landed.provider = {"kind": "proxy", "token": "dead", "lane": True}, "youtube"
    old = QueueItem(track_id="p", media_file_id=None, source={"kind": "proxy", "token": "t"},
                    title="old", preview=True)
    mgr.queue.replace([landed, _track(2), old])
    mgr._persist_queue()
    items = stored["player.queue"]["items"]
    assert [d["track_id"] for d in items] == ["t-1", "t-2"]
    assert all(d["play"] is None and d["provider"] is None for d in items)
    after = PlaybackManager()
    after._restore_persisted_queue()
    assert [it.source for it in after.queue.snapshot()] == [{"kind": "track"}] * 2
    from playback.substitute import native_plays
    from playback import substitute
    monkeypatch.setattr(substitute, "db_query", lambda sql, params: [])
    after.queue.reresolve(lambda its: native_plays(its, "browser", None))
    assert [it.opener() for it in after.queue.snapshot()] == [{"kind": "pending"}] * 2
