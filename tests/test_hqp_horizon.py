"""HQPlayer holds the queue up to its first slot still on its way — its
horizon (playback.hqp_backend, QUEUE-STREAM-DESIGN §3): a streamed slot is
in the queue from the tap, HQPlayer's playlist ends before it until its
stream lands, a landing there is an append at the playlist's end, and an
HQPlayer that plays out the last slot it holds waits on the horizon in the
node. On the fake HQPlayer of test_hqp_backend, the media proxy's lane and a
provider whose every fetch is held until the test lets it go."""

import sys
import threading
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

pytest.importorskip("sqlalchemy")

from test_hqp_backend import (FakeHqp, _attach, _item, _queue_stays_in_memory,  # noqa: F401,E402
                              _wait, fake, listens)
from hqplayer_client import PlaybackState  # noqa: E402
from playback import hqp_link as hl  # noqa: E402
from playback.manager import PlaybackManager  # noqa: E402
from playback.queue import QueueItem  # noqa: E402
from streaming import service as streaming_service  # noqa: E402
from streaming.base import (FetchedAudio, ProviderManifest, StreamProvider,  # noqa: E402
                            TrackQuery)


class _Held(StreamProvider):
    """Each track's fetch waits until the test lets it go."""
    manifest = ProviderManifest(id="held", name="Held", kind="direct_url", lossless=True)

    def __init__(self):
        self.gates: dict = {}
        self.fails: set = set()

    def gate(self, tid):
        return self.gates.setdefault(tid, threading.Event())

    def fetch(self, query):
        from streaming.base import ProviderError
        self.gate(query.track_id).wait(20)
        if query.track_id in self.fails:
            raise ProviderError("the provider has it no more")
        return FetchedAudio(data=b"fLaC" + b"\x00" * 4092, mime="audio/flac", lossless=True)

    def land(self, *tids):
        for t in tids:
            self.gate(t).set()


def _streamed(n):
    return QueueItem(track_id=f"stream-{n}", media_file_id=None, source={"kind": "track"},
                     title=f"Stream {n}", artist="Artist", album="Album", album_id="alb",
                     duration_seconds=300.0)


@pytest.fixture
def lane(fake, monkeypatch):
    """The lane fed by the fake's media proxy: every streamed slot resolves at
    once to the held provider; every other slot opens as queued."""
    import routers.player as player
    from playback import substitute
    prov = _Held()
    proxy = streaming_service._proxy
    monkeypatch.setattr(streaming_service, "is_enabled", lambda: True)
    monkeypatch.setattr(streaming_service, "providers_preferred", lambda: [prov])
    monkeypatch.setattr(streaming_service, "providers_registered", lambda: True)
    monkeypatch.setattr(substitute, "native_plays", lambda items, output_id, endpoint_id: [
        substitute._stream_play(it) if it.source.get("kind") == "track" else None for it in items])
    monkeypatch.setattr(player, "_phantom_track_query", lambda tid, album_id=None: TrackQuery(
        artist="Artist", title=tid, album="Album", duration=300.0, track_id=tid, album_id=album_id))
    monkeypatch.setattr(player, "_resolve_waterfall", lambda queries: [[(prov, None)] for _ in queries])
    monkeypatch.setattr(player, "_refused_code", lambda tid: None)
    monkeypatch.setattr(hl.BuildWatch, "after", 0.3)
    return prov, proxy


def _manager(lane, monkeypatch, items):
    from playback import manager as manager_mod
    _prov, proxy = lane
    mgr = PlaybackManager()
    monkeypatch.setattr(manager_mod, "manager", mgr)
    monkeypatch.setattr(proxy, "track_ready_hooks", [lambda e: mgr.stream_landed(e.token, e.reshaped)])
    monkeypatch.setattr(proxy, "track_failed_hooks", [lambda e: mgr.stream_failed(e.token)])
    monkeypatch.setattr(proxy, "state_hooks", [])
    mgr.lane.attach(proxy)
    mgr.queue.replace(items)
    return mgr


