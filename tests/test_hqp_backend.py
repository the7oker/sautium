"""The HQPlayer output against a fake control port: the queue mirrored in the
URI form each file-access mode dictates, a running playlist adopted back into
queue items, drift read through HQPlayer's escapes, and the re-mirror after
HQPlayer restarts (a trial-mode Embedded stops every 30 minutes)."""

import re
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path
from xml.sax.saxutils import escape

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from config import settings  # noqa: E402
from hqplayer_client import (VOLUME_FIXED, HQPlayerClient, PlaybackState,  # noqa: E402
                             RepeatMode, TrackStatus)
from playback import hqp_backend as hb  # noqa: E402
from playback import hqp_diagnostics as diag  # noqa: E402
from playback.base import PlaybackStatus  # noqa: E402
from playback import queue as queue_mod  # noqa: E402
from playback import tracker  # noqa: E402
from playback.hqp_backend import HqpBackend  # noqa: E402
from playback.manager import PlaybackManager  # noqa: E402
from playback.queue import CanonicalQueue, QueueItem  # noqa: E402
from streaming import service as streaming_service  # noqa: E402
from streaming.proxy import MediaProxy  # noqa: E402

_CMD_RE = re.compile(r'<(\w+)((?:\s+[\w:]+="[^"]*")*)\s*/>')
_ATTR_RE = re.compile(r'([\w:]+)="([^"]*)"')


class _ReusableServer(socketserver.ThreadingTCPServer):
    # come_back() rebinds the port the box listened on before it went down.
    allow_reuse_address = True
    daemon_threads = True


class FakeHqp:
    """HQPlayer's control port as hqplayer_client speaks it: one XML element
    per command with NO newline after it, one line back per command."""

    def __init__(self, *, escape_brackets=True):
        self.playlist: list[str] = []
        self.state = int(PlaybackState.STOPPED)
        self.track = 0
        self.position = 0.0
        # Answers that replace the normal one, by command — what a refusing
        # HQPlayer says (`<Play result="Error">Empty transport</Play>`).
        self.replies: dict[str, str] = {}
        self.escape_brackets = escape_brackets
        self.commands: list[tuple] = []
        # Status polls after a Play during which the position stays at 0 — a
        # long filter initialising before any audio leaves.
        self.start_polls = 0
        self._starting = 0
        # PLAYING polls that still say track 0, length 0 while the metadata
        # names the entry being opened — Desktop 6.2.3's first tick after a
        # Play from stopped.
        self.opening = 0
        # HQPlayer's limiter count (<Status clips>) and the adaptive gain it
        # applies to the track (<metadata gain>); None: not reported.
        self.clips = None
        self.track_gain = None
        self.mute = False
        self._down = False
        # Until when it answers nothing, on any connection — busy building a
        # setting, or its CPU taken by one it cannot keep up with — and then
        # carries out what it was sent meanwhile (silence()).
        self._silent_until = 0.0
        self._lock = threading.Lock()
        self._conns: list[socket.socket] = []
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                if fake.mute or fake._down:
                    return              # a trial-stopped Embedded, or a box going down: dropped at once
                with fake._lock:
                    fake._conns.append(self.request)
                buf = ""
                while True:
                    try:
                        chunk = self.request.recv(65536)
                    except OSError:
                        return
                    if not chunk:
                        return
                    buf += chunk.decode("utf-8", "replace")
                    last = 0
                    for m in _CMD_RE.finditer(buf):
                        attrs = dict(_ATTR_RE.findall(m.group(2)))
                        wait = fake._silent_until - time.monotonic()
                        if wait > 0:
                            time.sleep(wait)
                        reply = fake._dispatch(m.group(1), attrs)
                        if reply is None:
                            return      # gone mid-conversation: the connection drops unanswered
                        try:
                            self.request.sendall(reply.encode() + b"\n")
                        except OSError:
                            return
                        last = m.end()
                    buf = buf[last:]

        self._handler = Handler
        self.port = 0
        self._serve()

    def silence(self, seconds: float) -> None:
        self._silent_until = time.monotonic() + seconds

    def _serve(self) -> None:
        self._srv = _ReusableServer(("127.0.0.1", self.port), self._handler)
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def _uri_out(self, uri: str) -> str:
        if self.escape_brackets:
            uri = uri.replace("[", "%5B").replace("]", "%5D")
        return escape(uri, {'"': "&quot;"})

    def _dispatch(self, cmd: str, attrs: dict) -> str:
        with self._lock:
            self.commands.append((cmd, attrs))
            if cmd in self.replies:
                return self.replies[cmd]
            if cmd == "Status":
                opening = self.state == int(PlaybackState.PLAYING) and self.opening > 0
                if opening:
                    self.opening -= 1
                elif self.state == int(PlaybackState.PLAYING):
                    if self._starting:
                        self._starting -= 1
                    else:
                        self.position += 1.0
                # The entry it reads, named as its playlist names it (Desktop
                # 6.2.3 sends it to an unauthenticated client).
                uri = (f' uri="{self._uri_out(self.playlist[self.track - 1])}"'
                       if 1 <= self.track <= len(self.playlist) else "")
                track, length = (0, 0) if opening else (self.track, 300)
                clips = f' clips="{self.clips}"' if self.clips is not None else ""
                return (f'<Status state="{self.state}" track="{track}" '
                        f'position="{self.position}" length="{length}" volume="-3"{clips} '
                        f'tracks_total="{len(self.playlist)}" process_speed="1.6" '
                        f'input_fill="0.9" output_fill="0.9" active_mode="PCM" '
                        f'active_filter="poly-sinc-gauss-long" active_shaper="none" '
                        f'active_rate="705600">'
                        f'<metadata artist="Fake artist" album="Fake album" '
                        f'song="Fake song" genre=""{uri}'
                        + (f' gain="{self.track_gain}"' if self.track_gain is not None else '')
                        + '/></Status>')
            if cmd == "GetInfo":
                return ('<GetInfo name="fake" product="Signalyst HQPlayer Fake" '
                        'version="6" platform="Linux" engine="6.2.3"/>')
            if cmd == "PlaylistAdd":
                if attrs.get("clear") == "1":
                    self.playlist = []
                    self.track = 0
                self.playlist.append(attrs["uri"])
                return '<PlaylistAdd result="OK"/>'
            if cmd == "PlaylistGet":
                items = "".join(f'<PlaylistItem uri="{self._uri_out(u)}">'
                                f'<metadata artist="" album="" song="" genre=""/>'
                                f'</PlaylistItem>' for u in self.playlist)
                return f"<PlaylistGet>{items}</PlaylistGet>"
            if cmd == "PlaylistClear":
                self.playlist = []
                return '<PlaylistClear result="OK"/>'
            if cmd == "PlaylistRemove":
                idx = int(attrs["index"])
                if 1 <= idx <= len(self.playlist):
                    del self.playlist[idx - 1]
                return '<PlaylistRemove result="OK"/>'
            if cmd == "Play":
                if self.playlist:
                    if self.state != int(PlaybackState.PLAYING):
                        self.position = 0.0
                        self._starting = self.start_polls
                    self.state = int(PlaybackState.PLAYING)
                    self.track = self.track or 1
                return '<Play result="OK"/>'
            if cmd == "Stop":
                self.state = int(PlaybackState.STOPPED)
                return '<Stop result="OK"/>'
            if cmd == "Pause":
                self.state = int(PlaybackState.PAUSED)
                return '<Pause result="OK"/>'
            if cmd == "SelectTrack":
                self.track = int(attrs["index"])
                self.position = 0.0
                return '<SelectTrack result="OK"/>'
            return f'<{cmd} result="OK"/>'

    def restart(self) -> None:
        """What a trial-mode Embedded does every 30 min: every connection is
        dropped, the playlist and the transport are gone, the port is back."""
        with self._lock:
            conns, self._conns = self._conns, []
            self.playlist = []
            self.state = int(PlaybackState.STOPPED)
            self.track = 0
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()

    def close(self) -> None:
        """Powered off: every connection dropped, the port refuses. The port
        stops first: a client reconnecting while the server winds down got
        an answering connection that outlived the "power-off" (the long
        outage test failed about one run in eight on it)."""
        self._down = True
        self._srv.shutdown()
        self._srv.server_close()
        self.restart()

    def come_back(self) -> None:
        """Booted again on the same port — playlist and transport empty."""
        self._down = False
        self._serve()


