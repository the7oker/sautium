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
from hqplayer_client import HQPlayerClient, PlaybackState, TrackStatus  # noqa: E402
from playback import hqp_backend as hb  # noqa: E402
from playback import hqp_diagnostics as diag  # noqa: E402
from playback import queue as queue_mod  # noqa: E402
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
        self._lock = threading.Lock()
        self._conns: list[socket.socket] = []
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
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
                        reply = fake._dispatch(m.group(1), attrs)
                        try:
                            self.request.sendall(reply.encode() + b"\n")
                        except OSError:
                            return
                        last = m.end()
                    buf = buf[last:]

        self._handler = Handler
        self.port = 0
        self._serve()

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
                if self.state == int(PlaybackState.PLAYING):
                    self.position += 1.0
                return (f'<Status state="{self.state}" track="{self.track}" '
                        f'position="{self.position}" length="300" volume="-3" '
                        f'tracks_total="{len(self.playlist)}" process_speed="1.6" '
                        f'input_fill="0.9" output_fill="0.9" active_mode="PCM" '
                        f'active_filter="poly-sinc-gauss-long" active_shaper="none" '
                        f'active_rate="705600">'
                        f'<metadata artist="Fake artist" album="Fake album" '
                        f'song="Fake song" genre=""/></Status>')
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
        """Powered off: every connection dropped, the port refuses."""
        self.restart()
        self._srv.shutdown()
        self._srv.server_close()

    def come_back(self) -> None:
        """Booted again on the same port — playlist and transport empty."""
        self._serve()


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
    # ...and its endpoint row: this HQPlayer's library is endpoint 7 here.
    monkeypatch.setattr(hqp_library, "endpoint_by_address", lambda host, port: {"id": 7, "name": "fake"})
    # The output switch re-reads every slot's copies from the database
    # (playback.substitute); here every item opens as queued.
    from playback import substitute
    monkeypatch.setattr(substitute, "native_plays",
                        lambda items, output_id, endpoint_id: [None] * len(items))
    yield f
    hb.reset_all_clients()
    f.close()


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
    st = TrackStatus(state=PlaybackState.PLAYING, track_index=1, track_id="",
                     position=5.0, length=200.0, volume=-3.0,
                     artist="HTTP stream", album="", song="HTTP stream")
    out = b._status_of(st)
    assert out.queue_index == 1 and "artist" not in out.extra and "song" not in out.extra
    monkeypatch.setattr(hb, "_stream_mode", lambda: False)
    out = b._status_of(st)
    assert out.extra["artist"] == "HTTP stream"
    b._drift = True
    out = b._status_of(st)
    assert out.queue_index == 0 and out.extra["source"] == "external"


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
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1), _item("E:/Music/A/02.flac", 2)])
    b = _attach(mgr)
    assert len(fake.playlist) == 2

    fake.close()
    # Not merely a failed tick: the first one only finds the socket dead and
    # keeps the client object, which a same-tick socket comparison still
    # catches on the return. The shape that fooled it live is a failed
    # CONNECT — the client reset to None while HQPlayer stays away.
    assert _wait(lambda: b._failures >= 1 and hb._hqp_status_client is None,
                 timeout=15.0), "the outage never reset the status client"
    fake.come_back()
    hb._hqp_unreachable_until = 0.0      # the breaker's wall clock is not under test
    b.poke()
    assert _wait(lambda: not b.healthy(), timeout=15.0), "the return never flagged the lost mirror"
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