def _playing(mgr, fake, monkeypatch, items):
    """HQPlayer attached, playing slot 1 of `items` at second 41."""
    m = _manager(*mgr, items) if isinstance(mgr, tuple) else mgr
    b = _attach(m)
    with fake._lock:
        fake.state, fake.track, fake.position = int(PlaybackState.PLAYING), 1, 41.0
    assert _wait(lambda: m._latest_status.get("state") == "playing")
    return m, b


def _previews(fake):
    return [u for u in fake.playlist if "/preview/" in u]


def test_streamed_slots_wait_behind_the_horizon_and_each_landing_is_an_append(fake, lane,
                                                                            monkeypatch):
    """B-124 on HQPlayer: an album streamed, then an owned one, both added to
    the end — the queue holds both in that order at once; HQPlayer's playlist
    ends before the first streamed slot until it lands, and each landing is a
    PlaylistAdd at its end, never a remove: the owned album goes in after the
    streamed one, never inside it."""
    prov, _proxy = lane
    mgr, b = _playing((lane, monkeypatch), fake, monkeypatch,
                      [_item(f"E:/Music/A/0{i}.flac", i) for i in (1, 2)])
    try:
        n = len(fake.commands)
        assert mgr.append([_streamed(3), _streamed(4)]) == 2
        assert mgr.append([_item("E:/Music/B/05.flac", 5), _item("E:/Music/B/06.flac", 6)]) == 2
        assert [it.track_id for it in mgr.queue.snapshot()] == [
            "track-1", "track-2", "stream-3", "stream-4", "track-5", "track-6"]
        assert fake.playlist == ["file:///E:/Music/A/01.flac", "file:///E:/Music/A/02.flac"]
        prov.land("stream-3")
        assert _wait(lambda: len(fake.playlist) == 3)
        assert len(_previews(fake)) == 1 and fake.playlist[:2] == [
            "file:///E:/Music/A/01.flac", "file:///E:/Music/A/02.flac"]
        prov.land("stream-4")
        assert _wait(lambda: len(fake.playlist) == 6)
        assert fake.playlist[4:] == ["file:///E:/Music/B/05.flac", "file:///E:/Music/B/06.flac"]
        assert not [c for c, _ in fake.commands[n:] if c in ("PlaylistRemove", "PlaylistClear", "Stop")]
        assert fake.state == int(PlaybackState.PLAYING) and fake.track == 1
        assert b._horizon is None and b.healthy()
    finally:
        prov.land("stream-3", "stream-4")
        b.shutdown()


def test_hqplayer_that_plays_out_the_last_slot_it_holds_waits_on_the_horizon(fake, lane,
                                                                           monkeypatch):
    """HQPlayer reaches its playlist's end before the horizon's stream lands:
    it stops; the start waits in the node on that slot — Now Playing
    "loading" it — and goes once the stream lands."""
    prov, _proxy = lane
    mgr, b = _playing((lane, monkeypatch), fake, monkeypatch,
                      [_item("E:/Music/A/01.flac", 1), _streamed(2)])
    try:
        with fake._lock:
            fake.position = fake.length - 3     # each Status read plays a second on
        assert _wait(lambda: mgr._latest_status.get("state") == "loading"
                     and mgr._latest_status.get("track_index") == 2), mgr._latest_status
        assert len(fake.playlist) == 1
        prov.land("stream-2")
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 2), \
            (fake.state, fake.track, fake.playlist)
        assert len(_previews(fake)) == 1
    finally:
        prov.land("stream-2")
        b.shutdown()


def test_a_streamed_album_played_waits_on_slot_one_and_hqplayer_takes_it_as_it_lands(fake, lane,
                                                                                  monkeypatch):
    """Play on a streamed album: every slot in the queue at once, HQPlayer's
    playlist emptied, the play waiting on slot 1; its landing is handed and
    started, the next landing appended behind it."""
    prov, _proxy = lane
    mgr = _manager(lane, monkeypatch, [_item("E:/Music/A/01.flac", 1)])
    b = _attach(mgr)
    try:
        assert _wait(lambda: fake.playlist == ["file:///E:/Music/A/01.flac"])
        added, _gen = mgr.replace_queue([_streamed(1), _streamed(2)], play=True)
        assert added == 2 and fake.playlist == []
        assert _wait(lambda: mgr._latest_status.get("state") == "loading"
                     and mgr._latest_status.get("track_index") == 1), mgr._latest_status
        prov.land("stream-1")
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 1), \
            (fake.state, fake.playlist)
        assert len(fake.playlist) == 1
        prov.land("stream-2")
        assert _wait(lambda: len(_previews(fake)) == 2)
        assert fake.track == 1
    finally:
        prov.land("stream-1", "stream-2")
        b.shutdown()


