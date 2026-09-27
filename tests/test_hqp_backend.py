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
from hqplayer_client import PlaybackState, TrackStatus  # noqa: E402
from playback import hqp_backend as hb  # noqa: E402
from playback import queue as queue_mod  # noqa: E402
from playback.hqp_backend import HqpBackend  # noqa: E402
from playback.manager import PlaybackManager  # noqa: E402
from playback.queue import QueueItem  # noqa: E402
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
            if cmd == "Status":
                return (f'<Status state="{self.state}" track="{self.track}" '
                        f'position="0" length="0" volume="-3">'
                        f'<metadata artist="Fake artist" album="Fake album" '
                        f'song="Fake song" genre=""/></Status>')
            if cmd == "GetInfo":
                return ('<GetInfo name="fake" product="Signalyst HQPlayer Fake" '
                        'version="6" platform="Linux" engine="6.2.3"/>')
            if cmd == "PlaylistAdd":
                if attrs.get("clear") == "1":
                    self.playlist = []
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
    monkeypatch.setattr(settings, "hqplayer_file_access", "path")
    monkeypatch.setattr(settings, "hqplayer_library_root", None)
    monkeypatch.setattr(settings, "media_proxy_advertised_host", "127.0.0.1")
    monkeypatch.setattr(streaming_service, "_proxy",
                        MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"k"))
    hb.reset_all_clients()
    hb._hqp_unreachable_until = 0.0
    # The attach re-checks the HQPlayer library hash through the database;
    # these tests have none.
    import hqp_library
    monkeypatch.setattr(hqp_library, "request_sync", lambda host, port: None)
    yield f
    hb.reset_all_clients()
    f.close()


def _item(path, mfid, fmt="FLAC", **span):
    return QueueItem(track_id=f"track-{mfid}", media_file_id=mfid,
                     source={"kind": "file", "path": path, "format": fmt, **span},
                     title=f"Song {mfid}", artist="Artist", album="Album")


def _held(path, n, port):
    """A file the HQPlayer holds in its own library (hqp_library.sync)."""
    return QueueItem(track_id=f"held-{n}", media_file_id=None,
                     source={"kind": "hqp", "path": path, "format": "FLAC",
                             "host": "127.0.0.1", "port": port},
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


def test_attach_mirrors_under_the_library_root(fake, monkeypatch):
    monkeypatch.setattr(settings, "hqplayer_library_root", "/mnt/music")
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1)])
    b = _attach(mgr)
    try:
        assert fake.playlist == ["file:///mnt/music/A/01.flac"]
    finally:
        b.shutdown()


def test_attach_streams_in_stream_mode(fake, monkeypatch):
    monkeypatch.setattr(settings, "hqplayer_file_access", "stream")
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1)])
    b = _attach(mgr)
    try:
        tok = streaming_service.get_proxy().file_token("/music/A/01.flac")
        assert fake.playlist == [f"http://127.0.0.1:0/file/{tok}"]
    finally:
        b.shutdown()


def test_busy_attach_registers_tokens_without_touching_the_playlist(fake, monkeypatch):
    monkeypatch.setattr(settings, "hqplayer_file_access", "stream")
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
    monkeypatch.setattr(settings, "hqplayer_file_access", "stream")
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1)])
    b = HqpBackend(emit=mgr._on_backend_status, queue=mgr.queue)
    st = TrackStatus(state=PlaybackState.PLAYING, track_index=1, track_id="",
                     position=5.0, length=200.0, volume=-3.0,
                     artist="HTTP stream", album="", song="HTTP stream")
    out = b._status_of(st)
    assert out.queue_index == 1 and "artist" not in out.extra and "song" not in out.extra
    monkeypatch.setattr(settings, "hqplayer_file_access", "path")
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
                       _held("/media/FLASH/Bonobo [FLAC]/01. Intro.flac", 1, fake.port)])
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

    def held(paths, host, port):
        assert paths == ["/media/FLASH/Bonobo [FLAC]/01. Intro.flac"]
        assert (host, port) == ("127.0.0.1", fake.port)
        return {paths[0]: _held(paths[0], 7, port)}
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
