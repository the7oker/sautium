"""The HQPlayer output against a fake control port: the queue mirrored in the
URI form each file-access mode dictates, a running playlist adopted back into
queue items, drift read through HQPlayer's escapes, and the re-mirror after
HQPlayer restarts (a trial-mode Embedded stops every 30 minutes)."""

import itertools
import re
import socket
import socketserver
import sys
import threading
import time
import traceback
import types
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
from playback import hqp_link as hl  # noqa: E402
from playback.base import PlaybackStatus  # noqa: E402
from playback import queue as queue_mod  # noqa: E402
from playback import tracker  # noqa: E402
from playback.hqp_backend import HqpBackend  # noqa: E402
from playback.manager import PlaybackManager  # noqa: E402
from playback.queue import CanonicalQueue, QueueItem  # noqa: E402
from streaming import service as streaming_service  # noqa: E402
from streaming.proxy import MediaProxy  # noqa: E402

_CMD_RE = re.compile(r'<(\w+)((?:\s+[\w:]+="[^"]*")*)\s*/>')
# What only asks: answered after a build, it changes nothing
_QUESTIONS = frozenset({"Status", "State", "GetInfo", "PlaylistGet", "VolumeRange", "GetModes",
                        "GetFilters", "GetShapers", "GetRates", "MatrixListProfiles",
                        "MatrixGetProfile"})
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
        # Further <Status> attributes, sent as they stand — HQPlayer 6's
        # apod, output_delay and their kin.
        self.status_extra: dict[str, str] = {}
        self.mute = False
        self._down = False
        # Until when it answers nothing, on any connection — busy building a
        # setting, or its CPU taken by one it cannot keep up with — and then
        # carries out what it was sent meanwhile (silence()).
        self._silent_until = 0.0
        self.silences: list[tuple] = []
        # The next command of a name that sets off a build (build()).
        self._builds: dict[str, tuple] = {}
        # Seconds a command of a name takes HQPlayer's one control thread to
        # carry out, everything after it waiting its turn
        self.service: dict[str, float] = {}
        # (time.time(), command) as each command ARRIVES — `commands` logs
        # them as they are carried out, after any silence, in the order they
        # arrived on whichever connection: HQPlayer's one control thread
        # works through what it was sent meanwhile.
        self.arrivals: list[tuple] = []
        self._tickets = itertools.count()
        self._served = 0
        self._order = threading.Condition()
        self._lock = threading.Lock()
        self._conns: list[socket.socket] = []
        # Bumped by every restart: what a dropped connection had queued into
        # the silence is gone with it, never carried out after the restart
        self._epoch = 0
        # (time.time(), connection, "in" | "out" | "close", command): what
        # each connection brought and was answered — max_outstanding reads it
        self.wire: list[tuple] = []
        self._conn_ids = itertools.count(1)
        # Told of each wire event as it is logged, under the lock — the
        # scenario runner's one event log
        self.on_wire = None
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                if fake.mute or fake._down:
                    return              # a trial-stopped Embedded, or a box going down: dropped at once
                with fake._lock:
                    fake._conns.append(self.request)
                    epoch = fake._epoch
                    conn = next(fake._conn_ids)
                try:
                    self._serve(conn, epoch)
                finally:
                    with fake._lock:
                        fake._log_wire(conn, "close", None)

            def _serve(self, conn, epoch):
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
                        with fake._lock:
                            fake.arrivals.append((time.time(), m.group(1)))
                            fake._log_wire(conn, "in", m.group(1))
                            built = fake._builds.pop(m.group(1), None)
                            ticket = next(fake._tickets)
                        if built is not None and built[1] == "last":
                            fake.silence(built[0])      # its own answer waits out the build
                        with fake._order:
                            while fake._served != ticket:
                                fake._order.wait()
                            while (left := fake._silent_until - time.monotonic()) > 0:
                                fake._order.wait(left)
                        try:
                            if fake._epoch != epoch:
                                return      # dropped by a restart: never carried out
                            time.sleep(fake.service.get(m.group(1), 0.0))
                            reply = fake._dispatch(m.group(1), attrs)
                            if built is not None and built[2]:
                                with fake._lock:
                                    fake.state = int(PlaybackState.STOPPED)
                            if reply is None:
                                return      # gone mid-conversation: the connection drops unanswered
                            # Logged before it goes: a client that reads it
                            # can ask again before this thread runs on
                            with fake._lock:
                                fake._log_wire(conn, "out", m.group(1))
                            try:
                                self.request.sendall(reply.encode() + b"\n")
                            except OSError:
                                return
                            if built is not None and built[1] == "first":
                                fake.silence(built[0])      # answered, then silent while it builds
                        finally:
                            with fake._order:
                                fake._served += 1
                                fake._order.notify_all()
                        last = m.end()
                    buf = buf[last:]

        self._handler = Handler
        self.port = 0
        self._serve()

    def silence(self, seconds: float) -> None:
        self._silent_until = time.monotonic() + seconds
        now = time.time()
        self.silences.append((now, now + seconds))

    def _log_wire(self, conn: int, what: str, command) -> None:
        """Called under the lock."""
        self.wire.append((time.time(), conn, what, command))
        if self.on_wire is not None:
            self.on_wire(self, conn, what, command)

    def max_outstanding(self) -> int:
        """The most requests outstanding at once, over every connection — one
        is outstanding from its arrival until it is answered or its
        connection closes. Read in the order the events were logged (under
        the lock): the wall clock jitters by microseconds between reads."""
        with self._lock:
            wire = list(self.wire)
        open_by: dict = {}
        most = 0
        for _, conn, what, _ in wire:
            if what == "in":
                open_by[conn] = open_by.get(conn, 0) + 1
            elif what == "out":
                open_by[conn] = max(open_by.get(conn, 0) - 1, 0)
            else:
                open_by.pop(conn, None)
            most = max(most, sum(open_by.values()))
        return most

    def sent_into_silence(self) -> list:
        """What arrived while it answered nothing, its questions aside — the
        commands a client sent into a build, to be carried out after it."""
        return [c for ts, c in self.arrivals
                if c not in _QUESTIONS and any(a <= ts < b for a, b in self.silences)]

    def build(self, command: str, seconds: float, *, answer: str = "first",
              stops: bool = False) -> None:
        """The next `command` sets off a build of `seconds`, its control port
        silent on every connection: answered first and silent after
        (Desktop 6.2.4), or its own answer held until the build is done
        ("last", as 6.2.3 was read). `stops`: HQPlayer is stopped once it is
        done (6.2.3 on Windows), else it plays on (6.2.4)."""
        with self._lock:
            self._builds[command] = (seconds, answer, stops)

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
                extra = "".join(f' {k}="{v}"' for k, v in self.status_extra.items())
                return (f'<Status state="{self.state}" track="{track}" '
                        f'position="{self.position}" length="{length}" volume="-3"{clips} '
                        f'tracks_total="{len(self.playlist)}" process_speed="1.6" '
                        f'input_fill="0.9" output_fill="0.9" active_mode="PCM" '
                        f'active_filter="poly-sinc-gauss-long" active_shaper="none" '
                        f'active_rate="705600"{extra}>'
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
                # The entry it reads stays, everything around it goes
                busy = self.state in (int(PlaybackState.PLAYING), int(PlaybackState.PAUSED))
                if busy and 1 <= self.track <= len(self.playlist):
                    self.playlist, self.track = [self.playlist[self.track - 1]], 1
                else:
                    self.playlist, self.track = [], 0
                return '<PlaylistClear result="OK"/>'
            if cmd == "PlaylistRemove":
                idx = int(attrs["index"])
                if 1 <= idx <= len(self.playlist):
                    del self.playlist[idx - 1]
                    if idx < self.track:
                        self.track -= 1
                    elif idx == self.track:
                        # the entry it read is gone: it stops there
                        self.state = int(PlaybackState.STOPPED)
                        self.track = min(self.track, len(self.playlist))
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
                if self.state == int(PlaybackState.PLAYING):
                    self.state = int(PlaybackState.PAUSED)
                return '<Pause result="OK"/>'
            if cmd == "SelectTrack":
                self.track = int(attrs["index"])
                self.position = 0.0
                if self.state == int(PlaybackState.STOPPED) and self.playlist:
                    # A stopped HQPlayer plays the slot it is given (its log:
                    # "GoTo 1", then "Play (1/0)"; Desktop 6.2.4).
                    self.state = int(PlaybackState.PLAYING)
                    self._starting = self.start_polls
                return '<SelectTrack result="OK"/>'
            return f'<{cmd} result="OK"/>'

    def restart(self) -> None:
        """What a trial-mode Embedded does every 30 min: every connection is
        dropped, the playlist, the transport and a build in progress are
        gone, the port is back."""
        with self._lock:
            conns, self._conns = self._conns, []
            self.playlist = []
            self.state = int(PlaybackState.STOPPED)
            self.track = 0
            self._builds.clear()
            self._epoch += 1
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()
        with self._order:
            self._silent_until = 0.0
            self._order.notify_all()

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
    _fresh_links()
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
    monkeypatch.setattr(hqp_benchmark, "recover", lambda endpoint_id, link: None)
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
    _fresh_links()
    f.close()
    with diag._lock:
        diag._ledger.clear()
        diag._by_slot.clear()


def test_a_queue_handed_to_an_hqplayer_that_does_not_answer_gives_up_at_once(fake, monkeypatch,
                                                                              caplog):
    monkeypatch.setattr(hl, "COMMAND_WAIT_S", 0.3)
    fake.silence(30)
    t = time.monotonic()
    link = _link()
    with link.command():
        added, unanswered = hb._add_uris_with_retry(
            link, [f"http://127.0.0.1:1/file/t{i}" for i in range(6)], clear_first=True)
    # one track's two tries, not six tracks' — and in what it is, not a refusal
    assert added == 0 and unanswered and time.monotonic() - t < 3
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


def _link() -> hl.HqpLink:
    """The selected HQPlayer's link — the fake's."""
    return hl.selected()


def _fresh_links() -> None:
    """Every link let go and the registry emptied: a test starts with none,
    and leaves no socket or waiter behind."""
    with hl._registry_lock:
        links = list(hl._links.values())
        hl._links.clear()
        hl._closed_why = None
        hl._chosen = None
    for ln in links:
        ln.close("the test is over")


def _limit(monkeypatch, seconds: float) -> None:
    """The build limit for this test — the live link's command waits too,
    which a link reads when it is made."""
    monkeypatch.setattr(hl, "BUILD_LIMIT_S", seconds)
    _link().client.waits.update(hl._command_waits())


def _stacks(threads) -> str:
    """What each thread still alive is doing — the message of a test that
    waited for it in vain."""
    frames = sys._current_frames()
    return "\n".join(f"{t.name}:\n" + "".join(traceback.format_stack(frames[t.ident]))
                     for t in threads if t.ident in frames)


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
    # ...and the foreign one is held by no HQPlayer library either.
    monkeypatch.setattr(queue_mod, "items_for_hqp_paths", lambda paths, endpoint_id: {})
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
    monkeypatch.setattr(hl, "CIRCUIT_COOLDOWN_S", 0.0)
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1), _item("E:/Music/A/02.flac", 2)])
    b = _attach(mgr)
    try:
        assert len(fake.playlist) == 2

        fake.close()
        # Not merely a failed tick: the shape that fooled it live is a failed
        # CONNECT — no connection at all while HQPlayer stays away.
        assert _wait(lambda: b._failures >= 1 and _link().client.socket is None,
                     timeout=15.0), "the outage never dropped the connection"
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
        # A poller left running reads the NEXT test's fake.
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
        with _link().asking() as c:
            playlist = c.get_playlist()
        assert playlist[1]["uri"] == "file:///media/FLASH/Bonobo %5BFLAC%5D/01. Intro.flac"
        assert b._check_drift(playlist) is False
        fake.playlist[1] = "file:///media/FLASH/Other/01.flac"
        with _link().asking() as c:
            playlist = c.get_playlist()
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
    assert b._spot_at() == (2, 0) and b.healthy()


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
    b.queue_start()                     # the manager's, once it let the queue lock go
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
    assert fake.state == int(PlaybackState.STOPPED) and b._spot_at()[0] == 2


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
    monkeypatch.setattr(hqp_benchmark, "recover",
                        lambda endpoint_id, link: calls.append(endpoint_id))
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
    monkeypatch.setattr(hqp_benchmark, "recover",
                        lambda endpoint_id, link: calls.append(endpoint_id))
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
    link = types.SimpleNamespace(client=Stub(), reset=lambda: None, attached=None)
    monkeypatch.setattr(hb, "_refusal", lambda hqp: ("Error", "cannot open it"))
    checked = []
    monkeypatch.setattr(hb, "_uri_in_playlist",
                        lambda hqp, uri: checked.append(uri) or uri == "b")
    monkeypatch.setattr(hb, "_stream_mode", lambda: False)
    woke = []
    monkeypatch.setattr(hb, "_notify_notices", lambda: woke.append(True))
    assert hb._add_uris_with_retry(link, ["a", "c", "b"]) == (1, True)
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
        traced.queue_start()             # and the start, once it let the queue lock go
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
    assert b.queue_clear_after_current(lambda: None) is False
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
        assert mgr.clear_for_radio(lambda: None) == gen
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
    from routers import player as player_router
    from routers import settings as settings_router
    from tools import definitions
    monkeypatch.setattr(settings_router, "_read",
                        lambda key: {"output.type": "hqplayer"}.get(key))
    mgr = _album(1)
    monkeypatch.setattr(player_router, "manager", mgr)
    b = _attach(mgr)
    try:
        fake.replies["VolumeUp"] = '<VolumeUp result="Error" />'
        fake.replies["VolumeRange"] = '<VolumeRange enabled="0" adaptive="0" min="-60" max="0"/>'
        assert definitions._h_hqplayer_volume_up() == f"Failed to change volume: {VOLUME_FIXED}"
        fake.replies["Pause"] = '<Pause result="Error">not authenticated and no internet access</Pause>'
        assert definitions._h_hqplayer_pause() == \
            "Failed to pause: not authenticated and no internet access"
    finally:
        b.shutdown()


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


# -- HQPlayer 6's readings, its lists and their descriptions (hqplayer_client) ----------

_LISTS_624 = Path(__file__).parent / "fixtures" / "hqp_lists" / "desktop_6.2.4.json"


@pytest.fixture
def client(fake):
    c = HQPlayerClient("127.0.0.1", fake.port, timeout=2.0)
    assert c.connect()
    yield c
    c.disconnect()


def test_adaptive_volume_is_switched_and_a_refusal_is_said(fake, client):
    assert client.set_adaptive_volume(True)
    assert fake.commands[-1] == ("SetAdaptiveVolume", {"value": "1"})
    assert client.set_adaptive_volume(False)
    assert fake.commands[-1] == ("SetAdaptiveVolume", {"value": "0"})
    fake.replies["SetAdaptiveVolume"] = ('<SetAdaptiveVolume result="Error">not authenticated '
                                         'and no internet access</SetAdaptiveVolume>')
    assert not client.set_adaptive_volume(True)
    assert client.refusal() == "not authenticated and no internet access"


def test_hqplayer_6s_status_readings_are_typed_and_none_where_a_build_has_none(fake, client):
    import json
    raw = json.loads(_LISTS_624.read_text())["raw"]
    fake.replies["Status"] = raw["status_playing"]          # Desktop 6.2.4, PCM sinc-MGa at 16×
    st = client.get_status()
    assert (st.apod, st.active_bits, st.active_channels) == (0, 32, 2)
    assert st.output_delay == pytest.approx(3.229092)       # 3229092 µs
    assert (st.correction, st.filter_20k, st.filter_junk) == (False, False, 0)
    assert st.display_position == pytest.approx(5.0155102040816324)
    del fake.replies["Status"]                               # a build that reports none of them
    st = client.get_status()
    assert [getattr(st, k) for k in ("apod", "active_bits", "active_channels", "output_delay",
                                     "correction", "filter_20k", "filter_junk",
                                     "display_position")] == [None] * 8