@pytest.fixture(autouse=True)
def _queue_stays_in_memory(monkeypatch):
    """A manager here never persists its queue. The write is a timer that
    outlives the test, and db_pool opens the configured database for it: run
    in the backend container, the suite overwrote the node's player.queue and
    the next start crash-looped on "track-1" (2026-10-09)."""
    monkeypatch.setattr(PlaybackManager, "_schedule_persist", lambda self: None)


@pytest.fixture
def fake(monkeypatch):
    f = FakeHqp()
    monkeypatch.setattr(settings, "hqplayer_host", "127.0.0.1")
    monkeypatch.setattr(settings, "hqplayer_port", f.port)
    monkeypatch.setattr(settings, "music_library_path", "/music")
    monkeypatch.setattr(settings, "music_host_path", "E:/Music")
    monkeypatch.setattr(settings, "media_proxy_advertised_host", "127.0.0.1")
    monkeypatch.setattr(streaming_service, "_proxy",
                        MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"k"))
    hb.reset_all_clients()
    hb._hqp_unreachable_until = 0.0
    # A failing run wakes the notices channel through the database.
    monkeypatch.setattr(hb, "_notify_notices", lambda: None)
    # The attach re-checks the HQPlayer library hash through the database;
    # these tests have none.
    import hqp_library
    monkeypatch.setattr(hqp_library, "request_sync", lambda host, port: None)
    # ...and what GetInfo said goes on the endpoint row, and a benchmark the
    # process died in is put back — both in the database.
    from playback import hqp_benchmark
    monkeypatch.setattr(hqp_library, "note_info", lambda *a, **k: None)
    monkeypatch.setattr(hqp_benchmark, "recover", lambda endpoint_id: None)
    # ...and its endpoint row: this HQPlayer's library is endpoint 7 here.
    monkeypatch.setattr(hqp_library, "endpoint_by_address", lambda host, port: {"id": 7, "name": "fake"})
    # The output switch re-reads every slot's copies from the database
    # (playback.substitute); here every item opens as queued.
    from playback import substitute
    monkeypatch.setattr(substitute, "native_plays",
                        lambda items, output_id, endpoint_id: [None] * len(items))
    # The hand-over ledger is process-wide: each test starts and leaves it empty.
    with diag._lock:
        diag._ledger.clear()
        diag._by_slot.clear()
    yield f
    hb.reset_all_clients()
    f.close()
    with diag._lock:
        diag._ledger.clear()
        diag._by_slot.clear()


def test_a_queue_handed_to_an_hqplayer_that_does_not_answer_gives_up_at_once(fake, monkeypatch,
                                                                              caplog):
    make = hb._make_client
    monkeypatch.setattr(hb, "_make_client", lambda timeout: make(timeout=0.3))
    fake.silence(30)
    t = time.monotonic()
    with hb._hqp_lock:
        added = hb._add_uris_with_retry([f"http://127.0.0.1:1/file/t{i}" for i in range(6)],
                                        clear_first=True)
    # one track's two tries, not six tracks' — and in what it is, not a refusal
    assert added == 0 and time.monotonic() - t < 3
    assert "HQPlayer is not answering — 6 of 6 tracks were not handed over" in caplog.text
    assert "refused 6 of 6" not in caplog.text


def _item(path, mfid, fmt="FLAC", **span):
    return QueueItem(track_id=f"track-{mfid}", media_file_id=mfid,
                     source={"kind": "file", "path": path, "format": fmt, **span},
                     title=f"Song {mfid}", artist="Artist", album="Album")


def _held(path, n, endpoint_id=7):
    """A file the HQPlayer holds in its own library (hqp_library.sync)."""
    return QueueItem(track_id=f"held-{n}", media_file_id=None,
                     source={"kind": "hqp", "path": path, "format": "FLAC", "endpoint": endpoint_id},
                     title=f"Held {n}", artist="Artist", album="Album")


def _attach(mgr):
    b = HqpBackend(emit=mgr._on_backend_status, queue=mgr.queue)
    mgr._active = b
    b.start()
    return b


def _wait(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_attach_mirrors_the_queue_in_path_mode(fake, monkeypatch):
    from streaming import transcode
    monkeypatch.setattr(transcode, "flac_slice_path_for_file", lambda *a, **k: "/tmp/cut.flac")
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1),
                       _item("E:/Music/A/img.flac", 2, cue_start=12.5, cue_end=200.0)])
    b = _attach(mgr)
    try:
        proxy = streaming_service.get_proxy()
        tok = proxy.file_token("/music/A/img.flac", 12.5, 200.0)
        assert fake.playlist == ["file:///E:/Music/A/01.flac",
                                 f"http://127.0.0.1:0/file/{tok}"]
        assert [c for c, _ in fake.commands if c == "Stop"]   # replace stops first
    finally:
        b.shutdown()


def test_attach_streams_in_stream_mode(fake, monkeypatch):
    monkeypatch.setattr(hb, "_stream_mode", lambda: True)   # HQPlayer elsewhere: streams
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1)])
    b = _attach(mgr)
    try:
        tok = streaming_service.get_proxy().file_token("/music/A/01.flac")
        assert fake.playlist == [f"http://127.0.0.1:0/file/{tok}"]
    finally:
        b.shutdown()


def test_busy_attach_registers_tokens_without_touching_the_playlist(fake, monkeypatch):
    monkeypatch.setattr(hb, "_stream_mode", lambda: True)   # HQPlayer elsewhere: streams
    proxy = streaming_service.get_proxy()
    tok = proxy.file_token("/music/A/01.flac")
    fake.playlist = [f"http://127.0.0.1:0/file/{tok}"]
    fake.state = int(PlaybackState.PLAYING)
    fake.track = 1
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1)])
    b = _attach(mgr)
    try:
        assert not [c for c, _ in fake.commands if c in ("PlaylistAdd", "Stop", "PlaylistClear")]
        assert proxy.file_entry(tok).path == "/music/A/01.flac"
    finally:
        b.shutdown()


def test_drift_is_read_through_hqplayers_escapes(fake):
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A [TR24]/01.flac", 1)])
    b = HqpBackend(emit=mgr._on_backend_status, queue=mgr.queue)
    assert b._check_drift([{"uri": "file:///E:/Music/A%20%5BTR24%5D/01.flac"}]) is False
    assert b._check_drift([{"uri": "file:///E:/Music/A%20%5BTR24%5D/02.flac"}]) is True
    assert b._check_drift([]) is True


def test_adopt_resolves_paths_tokens_and_foreign_uris(fake, monkeypatch):
    proxy = streaming_service.get_proxy()
    tok = proxy.register_file("/music/A/02.flac", "audio/flac")
    fake.playlist = ["file:///E:/Music/A/01.flac", f"http://127.0.0.1:0/file/{tok}",
                     "file:///X:/foreign.flac"]
    fake.state = int(PlaybackState.PLAYING)
    fake.track = 2

    def spans(spans_):
        # Every file:// path is asked about — the catalogue decides what is
        # ours; the foreign one simply has no row.
        assert sorted(spans_) == [("E:/Music/A/01.flac", None), ("E:/Music/A/02.flac", None),
                                  ("X:/foreign.flac", None)]
        return {("E:/Music/A/01.flac", None): _item("E:/Music/A/01.flac", 1),
                ("E:/Music/A/02.flac", None): _item("E:/Music/A/02.flac", 2)}
    monkeypatch.setattr(queue_mod, "items_for_file_spans", spans)
    mgr = PlaybackManager()
    b = _attach(mgr)
    try:
        items = mgr.queue.snapshot()
        assert [it.media_file_id for it in items] == [1, 2, None]
        assert items[2].source == {"kind": "uri", "uri": "file:///X:/foreign.flac"}
        assert not [c for c, _ in fake.commands if c == "PlaylistAdd"]
    finally:
        b.shutdown()


def test_status_omits_hqplayers_tags_for_http_slots(fake, monkeypatch):
    monkeypatch.setattr(hb, "_stream_mode", lambda: True)   # HQPlayer elsewhere: streams
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1)])
    b = HqpBackend(emit=mgr._on_backend_status, queue=mgr.queue)
    # No entry named (a build that sends no URI): the index stands, and the
    # canary's drift makes the tick external.
    st = TrackStatus(state=PlaybackState.PLAYING, track_index=1, track_id="",
                     position=5.0, length=200.0, volume=-3.0,
                     artist="HTTP stream", album="", song="HTTP stream")
    out = b._status_of(st, b._playing(st))
    assert out.queue_index == 1 and "artist" not in out.extra and "song" not in out.extra
    monkeypatch.setattr(hb, "_stream_mode", lambda: False)
    out = b._status_of(st, b._playing(st))
    assert out.extra["artist"] == "HTTP stream"
    b._drift = True
    out = b._status_of(st, b._playing(st))
    assert out.queue_index == 0 and out.extra["source"] == "external"