def test_a_slot_whose_stream_fails_is_walked_past_and_hqplayer_takes_what_follows(fake, lane,
                                                                               monkeypatch):
    prov, _proxy = lane
    mgr, b = _playing((lane, monkeypatch), fake, monkeypatch,
                      [_item("E:/Music/A/01.flac", 1), _streamed(2), _item("E:/Music/A/03.flac", 3)])
    try:
        assert fake.playlist == ["file:///E:/Music/A/01.flac"]
        prov.fails.add("stream-2")
        prov.land("stream-2")
        assert _wait(lambda: fake.playlist == ["file:///E:/Music/A/01.flac",
                                               "file:///E:/Music/A/03.flac"]), fake.playlist
        assert b.gap(mgr.queue.item_at(2)) is not None
        assert mgr.playlist()["tracks"][1]["stream"] == {"state": "failed", "reason": "unknown"}
    finally:
        b.shutdown()


def test_a_play_next_of_a_streamed_track_takes_the_held_part_after_the_playing_one_away(
        fake, lane, monkeypatch):
    """Play next of a streamed track before the horizon: it is the horizon
    now — what HQPlayer held after the playing slot leaves its playlist, and
    comes back behind the track once it lands."""
    prov, _proxy = lane
    mgr, b = _playing((lane, monkeypatch), fake, monkeypatch,
                      [_item(f"E:/Music/A/0{i}.flac", i) for i in (1, 2, 3)])
    try:
        mgr._latest_status = {**mgr._latest_status, "track_index": 1}
        assert mgr.append([_streamed(9)], "next") == 1
        assert [it.track_id for it in mgr.queue.snapshot()] == [
            "track-1", "stream-9", "track-2", "track-3"]
        assert fake.playlist == ["file:///E:/Music/A/01.flac"]
        assert fake.state == int(PlaybackState.PLAYING) and fake.track == 1
        prov.land("stream-9")
        assert _wait(lambda: len(fake.playlist) == 4)
        assert fake.playlist[2:] == ["file:///E:/Music/A/02.flac", "file:///E:/Music/A/03.flac"]
        assert "/preview/" in fake.playlist[1]
    finally:
        prov.land("stream-9")
        b.shutdown()


def test_removing_the_horizons_slot_hands_hqplayer_what_lay_behind_it(fake, lane, monkeypatch):
    prov, proxy = lane
    mgr, b = _playing((lane, monkeypatch), fake, monkeypatch,
                      [_item("E:/Music/A/01.flac", 1), _streamed(2), _item("E:/Music/A/03.flac", 3)])
    try:
        assert _wait(lambda: (mgr.queue.item_at(2).play or {}).get("token"))
        tok = mgr.queue.item_at(2).play["token"]
        assert mgr.remove(2, "stream-2")
        assert _wait(lambda: fake.playlist == ["file:///E:/Music/A/01.flac",
                                               "file:///E:/Music/A/03.flac"]), fake.playlist
        assert proxy._peek(tok) is None                 # its fetch cancelled at the source
        assert b._horizon is None
    finally:
        prov.land("stream-2")
        b.shutdown()


def test_a_tap_beyond_the_horizon_hands_hqplayer_the_queue_from_that_slot(fake, lane, monkeypatch):
    """A tap on an owned slot past a streamed one still on its way: HQPlayer
    is handed the queue afresh from the tapped slot, the stream left out
    behind the playhead, and plays it."""
    prov, _proxy = lane
    mgr, b = _playing((lane, monkeypatch), fake, monkeypatch,
                      [_item("E:/Music/A/01.flac", 1), _streamed(2), _item("E:/Music/A/03.flac", 3)])
    try:
        assert fake.playlist == ["file:///E:/Music/A/01.flac"]
        assert b.select(3, "track-3")
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 2
                     and fake.playlist == ["file:///E:/Music/A/01.flac", "file:///E:/Music/A/03.flac"]), \
            (fake.state, fake.track, fake.playlist)
        assert _wait(lambda: mgr._latest_status.get("track_index") == 3)
    finally:
        prov.land("stream-2")
        b.shutdown()