def test_hqplayer_6s_readings_reach_the_status_while_it_gives_them(fake, listens):
    mgr = PlaybackManager()
    mgr.queue.replace([_item("E:/Music/A/01.flac", 1)])
    b = _attach(mgr)
    try:
        with fake._lock:
            fake.state, fake.track = int(PlaybackState.PLAYING), 1
        assert _wait(lambda: mgr._latest_status.get("state") == "playing")
        assert "apod" not in mgr._latest_status and "output_delay" not in mgr._latest_status
        with fake._lock:
            fake.status_extra = {"apod": "4", "active_bits": "1", "active_channels": "2",
                                 "output_delay": "1743000", "correction": "1", "filter_20k": "0",
                                 "filter_junk": "2", "display_position": "12.5"}
        assert _wait(lambda: mgr._latest_status.get("apod") == 4)
        s = mgr._latest_status
        assert (s["active_bits"], s["active_channels"], s["output_delay"]) == (1, 2, 1.743)
        assert (s["correction"], s["filter_20k"], s["filter_junk"]) == (True, False, 2)
        assert s["display_position"] == 12.5
    finally:
        b.shutdown()


def test_hqplayer_6s_state_switches_are_read_and_none_where_a_build_has_none(fake, client):
    import json
    fake.replies["State"] = json.loads(_LISTS_624.read_text())["raw"]["state"]
    st = client.get_state()
    assert (st["filter_20k"], st["filter_junk"], st["random"], st["repeat"]) == (False, 0, False, 0)
    assert (st["mode"], st["filter1x"], st["filterNx"], st["shaper"]) == (2, 48, 51, 24)
    fake.replies["State"] = '<State mode="1" filter="3" shaper="1" volume="-3"/>'
    st = client.get_state()
    assert (st["filter_20k"], st["filter_junk"], st["random"], st["repeat"]) == (None,) * 4


_LIST_GETTERS = [
    ("get_modes", "GetModes", '<ModesItem index="1" name="PCM" value="0"/>',
     [{"index": 1, "name": "PCM", "value": 0}]),
    ("get_rates", "GetRates", '<RatesItem index="1" rate="705600"/>', [{"index": 1, "rate": 705600}]),
    ("get_filters", "GetFilters",
     '<FiltersItem index="57" name="sinc-MGa" value="20" arg="0" '
     'description="4/5 transients, timbre, space ⥮ 2^x up"/>',
     [{"index": 57, "name": "sinc-MGa", "value": 20, "arg": 0,
       "description": "4/5 transients, timbre, space ⥮ 2^x up",
       "parsed": {"rating": 4, "focus": ["space", "timbre", "transients"], "ratio": "2^x",
                  "up": True, "mode": "pcm"}}]),
    ("get_shapers", "GetShapers", '<ShapersItem index="24" name="ASDM7EC-fast" value="34" '
     'description="Gen7"/>',
     [{"index": 24, "name": "ASDM7EC-fast", "value": 34, "description": "Gen7",
       "parsed": {"generation": 7}}]),
    ("get_inputs", "GetInputs", '<InputsItem name="optical"/>', ["optical"]),
    ("matrix_list_profiles", "MatrixListProfiles", '<MatrixProfile name="room"/>', ["room"]),
]


@pytest.mark.parametrize("method,command,item,listed", _LIST_GETTERS,
                         ids=[g[0] for g in _LIST_GETTERS])
def test_a_list_hqplayer_did_not_give_is_none_never_empty(fake, client, method, command, item,
                                                          listed):
    get = getattr(client, method)
    fake.replies[command] = f"<{command}>{item}</{command}>"
    assert get() == listed
    fake.replies[command] = f"<{command}/>"                          # answered, and empty
    assert get() == []
    fake.replies[command] = f'<{command} result="Error">no output device</{command}>'
    assert get() is None and client.refusal() == "no output device"
    fake.replies[command] = '<Stop result="OK"/>'                    # another command's answer
    assert get() is None
    assert (client.last_error.result, client.last_error.message) == ("unparsed", "answered <Stop>")
    fake.replies[command] = None                                     # the connection drops unanswered
    assert get() is None and client.last_error.result == "lost"


def test_a_list_hqplayer_builds_past_the_wait_for_is_none(fake):
    c = HQPlayerClient("127.0.0.1", fake.port, timeout=0.3)
    assert c.connect()
    try:
        fake.silence(0.8)
        assert c.get_filters() is None and c.timed_out
    finally:
        c.disconnect()


# Every distinct filter description HQPlayer Desktop 6.2.4 gives (77 SDM and
# 67 PCM filters; 6.2.3 gives the same), read: (rating, focus, ratio, up, mode).
_DESCRIPTIONS = [
    ("1/5 ⥮ 1:1", (1, [], "1:1", False, "pcm")),
    ("2/5 ⥣ 2^x", (2, [], "2^x", False, "sdm")),
    ("3/5 ⥣ 2^x", (3, [], "2^x", False, "sdm")),
    ("4/5 space, timbre ⥣ 2^x", (4, ["space", "timbre"], "2^x", False, "sdm")),
    ("4/5 transients, timbre, space ⥣ 2^x", (4, ["space", "timbre", "transients"], "2^x", False, "sdm")),
    ("4/5 ⥣ 2^x", (4, [], "2^x", False, "sdm")),
    ("4/5 ⥮ 2^x", (4, [], "2^x", False, "pcm")),
    ("2/5 ⥣ Any", (2, [], "Any", False, "sdm")),
    ("2/5 ⥮ Any", (2, [], "Any", False, "pcm")),
    ("3/5 space, transients ⥮ Any", (3, ["space", "transients"], "Any", False, "pcm")),
    ("3/5 transients ⥣ Any", (3, ["transients"], "Any", False, "sdm")),
    ("3/5 transients ⥮ Any", (3, ["transients"], "Any", False, "pcm")),
    ("3/5 transients, space ⥣ Any", (3, ["space", "transients"], "Any", False, "sdm")),
    ("3/5 transients, timbre, space ⥣ Any", (3, ["space", "timbre", "transients"], "Any", False, "sdm")),
    ("3/5 transients, timbre, space ⥮ Any", (3, ["space", "timbre", "transients"], "Any", False, "pcm")),
    ("3/5 ⥣ Any", (3, [], "Any", False, "sdm")),
    ("3/5 ⥮ Any", (3, [], "Any", False, "pcm")),
    ("4/5 space ⥣ Any", (4, ["space"], "Any", False, "sdm")),
    ("4/5 space ⥮ Any", (4, ["space"], "Any", False, "pcm")),
    ("4/5 space, transients ⥣ Any", (4, ["space", "transients"], "Any", False, "sdm")),
    ("4/5 space, transients ⥮ Any", (4, ["space", "transients"], "Any", False, "pcm")),
    ("4/5 space, ⥮ Any", (4, ["space"], "Any", False, "pcm")),
    ("4/5 timbre ⥣ Any", (4, ["timbre"], "Any", False, "sdm")),
    ("4/5 timbre ⥮ Any", (4, ["timbre"], "Any", False, "pcm")),
    ("4/5 transients ⥣ Any", (4, ["transients"], "Any", False, "sdm")),
    ("4/5 transients ⥮ Any", (4, ["transients"], "Any", False, "pcm")),
    ("4/5 transients, timbre ⥣ Any", (4, ["timbre", "transients"], "Any", False, "sdm")),
    ("4/5 transients, timbre ⥮ Any", (4, ["timbre", "transients"], "Any", False, "pcm")),
    ("4/5 transients, timbre, space ⥣ Any", (4, ["space", "timbre", "transients"], "Any", False, "sdm")),
    ("4/5 transients, timbre, space ⥮ Any", (4, ["space", "timbre", "transients"], "Any", False, "pcm")),
    ("4/5 transients, ⥮ Any", (4, ["transients"], "Any", False, "pcm")),
    ("4/5 ⥣ Any", (4, [], "Any", False, "sdm")),
    ("4/5 ⥮ Any", (4, [], "Any", False, "pcm")),
    ("5/5 timbre ⥣ Any", (5, ["timbre"], "Any", False, "sdm")),
    ("5/5 timbre ⥮ Any", (5, ["timbre"], "Any", False, "pcm")),
    ("5/5 timbre, transients ⥣ Any", (5, ["timbre", "transients"], "Any", False, "sdm")),
    ("5/5 transients, timbre, space ⥣ Any", (5, ["space", "timbre", "transients"], "Any", False, "sdm")),
    ("5/5 transients, timbre, space ⥮ Any", (5, ["space", "timbre", "transients"], "Any", False, "pcm")),
    ("1/5 ⥣ Int", (1, [], "Int", False, "sdm")),
    ("2/5 transients ⥣ Int", (2, ["transients"], "Int", False, "sdm")),
    ("2/5 ⥣ Int", (2, [], "Int", False, "sdm")),
    ("2/5 ⥮ Int", (2, [], "Int", False, "pcm")),
    ("3/5 transients ⥣ Int", (3, ["transients"], "Int", False, "sdm")),
    ("3/5 ⥣ Int", (3, [], "Int", False, "sdm")),
    ("3/5 ⥮ Int", (3, [], "Int", False, "pcm")),
    ("4/5 timbre ⥣ Int", (4, ["timbre"], "Int", False, "sdm")),
    ("4/5 ⥣ Int", (4, [], "Int", False, "sdm")),
    ("4/5 ⥮ Int", (4, [], "Int", False, "pcm")),
    ("1/5 ⥮ Int up", (1, [], "Int", True, "pcm")),
    ("2/5 transients ⥮ Int up", (2, ["transients"], "Int", True, "pcm")),
    ("2/5 ⥮ 2^x up", (2, [], "2^x", True, "pcm")),
    ("2/5 ⥮ Any up", (2, [], "Any", True, "pcm")),
    ("3/5 transients, ⥮ Int up", (3, ["transients"], "Int", True, "pcm")),
    ("3/5 ⥮ 2^x up", (3, [], "2^x", True, "pcm")),
    ("3/5 ⥮ Any up", (3, [], "Any", True, "pcm")),
    ("4/5 space, timbre ⥮ 2^x up", (4, ["space", "timbre"], "2^x", True, "pcm")),
    ("4/5 timbre ⥮ Int up", (4, ["timbre"], "Int", True, "pcm")),
    ("4/5 transients ⥮ Int up", (4, ["transients"], "Int", True, "pcm")),
    ("4/5 transients, timbre, space ⥮ 2^x up", (4, ["space", "timbre", "transients"], "2^x", True, "pcm")),
    ("4/5 ⥮ 2^x up", (4, [], "2^x", True, "pcm")),
]


def test_every_filter_description_hqplayer_6_gives_is_read():
    import json
    from hqplayer_client import parse_filter_description
    lists = json.loads(_LISTS_624.read_text())
    given = {f["description"] for f in lists["filters"] + lists["pcm"]["filters"]}
    assert given == {d for d, _ in _DESCRIPTIONS}                 # the table is the whole set
    for text, (rating, focus, ratio, up, mode) in _DESCRIPTIONS:
        assert parse_filter_description(text) == {"rating": rating, "focus": focus, "ratio": ratio,
                                                   "up": up, "mode": mode}, text


def test_one_filter_described_in_both_modes_compares_on_rating_focus_and_ratio():
    from hqplayer_client import parse_filter_description as read
    # poly-sinc-short-lp: SDM and PCM order its focus words differently;
    # poly-sinc-long-lp: PCM keeps a trailing comma; sinc-L: only PCM says `up`.
    for sdm, pcm in (("3/5 transients, space ⥣ Any", "3/5 space, transients ⥮ Any"),
                     ("4/5 space ⥣ Any", "4/5 space, ⥮ Any"),
                     ("3/5 ⥣ 2^x", "3/5 ⥮ 2^x up")):
        a, b = read(sdm), read(pcm)
        assert (a["mode"], b["mode"]) == ("sdm", "pcm")
        assert [a[k] for k in ("rating", "focus", "ratio")] == [b[k] for k in ("rating", "focus", "ratio")]


def test_modulator_generations_are_read_and_a_dither_has_none():
    import json
    from hqplayer_client import parse_shaper_description
    lists = json.loads(_LISTS_624.read_text())
    assert ({s["description"] for s in lists["shapers"]}
            == {f"Gen{n}" for n in range(1, 9)})                  # SDM: the modulators
    for n in range(1, 9):
        assert parse_shaper_description(f"Gen{n}") == {"generation": n}
    assert {s["description"] for s in lists["pcm"]["shapers"]} == {""}   # PCM: the dithers
    assert parse_shaper_description("") is None


def test_a_description_in_a_shape_not_read_here_is_kept_raw_and_said_once(caplog, monkeypatch):
    import hqplayer_client
    from hqplayer_client import parse_filter_description, parse_shaper_description
    # Said once per process: this test starts from a process that said none
    monkeypatch.setattr(hqplayer_client, "_unread_descriptions", set())
    assert parse_filter_description("") is None                      # HQPlayer 5: no description
    for _ in range(3):
        assert parse_filter_description("5/5 warmth ⥮ Any") is None    # a focus word not known here
        assert parse_filter_description("five stars") is None
        assert parse_shaper_description("Gen 9") is None
    said = [r.getMessage() for r in caplog.records if "does not read" in r.getMessage()]
    assert len(said) == 3
    assert any("'5/5 warmth ⥮ Any'" in m for m in said)


def test_a_filter_hqplayer_describes_unreadably_keeps_its_description(fake, client):
    fake.replies["GetFilters"] = ('<GetFilters><FiltersItem index="1" name="new-filter" value="9" '
                                  'arg="0" description="6/5 ⥮ Any"/></GetFilters>')
    assert client.get_filters() == [{"index": 1, "name": "new-filter", "value": 9, "arg": 0,
                                     "description": "6/5 ⥮ Any", "parsed": None}]


def test_the_hqplayer_screen_is_told_which_lists_hqplayer_did_not_give(fake, monkeypatch):
    import hqp_library
    from routers import hqplayer as hqp_router
    from routers import settings as settings_router
    monkeypatch.setattr(hqp_library, "endpoint_by_address", lambda host, port: None)
    monkeypatch.setattr(settings_router, "_hqp_library_state", lambda: {})
    monkeypatch.setattr(hqp_router, "_load_favorites", lambda: [])
    fake.replies["GetModes"] = ('<GetModes><ModesItem index="0" name="[source]" value="-1"/>'
                                '</GetModes>')
    fake.replies["GetShapers"] = '<GetShapers result="Error"/>'
    fake.replies["MatrixListProfiles"] = None
    s = hqp_router.get_state()
    assert s["connected"]
    assert s["modes"] == [{"index": 0, "name": "[source]", "value": -1}]
    assert s["filters"] == [] and s["rates"] == []                  # answered, and empty
    assert s["shapers"] is None and s["matrix_profiles"] is None
    assert s["missing"] == ["shapers", "matrix_profiles"]
    # the marks of a queued file are not made from a list it did not give
    lists = {"modes": s["modes"], "rates": [], "filters": [], "shapers": None}
    assert hqp_router._headroom({"id": 7, "hqp_engine": "6.2.4"}, {"mode": 0}, None, lists) == {}