def _reading_at(b, uri, track=1):
    """What _playing makes of a PLAYING tick at HQPlayer's slot `track` that
    names `uri`."""
    return b._playing(TrackStatus(state=PlaybackState.PLAYING, track_index=track, track_id="",
                                  position=3.0, length=200.0, volume=-3.0, uri=uri))


@pytest.fixture
def listens(monkeypatch):
    """The play tracker's writes and Last.fm calls, recorded instead of made."""
    writes, lastfm = [], []
    monkeypatch.setattr(tracker, "_db_execute", lambda sql, params=None: writes.append((sql, params)))
    monkeypatch.setattr(tracker, "_scrobble_async", lambda method, **kw: lastfm.append(method))
    monkeypatch.setattr(tracker, "_scrobbling_enabled", lambda: True)
    monkeypatch.setattr(tracker, "_play_session", None)
    return writes, lastfm


def _statuses_after(fake, n, count=3):
    """Has the fake answered `count` Status polls since it had `n` commands?"""
    return sum(1 for c, _ in fake.commands[n:] if c == "Status") >= count


def test_the_entry_hqplayer_reads_names_its_slot_whatever_the_index(fake):
    q = CanonicalQueue()
    q.replace([_item("E:/Music/A [TR24]/01.flac", 1), _item("E:/Music/A [TR24]/02.flac", 2)])
    b = HqpBackend(emit=lambda *_: None, queue=q)
    # Desktop 6 names a file file://E:/… with its brackets escaped
    assert _reading_at(b, "file://E:/Music/A %5BTR24%5D/01.flac") == (1, q.item_at(1), False)
    # a mutation moved the entry before the queue committed: it is the slot it names
    assert _reading_at(b, "file://E:/Music/A %5BTR24%5D/02.flac") == (2, q.item_at(2), False)
    # an edit in HQPlayer's own window: not ours, whatever slot it sits at
    assert _reading_at(b, "file://D:/elsewhere/ref768.wav") == (0, None, True)
    # the canary's drift (a slot HQPlayer dropped, an edit elsewhere in the
    # playlist) takes no listen of ours away
    b._drift = True
    assert _reading_at(b, "file://E:/Music/A %5BTR24%5D/01.flac") == (1, q.item_at(1), False)
    # HQPlayer opening the entry (PLAYING at track 0): the slot comes with the next tick
    assert _reading_at(b, "file://E:/Music/A %5BTR24%5D/01.flac", track=0) is None


def test_a_stream_is_named_by_its_track_and_a_token_of_another_session_by_nothing(
        fake, monkeypatch):
    proxy = streaming_service.get_proxy()
    meta = {"tokP": {"track_id": "track-9", "media_file_id": None},   # a phantom's stream
            "tokM": {"track_id": None, "media_file_id": 4}}           # an m4a transcoded on play
    monkeypatch.setattr(proxy, "preview_meta", lambda token: meta.get(token))
    q = CanonicalQueue()
    q.replace([_item("E:/Music/A/01.flac", 1),
               QueueItem(track_id="track-9", media_file_id=None,
                         source={"kind": "proxy", "token": "tokP"}, title="P", artist="X"),
               _item("E:/Music/A/04.m4a", 4, fmt="M4A")])
    b = HqpBackend(emit=lambda *_: None, queue=q)
    ours = lambda token: proxy.url_for(token, host=b._url_host)          # noqa: E731
    assert _reading_at(b, ours("tokP"), 1) == (2, q.item_at(2), False)
    assert _reading_at(b, ours("tokM"), 3) == (3, q.item_at(3), False)
    # another node's media proxy, a NAS path: not ours, whatever the token
    assert _reading_at(b, "http://192.0.2.7:8832/preview/tokP", 2) == (0, None, True)
    assert _reading_at(b, "http://nas.invalid/music/file/x.flac", 1) == (0, None, True)
    # a token at our address this process does not know cannot say: the
    # index stands, external only while the canary says the playlist differs
    assert _reading_at(b, ours("gone"), 2) == (2, q.item_at(2), False)
    b._drift = True
    assert _reading_at(b, ours("gone"), 2) == (0, None, True)


def test_a_cue_slice_is_named_by_its_start(fake):
    proxy = streaming_service.get_proxy()
    tok = proxy.register_file("/music/A/disc.flac", "audio/flac", start=200.0, end=400.0)
    q = CanonicalQueue()
    q.replace([_item("E:/Music/A/disc.flac", 1, cue_start=0.0, cue_end=200.0),
               _item("E:/Music/A/disc.flac", 2, cue_start=200.0, cue_end=400.0)])
    b = HqpBackend(emit=lambda *_: None, queue=q)
    assert _reading_at(b, proxy.file_url(tok, host=b._url_host), 1) == (2, q.item_at(2), False)


def test_a_cue_image_whose_cut_failed_stays_with_the_slice_at_hqplayers_slot(fake, monkeypatch):
    from streaming import transcode

    def no_cut(*a, **k):
        raise RuntimeError("ffmpeg failed")
    monkeypatch.setattr(transcode, "flac_slice_path_for_file", no_cut)
    q = CanonicalQueue()
    q.replace([_item("E:/Music/A/disc.flac", 1, cue_start=0.0, cue_end=200.0),
               _item("E:/Music/A/disc.flac", 2, cue_start=200.0, cue_end=400.0)])
    b = HqpBackend(emit=lambda *_: None, queue=q)
    # every slice is handed the whole image: the ledger keeps the last one
    assert {b._uri_for(it) for it in q.snapshot()} == {"file:///E:/Music/A/disc.flac"}
    assert _reading_at(b, "file://E:/Music/A/disc.flac", 1) == (1, q.item_at(1), False)
    assert _reading_at(b, "file://E:/Music/A/disc.flac", 2) == (2, q.item_at(2), False)


def test_an_entry_started_in_hqplayers_own_window_is_external_from_its_first_tick(fake):
    # The status goes nowhere but this list: no manager, no play tracker.
    q = CanonicalQueue()
    q.replace([_item("E:/Music/A/01.flac", 1), _item("E:/Music/A/02.flac", 2)])
    seen = []
    b = HqpBackend(emit=lambda sender, s: seen.append(s), queue=q)
    b.start()
    try:
        with fake._lock:
            fake.state, fake.track = int(PlaybackState.PLAYING), 1
        assert _wait(lambda: seen and seen[-1].state == "playing" and seen[-1].queue_index == 1)
        assert "source" not in seen[-1].extra and seen[-1].item is q.item_at(1)
        # the owner clears HQPlayer's playlist in its window and plays a file of their own
        with fake._lock:
            fake.playlist = ["file:///D:/elsewhere/ref768.wav"]
            fake.position = 0.0
        assert _wait(lambda: seen[-1].extra.get("source") == "external", timeout=3.0)
        assert seen[-1].queue_index == 0 and seen[-1].item is None
        assert not b.drift          # the canary has not looked again: the entry said it
    finally:
        b.shutdown()


@pytest.mark.parametrize("theirs", [
    ["file:///D:/elsewhere/ref768.wav"],                                 # a playlist of another length
    ["file:///D:/elsewhere/a.wav", "file:///D:/elsewhere/b.wav"],        # the same length as the queue
])
def test_a_file_played_from_hqplayers_own_window_is_never_a_listen(fake, listens, theirs):
    writes, lastfm = listens
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1), _item("E:/Music/A/02.flac", 2)])
    b = _attach(mgr)
    try:
        # past the first tick, which is a canary's: the edit lands between two
        assert _wait(lambda: _statuses_after(fake, 0, 2))
        with fake._lock:          # the owner replaces HQPlayer's playlist and plays it
            fake.playlist = list(theirs)
            fake.state, fake.track, fake.position = int(PlaybackState.PLAYING), 1, 0.0
        assert _wait(lambda: mgr._latest_status.get("source") == "external", timeout=3.0)
        n = len(fake.commands)
        assert _wait(lambda: _statuses_after(fake, n))
        assert mgr._latest_status["track_index"] == 0
        assert lastfm == [] and writes == [] and tracker._play_session is None
    finally:
        b.shutdown()