def test_an_owned_m4a_and_a_cue_slice_are_prepared_by_the_lane_in_the_queues_order(fake, lane,
                                                                                  monkeypatch):
    """An owned album HQPlayer cannot open as it is — an m4a, a CUE slice
    whose cut is not on disk — is committed whole at once, and each such slot
    waits behind the horizon until the lane has made its transcode or its
    cut, in the queue's order: never one slow transcode inside a mirror."""
    from playback import substitute
    from streaming import transcode
    prov, proxy = lane
    made = {"cut": threading.Event()}

    class _Transcoder(_Held):
        manifest = ProviderManifest(id="local", name="Local", kind="transcode", lossless=True)

        def download(self, path):
            self.gate(path).wait(20)
            return FetchedAudio(data=b"fLaC" + b"\x01" * 2048, mime="audio/flac", lossless=True)
    xcode = _Transcoder()
    monkeypatch.setattr(substitute, "_local_transcoder", lambda: xcode)
    monkeypatch.setattr(substitute, "native_plays", lambda items, output_id, endpoint_id: [
        substitute._prepared(it, None) for it in items])
    monkeypatch.setattr(transcode, "flac_slice_cached", lambda path, start, end: made["cut"].is_set())

    def cut(path, start, end, tags):
        made["cut"].wait(20)
        return "/tmp/cut.flac"
    monkeypatch.setattr(transcode, "flac_slice_path_for_file", cut)
    album = [_item("E:/Music/A/01.flac", 1), _item("E:/Music/A/02.m4a", 2, fmt="M4A"),
             _item("E:/Music/A/disc.flac", 3, cue_start=0.0, cue_end=200.0)]
    mgr, b = _playing((lane, monkeypatch), fake, monkeypatch, album)
    try:
        assert fake.playlist == ["file:///E:/Music/A/01.flac"]
        assert mgr.queue.item_at(2).opener()["kind"] == "pending"
        xcode.land("/music/A/02.m4a")
        assert _wait(lambda: len(fake.playlist) == 2), fake.playlist
        assert "/preview/" in fake.playlist[1]
        made["cut"].set()
        assert _wait(lambda: len(fake.playlist) == 3), fake.playlist
        assert "/file/" in fake.playlist[2]
        # the transcode is an owned slot's way in, never a stream of a provider's
        assert mgr.queue.item_at(2).stream_token is None
        assert mgr.playlist()["tracks"][1].get("stream") is None
    finally:
        xcode.land("/music/A/02.m4a")
        made["cut"].set()
        b.shutdown()


def _held_uris(b):
    return len(b._layout().handed)


def test_a_stream_landing_behind_the_playhead_is_no_slot_hqplayer_holds(fake, lane, monkeypatch):
    """H2: a tap past a slot still on its way hands HQPlayer the queue from
    the tapped slot; that slot's stream landing afterwards is never counted
    as one HQPlayer holds — the layout stays its playlist — and a tap on it
    hands HQPlayer the queue from it, which plays it."""
    prov, _proxy = lane
    mgr, b = _playing((lane, monkeypatch), fake, monkeypatch,
                      [_item("E:/Music/A/01.flac", 1), _streamed(2), _item("E:/Music/A/03.flac", 3)])
    try:
        assert b.select(3, "track-3")
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 2)
        prov.land("stream-2")
        assert _wait(lambda: mgr.queue.item_at(2).opener()["kind"] == "proxy")
        assert _wait(lambda: mgr._latest_status.get("track_index") == 3)
        assert _held_uris(b) == len(fake.playlist) == 2
        assert b.gap(mgr.queue.item_at(2)) is None              # nothing wrong with it
        assert b.select(2, "stream-2")
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING)
                     and "/preview/" in (fake.playlist[fake.track - 1] if fake.track else "")), \
            (fake.state, fake.track, fake.playlist)
        assert _held_uris(b) == len(fake.playlist) == 3
    finally:
        prov.land("stream-2")
        b.shutdown()