def test_the_benchmark_block_says_why_it_has_no_estimate(monkeypatch):
    # an absent estimate read as "[source]" whatever kept it away
    import hqp_library
    from playback import hqp_benchmark
    from routers import hqplayer as hqp_router
    monkeypatch.setattr(hqp_benchmark, "summary", lambda ep: {"job": {"running": False}, "last_run": None})
    monkeypatch.setattr(hqp_library, "local_cuda", lambda: (False, None))
    ep, state = {"id": 7, "hqp_engine": "6.2.4"}, {"mode": 2, "volume": -3.0}
    lists = {"modes": [{"index": 0, "name": "[source]", "value": -1},
                       {"index": 1, "name": "PCM", "value": 0},
                       {"index": 2, "name": "SDM (DSD)", "value": 1}],
             "rates": [], "filters": None, "shapers": []}

    def why(ep, state, lists):
        block = hqp_router._benchmark_block(ep, state, lists, None)
        assert block["estimate"] is None
        return block["no_estimate"], block["other_modes"]
    assert why(None, state, lists) == ("unregistered", [])
    assert why({"id": 7, "hqp_engine": None}, state, lists) == ("unregistered", [])
    assert why(ep, None, lists) == ("state", [])
    assert why(ep, state, lists) == ("lists", [])
    assert why(ep, {"mode": 0, "volume": -3.0}, {**lists, "filters": []}) == ("source", ["pcm", "sdm"])


def test_the_assistant_is_told_a_list_hqplayer_did_not_give(fake, monkeypatch):
    import assistant_queries as aq
    from routers import settings as settings_router
    from tools import definitions
    monkeypatch.setattr(settings_router, "_read", lambda key: "hqplayer" if key == "output.type" else None)
    fake.replies["GetFilters"] = '<GetFilters result="Error">no output device</GetFilters>'
    assert definitions._h_hqplayer_set_filter("sinc-MGa") == (
        "HQPlayer did not list its filters (no output device) — nothing was changed.")
    assert "SetFilter" not in [c for c, _ in fake.commands]
    text = definitions._h_hqplayer_get_settings()
    assert "Available filters: HQPlayer did not list them (no output device)." in text
    assert "Output modes: none." in text                            # answered, and empty
    lines: list = []
    aq.hqp_list(lines, "Sample rates", [{"index": 1, "rate": 705600}], None,
                lambda r: f"  [{r['index']}] {r['rate'] / 1000:.1f} kHz")
    assert lines == ["Sample rates (1):", "  [1] 705.6 kHz"]


def test_a_run_does_not_plan_on_a_list_hqplayer_did_not_give(fake):
    from playback import hqp_benchmark as bench
    fake.replies["GetShapers"] = '<GetShapers result="Error"/>'
    link = bench._Link(lambda s: True)
    assert link.connect()
    try:
        with pytest.raises(bench._Abort, match="^HQPlayer did not list its shapers$"):
            bench._enter_mode(link, 1, switched=False)
    finally:
        link.close()


def test_hqplayer_is_reached_by_address_by_default():
    from config import Settings
    from hqplayer_client import HQPlayerConnection
    assert HQPlayerClient().host == "127.0.0.1"
    assert HQPlayerConnection().client.host == "127.0.0.1"
    assert Settings.model_fields["hqplayer_host"].default == "127.0.0.1"


# -- SelectTrack starts a stopped HQPlayer ------------------------------------------------

def test_a_new_order_taken_while_stopped_plays_only_on_the_play_press(fake):
    from playback.base import ReorderPlan
    mgr = _album(3)
    b = _mirrored(mgr)
    fake.track = 2                      # stopped on slot 2: a build that keeps the index
    fake.commands.clear()
    items = mgr.queue.snapshot()
    plan = ReorderPlan(seamless=False, status_idx=2, new_status_idx=1, old_before=["track-1"],
                       new_before=[], old_after=["track-3"], new_after=["track-1", "track-3"],
                       order=["track-2", "track-1", "track-3"],
                       items_by_id={it.track_id: it for it in items}, position=0, resume=False)
    assert b.queue_reorder(plan)["interrupted"]
    mgr.queue.reorder_by_track_ids(plan.order)      # the manager's commit
    assert fake.playlist == ["file:///E:/Music/A/02.flac", "file:///E:/Music/A/01.flac",
                             "file:///E:/Music/A/03.flac"]
    assert "SelectTrack" not in [c for c, _ in fake.commands]
    assert fake.state == int(PlaybackState.STOPPED) and b._spot_at() == (1, 0)
    fake.commands.clear()
    assert b.play()
    assert [(c, a) for c, a in fake.commands if c in ("SelectTrack", "Play")][:2] == [
        ("SelectTrack", {"index": "1"}), ("Play", {})]
    assert (fake.state, fake.track) == (int(PlaybackState.PLAYING), 1)


# -- a DSP change waits out a build (hqp_backend.BuildWatch, change_dsp) -----------------
#
# Desktop answers nothing on its control port while it builds a setting. The
# watch's moment (BUILDING_AFTER_S) is shortened here: a build of a couple of
# seconds is what a long one is, with its silence, its answer and its end.

@pytest.fixture
def output(fake, listens, monkeypatch):
    """HQPlayer attached as THE output — the manager change_dsp reads — and
    playing slot 1 of three at second 42."""
    from playback import manager as manager_mod
    monkeypatch.setattr(hl.BuildWatch, "after", 0.3)
    mgr = _album(3)
    monkeypatch.setattr(manager_mod, "manager", mgr)
    b = _attach(mgr)
    with fake._lock:
        fake.state, fake.track, fake.position = int(PlaybackState.PLAYING), 1, 41.0
    assert _wait(lambda: mgr._latest_status.get("state") == "playing"
                 and mgr._latest_status.get("position", 0) >= 42)
    yield mgr, b
    b.shutdown()


def _seen(mgr, key):
    """Every value of `key` the manager's status carried from here on —
    every push, "disconnected" among them: the status observers never hear
    that one."""
    seen = []
    push = mgr._push_status

    def recorded(data):
        seen.append(data.get(key))
        push(data)
    mgr._push_status = recorded
    return seen


def test_a_change_answered_before_its_build_answers_building_and_sends_nothing_into_it(fake, output):
    mgr, b = output
    buildings = _seen(mgr, "building")
    fake.build("SetFilter", 2.0)                     # answered, then silent (6.2.4)
    t0 = time.monotonic()
    out = hb.change_dsp({"filter": 57, "filter1x": 57})
    assert time.monotonic() - t0 < 1.5               # at once, not after the build
    assert out["building"] is not None and out["ok"] is None
    assert out["applied"] == {"filter": 57, "filter1x": 57} and out["pending"] == []
    assert ("SetFilter", {"value": "57", "value1x": "57"}) in fake.commands
    assert _wait(lambda: mgr._latest_status.get("building") is not None)    # the poller says it
    with pytest.raises(hl.Building):
        b.pause()                                    # refused, never sent into the silence
    began = out["building"]["since"]
    assert _wait(lambda: "building" not in mgr._latest_status
                 and mgr._latest_status["state"] == "playing")
    during = [c for ts, c in fake.arrivals if ts >= began and c != "Status"]
    assert during == []                              # nothing but Status asks reached it
    assert "Pause" not in [c for c, _ in fake.commands]
    assert "SelectTrack" not in [c for c, _ in fake.commands]    # it played on: no resume
    assert any(x and x["silent"] for x in buildings)


def test_a_change_silent_itself_resumes_the_spot_when_hqplayer_comes_back_stopped(fake, output):
    mgr, b = output
    fake.build("SetFilter", 2.0, answer="last", stops=True)     # 6.2.3 on Windows
    out = hb.change_dsp({"filter": 57, "filter1x": 57})
    assert out["building"] is not None
    assert out["applied"] == {} and out["pending"] == ["filter"]
    assert _wait(lambda: any(c == "SelectTrack" for c, _ in fake.commands))
    start = [(c, a) for c, a in fake.commands if c in ("SelectTrack", "Seek", "Play")]
    assert start[0] == ("SelectTrack", {"index": "1"})
    assert start[1][0] == "Seek" and int(start[1][1]["position"]) >= 42
    assert start[2] == ("Play", {})
    assert _wait(lambda: mgr._latest_status["state"] == "playing")
    assert _link().build.last_change["applied"] == {"filter": 57, "filter1x": 57}


def test_the_knobs_after_a_build_follow_it_in_order(fake, output):
    mgr, b = output
    fake.build("SetFilter", 1.5)
    out = hb.change_dsp({"shaper": 2, "filter": 57, "filter1x": 57})
    assert out["building"] is not None and out["pending"] == ["shaper"]
    assert _wait(lambda: _link().build.last_change is not None)
    sent = [c for c, _ in fake.commands if c in ("SetFilter", "SetShaping", "Status")]
    i = sent.index("SetFilter")
    assert sent[i + 1] == "Status" and "SetShaping" in sent[i + 2:]
    assert _link().build.last_change["applied"] == {"filter": 57, "filter1x": 57, "shaper": 2}
    assert _link().build.last_change["failed"] == {}


def test_a_knob_refused_outright_is_reported_in_hqplayers_words(fake, output):
    fake.replies["SetShaping"] = '<SetShaping result="Error">Modulator needs 512x rate</SetShaping>'
    out = hb.change_dsp({"filter": 57, "filter1x": 57, "shaper": 30})
    assert out["building"] is None and out["ok"] is False
    assert out["applied"] == {"filter": 57, "filter1x": 57}
    assert out["failed"] == {"shaper": "Modulator needs 512x rate"}


def test_a_change_while_another_builds_is_refused(fake, output):
    from fastapi import HTTPException
    from routers import hqplayer as hqp_router
    fake.build("SetFilter", 2.0)
    assert hb.change_dsp({"filter": 57, "filter1x": 57})["building"] is not None
    with pytest.raises(HTTPException) as e:
        hqp_router.set_config(hqp_router.ConfigRequest(shaper=2))
    assert e.value.status_code == 409 and "building the filter" in e.value.detail
    with pytest.raises(HTTPException) as e:
        hqp_router.nudge_volume(hqp_router.VolumeRequest(delta=1))
    assert e.value.status_code == 409
    assert _wait(lambda: _link().build.state is None)


def _quick_polls(monkeypatch, threshold=2):
    """The status poller's misses come quickly: a 0.3 s read, and `threshold`
    of them say HQPlayer is away."""
    monkeypatch.setattr(hb, "QUESTION_WAIT_S", 0.3)
    monkeypatch.setattr(hb, "STATUS_FAILURE_THRESHOLD", threshold)
    monkeypatch.setattr(hl, "CIRCUIT_COOLDOWN_S", 0.0)


def test_past_the_limit_a_silent_hqplayer_is_an_unfinished_build_and_a_gone_one_disconnected(
        fake, output, monkeypatch):
    mgr, b = output
    _quick_polls(monkeypatch)
    _limit(monkeypatch, 1.0)
    states = _seen(mgr, "state")
    buildings = _seen(mgr, "building")
    fake.build("SetFilter", 60.0)
    assert hb.change_dsp({"filter": 57, "filter1x": 57})["building"] is not None
    # Asked again and again past the limit, silent each time — misses past
    # the threshold that are the build's, never "disconnected"
    assert _wait(lambda: sum(1 for x in buildings if x and x.get("unfinished")) >= 3, timeout=15)
    assert "disconnected" not in states
    fake.close()                                     # gone: the port refuses
    assert _wait(lambda: "disconnected" in states, timeout=15)


def test_a_play_from_stopped_waits_out_a_silent_build_longer_than_five_seconds(fake, output):
    mgr, b = output
    b.stop()
    assert _wait(lambda: mgr._latest_status["state"] == "stopped")
    buildings = _seen(mgr, "building")
    fake.build("Play", 6.0, answer="last")           # Play answers when the start is built
    t0 = time.monotonic()
    assert b.play()                                  # five seconds are no error
    assert time.monotonic() - t0 >= 6.0
    assert any(x and x["command"] == "Play" for x in buildings)
    assert _wait(lambda: mgr._latest_status["state"] == "playing"
                 and "building" not in mgr._latest_status)


def test_a_stop_silent_longer_than_five_seconds_is_no_build(fake, output):
    mgr, b = output
    buildings = _seen(mgr, "building")
    fake.build("Stop", 6.0, answer="last")
    assert b.stop()
    assert not any(buildings) and _link().build.state is None


def test_an_embedded_box_answering_through_its_build_is_said_to_build_and_refuses_nothing(
        fake, listens, monkeypatch):
    fake.replies["GetInfo"] = ('<GetInfo name="HQPlayerEmbedded" product="Signalyst HQPlayer '
                               'Embedded" version="6" platform="Linux" engine="6.2.3"/>')
    mgr = _album(1)
    b = _attach(mgr)
    try:
        fake.replies["Status"] = ('<Status state="2" track="1" position="0" length="300" '
                                  'volume="-3" process_speed="0" input_fill="0" output_fill="0" '
                                  'tracks_total="1"/>')
        assert _wait(lambda: (mgr._latest_status.get("building") or {}).get("silent") is False)
        assert b.pause()                             # it answers: nothing to refuse
        del fake.replies["Status"]
        with fake._lock:
            fake.state, fake.track = int(PlaybackState.PLAYING), 1
        assert _wait(lambda: "building" not in mgr._latest_status)
    finally:
        b.shutdown()


def test_a_reorder_while_paused_leaves_hqplayer_stopped_at_its_spot(fake, listens):
    from playback.base import ReorderPlan
    mgr = _album(3)
    b = _mirrored(mgr)
    with fake._lock:
        fake.state, fake.track, fake.position = int(PlaybackState.PAUSED), 2, 150.0
    fake.commands.clear()
    items = mgr.queue.snapshot()
    plan = ReorderPlan(seamless=False, status_idx=2, new_status_idx=1, old_before=["track-1"],
                       new_before=[], old_after=["track-3"], new_after=["track-1", "track-3"],
                       order=["track-2", "track-1", "track-3"],
                       items_by_id={it.track_id: it for it in items}, position=150, resume=False)
    b.queue_reorder(plan)
    mgr.queue.reorder_by_track_ids(plan.order)      # the manager's commit
    assert fake.state == int(PlaybackState.STOPPED)
    assert not [c for c, _ in fake.commands if c in ("SelectTrack", "Seek", "Play")]   # no sound
    stopped = TrackStatus(state=PlaybackState.STOPPED, track_index=0, track_id="",
                          position=0.0, length=0.0, volume=-3.0)
    assert b._status_of(stopped, (0, None, False)).extra["resume"] == {"index": 1, "position": 150}
    fake.commands.clear()
    assert b.play()
    assert [(c, a) for c, a in fake.commands if c in ("SelectTrack", "Seek", "Play")] == [
        ("SelectTrack", {"index": "1"}), ("Seek", {"position": "150"}), ("Play", {})]


def test_the_assistant_changes_the_filter_by_the_screens_path_and_says_it_builds(
        fake, output, monkeypatch):
    from routers import settings as settings_router
    from tools import definitions
    monkeypatch.setattr(settings_router, "_read",
                        lambda key: "hqplayer" if key == "output.type" else None)
    fake.replies["GetFilters"] = ('<GetFilters><FiltersItem index="57" name="sinc-MGa" value="20" '
                                  'arg="0" description="4/5 transients, timbre, space ⥮ 2^x up"/>'
                                  '</GetFilters>')
    fake.build("SetFilter", 1.5)
    text = definitions._h_hqplayer_set_filter("sinc-MGa")
    assert text.startswith("HQPlayer took the filter sinc-MGa and is building it")
    assert ("SetFilter", {"value": "57", "value1x": "57"}) in fake.commands     # both slots
    assert definitions._h_hqplayer_set_filter("sinc-MGa").startswith(
        "Error setting filter: HQPlayer is building the filter")
    assert _wait(lambda: _link().build.state is None)


def test_localhost_is_dialled_as_the_ipv4_loopback():
    from hqplayer_client import connect_address
    assert connect_address("localhost") == "127.0.0.1"
    assert connect_address(" LocalHost ") == "127.0.0.1"
    assert connect_address("192.0.2.7") == "192.0.2.7"
    assert connect_address("host.docker.internal") == "host.docker.internal"