def test_a_listen_cut_by_hqplayers_own_window_keeps_only_its_own_seconds(fake, listens):
    writes, lastfm = listens
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1)])
    b = _attach(mgr)
    try:
        with fake._lock:
            fake.state, fake.track, fake.position = int(PlaybackState.PLAYING), 1, 0.0
        assert _wait(lambda: tracker._play_session is not None
                     and tracker._play_session.max_position >= 3.0)
        with fake._lock:          # their file, from the top: it outplays ours
            ours = fake.position
            fake.playlist = ["file:///D:/elsewhere/ref768.wav"]
            fake.position = 0.0
        assert _wait(lambda: tracker._play_session is None)
        assert _wait(lambda: fake.position >= ours + 3)
        rows = [p for sql, p in writes if "INSERT INTO listening_history" in sql]
        assert len(rows) == 1 and rows[0]["tid"] == "track-1"
        assert rows[0]["dur"] <= ours and not rows[0]["comp"]
        assert lastfm == ["update_now_playing"]
    finally:
        b.shutdown()


def test_a_listen_started_on_hqplayers_opening_tick_knows_the_tracks_length(fake, listens):
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1)])
    b = _attach(mgr)
    try:
        assert _wait(lambda: _statuses_after(fake, 0, 2))
        with fake._lock:          # Play from stopped: the first tick names the entry, track 0, length 0
            fake.state, fake.track, fake.position, fake.opening = int(PlaybackState.PLAYING), 1, 0.0, 1
        assert _wait(lambda: tracker._play_session is not None
                     and tracker._play_session.max_position >= 2.0)
        assert tracker._play_session.track_length == 300
    finally:
        b.shutdown()


def test_hqplayers_limiter_count_reaches_the_status(fake, listens):
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1)])
    b = _attach(mgr)
    try:
        with fake._lock:
            fake.state, fake.track = int(PlaybackState.PLAYING), 1
        assert _wait(lambda: mgr._latest_status.get("state") == "playing")
        assert "limited" not in mgr._latest_status          # a build that does not report it
        assert mgr._latest_status["active_mode"] == "PCM"   # the mode it runs, for the meter
        with fake._lock:
            fake.clips = 3
        assert _wait(lambda: mgr._latest_status.get("limited") == 3)
    finally:
        b.shutdown()


def test_the_meter_lives_at_the_control_port_plus_one_while_attached(fake):
    from playback.hqp_meter import meter
    mgr = PlaybackManager()
    b = _attach(mgr)
    try:
        assert meter._target == ("127.0.0.1", fake.port + 1)
        # what HQPlayer applies ahead of its limiter, from every status tick
        assert _wait(lambda: meter._gain_db == -3.0)
        with fake._lock:
            fake.track_gain = -9.68
        assert _wait(lambda: meter._gain_db == pytest.approx(-12.68))
    finally:
        b.shutdown()
    assert meter._target is None
    # a late shutdown of a replaced backend leaves the newer attach standing
    newer = _attach(mgr)
    try:
        b.shutdown()
        assert meter._target == ("127.0.0.1", fake.port + 1)
    finally:
        newer.shutdown()


def test_the_item_a_status_names_keeps_its_slot_through_a_commit(fake, listens):
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1), _item("E:/Music/A/02.flac", 2),
                       _item("E:/Music/A/03.flac", 3)])
    b = HqpBackend(emit=mgr._on_backend_status, queue=mgr.queue)
    mgr._active = b
    playing = mgr.queue.item_at(3)
    st = PlaybackStatus(state="playing", position=10.0, length=300.0, queue_index=3, item=playing)
    mgr.queue.remove(1)           # committed after the backend looked: it is slot 2 now
    mgr._on_backend_status(b, st)
    assert mgr._latest_status["track_index"] == 2
    assert mgr._latest_status["track_id"] == "track-3"
    assert tracker._play_session.track_id == "track-3"


def test_a_mutation_in_flight_holds_the_verdict_on_an_entry_the_queue_lacks(fake):
    q = CanonicalQueue()
    q.replace([_item("E:/Music/A/01.flac", 1), _item("E:/Music/A/02.flac", 2)])
    b = HqpBackend(emit=lambda *_: None, queue=q)
    with q.mutation():
        # a replace plays its first track before the queue commits it
        assert _reading_at(b, "file://E:/Music/B/01.flac") is None
        # a removal shifted HQPlayer's index first: slot 2 of this queue is
        # another item once the removal commits
        assert _reading_at(b, "file://E:/Music/A/02.flac", 1) is None
        assert _reading_at(b, "file://E:/Music/A/01.flac") == (1, q.item_at(1), False)
    assert _reading_at(b, "file://E:/Music/B/01.flac") == (0, None, True)
    assert _reading_at(b, "file://E:/Music/A/02.flac", 1) == (2, q.item_at(2), False)


def test_the_canary_gives_no_verdict_on_a_mutation_in_flight(fake, monkeypatch):
    monkeypatch.setattr(hb, "DRIFT_CHECK_EVERY", 1)
    q = CanonicalQueue()
    q.replace([_item("E:/Music/A/01.flac", 1)])
    b = HqpBackend(emit=lambda *_: None, queue=q)
    b.start()

    def reads(count=2):
        n = len(fake.commands)
        return _wait(lambda: sum(1 for c, _ in fake.commands[n:] if c == "PlaylistGet") >= count)
    try:
        with q.mutation():        # the manager's append: mirrored first, committed after
            with fake._lock:
                fake.playlist.append("file:///E:/Music/A/02.flac")
            assert reads() and not b.drift
            q.append([_item("E:/Music/A/02.flac", 2)])
        assert reads() and not b.drift
        with fake._lock:          # an entry nobody of ours added
            fake.playlist.append("file:///D:/elsewhere/x.wav")
        assert _wait(lambda: b.drift)
    finally:
        b.shutdown()


def test_a_slot_re_bound_under_its_listen_stays_ours(fake):
    q = CanonicalQueue()
    q.replace([_item("E:/Music/A/01.flac", 1)])
    b = HqpBackend(emit=lambda *_: None, queue=q)
    b._uri_for(q.item_at(1))      # handed over: the ledger names its track
    # queue_insert_next re-appends HQPlayer's own form: a track-less entry
    diag.note_add("file://E:/Music/A/01.flac", True, "OK", "")
    q.rebind(lambda items: [{"media_file_id": 5,
                             "source": {"kind": "file", "path": "E:/Music/B/01.flac",
                                        "format": "FLAC"}}], None)
    # the file left the library: the slot is another copy of its track, and
    # HQPlayer still reads the file it was handed
    assert _reading_at(b, "file://E:/Music/A/01.flac") == (1, q.item_at(1), False)


def test_a_slot_that_is_the_file_itself_is_the_entry_whatever_the_ledger_says(fake):
    handed = _item("E:/Music/A/01.flac", 1)
    q = CanonicalQueue()
    q.replace([handed])
    b = HqpBackend(emit=lambda *_: None, queue=q)
    b._uri_for(handed)            # the ledger: track-1
    # its track left the catalogue: the slot is the file, passed through
    q.replace([QueueItem(track_id=None, media_file_id=None,
                         source={"kind": "uri", "uri": "file:///E:/Music/A/01.flac"},
                         title="01", artist="")])
    assert _reading_at(b, "file://E:/Music/A/01.flac") == (1, q.item_at(1), False)


def test_a_pass_through_entry_of_the_queue_is_ours_though_it_names_no_track(fake):
    q = CanonicalQueue()
    q.replace([QueueItem(track_id=None, media_file_id=None,      # adopted from HQPlayer's playlist
                         source={"kind": "uri", "uri": "http://elsewhere.invalid/a.flac"},
                         title="A", artist="X")])
    b = HqpBackend(emit=lambda *_: None, queue=q)
    b._uri_for(q.item_at(1))      # handed over verbatim: the ledger holds it with no track
    assert _reading_at(b, "http://elsewhere.invalid/a.flac") == (1, q.item_at(1), False)
    assert _reading_at(b, "http://elsewhere.invalid/b.flac") == (0, None, True)


def test_both_forms_hqplayer_names_a_file_by_are_one_path():
    from hqplayer_client import uri_to_file_path
    # as handed, and as Desktop 6 reports it back: two slashes, brackets escaped
    assert (uri_to_file_path("file:///D:/ai/x [y].wav")
            == uri_to_file_path("file://D:/ai/x %5By%5D.wav") == "D:/ai/x [y].wav")