def test_a_tap_on_a_slot_still_on_its_way_behind_the_playhead_waits_and_plays_it(fake, lane,
                                                                               monkeypatch):
    prov, _proxy = lane
    mgr, b = _playing((lane, monkeypatch), fake, monkeypatch,
                      [_item("E:/Music/A/01.flac", 1), _streamed(2), _item("E:/Music/A/03.flac", 3)])
    try:
        assert b.select(3, "track-3")
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 2)
        assert _wait(lambda: mgr._latest_status.get("track_index") == 3)
        assert b.select(2, "stream-2")
        assert _wait(lambda: mgr._latest_status.get("state") == "loading"
                     and mgr._latest_status.get("track_index") == 2), mgr._latest_status
        prov.land("stream-2")
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING)
                     and "/preview/" in (fake.playlist[fake.track - 1] if fake.track else "")), \
            (fake.state, fake.track, fake.playlist)
    finally:
        prov.land("stream-2")
        b.shutdown()


def test_a_new_order_behind_a_late_slot_removes_none_of_what_hqplayer_holds(fake, lane, monkeypatch):
    """A slot left behind the playhead whose stream came since (late) is no
    entry HQPlayer holds: a new order of what follows the playing slot keeps
    every entry before it, and the layout stays HQPlayer's playlist."""
    import routers.player as player
    prov, _proxy = lane
    mgr, b = _playing((lane, monkeypatch), fake, monkeypatch,
                      [_streamed(1)] + [_item(f"E:/Music/A/0{i}.flac", i) for i in (2, 3, 4, 5)])
    monkeypatch.setattr(player, "manager", mgr)
    try:
        assert b.select(3, "track-3")
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and len(fake.playlist) == 4
                     and fake.track == 2), (fake.state, fake.track, fake.playlist)
        assert _wait(lambda: mgr._latest_status.get("track_index") == 3)
        prov.land("stream-1")
        assert _wait(lambda: mgr.queue.item_at(1).opener()["kind"] == "proxy")
        player.reorder(player.ReorderRequest(order=["stream-1", "track-2", "track-3", "track-5",
                                                    "track-4"]))
        assert fake.playlist == ["file:///E:/Music/A/02.flac", "file:///E:/Music/A/03.flac",
                                 "file:///E:/Music/A/05.flac", "file:///E:/Music/A/04.flac"]
        assert len(b._layout().handed) == len(fake.playlist)
        assert fake.track == 2 and fake.state == int(PlaybackState.PLAYING)
    finally:
        prov.land("stream-1")
        b.shutdown()


def test_a_new_queue_hqplayer_did_not_take_leaves_the_horizon_and_its_landing_to_the_old(
        fake, lane, monkeypatch):
    """A tap on a new queue that does not commit (HQPlayer busy) while the old
    queue's horizon lands: the old queue keeps its horizon, the landing is no
    late slot, and HQPlayer is handed it at its playlist's end."""
    prov, _proxy = lane
    mgr, b = _playing((lane, monkeypatch), fake, monkeypatch,
                      [_item("E:/Music/A/01.flac", 1), _streamed(2), _item("E:/Music/A/03.flac", 3)])
    try:
        real = b._afresh
        old = mgr.queue.item_at(2)

        def busy(*a, **k):
            prov.land("stream-2")
            assert _wait(lambda: old.opener()["kind"] == "proxy")
            raise hl.Dropped("a newer intent came first")
        monkeypatch.setattr(b, "_afresh", busy)
        with pytest.raises(hl.Dropped):
            mgr.replace_queue([_item("E:/Music/C/01.flac", 31)], play=True)
        monkeypatch.setattr(b, "_afresh", real)
        assert b._horizon is old and not b._late
        assert _wait(lambda: len(fake.playlist) == 3), fake.playlist
        assert "/preview/" in fake.playlist[1]
    finally:
        prov.land("stream-2")
        b.shutdown()