def test_an_output_switch_during_a_build_is_at_once_and_stops_hqplayer_once_it_is_done(
        fake, output, monkeypatch):
    from routers import settings as settings_router
    mgr, b = output
    monkeypatch.setattr(settings_router, "_read", lambda key: {"output.type": "browser"}.get(key))
    fake.build("SetFilter", 3.0)
    began = hb.change_dsp({"filter": 57, "filter1x": 57})["building"]["since"]
    t0 = time.monotonic()
    mgr.activate(None)                               # the switch away, as the picker makes it
    assert time.monotonic() - t0 < 1.5
    assert fake.state == int(PlaybackState.PLAYING)  # nothing sent into the build
    assert _wait(lambda: fake.state == int(PlaybackState.STOPPED), timeout=10), \
        "HQPlayer played on, undriven, beside the new output"
    assert fake.sent_into_silence() == []
    assert [c for ts, c in fake.arrivals if ts >= began and c != "Status"] == ["Stop"]


def test_hqplayer_picked_again_before_its_build_ends_plays_on_unstopped(fake, output, monkeypatch):
    from routers import settings as settings_router
    mgr, b = output
    monkeypatch.setattr(settings_router, "_read", lambda key: {"output.type": "hqplayer"}.get(key))
    fake.build("SetFilter", 3.0)
    began = hb.change_dsp({"filter": 57, "filter1x": 57})["building"]["since"]
    mgr.activate(None)
    mgr.activate("hqplayer")                         # back before the build ends
    live = mgr.active
    try:
        assert live is not b
        assert _link().build.silent() is not None         # still watched
        assert _wait(lambda: _link().build.state is None)
        n = len(fake.commands)
        assert _wait(lambda: _statuses_after(fake, n))
        assert "Stop" not in [c for ts, c in fake.arrivals if ts >= began]
        assert fake.state == int(PlaybackState.PLAYING) and len(fake.playlist) == 3
        assert fake.sent_into_silence() == []
    finally:
        live.shutdown()


def test_a_session_queued_while_hqplayer_builds_is_refused_as_busy(fake, output, monkeypatch):
    from fastapi import HTTPException
    from routers import player as player_router
    mgr, b = output
    monkeypatch.setattr(player_router, "manager", mgr)
    monkeypatch.setattr(player_router, "_session_segments",
                        lambda session_id: ([("owned", [{"id": 9}])], 0))
    monkeypatch.setattr(queue_mod, "items_for_owned_rows",
                        lambda rows: [_item("E:/Music/B/09.flac", 9)])
    fake.build("SetFilter", 2.0)
    began = hb.change_dsp({"filter": 57, "filter1x": 57})["building"]["since"]
    with pytest.raises(HTTPException) as e:
        player_router.queue_session(player_router.QueueSessionRequest(session_id="s"))
    assert e.value.status_code == 409 and "building the filter" in e.value.detail
    assert len(mgr.queue) == 3                       # the queue as it was
    assert _wait(lambda: _link().build.state is None)
    assert "PlaylistAdd" not in [c for ts, c in fake.arrivals if ts >= began]


def test_the_convolver_goes_by_the_dsp_path_and_never_into_a_builds_silence(
        fake, output, monkeypatch):
    from fastapi import HTTPException
    from routers import hqplayer as hqp_router
    from routers import settings as settings_router
    from tools import definitions
    monkeypatch.setattr(settings_router, "_read",
                        lambda key: "hqplayer" if key == "output.type" else None)
    assert definitions._h_hqplayer_set_convolution(True) == "Convolution enabled."
    sent = [c for c, _ in fake.commands]
    i = sent.index("SetConvolution")
    assert fake.commands[i] == ("SetConvolution", {"value": "1"}) and sent[i + 1] == "Status"
    fake.build("SetFilter", 2.0)
    began = hb.change_dsp({"filter": 57, "filter1x": 57})["building"]["since"]
    assert definitions._h_hqplayer_set_convolution(False).startswith(
        "Error: HQPlayer is building the filter")
    with pytest.raises(HTTPException) as e:
        hqp_router.set_config(hqp_router.ConfigRequest(convolution=False))
    assert e.value.status_code == 409
    assert _wait(lambda: _link().build.state is None)
    assert "SetConvolution" not in [c for ts, c in fake.arrivals if ts >= began]


# -- background work meets a build (through_builds); the spot; the takeover ---------------
#
# People are refused while HQPlayer builds (409); work behind them — a filler,
# a re-bind HQPlayer follows, the Stop an output switch owes — waits for the
# build's end and goes on once it is done, or gives up with why.

def _routes_on(monkeypatch, mgr):
    from routers import player as player_router
    monkeypatch.setattr(player_router, "manager", mgr)
    return player_router


def test_a_filler_waits_out_a_build_and_adds_once_it_is_done(fake, output, monkeypatch):
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    fake.build("SetFilter", 2.0)
    began = hb.change_dsp({"filter": 57, "filter1x": 57})["building"]["since"]
    assert player_router._filler_append(_item("E:/Music/B/09.flac", 9), mgr.queue.generation)
    assert time.time() >= began + 1.0                # it waited
    assert fake.playlist[-1] == "file:///E:/Music/B/09.flac" and len(mgr.queue) == 4
    assert fake.sent_into_silence() == []


@pytest.mark.parametrize("ending,why", [("unfinished", "HQPlayer did not finish building the filter"),
                                        ("gone", "HQPlayer went away during the build")])
def test_a_filler_whose_build_does_not_end_with_an_answer_loses_nothing(fake, output, monkeypatch,
                                                                       caplog, ending, why):
    """I11: the build a filler waits out ran out, or HQPlayer went away
    during it: what the filler took goes into the queue alone, the mirror
    lost — and the HQPlayer that answers again gets the whole queue."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    _limit(monkeypatch, 1.5)
    waiting = threading.Event()
    wait_out = hl.BuildWatch.wait_out
    monkeypatch.setattr(hl.BuildWatch, "wait_out",
                        lambda self, until: (waiting.set(), wait_out(self, until))[1])
    fake.build("SetFilter", 4.0 if ending == "unfinished" else 60.0)
    hb.change_dsp({"filter": 57, "filter1x": 57})
    album = [_item(f"E:/Music/B/0{i}.flac", 10 + i) for i in range(1, 4)]
    t = threading.Thread(target=lambda: player_router._owned_filler(album, mgr.queue.generation),
                         daemon=True)
    t.start()
    assert waiting.wait(5)
    if ending == "gone":
        fake.close()
    t.join(10)
    assert [it.track_id for it in mgr.queue.snapshot()] == [
        "track-1", "track-2", "track-3", "track-11", "track-12", "track-13"]
    assert why in caplog.text and "go into the queue alone" in caplog.text
    if ending == "gone":
        fake.come_back()                             # restarted: stopped, its playlist empty
    # HQPlayer answers again (the build over, or back): it gets the whole queue
    want = [f"file:///E:/Music/{d}/0{i}.flac" for d, i in
            (("A", 1), ("A", 2), ("A", 3), ("B", 1), ("B", 2), ("B", 3))]
    assert _wait(lambda: (b.poke(), fake.playlist[-3:] == want[-3:])[1], timeout=15)
    assert _wait(lambda: b.healthy(), timeout=10)
    if ending == "gone":
        assert fake.playlist == want


def test_a_radio_start_while_hqplayer_builds_is_busy_and_splits_no_history(fake, output, monkeypatch):
    from fastapi import HTTPException
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    monkeypatch.setattr(player_router, "_db_query_one", lambda sql, params: {"artist": "Artist"})
    rotated = []
    monkeypatch.setattr(player_router.sessions, "rotate_session",
                        lambda *a, **k: rotated.append(k))
    fake.build("SetFilter", 2.0)
    hb.change_dsp({"filter": 57, "filter1x": 57})
    with pytest.raises(HTTPException) as e:
        player_router.radio_start(player_router.RadioStartRequest(
            track_uuid="0b8f1a2e-1d2c-5e3f-9a4b-5c6d7e8f9a0b"))
    assert e.value.status_code == 409 and "building the filter" in e.value.detail
    assert rotated == [] and not mgr.radio_mode and len(mgr.queue) == 3
    assert _wait(lambda: _link().build.state is None)


def test_a_rebind_during_a_build_is_followed_once_hqplayer_answers(fake, output):
    mgr, b = output                                  # playing slot 1 of 3
    fake.build("SetFilter", 2.0)
    hb.change_dsp({"filter": 57, "filter1x": 57})
    _move(mgr, 3, "E:/Music/B/03.flac", 30)
    t0 = time.monotonic()
    b.queue_changed("rebind")                        # as rebind_files does, under the queue lock
    assert time.monotonic() - t0 < 1.0               # never waited there
    assert _wait(lambda: fake.playlist[2:] == ["file:///E:/Music/B/03.flac"]), \
        "HQPlayer's playlist never followed the re-bind"
    assert b.healthy() and fake.state == int(PlaybackState.PLAYING)
    assert fake.sent_into_silence() == []


def test_a_restart_during_a_build_is_hqplayer_back_with_its_mirror_lost(fake, output, monkeypatch,
                                                                       caplog):
    """A restart mid-build is HQPlayer gone and back, never an unfinished
    build: its playlist read on its first answer differs, the mirror is lost
    — and, stopped, it takes the queue afresh."""
    mgr, b = output
    monkeypatch.setattr(hl, "CIRCUIT_COOLDOWN_S", 0.0)
    buildings = _seen(mgr, "building")
    fake.build("SetFilter", 60.0)
    hb.change_dsp({"filter": 57, "filter1x": 57})
    fake.restart()                                   # the held answer's connection breaks
    assert _wait(lambda: b._lost >= 1, timeout=12.0), "the restart never lost the mirror"
    assert "came back with a different playlist" in caplog.text
    assert _link().build.state is None
    assert not any(x and x.get("unfinished") for x in buildings)
    assert _wait(lambda: len(fake.playlist) == 3 and b.healthy(), timeout=10)


def test_hqplayer_gone_during_a_build_is_disconnected_not_an_unfinished_build(fake, output,
                                                                             monkeypatch):
    mgr, b = output
    _quick_polls(monkeypatch)
    states, buildings = _seen(mgr, "state"), _seen(mgr, "building")
    fake.build("SetFilter", 60.0)
    hb.change_dsp({"filter": 57, "filter1x": 57})
    fake.close()
    assert _wait(lambda: "disconnected" in states, timeout=15)
    assert not any(x and x.get("unfinished") for x in buildings)
    assert _link().build.state is None


def _paused_on_2_at_150(fake, mgr):
    with fake._lock:
        fake.state, fake.track, fake.position = int(PlaybackState.PAUSED), 2, 150.0
    assert _wait(lambda: mgr._latest_status.get("state") == "paused"
                 and mgr._latest_status.get("track_index") == 2)


def test_a_reorder_while_paused_keeps_its_slot_for_the_next_reorder_and_the_play_press(
        fake, listens, monkeypatch):
    mgr = _album(3)
    player_router = _routes_on(monkeypatch, mgr)
    b = _attach(mgr)
    try:
        _paused_on_2_at_150(fake, mgr)
        # Track 3 ahead of track 1: no seamless way — rebuilt, HQPlayer left stopped
        out = player_router.reorder(player_router.ReorderRequest(
            order=["track-3", "track-2", "track-1"]))
        assert out["interrupted"] and fake.state == int(PlaybackState.STOPPED)
        assert _wait(lambda: mgr._latest_status.get("state") == "stopped"
                     and mgr._latest_status.get("track_index") == 2)
        assert mgr._latest_status["resume"] == {"index": 2, "position": 150}
        # The next reorder anchors on that slot, and the spot follows it
        player_router.reorder(player_router.ReorderRequest(
            order=["track-2", "track-3", "track-1"]))
        assert _wait(lambda: mgr._latest_status.get("track_index") == 1
                     and mgr._latest_status.get("resume") == {"index": 1, "position": 150})
        fake.commands.clear()
        assert b.play()
        assert [(c, a) for c, a in fake.commands if c in ("SelectTrack", "Seek", "Play")] == [
            ("SelectTrack", {"index": "1"}), ("Seek", {"position": "150"}), ("Play", {})]
        assert fake.playlist[0] == "file:///E:/Music/A/02.flac"
    finally:
        b.shutdown()


def test_the_spot_stays_with_its_item_through_queue_edits_and_play_next_anchors_on_it(
        fake, listens, monkeypatch):
    """I10: the spot names a queue item — a "play next" goes right after it,
    an append behind it changes nothing, a removal before it moves its
    index, and the play press resumes it where it was."""
    mgr = _album(3)
    player_router = _routes_on(monkeypatch, mgr)
    b = _attach(mgr)
    try:
        _paused_on_2_at_150(fake, mgr)
        player_router.reorder(player_router.ReorderRequest(
            order=["track-3", "track-2", "track-1"]))
        assert _wait(lambda: mgr._latest_status.get("resume") == {"index": 2, "position": 150})
        mgr.append([_item("E:/Music/B/09.flac", 9)], "next")
        mgr.append([_item("E:/Music/B/08.flac", 8)])
        assert [it.track_id for it in mgr.queue.snapshot()] == [
            "track-3", "track-2", "track-9", "track-1", "track-8"]
        assert mgr.remove(1, "track-3")
        assert _wait(lambda: mgr._latest_status.get("resume") == {"index": 1, "position": 150})
        fake.commands.clear()
        assert b.play()
        assert [(c, a) for c, a in fake.commands if c in ("SelectTrack", "Seek", "Play")] == [
            ("SelectTrack", {"index": "1"}), ("Seek", {"position": "150"}), ("Play", {})]
        assert fake.playlist[0] == "file:///E:/Music/A/02.flac"
    finally:
        b.shutdown()


def test_a_play_refused_while_hqplayer_builds_keeps_the_spot(fake, listens, monkeypatch):
    from playback import manager as manager_mod
    from playback.base import ReorderPlan
    mgr = _album(3)
    monkeypatch.setattr(manager_mod, "manager", mgr)
    monkeypatch.setattr(hl.BuildWatch, "after", 0.3)
    b = _mirrored(mgr)
    with fake._lock:
        fake.state, fake.track, fake.position = int(PlaybackState.PAUSED), 2, 150.0
    items = mgr.queue.snapshot()
    b.queue_reorder(ReorderPlan(seamless=False, status_idx=2, new_status_idx=2,
                                old_before=["track-1"], new_before=["track-3"],
                                old_after=["track-3"], new_after=["track-1"],
                                order=["track-3", "track-2", "track-1"],
                                items_by_id={it.track_id: it for it in items},
                                position=150, resume=False))
    fake.build("SetFilter", 2.0)
    hb.change_dsp({"filter": 57, "filter1x": 57})
    with pytest.raises(hl.Building):
        b.play()
    assert _wait(lambda: _link().build.state is None)
    fake.commands.clear()
    assert b.play()
    assert [(c, a) for c, a in fake.commands if c in ("SelectTrack", "Seek", "Play")] == [
        ("SelectTrack", {"index": "2"}), ("Seek", {"position": "150"}), ("Play", {})]


def test_a_play_building_its_start_holds_off_no_queue_edit(fake, listens, monkeypatch):
    mgr = _album(3)
    b = _attach(mgr)
    monkeypatch.setattr(hl.BuildWatch, "after", 0.3)
    try:
        fake.build("Play", 4.0, answer="last")
        items = [_item("E:/Music/B/01.flac", 11), _item("E:/Music/B/02.flac", 12)]
        t = threading.Thread(target=lambda: mgr.replace_queue(items, play=True), daemon=True)
        t.start()
        assert _wait(lambda: _link().build.silent() is not None, timeout=5)
        t0 = time.monotonic()
        with pytest.raises(hl.Building):
            mgr.remove(2)                            # someone's edit: refused at once
        assert time.monotonic() - t0 < 1.0
        t.join(10)
        assert [it.track_id for it in mgr.queue.snapshot()] == ["track-11", "track-12"]
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING))
    finally:
        b.shutdown()


def test_a_play_intent_while_a_start_is_still_answering_is_busy_not_offline(fake, listens):
    mgr = _album(3)
    b = _attach(mgr)                                 # stopped
    try:
        # SelectTrack answered at once, then silent: the start builds on the
        # Play after it (Desktop 6.2.4)
        fake.build("SelectTrack", 3.0)
        t = threading.Thread(target=lambda: b.select(2), daemon=True)
        t.start()
        assert _wait(lambda: any(c == "SelectTrack" for _, c in fake.arrivals))
        with pytest.raises(hl.Building):
            mgr.ensure_active()                      # the gate meets the start's silence
        assert not b._gone
        assert not [a for a in diag.attempts() if a.intent == "play" and a.error]
        t.join(10)
        assert fake.state == int(PlaybackState.PLAYING) and fake.track == 2
        assert fake.sent_into_silence() == ["Play"]     # the start's own, answered once built
    finally:
        b.shutdown()


def test_the_assistants_play_goes_by_the_client_the_build_watch_hears(fake, listens, monkeypatch):
    from routers import settings as settings_router
    from tools import definitions
    monkeypatch.setattr(settings_router, "_read", lambda key: {"output.type": "hqplayer"}.get(key))
    mgr = _album(3)
    _routes_on(monkeypatch, mgr)
    monkeypatch.setattr(hl.BuildWatch, "after", 0.3)
    b = _attach(mgr)
    try:
        fake.build("Play", 2.0, answer="last")
        said = []
        t = threading.Thread(target=lambda: said.append(definitions._h_hqplayer_play()),
                             daemon=True)
        t.start()
        assert _wait(lambda: _link().build.silent() is not None, timeout=5), \
            "the assistant's Play set off a build the watch never heard of"
        with pytest.raises(hl.Building):
            b.pause()
        t.join(10)
        assert said == ["Playback started."]
    finally:
        b.shutdown()


def test_the_mcp_assistants_transport_goes_through_the_backend(fake, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "assistant_server_under_test", BACKEND.parent / "mcp" / "assistant_server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    posted = []
    monkeypatch.setattr(server, "_output_prefs", lambda: {"type": "hqplayer"})
    monkeypatch.setattr(server, "_backend_post",
                        lambda path, body, timeout=30.0: posted.append((path, body)) or
                        ({"ok": True} if path != "/api/player/volume/up"
                         else {"ok": False, "refusal": VOLUME_FIXED}))
    assert server.hqplayer_play() == "Playback started."
    assert server.hqplayer_next() == "Skipped to next track."
    assert server.hqplayer_set_volume(-20.0) == "Volume set to -20.0."
    assert server.hqplayer_volume_up() == f"Failed to change volume: {VOLUME_FIXED}"
    assert posted == [("/api/player/play", {}), ("/api/player/next", {}),
                      ("/api/player/volume", {"level": -20.0}), ("/api/player/volume/up", {})]
    assert not [c for _, c in fake.arrivals]        # nothing of its own reached HQPlayer


def test_a_dsp_changes_outcome_is_not_carried_past_its_attachment(fake, output):
    mgr, b = output
    fake.replies["SetShaping"] = '<SetShaping result="Error">Modulator needs 512x rate</SetShaping>'
    fake.build("SetFilter", 1.0)
    hb.change_dsp({"filter": 57, "filter1x": 57, "shaper": 30})
    assert _wait(lambda: _link().build.last_change is not None)
    assert _link().build.last_change["failed"] == {"shaper": "Modulator needs 512x rate"}
    b.shutdown()
    assert _link().build.last_change is None


@pytest.mark.parametrize("how", ["detach", "address change"])
def test_a_build_begun_while_a_takeover_waits_is_not_waited_out(fake, output, monkeypatch, how):
    mgr, b = output
    # The poller's own read, if one is out, is short: the command lock is what is timed
    _quick_polls(monkeypatch)
    fake.build("SetShaping", 6.0)                    # the second knob's build
    t = threading.Thread(target=lambda: hb.change_dsp({"filter": 57, "filter1x": 57, "shaper": 2}),
                         daemon=True)
    t.start()
    assert _wait(lambda: any(c == "SetFilter" for _, c in fake.arrivals))
    t0 = time.monotonic()
    b.shutdown() if how == "detach" else hl.choose("127.0.0.1", fake.port + 1)
    assert time.monotonic() - t0 < 2.5
    t.join(10)


def test_a_jump_to_a_slot_hqplayer_refuses_plays_nothing(fake):
    mgr = _album(3)
    b = _mirrored(mgr)
    fake.replies["SelectTrack"] = '<SelectTrack result="Error">No such track</SelectTrack>'
    fake.commands.clear()
    assert not b.select(3)
    assert "Play" not in [c for c, _ in fake.commands]
    assert fake.state == int(PlaybackState.STOPPED)


def test_picking_the_building_hqplayer_again_keeps_its_build_watched(fake, output, monkeypatch):
    from routers import settings as settings_router
    mgr, b = output
    saved = {"output.type": "hqplayer", "hqplayer.host": "127.0.0.1", "hqplayer.port": fake.port}
    monkeypatch.setattr(settings_router, "_read", lambda key: saved.get(key))
    monkeypatch.setattr(settings_router, "_write", lambda key, value: saved.__setitem__(key, value))
    monkeypatch.setattr(settings_router, "notify_library_subscribers", lambda: None)
    fake.build("SetFilter", 3.0)
    hb.change_dsp({"filter": 57, "filter1x": 57})
    t0 = time.monotonic()
    settings_router.put_output_prefs(settings_router.OutputPrefs(
        type="hqplayer", hqplayer={"host": "127.0.0.1", "port": fake.port}))
    assert time.monotonic() - t0 < 2.0
    live = mgr.active
    try:
        assert live is not b and _link().build.silent() is not None
        assert _wait(lambda: _link().build.state is None)
        assert fake.sent_into_silence() == []
        assert fake.state == int(PlaybackState.PLAYING) and len(fake.playlist) == 3
    finally:
        live.shutdown()


def test_a_play_holding_its_answer_past_the_limit_is_an_unfinished_build(fake, output, monkeypatch):
    mgr, b = output
    b.stop()
    assert _wait(lambda: mgr._latest_status["state"] == "stopped")
    _limit(monkeypatch, 1.0)
    buildings = _seen(mgr, "building")
    fake.build("Play", 3.0, answer="last")
    t0 = time.monotonic()
    assert not b.play()
    assert time.monotonic() - t0 < 2.5
    assert _wait(lambda: any(x and x.get("unfinished") for x in buildings))
    assert _wait(lambda: _link().build.state is None)     # HQPlayer answered the poller again


def test_the_knobs_after_a_build_hqplayer_does_not_finish_are_not_sent(fake, output, monkeypatch):
    mgr, b = output
    _limit(monkeypatch, 1.0)
    fake.build("SetFilter", 3.0)
    hb.change_dsp({"filter": 57, "filter1x": 57, "shaper": 2})
    assert _wait(lambda: _link().build.last_change is not None)
    assert _link().build.last_change["failed"] == {
        "shaper": "not sent: HQPlayer did not finish building the filter — restart HQPlayer"}
    assert "SetShaping" not in [c for _, c in fake.arrivals]


def test_a_filter_hqplayer_builds_before_it_answers_is_told_as_building_not_taken(
        fake, output, monkeypatch):
    from routers import settings as settings_router
    from tools import definitions
    monkeypatch.setattr(settings_router, "_read",
                        lambda key: "hqplayer" if key == "output.type" else None)
    fake.replies["GetFilters"] = ('<GetFilters><FiltersItem index="57" name="sinc-MGa" value="20" '
                                  'arg="0" description="4/5 transients, timbre, space ⥮ 2^x up"/>'
                                  '</GetFilters>')
    fake.build("SetFilter", 1.5, answer="last")      # its own answer waits out the build
    text = definitions._h_hqplayer_set_filter("sinc-MGa")
    assert text.startswith("HQPlayer is building the filter sinc-MGa — it answers nothing")
    assert _wait(lambda: _link().build.state is None)


def test_a_status_that_cannot_be_said_during_a_build_does_not_stop_the_poller(fake, output,
                                                                             monkeypatch):
    mgr, b = output
    said = b._emit_building
    failed = []

    def once(state):
        if not failed:
            failed.append(state)
            raise RuntimeError("the status handler failed")
        said(state)
    monkeypatch.setattr(b, "_emit_building", once)
    fake.build("SetFilter", 1.0)
    hb.change_dsp({"filter": 57, "filter1x": 57})
    assert _wait(lambda: failed)
    assert _wait(lambda: _link().build.state is None)
    n = len(fake.commands)
    assert _wait(lambda: _statuses_after(fake, n)), "the poller died with the failed status"


# -- the link: invariants 1–9 (listed in test_hqp_link_scenarios.py) ------------------------
#
# Everything this node says to an HQPlayer goes through its link: one request
# at a time on one connection, its build watched, the intents it accepted in
# order, cancelled as a whole when the node stops. The scenario runner
# (test_hqp_link_scenarios.py) checks the same invariants under random
# interleavings; these pin each one on a case of its own.

@pytest.fixture
def fake_b():
    """A second HQPlayer, at another address."""
    f = FakeHqp()
    yield f
    f.close()


def _picker(monkeypatch, fake):
    """The Output picker's settings, kept in memory: HQPlayer at `fake`."""
    from routers import settings as settings_router
    saved = {"output.type": "hqplayer", "hqplayer.host": "127.0.0.1", "hqplayer.port": fake.port}
    monkeypatch.setattr(settings_router, "_read", lambda key: saved.get(key))
    monkeypatch.setattr(settings_router, "_write", lambda key, value: saved.__setitem__(key, value))
    monkeypatch.setattr(settings_router, "notify_library_subscribers", lambda: None)
    return settings_router