def test_restart_loses_the_mirror_and_the_play_intent_remirrors(fake, monkeypatch):
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1), _item("E:/Music/A/02.flac", 2)])
    b = _attach(mgr)
    assert len(fake.playlist) == 2
    assert b.healthy() and b.reachable()

    fake.restart()
    assert _wait(lambda: not b.healthy(), timeout=12.0), "reconnect never flagged the lost mirror"
    assert fake.playlist == []

    from routers import settings as settings_router
    prefs = {"output.type": "hqplayer"}
    monkeypatch.setattr(settings_router, "_read", lambda key: prefs.get(key))
    live = mgr.ensure_active()
    try:
        assert live is not b and live.healthy()
        assert fake.playlist == ["file:///E:/Music/A/01.flac", "file:///E:/Music/A/02.flac"]
    finally:
        live.shutdown()


def test_a_return_after_a_long_outage_also_loses_the_mirror(fake, monkeypatch):
    """The Pi was power-cycled for half a minute (live, 2026-09-27): the
    failed polls had already reset the status client, so a same-tick socket
    comparison saw no reconnect, the empty playlist passed as an external
    edit and Play landed on nothing."""
    # The breaker's wall clock is not under test: a failed tick between the
    # return and a hand reset re-armed it, and the back-off then outlasted
    # the wait (the test failed about one run in eight).
    monkeypatch.setattr(hb, "HQP_CIRCUIT_COOLDOWN", 0.0)
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1), _item("E:/Music/A/02.flac", 2)])
    b = _attach(mgr)
    try:
        assert len(fake.playlist) == 2

        fake.close()
        # Not merely a failed tick: the first one only finds the socket dead
        # and keeps the client object, which a same-tick socket comparison
        # still catches on the return. The shape that fooled it live is a
        # failed CONNECT — the client reset to None while HQPlayer stays away.
        assert _wait(lambda: b._failures >= 1 and hb._hqp_status_client is None,
                     timeout=15.0), "the outage never reset the status client"
        fake.come_back()
        assert _wait(lambda: (b.poke(), not b.healthy())[1], timeout=15.0), \
            "the return never flagged the lost mirror"
        assert fake.playlist == []

        from routers import settings as settings_router
        prefs = {"output.type": "hqplayer"}
        monkeypatch.setattr(settings_router, "_read", lambda key: prefs.get(key))
        live = mgr.ensure_active()
        try:
            assert live is not b and live.healthy()
            assert fake.playlist == ["file:///E:/Music/A/01.flac", "file:///E:/Music/A/02.flac"]
        finally:
            live.shutdown()
    finally:
        # A poller left running reads the NEXT test's fake through the
        # module's shared status client.
        b.shutdown()


def test_reachable_needs_a_protocol_answer(fake):
    b = HqpBackend(emit=lambda *_: None, queue=PlaybackManager().queue)
    assert b.reachable()
    fake.close()
    t0 = time.monotonic()
    assert not b.reachable()
    assert time.monotonic() - t0 < 3.0


def test_held_files_mirror_as_their_own_paths_and_drift_clean(fake):
    """A file in HQPlayer's own library is opened by HQPlayer at that very
    path — no library-root remap, no proxy — and the drift canary reads it
    back as the same slot through HQPlayer's escaping of brackets."""
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1),
                       _held("/media/FLASH/Bonobo [FLAC]/01. Intro.flac", 1)])
    b = _attach(mgr)
    try:
        assert fake.playlist == ["file:///E:/Music/A/01.flac",
                                 "file:///media/FLASH/Bonobo [FLAC]/01. Intro.flac"]
        with hb._hqp_status_lock:
            playlist = hb._get_hqp_status().get_playlist()
        assert playlist[1]["uri"] == "file:///media/FLASH/Bonobo %5BFLAC%5D/01. Intro.flac"
        assert b._check_drift(playlist) is False
        fake.playlist[1] = "file:///media/FLASH/Other/01.flac"
        with hb._hqp_status_lock:
            playlist = hb._get_hqp_status().get_playlist()
        assert b._check_drift(playlist) is True
    finally:
        b.shutdown()


def test_adopt_resolves_a_held_file_by_its_path(fake, monkeypatch):
    """A playlist HQPlayer still holds after a backend restart: a file://
    slot that is no local file is looked up among the files held at this
    endpoint, by the path HQPlayer opens."""
    fake.playlist = ["file:///media/FLASH/Bonobo%20%5BFLAC%5D/01.%20Intro.flac"]
    fake.state = int(PlaybackState.PLAYING)
    fake.track = 1
    monkeypatch.setattr(queue_mod, "items_for_file_spans", lambda spans: {})

    def held(paths, endpoint_id):
        assert paths == ["/media/FLASH/Bonobo [FLAC]/01. Intro.flac"]
        assert endpoint_id == 7
        return {paths[0]: _held(paths[0], 7)}
    monkeypatch.setattr(queue_mod, "items_for_hqp_paths", held)
    mgr = PlaybackManager()
    b = _attach(mgr)
    try:
        items = mgr.queue.snapshot()
        assert [it.track_id for it in items] == ["held-7"]
        assert items[0].source["kind"] == "hqp"
        assert not [c for c, _ in fake.commands if c == "PlaylistAdd"]
    finally:
        b.shutdown()


# -- a file that left the library under the mirrored queue ----------------------
#
# PlaybackManager.rebind_files re-binds the slot in the canonical queue and
# tells the output; HQPlayer's playlist still names the gone path. The backend
# is mirrored by hand and never started: a PLAYING fake would otherwise feed
# the manager's tracker from the poller.

def _mirrored(mgr):
    b = HqpBackend(emit=lambda *_: None, queue=mgr.queue)
    assert b.queue_replace(mgr.queue.snapshot(), play=False) == len(mgr.queue)
    return b


def _move(mgr, slot, path, mfid):
    """What rebind_files leaves on a slot whose file moved: re-bound in place."""
    moved = mgr.queue.rebind(
        lambda items: [{"media_file_id": mfid,
                        "source": {"kind": "file", "path": path, "format": "FLAC"}}
                       if i == slot else None for i, _ in enumerate(items, start=1)],
        None)
    assert len(moved) == 1


def _album(n):
    mgr = PlaybackManager()
    mgr.queue.replace([_item(f"E:/Music/A/0{i}.flac", i) for i in range(1, n + 1)])
    return mgr


def test_a_rebind_while_stopped_remirrors_and_resumes_at_the_slot(fake):
    mgr = _album(3)
    b = _mirrored(mgr)
    fake.track = 2                                  # stopped on slot 2
    _move(mgr, 3, "E:/Music/B/03.flac", 30)
    b.queue_changed("rebind")
    assert fake.playlist == ["file:///E:/Music/A/01.flac", "file:///E:/Music/A/02.flac",
                             "file:///E:/Music/B/03.flac"]
    assert b._resume_index == 2 and b.healthy()


def test_a_rebind_past_the_playing_slot_reappends_the_tail(fake):
    mgr = _album(4)
    b = _mirrored(mgr)
    fake.state, fake.track = int(PlaybackState.PLAYING), 1
    fake.commands.clear()
    _move(mgr, 3, "E:/Music/B/03.flac", 30)
    b.queue_changed("rebind")
    assert fake.playlist == ["file:///E:/Music/A/01.flac", "file:///E:/Music/A/02.flac",
                             "file:///E:/Music/B/03.flac", "file:///E:/Music/A/04.flac"]
    sent = [c for c, _ in fake.commands]
    assert "Stop" not in sent and "SelectTrack" not in sent   # the playing slot is never touched
    assert [a["index"] for c, a in fake.commands if c == "PlaylistRemove"] == ["3", "3"]
    assert (fake.state, fake.track) == (int(PlaybackState.PLAYING), 1)


def test_a_rebind_before_the_playing_slot_rebuilds_around_it(fake):
    mgr = _album(3)
    b = _mirrored(mgr)
    fake.state, fake.track = int(PlaybackState.PLAYING), 2
    fake.commands.clear()
    _move(mgr, 1, "E:/Music/B/01.flac", 10)
    b.queue_changed("rebind")
    assert fake.playlist == ["file:///E:/Music/B/01.flac", "file:///E:/Music/A/02.flac",
                             "file:///E:/Music/A/03.flac"]
    assert [(c, a) for c, a in fake.commands if c in ("Stop", "SelectTrack", "Play")] == [
        ("Stop", {}), ("SelectTrack", {"index": "2"}), ("Play", {})]
    assert (fake.state, fake.track) == (int(PlaybackState.PLAYING), 2)