def _pick(settings_router, **prefs):
    settings_router.put_output_prefs(settings_router.OutputPrefs(**prefs))


def test_one_request_at_a_time_reaches_hqplayer_whoever_asks(fake, listens, monkeypatch):
    """I1: the status poller, the assistant's reads, the library's hash, the
    play gate and the transport, all at once — never two requests
    outstanding at HQPlayer."""
    import hqp_library
    from tools import definitions
    _picker(monkeypatch, fake)
    fake.replies["LibraryGetHash"] = '<LibraryGetHash value="h"/>'
    monkeypatch.setattr(hb, "_poll_interval", lambda failures: 0.02)
    mgr = _album(3)
    b = _attach(mgr)
    stop, errors = threading.Event(), []

    def loop(fn):
        while not stop.is_set():
            try:
                fn()
            except Exception as e:
                errors.append(e)
    jobs = [definitions._h_hqplayer_get_dsp_state,
            lambda: hqp_library.library_hash("127.0.0.1", fake.port),
            b.reachable, lambda: b.set_volume(-10.0), b.pause, b.play]
    threads = [threading.Thread(target=loop, args=(job,), daemon=True) for job in jobs]
    try:
        for t in threads:
            t.start()
        time.sleep(2.0)
    finally:
        stop.set()
        for t in threads:
            t.join(10)
        b.shutdown()
    assert errors == []
    assert len(fake.arrivals) > 50
    assert fake.max_outstanding() == 1


@pytest.mark.parametrize("plays_on", [True, False])
def test_what_was_made_for_one_hqplayer_never_reaches_the_next(fake, fake_b, output, monkeypatch,
                                                              plays_on):
    """I2: HQPlayer B is picked while A builds a DSP change. A hears nothing
    after the pick but the Stop it is owed — and the Status that decides it:
    it stops only an entry of ours that plays on — and B nothing made for A:
    not the knob A's build held back, not the resume of A's spot."""
    mgr, b = output
    settings_router = _picker(monkeypatch, fake)
    link_a = _link()
    fake.build("SetFilter", 1.5, answer="last", stops=not plays_on)
    hb.change_dsp({"filter": 57, "filter1x": 57, "shaper": 2})
    picked = time.time()
    _pick(settings_router, type="hqplayer", hqplayer={"host": "127.0.0.1", "port": fake_b.port})
    live = mgr.active
    try:
        assert live is not b
        # A's link closes once what it owed went
        assert _wait(lambda: link_a.state == "closed", timeout=10)
        after = [c for ts, c in fake.arrivals if ts >= picked]
        assert set(after) <= {"Status", "Stop"}
        assert ("Stop" in after) == plays_on
        assert fake.state == int(PlaybackState.STOPPED)
        assert not {"SetFilter", "SetShaping", "SelectTrack", "Seek", "Play"} & {
            c for _, c in fake_b.arrivals}
    finally:
        live.shutdown()


@pytest.mark.parametrize("kind", ["start", "resume", "late_stop", "dsp_knob"])
def test_an_item_is_superseded_by_exactly_the_kinds_the_design_names(kind):
    """Invariant 3's rule: which newer intents drop an item not
    yet sent — and which leave it."""
    design = {"start": {"transport", "queue", "rebuild", "start", "switch"},
              "resume": {"transport", "queue", "rebuild", "start", "dsp", "switch"},
              "late_stop": {"attach"},
              "dsp_knob": {"dsp", "switch"}}
    for newer in ("transport", "queue", "rebuild", "start", "dsp", "switch", "attach", "volume"):
        link = hl.HqpLink(("table", 0), "127.0.0.1", 9)
        item = link.item(kind, link.accept("dsp"))
        link.accept(newer)
        assert (link.superseded(item) is not None) == (newer in design[kind]), (kind, newer)


def _build_after_the_commit(monkeypatch, b, seconds):
    """Someone else's build begins between a replace's mirror and its start:
    a DSP change set off as the manager commits the replace."""
    changed = b.queue_changed

    def commit(kind, *, play=False):
        changed(kind, play=play)
        if kind == "replace":
            fake_build_once.append(hb.change_dsp({"filter": 57, "filter1x": 57}))
    fake_build_once: list = []
    monkeypatch.setattr(b, "queue_changed", commit)
    return fake_build_once


@pytest.mark.parametrize("switched", [False, True])
def test_a_start_that_meets_someone_elses_build_waits_and_a_switch_drops_it(fake, output,
                                                                           monkeypatch, switched):
    """I3: a committed replace whose start meets a build is
    answered at once, never 409 — the start waits on the link and goes once
    the build is done; a switch away before that supersedes it, and it is
    never sent."""
    mgr, b = output
    _picker(monkeypatch, fake)
    fake.build("SetFilter", 1.5)
    began = _build_after_the_commit(monkeypatch, b, 1.5)
    t0 = time.monotonic()
    added, _ = mgr.replace_queue([_item("E:/Music/B/01.flac", 11), _item("E:/Music/B/02.flac", 12)],
                                 play=True)
    assert added == 2 and time.monotonic() - t0 < 1.4
    assert began and began[0]["building"] is not None
    if switched:
        mgr.activate(None)
    time.sleep(2.5)
    played = [c for ts, c in fake.arrivals if ts >= began[0]["building"]["since"]]
    if switched:
        assert "Play" not in played and fake.state != int(PlaybackState.PLAYING)
    else:
        assert "Play" in played and fake.state == int(PlaybackState.PLAYING)
        assert fake.playlist == ["file:///E:/Music/B/01.flac", "file:///E:/Music/B/02.flac"]
    assert fake.sent_into_silence() == []


def test_waiting_out_a_build_holds_neither_the_command_lock_nor_the_queue_lock(fake, output,
                                                                             monkeypatch):
    """I4: a filler waiting for a build's end holds no lock — the queue lock
    is free for a queue edit, the command lock is the build's own."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    waiting, waiter = threading.Event(), []
    wait_out = hl.BuildWatch.wait_out

    def recorded(self, until):
        waiter.append(threading.get_ident())
        waiting.set()
        return wait_out(self, until)
    monkeypatch.setattr(hl.BuildWatch, "wait_out", recorded)
    fake.build("SetFilter", 2.0)
    hb.change_dsp({"filter": 57, "filter1x": 57})
    t = threading.Thread(target=lambda: player_router._filler_append(
        _item("E:/Music/B/09.flac", 9), mgr.queue.generation), daemon=True)
    t.start()
    assert waiting.wait(5)
    assert _link().lock.owner != waiter[0]
    assert mgr._mutate_lock.acquire(timeout=1.0), "the waiter holds the queue lock"
    mgr._mutate_lock.release()
    t.join(10)
    assert len(mgr.queue) == 4


def test_the_node_stop_lets_go_of_everything_within_two_seconds(fake, output, monkeypatch):
    """I5: mid-build — a DSP change holding its answer, a filler waiting it
    out, the Stop a switch owes, the meter open — the node stop returns at
    once and lets every one of them go within 2 s; the meter's socket is
    closed, and a link asked for afterwards sends nothing."""
    import asyncio
    from playback import hqp_meter
    from test_hqp_meter import FakeMeterPort, Page
    mgr, b = output
    _picker(monkeypatch, fake)
    player_router = _routes_on(monkeypatch, mgr)
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    meter = hqp_meter.HqpMeter()
    monkeypatch.setattr(hqp_meter, "meter", meter)
    port = FakeMeterPort()
    try:
        meter.attach("127.0.0.1", port.port)
        page = Page(meter, loop, "np")
        page.want(True)
        page.until(lambda m: m.get("state") == "open")
        fake.build("SetFilter", 60.0, answer="last")
        hb.change_dsp({"filter": 57, "filter1x": 57, "shaper": 2})
        filler = threading.Thread(target=lambda: player_router._filler_append(
            _item("E:/Music/B/09.flac", 9), mgr.queue.generation), daemon=True)
        filler.start()
        mgr.activate(None)                           # the switch: its Stop is owed
        assert _wait(lambda: any(t.name == "hqp-late-stop" for t in threading.enumerate()))
        workers = [t for t in threading.enumerate()
                   if t.name in ("hqp-dsp-change", "hqp-late-stop")] + [filler]
        n = len(fake.arrivals)
        t0 = time.monotonic()
        hl.close_all("the node is stopping")
        assert time.monotonic() - t0 < 0.5
        for t in workers:
            t.join(max(0.0, 2.0 - (time.monotonic() - t0)))
        alive = [t for t in workers if t.is_alive()]
        assert not alive, _stacks(alive)
        port.wait(lambda: port.ends == 1)
        assert fake.arrivals[n:] == []               # a stopping node leaves HQPlayer be
        assert _link().state == "closed"
    finally:
        port.close()
        loop.call_soon_threadsafe(loop.stop)


@pytest.mark.parametrize("what,stopped", [("ours", True), ("ours paused", True),
                                          ("another controller's", False), ("nothing", False)])
def test_the_switch_away_stops_hqplayer_only_while_it_plays_an_entry_of_ours(fake, listens,
                                                                            monkeypatch, what,
                                                                            stopped):
    """I6: the switch away stops what plays or pauses an entry of
    ours; another controller's entry, and a stopped HQPlayer, are left be."""
    _picker(monkeypatch, fake)
    mgr = _album(2)
    b = _attach(mgr)
    with fake._lock:
        if what == "another controller's":
            fake.playlist.append("file:///Elsewhere/theirs.flac")
            fake.track = 3
        else:
            fake.track = 1
        fake.state = int({"ours": PlaybackState.PLAYING, "ours paused": PlaybackState.PAUSED,
                          "another controller's": PlaybackState.PLAYING,
                          "nothing": PlaybackState.STOPPED}[what])
    n = len(fake.arrivals)
    mgr.activate(None)
    assert ("Stop" in [c for _, c in fake.arrivals[n:]]) == stopped
    assert b is not mgr.active


class _QuietLink:
    """What a build watch asks of its link, and nothing more."""
    state, why = "open", None

    def _waitable(self):
        return True

    def _build_began(self):
        pass

    def _build_ended(self, how):
        pass

    def _poke(self):
        pass


@pytest.mark.parametrize("how,why", [("answered", None), ("timeout", hl.UNFINISHED),
                                     ("lost", hl.LOST)])
def test_a_builds_end_is_never_lost(how, why):
    """I7: a wait that starts after the build's end returns at once; one
    already waiting returns as soon as the end is told — with why, when the
    build did not end with HQPlayer's answer."""
    watch = hl.BuildWatch(_QuietLink())
    watch.begin("SetFilter", {})(how)
    t0 = time.monotonic()
    gave_up = watch.wait_out()
    assert time.monotonic() - t0 < 0.1
    assert (gave_up is None) if why is None else (str(gave_up) == why and gave_up.failed)
    end = watch.begin("SetFilter", {})
    got = []
    t = threading.Thread(target=lambda: got.append(watch.wait_out()), daemon=True)
    t.start()
    time.sleep(0.3)
    t0 = time.monotonic()
    end(how)
    t.join(2)
    assert len(got) == 1 and time.monotonic() - t0 < 0.5
    assert (got[0] is None) if why is None else str(got[0]) == why


def test_an_item_made_for_one_queue_is_not_sent_once_another_plays():
    """I8: a resume made for the queue of generation g is dropped at its
    send once the queue is past g — before anything reaches HQPlayer."""
    link = hl.HqpLink(("i8", 0), "127.0.0.1", 9)
    q = CanonicalQueue()
    q.replace([_item("E:/Music/A/01.flac", 1)])
    item = link.item("resume", link.accept("dsp"), queue=q, generation=q.generation)
    assert link.superseded(item) is None
    q.replace([_item("E:/Music/A/02.flac", 2)])
    assert link.superseded(item) == "another queue plays"
    with pytest.raises(hl.Dropped, match="another queue plays"):
        with link.command(item=item):
            link.client.get_status()
    assert link.client.socket is None                # never connected


def test_a_silence_no_build_of_ours_came_before_is_hqplayer_not_answering(fake, output,
                                                                         monkeypatch):
    """I9: HQPlayer silent on its own — no command of this node set a build
    off — is neither building nor unfinished, and never "disconnected": it
    is not answering, until it answers again."""
    mgr, b = output
    _quick_polls(monkeypatch)
    quiet, states, buildings = (_seen(mgr, "not_answering"), _seen(mgr, "state"),
                                _seen(mgr, "building"))
    fake.silence(4.0)
    assert _wait(lambda: any(quiet), timeout=10)
    assert not any(buildings) and "disconnected" not in states
    assert _wait(lambda: not mgr._latest_status.get("not_answering")
                 and mgr._latest_status.get("state") == "playing", timeout=10)


def test_picking_the_hqplayer_already_driven_leaves_it_playing(fake, listens, monkeypatch):
    """The HQPlayer the output drives, picked again in the Output picker,
    stays the output: nothing is stopped."""
    from playback import manager as manager_mod
    settings_router = _picker(monkeypatch, fake)
    mgr = _album(2)
    monkeypatch.setattr(manager_mod, "manager", mgr)
    _attach(mgr)
    with fake._lock:
        fake.state, fake.track = int(PlaybackState.PLAYING), 1
    n = len(fake.arrivals)
    _pick(settings_router, type="hqplayer", hqplayer={"host": "127.0.0.1", "port": fake.port})
    try:
        assert "Stop" not in [c for _, c in fake.arrivals[n:]]
        assert fake.state == int(PlaybackState.PLAYING)
    finally:
        mgr.active.shutdown()


def test_a_stop_owed_since_a_switch_away_is_superseded_by_picking_hqplayer_again(fake, output,
                                                                                monkeypatch):
    """Switched away while HQPlayer builds, then picked again — and
    the build ends while the output it switched to is still let go. The pick
    registered its attach first: the Stop it owed is never sent."""
    from playback import browser_backend
    mgr, b = output
    settings_router = _picker(monkeypatch, fake)
    fake.build("SetFilter", 1.0)
    began = hb.change_dsp({"filter": 57, "filter1x": 57})["building"]["since"]
    _pick(settings_router, type="browser")
    assert _wait(lambda: any(t.name == "hqp-late-stop" for t in threading.enumerate()))
    let_go = browser_backend.BrowserBackend.shutdown

    def slow_shutdown(self):
        assert _wait(lambda: _link().build.state is None, timeout=10)    # the build ends here
        time.sleep(0.3)
        let_go(self)
    monkeypatch.setattr(browser_backend.BrowserBackend, "shutdown", slow_shutdown)
    _pick(settings_router, type="hqplayer", hqplayer={"host": "127.0.0.1", "port": fake.port})
    try:
        assert "Stop" not in [c for ts, c in fake.arrivals if ts >= began]
        assert fake.state == int(PlaybackState.PLAYING)
    finally:
        mgr.active.shutdown()


def test_a_play_intent_on_an_hqplayer_that_takes_the_connection_and_says_nothing_is_busy(
        fake, listens, monkeypatch):
    """The play gate's probe meets an HQPlayer silent on its own — it
    takes the connection and answers nothing: busy (409, "HQPlayer is not
    answering"), never "offline" with the output marked gone."""
    from fastapi import HTTPException
    from routers import player as player_router
    _picker(monkeypatch, fake)
    mgr = _album(2)
    monkeypatch.setattr(player_router, "manager", mgr)
    b = _attach(mgr)
    try:
        fake.silence(15.0)      # past the poller's question in hand and the probe's own wait
        with pytest.raises(HTTPException) as e:
            player_router.play()
        assert e.value.status_code == 409 and "not answering" in e.value.detail
        assert not b._gone
    finally:
        b.shutdown()


# -- the policies on the link: the spot, a lost mirror, the gate, a change's outcome ----------

def _stopped_at_spot(fake, mgr, monkeypatch):
    """HQPlayer paused on slot 2 at 2:30, its playlist then rebuilt by a
    reorder: left stopped there, a spot on track-2."""
    player_router = _routes_on(monkeypatch, mgr)
    _paused_on_2_at_150(fake, mgr)
    player_router.reorder(player_router.ReorderRequest(order=["track-3", "track-2", "track-1"]))
    assert _wait(lambda: mgr._latest_status.get("resume") == {"index": 2, "position": 150}
                 and mgr._latest_status.get("state") == "stopped")
    return player_router


def test_next_and_previous_at_the_spot_move_it_and_send_nothing(fake, listens, monkeypatch):
    """I10: stopped at the spot, Next and Previous move it a slot, at its
    start — no command reaches HQPlayer, nothing sounds — and the play
    press starts where it went."""
    mgr = _album(3)
    b = _attach(mgr)
    try:
        player_router = _stopped_at_spot(fake, mgr, monkeypatch)
        fake.commands.clear()
        assert player_router.next_track() == {"ok": True}
        assert _wait(lambda: mgr._latest_status.get("resume") == {"index": 3, "position": 0})
        assert player_router.next_track() == {"ok": True}           # the last slot: it stays
        assert player_router.previous_track() == {"ok": True}
        assert _wait(lambda: mgr._latest_status.get("resume") == {"index": 2, "position": 0})
        assert not [c for c, _ in fake.commands if c not in _QUESTIONS]   # the gate asks, no more
        assert fake.state == int(PlaybackState.STOPPED)
        assert b.play()
        assert [(c, a) for c, a in fake.commands if c in ("SelectTrack", "Seek", "Play")] == [
            ("SelectTrack", {"index": "2"}), ("Play", {})]
    finally:
        b.shutdown()


def test_a_tap_on_a_track_at_the_spot_plays_it_and_the_spot_is_spent(fake, listens, monkeypatch):
    """I10: a tap on a track in the queue is "play" — it plays that track;
    a start taken spends the spot."""
    mgr = _album(3)
    b = _attach(mgr)
    try:
        player_router = _stopped_at_spot(fake, mgr, monkeypatch)
        player_router.jump(player_router.JumpRequest(index=3))
        assert _wait(lambda: fake.state == int(PlaybackState.PLAYING) and fake.track == 3)
        assert b._spot is None
    finally:
        b.shutdown()


@pytest.mark.parametrize("playing", [False, True])
def test_an_output_switch_keeps_the_slot_as_a_spot_only_for_a_stopped_hqplayer(fake, listens,
                                                                              monkeypatch, playing):
    """I10: back on HQPlayer after another output, the slot the queue was on
    is where the play press starts — when the attach found HQPlayer stopped;
    one that plays keeps its own place."""
    from playback import manager as manager_mod
    settings_router = _picker(monkeypatch, fake)
    mgr = _album(3)
    monkeypatch.setattr(manager_mod, "manager", mgr)
    _pick(settings_router, type="hqplayer", hqplayer={"host": "127.0.0.1", "port": fake.port})
    mgr._last_slot = 2
    _pick(settings_router, type="browser")
    if playing:
        with fake._lock:
            fake.state, fake.track = int(PlaybackState.PLAYING), 1
    _pick(settings_router, type="hqplayer", hqplayer={"host": "127.0.0.1", "port": fake.port})
    try:
        assert (mgr.active._spot_at() == (2, 0)) == (not playing)
    finally:
        mgr.active.shutdown()


def test_past_the_limit_a_silent_hqplayer_is_asked_less_and_less(fake, output, monkeypatch):
    """A build HQPlayer did not finish is asked again — with the back-off of
    an HQPlayer away, not a new connection every few seconds."""
    mgr, b = output
    monkeypatch.setattr(hb, "QUESTION_WAIT_S", 0.3)
    monkeypatch.setattr(hb, "_poll_interval", lambda failures: 0.05 if failures <= 0
                        else min(0.5 * 2 ** min(failures, 4), 15.0))
    _limit(monkeypatch, 0.5)
    fake.build("SetFilter", 60.0)
    hb.change_dsp({"filter": 57, "filter1x": 57})
    assert _wait(lambda: (_link().build.state or {}).get("unfinished"), timeout=5)
    t0 = time.time()
    time.sleep(6.0)
    asks = [ts for ts, c in fake.arrivals if c == "Status" and ts >= t0]
    # 0.3 s silent each, then 1, 2, 4 s apart: four or five in six seconds, not twenty
    assert len(asks) <= 6, len(asks)


@pytest.mark.parametrize("how,status,said", [
    ("building", 409, "HQPlayer is building the filter"),
    ("unfinished", 409, "HQPlayer did not finish building the filter — restart HQPlayer"),
    ("closed", 503, "is offline"),
])
def test_the_play_gate_says_busy_or_offline_in_the_words_for_each(fake, output, monkeypatch, how,
                                                                status, said):
    """The play gate: a build in progress and one past the limit are busy
    (409) in their own words; an HQPlayer that refuses the connection is
    offline (503), the output marked gone."""
    from fastapi import HTTPException
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    if how == "closed":
        monkeypatch.setattr(hl, "CIRCUIT_COOLDOWN_S", 0.0)
        fake.close()
    else:
        _limit(monkeypatch, 0.6 if how == "unfinished" else 30.0)
        fake.build("SetFilter", 20.0)
        hb.change_dsp({"filter": 57, "filter1x": 57})
        if how == "unfinished":
            assert _wait(lambda: (_link().build.state or {}).get("unfinished"), timeout=5)
    with pytest.raises(HTTPException) as e:
        player_router.play()
    assert e.value.status_code == status and said in e.value.detail
    assert b._gone == (how == "closed")


def test_a_dsp_changes_outcome_is_its_attachments_and_its_id_is_never_repeated(fake, output,
                                                                             monkeypatch):
    """A change's outcome rides the status of the attachment it was made
    in, only; its id is no counter a restart starts again."""
    from playback import manager as manager_mod
    mgr, b = output
    fake.replies["SetShaping"] = '<SetShaping result="Error">Modulator needs 512x rate</SetShaping>'
    fake.build("SetFilter", 0.8)
    hb.change_dsp({"filter": 57, "filter1x": 57, "shaper": 30})
    assert _wait(lambda: (mgr._latest_status.get("dsp_change") or {}).get("failed"))
    first = mgr._latest_status["dsp_change"]["id"]
    _fresh_links()                                   # a restart: every link made afresh
    b.shutdown()
    b2 = _attach(mgr)
    try:
        assert _wait(lambda: "dsp_change" not in mgr._latest_status
                     and mgr._latest_status.get("state") == "playing")
        fake.build("SetFilter", 0.8)
        hb.change_dsp({"filter": 57, "filter1x": 57, "shaper": 30})
        assert _wait(lambda: (mgr._latest_status.get("dsp_change") or {}).get("id")
                     not in (None, first))
    finally:
        b2.shutdown()
    assert manager_mod.manager is mgr