def test_a_rebind_at_the_paused_slot_resumes_it_on_the_play_press(fake):
    mgr = _album(2)
    b = _mirrored(mgr)
    fake.state, fake.track = int(PlaybackState.PAUSED), 2
    _move(mgr, 2, "E:/Music/B/02.flac", 20)
    b.queue_changed("rebind")
    assert fake.playlist == ["file:///E:/Music/A/01.flac", "file:///E:/Music/B/02.flac"]
    assert fake.state == int(PlaybackState.STOPPED) and b._resume_index == 2


def test_a_rebind_leaves_a_drifted_playlist_to_the_canary(fake):
    mgr = _album(2)
    b = _mirrored(mgr)
    fake.playlist.append("file:///X:/foreign.flac")    # an edit in HQPlayer's own GUI
    fake.commands.clear()
    _move(mgr, 1, "E:/Music/B/01.flac", 10)
    b.queue_changed("rebind")
    assert [c for c, _ in fake.commands if c not in ("Status", "PlaylistGet")] == []
    assert b.healthy()


def test_a_rebind_hqplayer_cannot_follow_is_remirrored_on_the_next_play(fake):
    mgr = _album(2)
    b = _mirrored(mgr)
    fake.close()
    _move(mgr, 1, "E:/Music/B/01.flac", 10)
    b.queue_changed("rebind")
    assert not b.healthy()           # the play-intent gate re-attaches and re-mirrors


# -- the playback trace (playback.hqp_diagnostics) ------------------------------------
# A backend with its status going nowhere: no manager, so no play tracker —
# a test play never writes a listen or reaches Last.fm.

@pytest.fixture
def traced(fake, monkeypatch):
    monkeypatch.setattr(diag, "OBSERVE_S", 1.5)
    with diag._lock:
        diag._ring.clear()
        diag._ledger.clear()
        diag._by_slot.clear()
    diag._run_key = None
    HQPlayerClient._error_ring.clear()
    q = CanonicalQueue()
    # Brackets: HQPlayer reports them escaped, never as it was handed them.
    q.replace([_item("E:/Music/A [TR24]/01.flac", 1), _item("E:/Music/A [TR24]/02.flac", 2)])
    b = HqpBackend(emit=lambda *_: None, queue=q)
    b.start()
    yield b
    b.shutdown()


def _closed(timeout=10.0):
    assert _wait(lambda: diag.attempts(), timeout=timeout), "the attempt never closed"
    return diag.attempts()[0]


def test_a_jump_that_plays_is_judged_played(traced, fake):
    assert traced.select(2)
    a = _closed()
    assert a.intent == "select" and a.end == "window"
    assert a.verdict["code"] == "played", a.verdict
    f = a.facts
    assert f["slot"] == 2 and f["mode"] == "path" and f["proxy"] is None
    assert f["expected_uri"] == f["uri"] == "file:///E:/Music/A [TR24]/02.flac"
    assert f["playlist"]["at_slot"] == "file:///E:/Music/A %5BTR24%5D/02.flac"
    assert f["handover"]["ok"] is True and f["handover"]["title"] == "Song 2"
    assert [s["command"] for s in f["steps"]] == ["SelectTrack", "Play"]
    assert f["playlist"]["at_slot_expected"] and f["ticks"][-1]["filter"] == "poly-sinc-gauss-long"
    assert f["item"]["title"] == "Song 2"


def test_a_run_cut_short_is_put_back_only_once_hqplayer_answers(fake, monkeypatch):
    from playback import hqp_benchmark
    calls = []
    monkeypatch.setattr(hqp_benchmark, "recover", lambda endpoint_id: calls.append(endpoint_id))
    b = _attach(PlaybackManager())
    try:
        assert calls == [7]                       # at the attach: GetInfo answered
        fake.mute = True                          # a trial-stopped Embedded: it takes every
        fake.restart()                            # connection and drops it unanswered
        assert _wait(lambda: (b.poke(), b._failures >= 2)[1], 15)
        assert calls == [7]                       # reconnected, never answered: not yet
        fake.mute = False
        assert _wait(lambda: (b.poke(), len(calls) == 2)[1], 15)
    finally:
        b.shutdown()


def test_a_run_cut_short_is_put_back_at_the_first_answer_when_getinfo_missed_the_attach(
        fake, monkeypatch):
    from playback import hqp_benchmark
    calls = []
    monkeypatch.setattr(hqp_benchmark, "recover", lambda endpoint_id: calls.append(endpoint_id))
    fake.replies["GetInfo"] = "<Busy/>"               # no info at the attach
    b = _attach(PlaybackManager())
    try:
        assert calls == []
        assert _wait(lambda: (b.poke(), calls == [7])[1], 15)   # the first status that answered
        b.poke()
        time.sleep(0.5)
        assert calls == [7]                                      # once, not on every tick
    finally:
        b.shutdown()


def test_the_poll_backs_off_to_15_s_and_survives_a_night_of_misses():
    # HQPlayer switched off overnight: ~1000 misses made 2.0 ** failures
    # overflow and the poller thread died (OverflowError, 2026-10-04)
    assert [hb._poll_interval(n) for n in (0, 1, 2, 3, 4, 5)] == [1.0, 2.0, 4.0, 8.0, 15.0, 15.0]
    assert hb._poll_interval(5000) == 15.0


def test_a_refusal_before_the_silence_that_stops_the_batch_still_wakes_the_notices(monkeypatch):
    # "a" refused (the file cannot be opened); "b"'s add met HQPlayer's
    # silence and stops the batch at once — no playlist check, no retry,
    # each would wait out the same silence. Both reasons are told: the
    # notices re-derive for the refusal (a dropped music mount)
    class Stub:
        timed_out = False

        def is_connected(self):
            return True

        def playlist_add(self, uri, clear=False):
            self.timed_out = uri == "b"
            return uri not in ("a", "b")
    stub = Stub()
    monkeypatch.setattr(hb, "_get_hqp", lambda: stub)
    monkeypatch.setattr(hb, "_reset_hqp", lambda: None)
    monkeypatch.setattr(hb, "_refusal", lambda hqp: ("Error", "cannot open it"))
    checked = []
    monkeypatch.setattr(hb, "_uri_in_playlist", lambda uri: checked.append(uri) or uri == "b")
    monkeypatch.setattr(hb, "_stream_mode", lambda: False)
    woke = []
    monkeypatch.setattr(hb, "_notify_notices", lambda: woke.append(True))
    assert hb._add_uris_with_retry(["a", "c", "b"]) == 1
    assert woke == [True] and "b" not in checked


def test_a_play_that_initialises_for_seconds_is_judged_played(traced, fake):
    # HQPlayer says it plays while a long filter initialises — the position at
    # 0 for several polls — and the audio leaves past the window (sinc-MGa at
    # DSD256, 7 s from Play to sound, 2026-10-03): still played, not unknown
    fake.start_polls = 4
    assert traced.select(2)
    a = _closed(timeout=15.0)
    assert a.verdict["code"] == "played", a.verdict
    assert a.facts["ticks"][0]["position"] == 0.0


def test_a_failure_the_slot_then_played_is_not_offered_when_it_stops(traced, fake, monkeypatch):
    monkeypatch.setattr(diag, "START_LIMIT_S", 3.0)     # judged before the long start is over
    fake.start_polls = 6
    assert traced.select(2)
    a = _closed(timeout=10.0)
    assert a.verdict["code"] == "unknown"
    assert _wait(lambda: traced._last_closed is a, 5.0)       # kept by the backend right after the ring
    stopped = TrackStatus(state=PlaybackState.STOPPED, track_index=2, track_id="", position=0.0,
                          length=300.0, volume=-3.0)
    assert traced._diagnosis(stopped)["code"] == "unknown"     # nothing has played since
    # then the slot plays on, the position moving, the speed keeping up — the
    # end of the album (HQPlayer stopping) does not bring the failure back
    assert _wait(lambda: a.cleared, 10.0)
    assert traced._diagnosis(stopped) is None


def test_a_refused_play_is_judged_at_once_in_hqplayers_words(traced, fake):
    fake.replies["Play"] = '<Play result="Error">Empty transport</Play>'
    assert traced.play() is False
    a = _closed(timeout=5.0)
    assert a.end == "hard" and a.verdict["code"] == "rejected"
    assert a.verdict["quote"] == "Empty transport"
    assert HQPlayerClient.last_errors()[0]["message"] == "Empty transport"


def test_an_unanswered_play_intent_is_an_unreachable_attempt(traced, fake):
    fake.close()
    assert traced.reachable() is False
    a = diag.attempts()[0]
    assert a.verdict["code"] == "unreachable" and "GetInfo" in a.verdict["sentence"]


def test_a_stop_from_elsewhere_ends_the_watch_as_the_owners(traced, fake, monkeypatch):
    monkeypatch.setattr(diag, "OBSERVE_S", 60.0)
    assert traced.select(1)
    stopper = threading.Thread(target=traced.stop)
    stopper.start()
    stopper.join(5)
    a = _closed(timeout=5.0)
    assert a.end == "owner" and a.verdict["code"] in ("played", "interrupted")


def test_a_stop_hqplayer_refuses_leaves_the_watch_running(traced, fake, monkeypatch):
    monkeypatch.setattr(diag, "OBSERVE_S", 60.0)
    assert traced.select(1)
    a = traced._attempt
    fake.replies["Stop"] = '<Stop result="Error">not authenticated and no internet access</Stop>'
    stopper = threading.Thread(target=traced.stop)
    stopper.start()
    stopper.join(5)
    assert traced._attempt is a and a.end is None       # nothing stopped: the owner moved nowhere
    del fake.replies["Stop"]
    stopper = threading.Thread(target=traced.stop)
    stopper.start()
    stopper.join(5)
    assert _closed(timeout=5.0).end == "owner"


def test_replacing_the_queue_with_play_is_one_attempt_and_never_deadlocks(traced, fake):
    items = [_item("E:/Music/B/01.flac", 11), _item("E:/Music/B/02.flac", 12)]
    done = threading.Event()

    def replace():
        traced.queue_replace(items, play=True)
        traced._queue.replace(items)     # the manager's commit after the mirror
        done.set()
    threading.Thread(target=replace, daemon=True).start()
    assert done.wait(10), "queue_replace(play=True) hung"
    a = _closed()
    assert a.intent == "replace" and a.verdict["code"] == "played", a.verdict
    assert a.facts["expected_uri"] == "file:///E:/Music/B/01.flac"
    assert a.facts["adds"]["count"] == 2
    assert [s["command"] for s in a.facts["steps"]] == ["Stop", "Play"]


def test_hqplayers_error_text_reaches_the_caller(fake):
    fake.replies["SetFilter"] = '<SetFilter result="Error">Filter not available in this mode</SetFilter>'
    c = HQPlayerClient("127.0.0.1", fake.port)
    assert c.connect()
    try:
        assert c.set_filter(3) is False
        assert c.refusal() == "Filter not available in this mode"
        assert c.last_error.command == "SetFilter"
        fake.replies["SetFilter"] = "<SetFilter/>"      # a bare answer is acceptance
        assert c.set_filter(3) is True and c.last_error is None
    finally:
        c.disconnect()


def test_an_answer_that_refuses_is_no_success(fake):
    """Fifteen commands said True for any answer: a fixed volume answers
    <Volume result="Error" /> (Direct SDM, Desktop 6.2.3, seen live), and
    HQPlayer 6 without internet access refuses every state change so."""
    c = HQPlayerClient("127.0.0.1", fake.port)
    assert c.connect()
    try:
        calls = {"Pause": c.pause, "Stop": c.stop, "Next": c.next, "Previous": c.previous,
                 "Forward": c.forward, "Backward": c.backward, "Seek": lambda: c.seek(30),
                 "VolumeUp": c.volume_up, "VolumeDown": c.volume_down,
                 "VolumeMute": c.volume_mute, "Volume": lambda: c.set_volume(-3.0),
                 "PlaylistClear": c.playlist_clear, "PlaylistRemove": lambda: c.playlist_remove(1),
                 "SetRepeat": lambda: c.set_repeat(RepeatMode.ALL),
                 "SetRandom": lambda: c.set_random(True)}
        for command, call in calls.items():
            fake.replies[command] = f'<{command} result="Error" />'
            assert call() is False, command
            assert c.last_error.command == command
            fake.replies[command] = f"<{command}/>"
            assert call() is True, command
        # one rule with the error ring: an answer neither OK nor bare is no success
        fake.replies["Stop"] = '<Stop result="Busy" />'
        assert c.stop() is False and c.last_error.result == "Busy"
    finally:
        c.disconnect()


def test_a_fixed_volume_is_named_where_hqplayer_says_only_error(fake):
    fake.replies["Volume"] = '<Volume result="Error" />'
    fake.replies["VolumeRange"] = '<VolumeRange enabled="0" adaptive="0" min="-60" max="0"/>'
    c = HQPlayerClient("127.0.0.1", fake.port)
    assert c.connect()
    try:
        assert c.set_volume(0.0) is False
        assert c.volume_refusal() == VOLUME_FIXED
        fake.replies["VolumeRange"] = '<VolumeRange enabled="1" adaptive="0" min="-60" max="0"/>'
        assert c.set_volume(0.0) is False
        assert c.volume_refusal() == "HQPlayer refused Volume"
        # its own words stand, whatever the range says (a missing `enabled` reads fixed)
        fake.replies["VolumeRange"] = '<VolumeRange min="-60" max="0"/>'
        fake.replies["Volume"] = '<Volume result="Error">not authenticated and no internet access</Volume>'
        assert c.set_volume(0.0) is False
        assert c.volume_refusal() == "not authenticated and no internet access"
    finally:
        c.disconnect()


def test_a_volume_command_that_lost_the_connection_asks_nothing_more(fake):
    # A range asked on a dead socket was a second, made-up failure in the
    # ring support diagnostics ship
    HQPlayerClient._error_ring.clear()
    c = HQPlayerClient("127.0.0.1", fake.port, timeout=0.3)
    assert c.connect()
    fake.silence(1.0)
    assert c.volume_up() is False and not c.is_connected()
    assert c.volume_refusal() == "VolumeUp: no answer within 0.3 s"
    assert [e["during"] for e in HQPlayerClient.last_errors()] == ["VolumeUp"]


def test_a_command_hqplayer_refuses_is_logged_in_its_words(fake, caplog):
    fake.replies["Stop"] = '<Stop result="Error">not authenticated and no internet access</Stop>'
    fake.replies["Volume"] = '<Volume result="Error" />'
    fake.replies["PlaylistClear"] = '<PlaylistClear result="Error">busy</PlaylistClear>'
    b = HqpBackend(emit=lambda *_: None, queue=CanonicalQueue())
    assert b.stop() is False and b.set_volume(-10.0) is False
    assert b.queue_clear_after_current() is False
    assert "HQPlayer did not take Stop: not authenticated and no internet access" in caplog.text
    assert "HQPlayer did not take Volume: HQPlayer refused Volume" in caplog.text
    assert "HQPlayer did not take PlaylistClear: busy" in caplog.text


def test_a_slot_hqplayer_does_not_remove_stays_in_the_queue(fake):
    # Mirror first: the canonical queue drops the slot only once HQPlayer did
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1), _item("E:/Music/A/02.flac", 2)])
    b = _attach(mgr)
    try:
        fake.replies["PlaylistRemove"] = '<PlaylistRemove result="Error">busy</PlaylistRemove>'
        with pytest.raises(RuntimeError, match="HQPlayer did not remove it: busy"):
            mgr.remove(2)
        assert len(mgr.queue) == 2 and len(fake.playlist) == 2
        del fake.replies["PlaylistRemove"]
        assert mgr.remove(2) is True
        assert len(mgr.queue) == 1 and len(fake.playlist) == 1
    finally:
        b.shutdown()


def test_a_refused_removal_is_not_called_a_changed_queue(fake, monkeypatch):
    from fastapi import HTTPException
    from routers import player as player_router
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1), _item("E:/Music/A/02.flac", 2)])
    b = _attach(mgr)
    monkeypatch.setattr(player_router, "manager", mgr)
    try:
        fake.replies["PlaylistRemove"] = ('<PlaylistRemove result="Error">not authenticated '
                                          'and no internet access</PlaylistRemove>')
        with pytest.raises(HTTPException) as e:
            player_router.remove(player_router.RemoveRequest(index=2, track_id="track-2"))
        assert e.value.status_code == 503
        assert e.value.detail == ("HQPlayer did not remove it: not authenticated "
                                  "and no internet access")
        # the slot holding another track is the queue having moved
        with pytest.raises(HTTPException) as e:
            player_router.remove(player_router.RemoveRequest(index=2, track_id="track-1"))
        assert e.value.status_code == 409
    finally:
        b.shutdown()