def test_a_filler_whose_hqplayer_vanished_loses_nothing_and_the_return_gets_the_queue(
        fake, output, monkeypatch):
    """I11, I13: HQPlayer gone mid-fill, no build at all — what the filler
    took goes into the queue alone, the mirror lost; the HQPlayer that comes
    back, restarted and stopped, takes the whole queue."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    monkeypatch.setattr(hl, "CIRCUIT_COOLDOWN_S", 0.0)
    fake.close()
    album = [_item(f"E:/Music/B/0{i}.flac", 10 + i) for i in range(1, 4)]
    player_router._owned_filler(album, mgr.queue.generation)
    assert [it.track_id for it in mgr.queue.snapshot()] == [
        "track-1", "track-2", "track-3", "track-11", "track-12", "track-13"]
    assert not b.healthy()
    fake.come_back()
    want = [f"file:///E:/Music/{d}/0{i}.flac" for d, i in
            (("A", 1), ("A", 2), ("A", 3), ("B", 1), ("B", 2), ("B", 3))]
    assert _wait(lambda: (b.poke(), fake.playlist == want)[1], timeout=20)
    assert _wait(lambda: b.healthy(), timeout=10)


def test_a_filler_waiting_on_one_hqplayer_when_another_is_picked_lands_on_the_new_one(
        fake, fake_b, output, monkeypatch):
    """I11, I2: a filler waits out HQPlayer A's build, and HQPlayer B is
    picked meanwhile: the wait gives up with A's link — the filler is not
    stopped by it: it goes on through the manager, and B gets the queue
    whole."""
    mgr, b = output
    settings_router = _picker(monkeypatch, fake)
    player_router = _routes_on(monkeypatch, mgr)
    waiting = threading.Event()
    wait_out = hl.BuildWatch.wait_out
    monkeypatch.setattr(hl.BuildWatch, "wait_out",
                        lambda self, until: (waiting.set(), wait_out(self, until))[1])
    fake.build("SetFilter", 3.0)
    hb.change_dsp({"filter": 57, "filter1x": 57})
    album = [_item(f"E:/Music/B/0{i}.flac", 10 + i) for i in range(1, 4)]
    t = threading.Thread(target=lambda: player_router._owned_filler(album, mgr.queue.generation),
                         daemon=True)
    t.start()
    assert waiting.wait(5)
    _pick(settings_router, type="hqplayer", hqplayer={"host": "127.0.0.1", "port": fake_b.port})
    t.join(15)
    live = mgr.active
    try:
        assert [it.track_id for it in mgr.queue.snapshot()] == [
            "track-1", "track-2", "track-3", "track-11", "track-12", "track-13"]
        want = [f"file:///E:/Music/{d}/0{i}.flac" for d, i in
                (("A", 1), ("A", 2), ("A", 3), ("B", 1), ("B", 2), ("B", 3))]
        assert _wait(lambda: (live.poke(), fake_b.playlist == want)[1], timeout=15)
        assert "PlaylistAdd" not in [c for ts, c in fake.arrivals if ts >= _silence_began(fake)]
    finally:
        live.shutdown()


def _silence_began(fake) -> float:
    """When the fake's latest silence began — the build the filler waited on."""
    return fake.silences[-1][0] if fake.silences else 0.0


# -- the routes: history rotates once the output took the queue; the assistants ---------------

@pytest.fixture
def play_route(fake, output, monkeypatch):
    """POST /api/player/play-track for owned file 9, its row in memory, and
    every session rotation recorded with the queue it found."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    monkeypatch.setattr(player_router, "_db_query_one",
                        lambda sql, params=None: {"id": 9, "file_path": "E:/Music/B/09.flac",
                                                  "file_format": "FLAC", "title": "Song 9",
                                                  "artist": "Artist", "album": "Album"})
    monkeypatch.setattr(queue_mod, "items_for_owned_rows",
                        lambda rows: [_item(r["file_path"], r["id"]) for r in rows])
    rotations = []
    monkeypatch.setattr(player_router.sessions, "rotate_session",
                        lambda queue, origin, **k: rotations.append(
                            [it.track_id for it in queue.snapshot()]))

    def play():
        return player_router.play_track(player_router.PlayTrackRequest(track_id=9))
    return mgr, b, play, rotations


def test_a_play_rotates_history_once_the_output_took_the_queue(fake, play_route):
    """I12: the rotation runs after the mirror, on the queue it replaces."""
    mgr, b, play, rotations = play_route
    assert play()["ok"]
    assert rotations == [["track-1", "track-2", "track-3"]]
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-9"]


@pytest.mark.parametrize("refusal", ["the gate", "the mirror", "HQPlayer's adds"])
def test_a_play_the_output_refuses_leaves_history_as_it_was(fake, play_route, monkeypatch,
                                                           refusal):
    """I12: refused at the gate (a build on), at the mirror (a build begun
    after the gate passed), or by HQPlayer itself (no entry taken): the
    sessions are as they were, the queue too."""
    from fastapi import HTTPException
    mgr, b, play, rotations = play_route
    if refusal == "the gate":
        fake.build("SetFilter", 2.0)
        hb.change_dsp({"filter": 57, "filter1x": 57})
    elif refusal == "the mirror":
        reachable = b.reachable

        def then_a_build():
            ok = reachable()
            fake.build("SetFilter", 2.0)
            hb.change_dsp({"filter": 57, "filter1x": 57})
            return ok
        monkeypatch.setattr(b, "reachable", then_a_build)
    else:
        fake.replies["PlaylistAdd"] = '<PlaylistAdd result="Error">cannot open it</PlaylistAdd>'
    with pytest.raises(HTTPException) as e:
        play()
    assert e.value.status_code == (503 if refusal == "HQPlayer's adds" else 409)
    assert rotations == []
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2", "track-3"]


def test_a_rotation_that_fails_commits_nothing_and_starts_nothing(fake, play_route, monkeypatch):
    """I12: the rotation itself fails (the database): nothing is committed,
    the start the mirror left is not sent, HQPlayer's playlist is no mirror —
    and it gets the queue back."""
    mgr, b, play, rotations = play_route
    from routers import player as player_router

    def fails(queue, origin, **k):
        raise RuntimeError("the database is gone")
    monkeypatch.setattr(player_router.sessions, "rotate_session", fails)
    fake.commands.clear()
    with pytest.raises(Exception):
        play()
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-1", "track-2", "track-3"]
    assert "Play" not in [c for c, _ in fake.commands]
    assert _wait(lambda: (b.poke(), fake.playlist == [
        "file:///E:/Music/A/01.flac", "file:///E:/Music/A/02.flac",
        "file:///E:/Music/A/03.flac"])[1], timeout=10)


@pytest.mark.parametrize("case", ["database", "clear refused", "taken"])
def test_the_radio_rotates_history_under_the_command_hold_before_the_clear(fake, output,
                                                                         monkeypatch, case):
    """The radio start: the rotation runs under HQPlayer's command hold,
    before PlaylistClear — a rotation that fails clears nothing; a clear
    HQPlayer does not take leaves the queue as it is, history rotated."""
    mgr, b = output
    player_router = _routes_on(monkeypatch, mgr)
    monkeypatch.setattr(player_router, "_db_query_one", lambda sql, params: {"artist": "Artist"})
    monkeypatch.setattr(player_router, "_radio_fill", lambda seed, gen: None)
    order = []

    def rotate(queue, origin, **k):
        order.append("PlaylistClear" in [c for c, _ in fake.commands])   # cleared before it?
        if case == "database":
            raise RuntimeError("the database is gone")
    monkeypatch.setattr(player_router.sessions, "rotate_session", rotate)
    if case == "clear refused":
        fake.replies["PlaylistClear"] = '<PlaylistClear result="Error">busy</PlaylistClear>'
    fake.commands.clear()
    req = player_router.RadioStartRequest(track_uuid="0b8f1a2e-1d2c-5e3f-9a4b-5c6d7e8f9a0b")
    if case == "database":
        with pytest.raises(RuntimeError):
            player_router.radio_start(req)
        assert "PlaylistClear" not in [c for c, _ in fake.commands]
        assert len(mgr.queue) == 3
    else:
        player_router.radio_start(req)
        assert "PlaylistClear" in [c for c, _ in fake.commands]
        assert len(mgr.queue) == (3 if case == "clear refused" else 1)
    assert order == [False]                         # rotated once, before the clear


def test_the_mcp_assistant_reads_hqplayer_through_the_backend_and_keeps_no_connection(
        fake, monkeypatch):
    """The MCP server (a process of its own) asks HQPlayer nothing: its
    reads go through the backend's routes, which ask on the link."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "assistant_server_reads", BACKEND.parent / "mcp" / "assistant_server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    assert not hasattr(server, "HQPlayerClient") and not hasattr(server, "_hqp_endpoint")
    state = {"connected": True, "info": {"product": "Signalyst HQPlayer Fake", "version": "6",
                                         "engine": "6.2.4"},
             "state": {"filter": 57, "shaper": 2, "mode": 1, "rate": 3, "convolution": False,
                       "matrix_profile": "", "invert": False},
             "filters": [{"index": 57, "name": "sinc-MGa", "description": ""}],
             "shapers": [{"index": 2, "name": "NS9"}], "modes": [{"index": 1, "name": "PCM"}],
             "rates": None, "matrix_profiles": ["Room"],
             "refused": {"rates": "no output device"}}
    status = {"state": "playing", "artist": "Artist", "song": "Song 1", "album": "Album",
              "position_formatted": "0:42", "length_formatted": "5:00", "volume": -3,
              "track_index": 1}
    asked, posted = [], []
    monkeypatch.setattr(server, "_output_prefs", lambda: {"type": "hqplayer"})
    monkeypatch.setattr(server, "_backend_get", lambda path, params: asked.append(path) or (
        state if path == "/api/hqplayer/state" else status))
    monkeypatch.setattr(server, "_backend_post", lambda path, body, timeout=30.0: posted.append(
        (path, body)) or {"ok": True, "applied": body, "failed": {}, "building": None})
    settings_text = server.hqplayer_get_settings()
    assert "sinc-MGa" in settings_text and "Sample rates: HQPlayer did not list them (no output device)." in settings_text
    assert server.hqplayer_set_filter("sinc-MGa").startswith("Filter set to: sinc-MGa")
    assert "Track: Artist - Song 1" in server.hqplayer_get_status()
    assert "Active filter index: 57 (sinc-MGa)" in server.hqplayer_get_dsp_state()
    assert "Room" in server.hqplayer_list_matrix_profiles()
    assert set(asked) == {"/api/hqplayer/state", "/api/player/status"}
    assert posted == [("/api/hqplayer/config", {"filter": 57, "filter1x": 57})]
    assert fake.arrivals == []


def test_a_name_is_picked_exactly_as_written_before_any_case_or_part_of_it():
    import assistant_queries as aq
    filters = [{"name": "poly-sinc-xtr"}, {"name": "poly-sinc-XTR"}, {"name": "poly-sinc-xtr-mp"}]
    pick = lambda name: aq.hqp_pick("filter", filters, name, "", key=lambda f: f["name"])[0]
    assert pick("poly-sinc-XTR") is filters[1]
    assert pick("poly-sinc-xtr") is filters[0]
    assert pick("POLY-SINC-XTR-MP") is filters[2]
    assert pick("xtr-mp") is filters[2]


def test_hqplayer_picked_again_during_a_build_says_what_it_is_with_its_first_answer(
        fake, output, monkeypatch):
    """An attach during a build asks HQPlayer nothing; what it says of
    itself (GetInfo) comes with the poller's first answer after it."""
    mgr, b = output
    fake.build("SetFilter", 1.0)
    hb.change_dsp({"filter": 57, "filter1x": 57})
    b.shutdown()
    again = _attach(mgr)
    try:
        assert again._info == {}
        assert _wait(lambda: again._info.get("engine") == "6.2.3", timeout=10)
    finally:
        again.shutdown()


def test_a_start_left_to_a_backend_detached_before_it_goes_is_dropped_quietly(fake, play_route,
                                                                            monkeypatch, caplog):
    """The output switched away between a replace's commit and its start:
    the play route answers what it committed, and the start is dropped —
    logged, never a 503 for a queue that did change."""
    import logging
    mgr, b, play, rotations = play_route
    changed = b.queue_changed

    def detach_after(kind, *, play=False):
        changed(kind, play=play)
        if kind == "replace":
            b.shutdown()
    monkeypatch.setattr(b, "queue_changed", detach_after)
    caplog.set_level(logging.INFO)
    fake.commands.clear()
    assert play()["ok"]
    assert [it.track_id for it in mgr.queue.snapshot()] == ["track-9"]
    assert "Play" not in [c for c, _ in fake.commands]
    assert "HQPlayer's start not sent: HQPlayer was detached from playback" in caplog.text


# -- The connection per request (2026-10-12) ---------------------------------------
# Each request takes the link's one connection for itself; after a command
# a silence may follow it passes to the sequence's next request or to a
# Status the link holds itself; a silence no build of ours explains ends
# the sequence that met it; a Stop's teardown is no build.


def _status_answers(fake, monkeypatch) -> list:
    """When each <Status/> was answered, from here on."""
    times = []
    real = fake._dispatch

    def dispatch(cmd, attrs):
        if cmd == "Status":
            times.append(time.monotonic())
        return real(cmd, attrs)
    monkeypatch.setattr(fake, "_dispatch", dispatch)
    return times


def test_the_poller_answers_between_the_adds_of_a_long_append(fake, output, monkeypatch):
    """I1: each request takes the connection for itself — while 80 tracks
    are appended the poller is silent no longer than one request plus its
    period: a question goes between the requests of a command sequence."""
    mgr, b = output
    fake.service["PlaylistAdd"] = 0.04                   # 80 adds: over three seconds
    answers = _status_answers(fake, monkeypatch)
    t0 = time.monotonic()
    assert mgr.append([_item(f"E:/Music/B/{i:02d}.flac", 100 + i) for i in range(80)]) == 80
    t1 = time.monotonic()
    assert t1 - t0 > 2.5
    during = [t for t in answers if t0 <= t <= t1]
    gaps = [later - earlier for earlier, later in zip([t0] + during, during + [t1])]
    assert max(gaps) < 1.0 + 0.5, gaps                   # the period, one add, slack


def _screen(monkeypatch):
    """The HQPlayer screen's route with its database reads answered here."""
    import hqp_library
    from routers import hqplayer as hqp_router
    from routers import settings as settings_router
    monkeypatch.setattr(hqp_library, "endpoint_by_address", lambda host, port: None)
    monkeypatch.setattr(settings_router, "_hqp_library_state", lambda: {})
    monkeypatch.setattr(hqp_router, "_load_favorites", lambda: [])
    return hqp_router


def test_a_pause_during_the_hqplayer_screens_read_waits_one_question(fake, output, monkeypatch):
    """I1: the HQPlayer screen's nine questions take the connection one at
    a time — a Pause sent while it reads waits for one of them, never
    for the block."""
    hqp_router = _screen(monkeypatch)
    mgr, b = output
    for q in ("GetInfo", "State", "GetModes", "GetRates", "GetFilters", "GetShapers",
              "MatrixListProfiles", "Status", "VolumeRange"):
        fake.service[q] = 0.3
    before = len(fake.arrivals)
    reading = threading.Thread(target=hqp_router.get_state, kwargs={"dsp": True}, daemon=True)
    reading.start()
    assert _wait(lambda: "GetInfo" in [c for _, c in fake.arrivals[before:]], timeout=5)
    t0 = time.monotonic()
    assert b.pause()
    waited = time.monotonic() - t0
    reading.join(10)
    assert waited < 0.3 + 0.3, waited                    # behind one question, never nine


def test_each_thread_reads_the_refusal_of_its_own_request(fake):
    """What a request came to is the thread's that sent it, read in the same
    call — a question answered on another thread between a refused command
    and the reading of its refusal leaves the refusal as it was."""
    link = _link()
    fake.replies["Pause"] = '<Pause result="Error">Nothing to pause</Pause>'
    sent, asked = threading.Event(), threading.Event()
    seen = {}

    def command():
        with link.command("transport"):
            seen["ok"] = link.client.pause()
            sent.set()
            asked.wait(2)
            seen["why"] = link.client.refusal()
    t = threading.Thread(target=command, daemon=True)
    t.start()
    assert sent.wait(5)
    with link.asking() as c:
        assert c.get_status() is not None
    asked.set()
    t.join(5)
    assert seen == {"ok": False, "why": "Nothing to pause"}


def test_a_silence_ends_the_sequence_that_met_it(fake, monkeypatch):
    """I1: a request HQPlayer leaves unanswered, with no build of ours behind
    the silence, ends its sequence — the rest is refused at once, never
    dialled again into the same silence."""
    monkeypatch.setattr(hl, "COMMAND_WAIT_S", 0.5)
    link = _link()
    fake.silence(30)
    t0 = time.monotonic()
    with pytest.raises(hl.NotAnswering):
        with link.command("rebuild"):
            for i in range(6):
                hb._add_one(link.client, f"http://127.0.0.1:1/file/t{i}")
    assert time.monotonic() - t0 < 2                     # one wait, not six
    assert [c for _, c in fake.arrivals] == ["PlaylistAdd"]


def test_the_hqplayer_screen_stops_at_its_first_silence(fake, monkeypatch):
    """I1: the HQPlayer screen's questions are one block — the first one
    HQPlayer leaves unanswered ends it, within one wait, far inside the
    assistant's 30 s."""
    hqp_router = _screen(monkeypatch)
    monkeypatch.setattr(hl, "COMMAND_WAIT_S", 0.5)
    _link()
    fake.silence(30)
    t0 = time.monotonic()
    out = hqp_router.get_state(dsp=True)
    assert time.monotonic() - t0 < 1.5
    assert out["error"] == hl.NOT_ANSWERING
    assert len(fake.arrivals) == 1


def test_a_change_of_three_knobs_goes_whole_through_a_build_after_the_first(fake, output):
    """The Status change_dsp holds between its knobs is the build's watch, never a silence that ends the sequence — a change
    of three knobs whose first sets off a build sends all three."""
    fake.build("SetRate", 1.0)                           # the first knob in DSP_KNOBS' order
    out = hb.change_dsp({"rate": 3, "filter": 57, "filter1x": 57, "shaper": 2})
    assert out["building"] is not None
    assert _wait(lambda: _link().build.last_change is not None)
    assert [c for c, _ in fake.commands if c.startswith("Set")] == ["SetRate", "SetFilter",
                                                                   "SetShaping"]
    assert _link().build.last_change["failed"] == {}


def test_a_play_that_builds_after_its_answer_meets_the_links_held_status(fake, output,
                                                                        monkeypatch):
    """I1: a replace whose Play HQPlayer answers before its build —
    the connection passes to a Status the link holds itself, watched: the
    poller never meets the silence nor drops its connection, it says
    "building" from the watch, and the held Status ends the build."""
    mgr, b = output
    _quick_polls(monkeypatch)
    buildings, quiet = _seen(mgr, "building"), _seen(mgr, "not_answering")
    fake.build("Play", 2.0)                              # answered, then silent
    connections = _link().client.connections
    mgr.replace_queue([_item(f"E:/Music/C/0{i}.flac", 200 + i) for i in range(1, 4)], play=True)
    assert _wait(lambda: any(buildings), timeout=5)
    assert _wait(lambda: _link().build.state is None
                 and mgr._latest_status.get("state") == "playing", timeout=10)
    assert not any(quiet)
    assert _link().client.connections == connections     # no question timed out on it
    after = [c for _, c in fake.arrivals]
    assert after[after.index("Play") + 1] == "Status"    # the silence fell on the link's own


def test_a_set_whose_next_item_is_superseded_leaves_its_silence_to_the_links_status(
        fake, output, monkeypatch):
    """I1, I3: the change's own held Status is dropped — a newer DSP
    change came first — so the connection goes to the link's held Status,
    and the build's silence falls on it, watched, never on the poller."""
    mgr, b = output
    _quick_polls(monkeypatch)
    link = _link()
    quiet = _seen(mgr, "not_answering")
    fake.build("SetFilter", 2.0)                         # answered, then silent

    def superseding(f, conn, what, command):
        if what == "out" and command == "SetFilter":
            link.accept("dsp")                           # the rest of this change goes
    fake.on_wire = superseding
    hb.change_dsp({"filter": 57, "filter1x": 57})
    assert _wait(lambda: link.build.state is not None, timeout=3)
    after = [c for _, c in fake.arrivals]
    assert after[after.index("SetFilter") + 1] == "Status"
    assert _wait(lambda: link.build.state is None, timeout=10)
    assert not any(quiet)


@pytest.mark.parametrize("answer", ["last", "first"])
def test_a_stops_silence_meets_no_question_and_never_says_building(fake, output, monkeypatch,
                                                                    answer):
    """I1, I9: a Stop's teardown (3.5 s in SDM) is a short
    silence, never a build. Answered at its end or at once, no question
    meets it, the status never says building, and a Play pressed meanwhile
    goes once it is over."""
    mgr, b = output
    _quick_polls(monkeypatch)
    buildings, quiet = _seen(mgr, "building"), _seen(mgr, "not_answering")
    fake.build("Stop", 3.5, answer=answer)
    stopping = threading.Thread(target=b.stop, daemon=True)
    stopping.start()
    assert _wait(lambda: any(c == "Stop" for _, c in fake.arrivals), timeout=3)
    time.sleep(0.5)                                      # inside the teardown
    assert b.play()                                      # waited for it, never refused
    stopping.join(10)
    assert not any(buildings) and not any(quiet)
    assert fake.sent_into_silence() == []
    began, over = fake.silences[0]
    assert len([c for ts, c in fake.arrivals if began <= ts < over and c == "Status"]) <= 1


def test_a_stop_unanswered_past_its_wait_is_hqplayer_not_answering(fake, output, monkeypatch):
    """I9: a short silence past STOP_ANSWER_S is HQPlayer not
    answering — said at once, never building — and a Play is refused with
    that line until HQPlayer answers again."""
    mgr, b = output
    _quick_polls(monkeypatch)
    monkeypatch.setattr(hl, "STOP_ANSWER_S", 0.6)
    _link().client.waits.update(hl._command_waits())
    quiet, buildings = _seen(mgr, "not_answering"), _seen(mgr, "building")
    fake.build("Stop", 3.0, answer="last")
    assert not b.stop()                                  # unanswered within its wait
    assert _wait(lambda: any(quiet), timeout=3)
    with pytest.raises(hl.NotAnswering, match=hl.NOT_ANSWERING):
        b.play()
    assert not any(buildings)
    assert _wait(lambda: _link().silent_since is None, timeout=10)    # its next answer ends it
    assert b.play()


def test_an_intent_accepted_while_its_request_connects_drops_it(fake, monkeypatch):
    """I3: the item a request serves is checked once its connection is
    made, right before the bytes go — a newer intent accepted while it
    connects drops it."""
    import hqplayer_client
    link = _link()
    start = link.item("start", link.accept("queue"))
    real = hqplayer_client.connect_address

    def connecting(host):
        link.accept("transport")                         # a Pause lands while it connects
        return real(host)
    monkeypatch.setattr(hqplayer_client, "connect_address", connecting)
    with pytest.raises(hl.Dropped, match="a newer request came first"):
        with link.command(item=start):
            link.client.play()
    assert "Play" not in [c for _, c in fake.arrivals]


def test_the_node_stop_during_a_connect_sends_nothing(fake, monkeypatch):
    """I5: the node stop landing while a request connects — the connection
    made is closed, and nothing is sent after the stop."""
    import hqplayer_client
    link = _link()
    real = hqplayer_client.connect_address

    def connecting(host):
        hl.close_all("the node is stopping")
        return real(host)
    monkeypatch.setattr(hqplayer_client, "connect_address", connecting)
    with pytest.raises(hl.Dropped, match="the node is stopping"):
        with link.asking() as c:
            c.get_status()
    assert fake.arrivals == []
    assert link.client.socket is None


def test_a_socket_closed_under_the_link_is_replaced_never_raised(fake):
    """A socket closed under the link — a close racing a request, or
    a descriptor select cannot take — is found by a peek that needs no
    select, and replaced: never a ValueError."""
    link = _link()
    with link.asking() as c:
        assert c.get_status() is not None
    link.client.socket.close()                           # closed, still the client's
    with link.asking() as c:
        assert c.get_status() is not None


def test_choosing_another_hqplayer_leaves_a_library_read_of_a_third_alone(fake, monkeypatch):
    """Choosing HQPlayer B retires the link of the HQPlayer chosen
    before — never another box's whose library is being read."""
    box, other = FakeHqp(), FakeHqp()
    try:
        _link()                                          # the HQPlayer chosen: the fake
        reading = hl.link_for("127.0.0.1", box.port)
        box.silence(1.0)
        result = {}

        def read():
            try:
                result["line"] = reading.ask_raw("<LibraryGet/>", 5)
            except OSError as e:
                result["error"] = e
        t = threading.Thread(target=read, daemon=True)
        t.start()
        assert _wait(lambda: any(c == "LibraryGet" for _, c in box.arrivals), timeout=3)
        monkeypatch.setattr(settings, "hqplayer_port", other.port)
        hl.choose("127.0.0.1", other.port)
        t.join(5)
        assert "line" in result, result
        assert reading.state == "open"
    finally:
        box.close()
        other.close()


def test_the_link_dials_the_alias_chosen_last(fake, monkeypatch):
    """One HQPlayer under two aliases is one link, and it dials the alias
    the owner chose last — on a Docker node the host's address replaces a
    loopback that reaches nothing in the container."""
    import auth_hmac
    import hqplayer_client
    monkeypatch.setattr(auth_hmac, "is_own_address",
                        lambda h: (h or "").lower() in ("nowhere.alias", "127.0.0.1", "localhost"))
    real = hqplayer_client.connect_address
    # An IPv6 address for an IPv4 socket: refused at once, as a loopback
    # that leads nowhere would be
    monkeypatch.setattr(hqplayer_client, "connect_address",
                        lambda h: "::1" if h == "nowhere.alias" else real(h))
    monkeypatch.setattr(settings, "hqplayer_host", "nowhere.alias")
    first = hl.selected()
    with pytest.raises(ConnectionError):
        with first.asking() as c:
            c.get_status()
    monkeypatch.setattr(settings, "hqplayer_host", "127.0.0.1")
    chosen = hl.choose("127.0.0.1", fake.port)
    assert chosen is first                               # one HQPlayer, one link
    with chosen.asking() as c:
        assert c.get_status() is not None


def test_an_answer_to_any_request_ends_an_unfinished_build(fake, monkeypatch):
    """I9: a build HQPlayer did not finish ends with its next answer to any
    request — the HQPlayer screen's, a library read's — not only the
    poller's, which runs only while HQPlayer is the output."""
    _limit(monkeypatch, 0.5)
    link = _link()
    fake.build("SetFilter", 1.5, answer="last")          # its own answer held past the limit
    with link.command("dsp"):
        assert not link.client.set_filter(1)
    assert link.build.state is not None and link.build.state.get("unfinished")
    assert _wait(lambda: time.monotonic() > fake._silent_until, timeout=3)
    with link.asking() as c:
        assert c.get_info() is not None
    assert link.build.state is None


def test_the_scan_lists_a_busy_hqplayer_as_answering(monkeypatch):
    """The scan asks each HQPlayer it found through its link; refused
    because it builds or is lent, it is busy, not gone — listed with control
    and its row, never as answering discovery only."""
    import asyncio

    import async_upnp_client.search as search
    import hqp_library
    import tls_gen
    from routers import player

    async def no_search(*a, **k):
        return None

    async def sweep(found, found_hqp):
        found_hqp["192.0.2.7"] = {"name": "box", "product": "Signalyst HQPlayer Embedded 6"}

    def building(host, port):
        raise hl.Building(hl.HqpLink(("box", port), host, port),
                          {"since": 0, "command": "SetFilter", "silent": True})
    monkeypatch.setattr(search, "async_search", no_search)
    monkeypatch.setattr(player, "_unicast_sweep", sweep)
    monkeypatch.setattr(tls_gen, "detect_private_host_ips", lambda: [])
    monkeypatch.setattr(hqp_library, "resolve_endpoint", building)
    monkeypatch.setattr(hqp_library, "endpoint_by_address", lambda host, port: {"id": 9})
    monkeypatch.setattr(player, "_configured_hqp", lambda: None)
    monkeypatch.setattr(player, "_dlna_pins", lambda: {})
    monkeypatch.setattr(player, "_hqplayer_entries", lambda: [])
    monkeypatch.setattr(player, "_hqp_discovered", {})
    asyncio.run(player._scan_run())
    entry = player._hqp_discovered[("192.0.2.7", 4321)]
    assert entry["control"] is True and entry["endpoint_id"] == 9


def test_a_lent_link_sends_nothing(fake):
    """I1: while the benchmark holds the output its link sends nothing — no
    command, no question."""
    link = _link()
    link.lend()
    with pytest.raises(hl.Dropped, match="lent"):
        with link.asking() as c:
            c.get_status()
    with pytest.raises(hl.Dropped, match="lent"):
        with link.command("transport"):
            link.client.play()
    assert fake.arrivals == []
    link.reclaim()
    with link.asking() as c:
        assert c.get_status() is not None


def test_a_request_waiting_its_turn_is_dropped_once_the_link_is_lent(fake):
    """The link's state is checked while a request waits for the connection,
    not only before: the benchmark's hold lands while it waits, and it never
    goes."""
    link = _link()
    fake.service["GetInfo"] = 1.0
    results = {}

    def ask(name, question):
        try:
            with link.asking() as c:
                results[name] = question(c)
        except hl.Dropped as e:
            results[name] = e
    first = threading.Thread(target=ask, args=("first", lambda c: c.get_info()), daemon=True)
    first.start()
    assert _wait(lambda: any(c == "GetInfo" for _, c in fake.arrivals), timeout=3)
    second = threading.Thread(target=ask, args=("second", lambda c: c.get_status()), daemon=True)
    second.start()
    time.sleep(0.3)                                      # it waits its turn behind the GetInfo
    link.lend()
    first.join(5)
    second.join(5)
    assert results["first"] is not None and not isinstance(results["first"], Exception)
    assert isinstance(results["second"], hl.Dropped)
    assert [c for _, c in fake.arrivals] == ["GetInfo"]
    link.reclaim()


def test_close_releases_work_waiting_a_build_out_at_once(fake, monkeypatch):
    """I5: the node stop wakes background work waiting a build out: it gives up
    with the stop's reason within a moment, never at the build's end."""
    monkeypatch.setattr(hl.BuildWatch, "after", 0.3)
    link = _link()
    fake.build("SetFilter", 30, answer="last")           # its own answer held for long

    def changing():
        with link.command("dsp"):
            link.client.set_filter(1)           # its read ends with the close
    threading.Thread(target=changing, daemon=True).start()
    assert _wait(lambda: link.build.state is not None, timeout=3)
    gave_up = {}

    def waiting():
        gave_up["why"] = link.build.wait_out()
    t = threading.Thread(target=waiting, daemon=True)
    t.start()
    time.sleep(0.2)
    t0 = time.monotonic()
    hl.close_all("the node is stopping")
    t.join(5)
    assert time.monotonic() - t0 < 2
    assert isinstance(gave_up["why"], hl.GaveUp) and "the node is stopping" in str(gave_up["why"])