def test_a_playlist_that_is_no_mirror_is_not_edited_by_index(fake):
    # An HQPlayer restarted empty, or holding an external playlist: the
    # removal is the queue's alone — by index it would refuse, or take an
    # entry that is not ours
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1), _item("E:/Music/A/02.flac", 2)])
    b = _attach(mgr)
    try:
        b._drift = True
        assert mgr.remove(2) is True
        assert len(mgr.queue) == 1 and len(fake.playlist) == 2
        assert "PlaylistRemove" not in [c for c, _ in fake.commands]
    finally:
        b.shutdown()


def test_an_insert_hqplayer_cannot_make_room_for_doubles_nothing(fake):
    mgr = PlaybackManager()
    mgr.queue.replace([_item(f"E:/Music/A/0{n}.flac", n) for n in (1, 2, 3)])
    b = _attach(mgr)
    try:
        fake.replies["PlaylistRemove"] = '<PlaylistRemove result="Error" />'
        assert b.queue_insert_next([_item("E:/Music/B/01.flac", 9)], 1) == 1
        assert fake.playlist == ["file:///E:/Music/A/01.flac", "file:///E:/Music/A/02.flac",
                                 "file:///E:/Music/A/03.flac", "file:///E:/Music/B/01.flac"]
        assert b.drift
    finally:
        b.shutdown()


def test_a_radio_clear_hqplayer_refuses_keeps_the_queue(fake):
    mgr = PlaybackManager()
    mgr.queue.replace([_item(f"E:/Music/A/0{n}.flac", n) for n in (1, 2, 3)])
    b = _attach(mgr)
    try:
        fake.replies["PlaylistClear"] = '<PlaylistClear result="Error" />'
        gen = mgr.queue.generation
        assert mgr.clear_for_radio() == gen
        assert len(mgr.queue) == 3 and len(fake.playlist) == 3
    finally:
        b.shutdown()


def test_a_new_order_hqplayer_refuses_keeps_the_queues(fake):
    from playback.base import ReorderPlan
    mgr = PlaybackManager()
    items = [_item(f"E:/Music/A/0{n}.flac", n) for n in (1, 2, 3)]
    mgr.queue.replace(items)
    b = _attach(mgr)
    try:
        fake.replies["PlaylistRemove"] = '<PlaylistRemove result="Error">busy</PlaylistRemove>'
        plan = ReorderPlan(seamless=True, status_idx=1, new_status_idx=1, old_before=[],
                           new_before=[], old_after=["track-2", "track-3"],
                           new_after=["track-3", "track-2"],
                           order=["track-1", "track-3", "track-2"],
                           items_by_id={it.track_id: it for it in items})
        with pytest.raises(RuntimeError, match="HQPlayer did not take the new order: busy"):
            mgr.apply_reorder(plan)
        assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2", "track-3"]
        assert len(fake.playlist) == 3 and b.drift
    finally:
        b.shutdown()


def test_the_volume_step_keeps_to_hqplayers_range_and_a_fixed_one_says_why(fake):
    from fastapi import HTTPException
    from routers import hqplayer as hqp_router

    def step(delta):
        return hqp_router.nudge_volume(hqp_router.VolumeRequest(delta=delta))
    fake.replies["VolumeRange"] = '<VolumeRange enabled="1" adaptive="0" min="-60" max="0"/>'
    fake.replies["State"] = '<State volume="-0.5"/>'
    assert step(1) == {"volume": 0.0} and fake.commands[-1] == ("Volume", {"value": "0.0"})
    fake.replies["State"] = '<State volume="-59.5"/>'
    assert step(-1) == {"volume": -60.0} and fake.commands[-1] == ("Volume", {"value": "-60.0"})
    # Direct SDM holds the volume: said so, and nothing is sent
    fake.replies["VolumeRange"] = '<VolumeRange enabled="0" adaptive="0" min="-60" max="0"/>'
    with pytest.raises(HTTPException) as e:
        step(1)
    assert e.value.status_code == 409 and e.value.detail == VOLUME_FIXED
    assert fake.commands[-1][0] == "VolumeRange"
    # a refusal the range did not foretell: HQPlayer's words, never a step it did not take
    fake.replies["VolumeRange"] = '<VolumeRange enabled="1" adaptive="0" min="-60" max="0"/>'
    fake.replies["Volume"] = '<Volume result="Error">not authenticated and no internet access</Volume>'
    with pytest.raises(HTTPException) as e:
        step(1)
    assert e.value.status_code == 503
    assert e.value.detail == "not authenticated and no internet access"
    # no range given: a step never goes above 0 dB
    del fake.replies["Volume"]
    fake.replies["VolumeRange"] = '<VolumeRange result="Error" />'
    fake.replies["State"] = '<State volume="-0.5"/>'
    assert step(40) == {"volume": 0.0} and fake.commands[-1] == ("Volume", {"value": "0.0"})


def test_the_assistant_says_the_volume_is_fixed_instead_of_increased(fake, monkeypatch):
    from routers import settings as settings_router
    from tools import definitions
    monkeypatch.setattr(settings_router, "_read",
                        lambda key: {"output.type": "hqplayer"}.get(key))
    monkeypatch.setattr(definitions, "_hqp_client", None)
    fake.replies["VolumeUp"] = '<VolumeUp result="Error" />'
    fake.replies["VolumeRange"] = '<VolumeRange enabled="0" adaptive="0" min="-60" max="0"/>'
    assert definitions._h_hqplayer_volume_up() == f"Failed to change volume: {VOLUME_FIXED}"
    fake.replies["Pause"] = '<Pause result="Error">not authenticated and no internet access</Pause>'
    assert definitions._h_hqplayer_pause() == \
        "Failed to pause: not authenticated and no internet access"
    definitions._hqp_client.disconnect()


def test_a_state_the_sdk_does_not_name_keeps_the_status(fake):
    # Embedded 6.2.3 answered state="5" while it started a play after a reset
    fake.replies["Status"] = ('<Status state="5" track="1" position="0" length="120" '
                              'volume="-3" process_speed="0" input_fill="0" output_fill="0"/>')
    c = HQPlayerClient("127.0.0.1", fake.port)
    assert c.connect()
    try:
        st = c.get_status()
        assert st is not None and st.state == 5 and st.state != PlaybackState.PLAYING
        assert hb.STATE_NAMES.get(st.state, "unknown") == "unknown"
    finally:
        c.disconnect()


def test_a_dropped_connection_is_a_connection_outcome(fake):
    HQPlayerClient._error_ring.clear()
    c = HQPlayerClient("127.0.0.1", fake.port, timeout=2.0)
    assert c.connect()
    fake.close()
    assert c.get_status() is None
    e = HQPlayerClient.last_errors()[0]
    assert e["command"] == "<connection>" and e["during"] == "Status" and e["result"] == "lost"


def test_overlapping_intents_keep_their_own_commands(fake):
    """A Next pressed while a replace still runs: each intent's commands are
    its own steps — the first one's Stop is not the owner ending the second,
    and the first one finishing does not blind the second."""
    from hqplayer_client import CommandOutcome
    b = HqpBackend(emit=lambda *_: None, queue=CanonicalQueue())
    out = lambda cmd, result="OK": CommandOutcome(1.0, "h", 1, cmd, {}, result)  # noqa: E731
    a_open, b_open, a_done = threading.Event(), threading.Event(), threading.Event()
    seen = {}

    def first():
        with b._intent("replace", slot=1) as att:
            seen["a"] = att
            a_open.set()
            b_open.wait(5)
            b._on_outcome(out("Stop"))          # A's own Stop, after B took over
        a_done.set()

    def second():
        a_open.wait(5)
        with b._intent("next") as att:
            seen["b"] = att
            b_open.set()
            a_done.wait(5)
            b._on_outcome(out("Next"))          # B's step, after A has finished

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    a, nxt = seen["a"], seen["b"]
    assert [s["command"] for s in a.steps] == ["Stop"] and a.end == "superseded"
    assert [s["command"] for s in nxt.steps] == ["Next"] and nxt.t0 == 1.0
    assert nxt.end is None                       # not ended as the owner's
    b._on_outcome(out("Stop"))                   # a Stop from no intent at all is
    assert nxt.end == "owner"
